"""Offline checks for child runs and nested workflows."""

from pathlib import Path

import pytest
from agent_framework import (
    Executor,
    InMemoryCheckpointStorage,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)
from pydantic import BaseModel

from agent_platform.agents import AgentDefinition, PlatformAgent, create_agent
from agent_platform.guardrails import ToolEffect, prepare_tool
from agent_platform.observability import AuditLog
from agent_platform.state.models import RegistryRejected
from agent_platform.testing import ScriptedChatClient
from agent_platform.workflows import (
    ChildEscalated,
    ChildRejected,
    WorkflowEnvelope,
    WorkflowRunError,
    approval_workflow,
    envelope,
    memory_workflow_store,
    nested_workflow,
    run_nested_workflow,
    run_workflow,
    sqlite_workflow_store,
)
from agent_platform.workflows.nesting import NestedWorkflowExecutor
from agent_platform.workflows.persistence import WorkflowStateStore
from agent_platform.workflows.runtime import AgentStep, CompiledWorkflow


class Note(BaseModel):
    text: str


class Echo(Executor):
    """Yield the same envelope. Used as a parent step that must not see the child."""

    def __init__(self, executor_id: str) -> None:
        super().__init__(id=executor_id)

    @handler
    async def handle(
        self,
        message: WorkflowEnvelope,
        ctx: WorkflowContext[WorkflowEnvelope, WorkflowEnvelope],
    ) -> None:
        await ctx.yield_output(message)


class Complete(Executor):
    def __init__(self) -> None:
        super().__init__(id="complete-child")
        self.calls = 0

    @handler
    async def handle(
        self,
        message: WorkflowEnvelope,
        ctx: WorkflowContext[WorkflowEnvelope, WorkflowEnvelope],
    ) -> None:
        self.calls += 1
        await ctx.yield_output(
            WorkflowEnvelope(
                correlation_id=message.correlation_id,
                source=self.id,
                body={"status": "completed", "seen": message.body.get("text")},
            )
        )


class Boom(Executor):
    def __init__(self, error: Exception) -> None:
        super().__init__(id="boom-child")
        self._error = error
        self.calls = 0

    @handler
    async def handle(self, message: WorkflowEnvelope, ctx: WorkflowContext) -> None:
        del message, ctx
        self.calls += 1
        raise self._error


class Notice(BaseModel):
    recipient: str


def _directory(store: WorkflowStateStore) -> None:
    store.register_organisation("Example org", "1")
    store.register_workflow("parent-flow")
    store.register_workflow("child-flow")
    store.register_workflow("grand-flow")
    store.allow_delegation("parent-flow", "child-flow")
    store.allow_delegation("child-flow", "grand-flow")


def _compiled(executor: Executor, name: str) -> CompiledWorkflow:
    checkpoints = InMemoryCheckpointStorage()
    workflow = WorkflowBuilder(
        name=name,
        start_executor=executor,
        checkpoint_storage=checkpoints,
        max_iterations=20,
    ).build()
    return CompiledWorkflow(name=name, workflow=workflow, checkpoints=checkpoints)


def _parent_workflow() -> CompiledWorkflow:
    return _compiled(Echo("parent-step"), "parent-flow")


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> WorkflowStateStore:
    if request.param == "memory":
        return memory_workflow_store("org-a")
    return sqlite_workflow_store(tmp_path / "state.sqlite", "org-a")


async def test_short_nested_run_uses_workflow_executor_and_typed_outcome(
    store: WorkflowStateStore,
) -> None:
    _directory(store)
    parent = store.start_run("parent-flow", "corr-parent", run_id="parent-1")
    child_step = Complete()
    child = _compiled(child_step, "child-flow")
    wrapped = nested_workflow(child.workflow, name="child-flow")
    executors = list(wrapped.workflow.executors.values())
    assert any(isinstance(item, NestedWorkflowExecutor) for item in executors)

    result = await run_nested_workflow(
        store,
        _parent_workflow().workflow,
        child,
        envelope("corr-child", Note(text="child-token")),
        parent_run_id=parent.run_id,
    )

    assert child_step.calls == 1
    assert result.outcome.status == "completed"
    assert result.output.body["seen"] == "child-token"
    assert store.get_run(parent.run_id).status == "running"
    assert store.get_run(result.child_run_id).parent_run_id == parent.run_id
    assert store.get_run(result.child_run_id).status == "completed"
    assert store.list_ledger(run_id=result.child_run_id)[0].kind == "child_outcome"


@pytest.mark.parametrize(
    ("error", "outcome", "raised"),
    [
        (ChildRejected(), "rejected", ChildRejected),
        (ChildEscalated(), "escalated", ChildEscalated),
        (RuntimeError("broke"), "failed", RuntimeError),
    ],
)
async def test_child_terminal_states_propagate(
    store: WorkflowStateStore,
    error: Exception,
    outcome: str,
    raised: type[Exception],
) -> None:
    _directory(store)
    parent = store.start_run("parent-flow", "corr-parent")
    child = _compiled(Boom(error), "child-flow")

    with pytest.raises(raised):
        await run_nested_workflow(
            store,
            _parent_workflow().workflow,
            child,
            envelope("corr-child", Note(text="x")),
            parent_run_id=parent.run_id,
        )

    runs = store.list_runs(workflow_name="child-flow")
    assert len(runs) == 1
    assert store.get_child_outcome(runs[0].run_id).status == outcome


