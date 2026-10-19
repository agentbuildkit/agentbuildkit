"""Typed workflow messages, agent steps, and pause/resume.

Executors call the Phase 3 agent helper. They do not read runtime tools from the
workflow, and they do not let a model-supplied name address an arbitrary agent.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from agent_framework import (
    Executor,
    FunctionTool,
    InMemoryCheckpointStorage,
    Workflow,
    WorkflowContext,
    WorkflowRunResult,
    handler,
    response_handler,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from agent_platform.agents import PlatformAgent
from agent_platform.guardrails import (
    ApprovalDecision,
    ToolEffect,
    execute_tool,
    tool_effect,
)
from agent_platform.observability import AuditEvent, AuditLog, argument_hash

_PENDING_KEY = "pending"


class WorkflowEnvelope(BaseModel):
    """One typed payload moving between workflow steps."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    source: str
    body: dict[str, Any]


class HumanApprovalRequest(BaseModel):
    """A pause that names the action and carries no argument values."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    action: str


class HumanApprovalResponse(BaseModel):
    """The explicit human decision for one paused action."""

    model_config = ConfigDict(frozen=True)

    approved: bool


class RouteRejected(Exception):
    """The route is not in the workflow's allowlist."""

    def __init__(self) -> None:
        super().__init__("Workflow route is not allowlisted.")


class WorkflowInputError(Exception):
    """A workflow message did not match the expected model."""


class WorkflowRunError(Exception):
    """The workflow finished without a usable typed result."""


def envelope(correlation_id: str, data: BaseModel, *, source: str = "input") -> WorkflowEnvelope:
    """Wrap a typed model for the first step of a workflow."""
    return WorkflowEnvelope(
        correlation_id=correlation_id,
        source=source,
        body=data.model_dump(),
    )


@dataclass(frozen=True)
class PendingHumanInput:
    """The checkpoint and typed request needed to resume a paused workflow."""

    request_id: str
    checkpoint_id: str
    request: HumanApprovalRequest


@dataclass(frozen=True)
class WorkflowRun:
    """Either a typed workflow output or a human-input pause."""

    correlation_id: str
    output: WorkflowEnvelope | None = None
    pending: PendingHumanInput | None = None


@dataclass
class CompiledWorkflow:
    """A built workflow and the in-memory checkpoints for that instance."""

    name: str
    workflow: Workflow
    checkpoints: InMemoryCheckpointStorage


class AgentStep(Executor):
    """Run one platform agent and pass its validated output onward."""

    def __init__(self, agent: PlatformAgent[Any, Any], *, terminal: bool) -> None:
        super().__init__(id=agent.definition.name)
        self._agent = agent
        self._terminal = terminal

    @handler
    async def handle(
        self,
        message: WorkflowEnvelope,
        ctx: WorkflowContext[WorkflowEnvelope, WorkflowEnvelope],
    ) -> None:
        model_type = cast(type[BaseModel], self._agent.definition.input_type)
        try:
            data = model_type.model_validate(message.body)
        except ValidationError:
            raise WorkflowInputError(
                f"Workflow input for '{self.id}' does not match "
                f"{self._agent.definition.input_type.__name__}."
            ) from None
        result = await self._agent.run(data, correlation_id=message.correlation_id)
        if not isinstance(result, BaseModel):
            raise WorkflowRunError(f"Agent '{self.id}' did not return a typed result.")
        outgoing = WorkflowEnvelope(
            correlation_id=message.correlation_id,
            source=self.id,
            body=result.model_dump(),
        )
        if self._terminal:
            await ctx.yield_output(outgoing)
            return
        await ctx.send_message(outgoing)


class RejectRoute(Executor):
    """Fail a workflow when no allowlisted route matches."""

    def __init__(self, executor_id: str = "reject-route") -> None:
        super().__init__(id=executor_id)

    @handler
    async def reject(self, message: WorkflowEnvelope, ctx: WorkflowContext) -> None:
        del message, ctx
        raise RouteRejected()


class ResultAggregator(Executor):
    """Collect one fan-in batch into a single typed envelope."""

    def __init__(self) -> None:
        super().__init__(id="aggregate")

    @handler
    async def aggregate(
        self,
        messages: list[WorkflowEnvelope],
        ctx: WorkflowContext[WorkflowEnvelope, WorkflowEnvelope],
    ) -> None:
        correlation_ids = {message.correlation_id for message in messages}
        if len(correlation_ids) != 1:
            raise WorkflowRunError("Workflow messages crossed a run boundary.")
        correlation_id = correlation_ids.pop()
        await ctx.yield_output(
            WorkflowEnvelope(
                correlation_id=correlation_id,
                source=self.id,
                body={"results": {message.source: message.body for message in messages}},
            )
        )


