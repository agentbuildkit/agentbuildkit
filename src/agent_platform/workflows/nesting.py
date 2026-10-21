"""Nested workflow runs.

Short-lived children execute through Microsoft Agent Framework's
``WorkflowExecutor``. A wait that may outlive the process uses a parent
checkpoint and a ``child_finished`` signal instead. Parent and child graphs
keep separate executors and agent instances.
"""

from dataclasses import dataclass
from typing import Any

from agent_framework import (
    InMemoryCheckpointStorage,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowExecutor,
    WorkflowRunResult,
    WorkflowRunState,
)

from agent_platform.state.models import ChildOutcome
from agent_platform.workflows.persistence import WorkflowStateStore
from agent_platform.workflows.runtime import (
    CompiledWorkflow,
    RouteRejected,
    WorkflowEnvelope,
    WorkflowRunError,
    run_workflow,
)


class ChildRejected(Exception):
    """Python in a child workflow rejected the run."""

    def __init__(self) -> None:
        super().__init__("Child workflow was rejected.")


class ChildEscalated(Exception):
    """Python in a child workflow escalated the run."""

    def __init__(self) -> None:
        super().__init__("Child workflow was escalated.")


class NestedWorkflowExecutor(WorkflowExecutor):
    """Run a child workflow and surface its typed terminal failure."""

    async def _process_workflow_result(
        self,
        result: WorkflowRunResult,
        ctx: WorkflowContext[Any],
    ) -> None:
        if result.get_final_state() == WorkflowRunState.FAILED:
            _raise_child_failure(result)
        await super()._process_workflow_result(result, ctx)


@dataclass(frozen=True)
class NestedExecution:
    """The child run and the typed outcome of a short-lived nested workflow."""

    child_run_id: str
    outcome: ChildOutcome
    output: WorkflowEnvelope


def nested_workflow(child: Workflow, *, name: str) -> CompiledWorkflow:
    """Wrap ``child`` in a parent graph whose only step is a ``WorkflowExecutor``.

    The wrapper gets its own checkpoint storage. It does not reuse the child's
    executors or the parent's agent instances.
    """
    wrapper = NestedWorkflowExecutor(child, id=f"{name}-child", allow_direct_output=True)
    checkpoints = InMemoryCheckpointStorage()
    workflow = WorkflowBuilder(
        name=name,
        start_executor=wrapper,
        checkpoint_storage=checkpoints,
        max_iterations=20,
    ).build()
    return CompiledWorkflow(name=name, workflow=workflow, checkpoints=checkpoints)


def assert_isolated_workflows(parent: Workflow, child: Workflow) -> None:
    """Fail closed when the two graphs share an executor or an agent instance."""
    if parent is child:
        raise WorkflowRunError("Parent and child workflows share an executor.")
    parent_executors = {id(executor) for executor in parent.executors.values()}
    for executor in child.executors.values():
        if id(executor) in parent_executors:
            raise WorkflowRunError("Parent and child workflows share an executor.")
    if _session_ids(parent) & _session_ids(child):
        raise WorkflowRunError("Parent and child workflows share an agent.")


async def run_nested_workflow(
    store: WorkflowStateStore,
    parent_workflow: Workflow,
    child: CompiledWorkflow,
    message: WorkflowEnvelope,
    *,
    parent_run_id: str,
    work_item_id: str | None = None,
    mutating: bool = False,
) -> NestedExecution:
    """Run a delegated child to a typed outcome inside the current process.

    The child workflow must already be allowlisted for the parent. A pause inside
    the child is not kept in memory: long waits use ``pause_for_child`` and
    ``finish_child_run``.
    """
    parent_run = store.get_run(parent_run_id)
    if parent_workflow.name != parent_run.workflow_name:
        raise WorkflowRunError("Parent workflow does not match the run.")
    assert_isolated_workflows(parent_workflow, child.workflow)
    child_run = store.start_child_run(
        parent_run_id,
        child.name,
        message.correlation_id,
        work_item_id=work_item_id,
        mutating=mutating,
    )
    nested = nested_workflow(child.workflow, name=f"nested-{child.name}")
    try:
        result = await run_workflow(nested, message)
    except ChildEscalated:
        store.finish_child_run(child_run.run_id, "escalated")
        raise
    except (ChildRejected, RouteRejected):
        store.finish_child_run(child_run.run_id, "rejected")
        raise
    except Exception:
        store.finish_child_run(child_run.run_id, "failed")
        raise
    if result.pending is not None or result.output is None:
        raise WorkflowRunError(
            "Nested workflow paused. A long wait must use a checkpoint and a child signal."
        )
    outcome = store.finish_child_run(child_run.run_id, "completed")
    return NestedExecution(child_run_id=child_run.run_id, outcome=outcome, output=result.output)


def _raise_child_failure(result: WorkflowRunResult) -> None:
    events = list(result)
    error_type = ""
    for event in events:
        if getattr(event, "type", None) != "failed":
            continue
        details = getattr(event, "details", None)
        kind = getattr(details, "error_type", None)
        if isinstance(kind, str):
            error_type = kind
            break
    if error_type == "ChildEscalated":
        raise ChildEscalated()
    if error_type == "ChildRejected":
        raise ChildRejected()
    if error_type == "RouteRejected":
        raise RouteRejected()
    raise WorkflowRunError("Nested workflow failed.")


def _session_ids(workflow: Workflow) -> set[int]:
    found: set[int] = set()
    for executor in workflow.executors.values():
        agent = getattr(executor, "_agent", None)
        if agent is None:
            continue
        found.add(id(agent))
        framework_agent = getattr(agent, "agent", None)
        if framework_agent is not None:
            found.add(id(framework_agent))
    return found