async def test_unknown_or_unauthorised_child_does_not_run(store: WorkflowStateStore) -> None:
    _directory(store)
    parent = store.start_run("parent-flow", "corr-parent")
    secret = Complete()
    hidden = _compiled(secret, "review-flow")

    with pytest.raises(RegistryRejected):
        store.allow_delegation("parent-flow", "missing-flow")
    with pytest.raises(RegistryRejected):
        store.start_child_run(parent.run_id, "missing-flow", "corr-child")
    with pytest.raises(RegistryRejected):
        await run_nested_workflow(
            store,
            _parent_workflow().workflow,
            hidden,
            envelope("corr-child", Note(text="x")),
            parent_run_id=parent.run_id,
        )

    assert secret.calls == 0
    assert store.list_runs(workflow_name="review-flow") == ()


async def test_authorised_child_may_join_the_parent_mutating_run(
    store: WorkflowStateStore,
) -> None:
    _directory(store)
    item = store.open_work_item(item_type="slice", title="Slice", acceptance="ship the slice")
    store.transition_work_item(item.work_item_id, "ready")
    parent = store.start_run(
        "parent-flow",
        "corr-parent",
        work_item_id=item.work_item_id,
        mutating=True,
        run_id="parent-1",
    )
    child = store.start_child_run(
        parent.run_id,
        "child-flow",
        "corr-child",
        work_item_id=item.work_item_id,
        mutating=True,
        run_id="child-1",
    )
    grandchild = store.start_child_run(
        child.run_id,
        "grand-flow",
        "corr-grand",
        work_item_id=item.work_item_id,
        mutating=True,
        run_id="grand-1",
    )

    assert store.get_work_item(item.work_item_id).active_run_id == parent.run_id
    assert grandchild.parent_run_id == child.run_id
    with pytest.raises(RegistryRejected):
        store.start_run(
            "grand-flow",
            "corr-other",
            work_item_id=item.work_item_id,
            mutating=True,
        )


async def test_paused_parent_reads_child_outcome_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = sqlite_workflow_store(path, "org-a")
    _directory(store)
    parent = store.start_run("parent-flow", "corr-parent", run_id="parent-1")

    def send_notice(recipient: str) -> str:
        return recipient

    tool = prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT)
    waiting = approval_workflow(tool, AuditLog(), name="parent-flow", storage=store)
    message = envelope("corr-parent", Notice(recipient="person"))
    paused_workflow = await run_workflow(waiting, message)
    assert paused_workflow.pending is not None
    await store.bind_checkpoint(parent.run_id, paused_workflow.pending.checkpoint_id)
    child = store.start_child_run(parent.run_id, "child-flow", "corr-child", run_id="child-1")
    paused = store.pause_for_child(parent.run_id, child.run_id)
    assert paused.status == "paused"
    store.finish_child_run(child.run_id, "rejected")

    reopened = sqlite_workflow_store(path, "org-a")
    outcome = reopened.take_child_outcome(parent.run_id, child.run_id)

    assert outcome.status == "rejected"
    assert reopened.get_run(parent.run_id).status == "running"
    assert reopened.get_child_outcome(child.run_id).status == "rejected"
    again = reopened.take_child_outcome(parent.run_id, child.run_id)
    assert again.status == "rejected"
    assert reopened.get_run(parent.run_id).status == "running"


async def test_parent_and_child_agents_do_not_share_session(store: WorkflowStateStore) -> None:
    _directory(store)
    parent_client = ScriptedChatClient(['{"text": "parent-token"}'])
    child_client = ScriptedChatClient(['{"text": "child-token"}'])
    parent_agent = _note_agent("parent-agent", parent_client)
    child_agent = _note_agent("child-agent", child_client)
    parent_flow = _compiled(AgentStep(parent_agent, terminal=True), "parent-flow")
    child_flow = _compiled(AgentStep(child_agent, terminal=True), "child-flow")
    parent = store.start_run("parent-flow", "corr-parent")
    await run_workflow(parent_flow, envelope("corr-parent", Note(text="from-parent")))

    await run_nested_workflow(
        store,
        parent_flow.workflow,
        child_flow,
        envelope("corr-child", Note(text="from-child")),
        parent_run_id=parent.run_id,
    )

    parent_text = _request_text(parent_client)
    child_text = _request_text(child_client)
    assert "from-parent" in parent_text
    assert "parent-token" not in child_text
    assert "from-child" in child_text
    assert "from-parent" not in child_text


async def test_shared_agent_is_rejected_before_the_child_runs(store: WorkflowStateStore) -> None:
    _directory(store)
    client = ScriptedChatClient(['{"text": "once"}', '{"text": "twice"}'])
    shared = _note_agent("shared-agent", client)
    parent_flow = _compiled(AgentStep(shared, terminal=True), "parent-flow")
    child_flow = _compiled(AgentStep(shared, terminal=True), "child-flow")
    parent = store.start_run("parent-flow", "corr-parent")

    with pytest.raises(WorkflowRunError):
        await run_nested_workflow(
            store,
            parent_flow.workflow,
            child_flow,
            envelope("corr-child", Note(text="from-child")),
            parent_run_id=parent.run_id,
        )

    assert client.requests == []
    assert store.list_runs(workflow_name="child-flow") == ()


def _note_agent(name: str, client: ScriptedChatClient) -> PlatformAgent[Note, Note]:
    return create_agent(
        AgentDefinition(
            name=name,
            purpose="Rewrite a note.",
            instructions="Return the note.",
            input_type=Note,
            output_type=Note,
        ),
        client=client,
        audit_log=AuditLog(),
    )


def _request_text(client: ScriptedChatClient) -> str:
    parts: list[str] = []
    for request in client.requests:
        for message in request:
            parts.append(message.text)
    return "\n".join(parts)