class ApprovalGate(Executor):
    """Pause for a human decision before a prepared side-effect tool runs."""

    def __init__(
        self,
        tool: FunctionTool,
        audit_log: AuditLog,
        *,
        executor_id: str = "approval-gate",
    ) -> None:
        super().__init__(id=executor_id)
        effect = tool_effect(tool)
        if effect is not ToolEffect.SIDE_EFFECT:
            raise WorkflowInputError("Approval gate requires a side-effect tool.")
        self._tool = tool
        self._audit_log = audit_log
        self._pending: dict[str, dict[str, Any]] = {}

    @handler
    async def request_approval(self, message: WorkflowEnvelope, ctx: WorkflowContext) -> None:
        self._pending[message.correlation_id] = dict(message.body)
        self._audit_log.record(
            AuditEvent(
                correlation_id=message.correlation_id,
                agent_name=self.id,
                tool_name=self._tool.name,
                approval="pending",
                outcome="pending_approval",
                argument_hash=argument_hash(message.body),
            )
        )
        await ctx.request_info(
            HumanApprovalRequest(correlation_id=message.correlation_id, action=self._tool.name),
            HumanApprovalResponse,
        )

    @response_handler
    async def receive_decision(
        self,
        original: HumanApprovalRequest,
        response: HumanApprovalResponse,
        ctx: WorkflowContext[WorkflowEnvelope, WorkflowEnvelope],
    ) -> None:
        arguments = self._pending.pop(original.correlation_id, None)
        if arguments is None:
            raise WorkflowRunError("Approval state for this run is missing.")
        if not response.approved:
            self._audit_log.record(
                AuditEvent(
                    correlation_id=original.correlation_id,
                    agent_name=self.id,
                    tool_name=self._tool.name,
                    approval="rejected",
                    outcome="rejected",
                    argument_hash=argument_hash(arguments),
                )
            )
            await ctx.yield_output(
                WorkflowEnvelope(
                    correlation_id=original.correlation_id,
                    source=self.id,
                    body={"status": "rejected"},
                )
            )
            return
        try:
            await execute_tool(
                self._tool,
                arguments,
                audit_log=self._audit_log,
                correlation_id=original.correlation_id,
                agent_name=self.id,
                decision=ApprovalDecision(self._tool.name, approved=True),
            )
        except Exception:
            raise WorkflowRunError("Side effect could not be completed.") from None
        await ctx.yield_output(
            WorkflowEnvelope(
                correlation_id=original.correlation_id,
                source=self.id,
                body={"status": "completed"},
            )
        )

    async def on_checkpoint_save(self) -> dict[str, Any]:
        return {"pending": {key: dict(value) for key, value in self._pending.items()}}

    async def on_checkpoint_restore(self, state: dict[str, Any]) -> None:
        restored = state.get(_PENDING_KEY, {})
        if not isinstance(restored, dict):
            self._pending = {}
            return
        pending: dict[str, dict[str, Any]] = {}
        for key, value in cast(Mapping[Any, Any], restored).items():
            if isinstance(key, str) and isinstance(value, dict):
                copied: dict[str, Any] = {}
                for item_key, item_value in cast(Mapping[Any, Any], value).items():
                    copied[str(item_key)] = item_value
                pending[key] = copied
        self._pending = pending


async def run_workflow(
    compiled: CompiledWorkflow,
    message: WorkflowEnvelope | None = None,
    *,
    responses: Mapping[str, BaseModel] | None = None,
    checkpoint_id: str | None = None,
) -> WorkflowRun:
    """Run or resume a workflow and return either output or a typed pause.

    This helper does not accept tools. Agents keep the tools from their definitions.
    """
    if responses is None:
        result = await compiled.workflow.run(message)
    else:
        result = await compiled.workflow.run(responses=dict(responses), checkpoint_id=checkpoint_id)
    return await _interpret(compiled, result, fallback_correlation_id=_correlation_id(message))


async def _interpret(
    compiled: CompiledWorkflow,
    result: WorkflowRunResult,
    *,
    fallback_correlation_id: str | None,
) -> WorkflowRun:
    requests = result.get_request_info_events()
    if requests:
        event = requests[0]
        request = event.data
        if not isinstance(request, HumanApprovalRequest):
            raise WorkflowRunError("Workflow paused without a typed approval request.")
        checkpoint_id = await _checkpoint_for_request(compiled, event.request_id)
        return WorkflowRun(
            correlation_id=request.correlation_id,
            pending=PendingHumanInput(
                request_id=event.request_id,
                checkpoint_id=checkpoint_id,
                request=request,
            ),
        )
    outputs = result.get_outputs()
    if len(outputs) != 1 or not isinstance(outputs[0], WorkflowEnvelope):
        raise WorkflowRunError("Workflow did not produce a typed result.")
    output = outputs[0]
    return WorkflowRun(
        correlation_id=output.correlation_id or fallback_correlation_id or "",
        output=output,
    )


async def _checkpoint_for_request(compiled: CompiledWorkflow, request_id: str) -> str:
    checkpoints = await compiled.checkpoints.list_checkpoints(workflow_name=compiled.name)
    matches = [
        checkpoint
        for checkpoint in checkpoints
        if request_id in checkpoint.pending_request_info_events
    ]
    if not matches:
        raise WorkflowRunError("Workflow paused without a checkpoint.")
    return max(matches, key=lambda checkpoint: checkpoint.timestamp).checkpoint_id


def _correlation_id(message: WorkflowEnvelope | None) -> str | None:
    if message is None:
        return None
    return message.correlation_id
