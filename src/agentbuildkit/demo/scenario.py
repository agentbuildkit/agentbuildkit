"""Run the offline demo through the real AgentBuildKit harness.

Scripted clients supply the model text. Routing, tool rejection, approval,
SQLite pause and resume, audit, and evaluation are the harness, not a scripted
trace.
"""

import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_framework import ChatResponse, Content, Message

from agentbuildkit.agents import AgentDefinition, AgentOutputError, create_agent
from agentbuildkit.demo.models import (
    Draft,
    Intake,
    Review,
    ReviewRequest,
    RouteDecision,
    VisitRequest,
)
from agentbuildkit.demo.present import agent_heading, summary
from agentbuildkit.evaluation import EvaluationCase, EvaluationReport, Expectation, trace_from_audit
from agentbuildkit.evaluation.runner import run_evaluation
from agentbuildkit.guardrails import ToolEffect, prepare_tool
from agentbuildkit.observability import (
    AuditEvent,
    AuditLog,
    bind_correlation_id,
    reset_correlation_id,
)
from agentbuildkit.testing import ScriptedChatClient
from agentbuildkit.workflows import (
    HumanApprovalResponse,
    PendingRequest,
    RouteRejected,
    WorkflowRunError,
    WorkflowStateStore,
    approval_workflow,
    envelope,
    manager_workflow,
    resume_approval,
    run_workflow,
    sqlite_workflow_store,
)

_PROVIDER = "scripted"
_MODEL = "offline"
_CORRELATION_ID = "demo-loading-bay"
_REFERENCE = "T-100"
_WHEN = "tomorrow"
_MESSAGE = "The loading-bay door is stuck. Please book a technician for tomorrow."
_READ_TOOL = "read_ticket"
_WRITE_TOOL = "schedule_visit"
_REJECTED_TOOL = "cancel_ticket"
_ROUTE = "service"
_ROUTE_WORKFLOW = "demo-route"
_ACTION_WORKFLOW = "demo-approval"
_SCOPE = "demo"
_AGENT_RUN = "agent-run"
_HIDDEN_ARGUMENT = ("secret", "token", "password", "api_key", "authorization")


class DemoError(Exception):
    """A demo failure that should be shown without a traceback."""


@dataclass(frozen=True)
class DemoResult:
    """What one offline run actually did."""

    ok: bool
    approved: bool
    reads: tuple[str, ...]
    visits: tuple[str, ...]
    visits_before_approval: int
    route: str
    report: EvaluationReport
    events: tuple[AuditEvent, ...]
    text: str


async def run_demo(
    *,
    decision: bool | None = None,
    reader: Callable[[str], str] | None = None,
    writer: Callable[[str], None] | None = None,
    store_path: Path | None = None,
) -> DemoResult:
    """Route a scripted request, gate the side effect, and evaluate the trace."""
    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)
        if writer is not None:
            writer(text)

    def note(text: str) -> None:
        lines.append(text)

    reads: list[str] = []
    visits: list[str] = []
    audit_log = AuditLog()
    token = bind_correlation_id(_CORRELATION_ID)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    store: WorkflowStateStore | None = None
    started = time.perf_counter()
    try:
        path, temporary = _store_path(store_path)
        store = sqlite_workflow_store(path, _SCOPE, audit_log=audit_log)
        intake = Intake(reference=_REFERENCE, message=_MESSAGE)
        alex, taylor, jordan = _team(audit_log, reads)
        emit("AgentBuildKit demo")
        emit("mode: offline")
        if {alex.definition.provider, taylor.definition.provider, jordan.definition.provider} == {
            _PROVIDER
        }:
            emit("provider: scripted")
        emit("")
        emit("AGENTBUILDKIT TRACE")
        step = _step(emit, 0, "USER", [f"    {intake.message}"])

        try:
            routed = await run_workflow(
                manager_workflow(alex, {_ROUTE: taylor}, name=_ROUTE_WORKFLOW),
                envelope(_CORRELATION_ID, intake),
            )
        except RouteRejected:
            raise DemoError("The request could not be routed.") from None
        except WorkflowRunError as exc:
            raise DemoError(str(exc)) from None
        if routed.output is None:
            raise DemoError("Routing did not produce a specialist response.")
        try:
            draft = Draft.model_validate(routed.output.body)
        except ValueError:
            raise DemoError("Specialist output was not a valid draft.") from None

        route = routed.output.route
        if not isinstance(route, str) or route == "":
            raise DemoError("The request could not be routed.")
        alex_run = _completed_run(audit_log, "alex")
        taylor_run = _completed_run(audit_log, "taylor")
        step = _step(
            emit,
            step,
            *_block(agent_heading(alex_run), [f"    route: {route}"]),
        )
        tool_lines = [
            f"    tool: {_READ_TOOL}",
            f"    result: {_tool_result(audit_log, _READ_TOOL)}",
        ]
        step = _step(emit, step, *_block(agent_heading(taylor_run), tool_lines))
        step = _step(
            emit,
            step,
            "TOOL BOUNDARY",
            [f"    tool: {_REJECTED_TOOL}", f"    result: {_rejection_result(audit_log)}"],
        )
        if not draft.needs_action:
            raise DemoError("The specialist did not propose a side effect.")
        if visits:
            raise DemoError("A side effect ran before approval.")

        action = VisitRequest(reference=draft.reference, when=draft.when)
        visit = _visit_tool(visits)
        approval = approval_workflow(
            visit,
            audit_log,
            name=_ACTION_WORKFLOW,
            storage=store,
        )
        try:
            paused = await run_workflow(approval, envelope(_CORRELATION_ID, action))
        except WorkflowRunError as exc:
            raise DemoError(str(exc)) from None
        if paused.pending is None or visits:
            raise DemoError("The side effect did not pause for approval.")
        visits_before = len(visits)
        pending = _pending(store, paused.pending.request_id)
        step = _step(
            emit,
            step,
            "SIDE EFFECT",
            [f"    tool: {pending.action}", "    approval: required"],
        )
        _show_approval(emit, pending.action, action)
        approved = decision if decision is not None else _ask(note, emit, reader or input)
        try:
            resumed = await resume_approval(
                approval_workflow(
                    visit,
                    audit_log,
                    name=_ACTION_WORKFLOW,
                    storage=store,
                ),
                request_id=pending.request_id,
                checkpoint_id=pending.checkpoint_id,
                correlation_id=pending.correlation_id,
                action=pending.action,
                response=HumanApprovalResponse(approved=approved),
                arguments=action.model_dump(),
            )
        except WorkflowRunError as exc:
            raise DemoError(str(exc)) from None
        if resumed.output is None:
            raise DemoError("The approval workflow did not finish.")
        status = _text(resumed.output.body, "status")
        _require_effect(approved, status, visits)
        if approved:
            emit("✓ Approved")
            emit(f"✓ {pending.action} executed")
        else:
            emit("✗ Rejected")
            emit(f"{pending.action} was not executed")

        try:
            review = await jordan.run(
                ReviewRequest(
                    reference=draft.reference,
                    summary=draft.summary,
                    action_status=status,
                ),
                correlation_id=_CORRELATION_ID,
            )
        except AgentOutputError:
            raise DemoError("Reviewer output was not valid.") from None
        step = _step(
            emit,
            step,
            *_block(
                agent_heading(_completed_run(audit_log, "jordan")),
                ["    structured output: valid"],
            ),
        )
        report, checks = await _evaluate(
            audit_log,
            route=route,
            review=review,
            started=started,
        )
        outcome = "PASS" if report.failed == 0 and report.total > 0 else "FAIL"
        _step(emit, step, "EVALUATION", [f"    result: {outcome}"])
        for line in summary(tuple(audit_log.events), report, checks):
            emit(line)
        ok = len(visits) == (1 if approved else 0) and all(passed for _, passed in checks)
        return DemoResult(
            ok=ok,
            approved=approved,
            reads=tuple(reads),
            visits=tuple(visits),
            visits_before_approval=visits_before,
            route=route,
            report=report,
            events=tuple(audit_log.events),
            text="\n".join(lines),
        )
    finally:
        reset_correlation_id(token)
        if store is not None:
            _close_store(store)
        if temporary is not None:
            temporary.cleanup()


def _team(audit_log: AuditLog, reads: list[str]):
    alex = create_agent(
        AgentDefinition(
            name="alex",
            purpose="Route a request to an allowlisted specialist.",
            instructions="Choose the service specialist for a facilities request.",
            input_type=Intake,
            output_type=RouteDecision,
            provider=_PROVIDER,
            model=_MODEL,
        ),
        client=_script(
            _model(
                RouteDecision(specialist=_ROUTE, reference=_REFERENCE, message=_MESSAGE),
            )
        ),
        audit_log=audit_log,
    )
    taylor = create_agent(
        AgentDefinition(
            name="taylor",
            purpose="Read a ticket and propose a visit.",
            instructions="Read the ticket. Do not cancel it and do not book the visit yourself.",
            input_type=RouteDecision,
            output_type=Draft,
            provider=_PROVIDER,
            model=_MODEL,
            tools=(_read_tool(reads),),
        ),
        client=_script(
            _call(_REJECTED_TOOL, {"reference": _REFERENCE}),
            _call(_READ_TOOL, {"reference": _REFERENCE}),
            _model(
                Draft(
                    reference=_REFERENCE,
                    summary="The loading-bay door is stuck.",
                    needs_action=True,
                    when=_WHEN,
                )
            ),
        ),
        audit_log=audit_log,
    )
    jordan = create_agent(
        AgentDefinition(
            name="jordan",
            purpose="Check the specialist's structured result.",
            instructions="Accept a review request only when its fields are present.",
            input_type=ReviewRequest,
            output_type=Review,
            provider=_PROVIDER,
            model=_MODEL,
        ),
        client=_script(_model(Review(accepted=True, response="Review recorded."))),
        audit_log=audit_log,
    )
    return alex, taylor, jordan


def _read_tool(reads: list[str]):
    def read_ticket(reference: str) -> str:
        """Read a facilities ticket."""
        reads.append(reference)
        return "The ticket is open. A technician has not been booked."

    return prepare_tool(read_ticket, effect=ToolEffect.READ, name=_READ_TOOL)


def _visit_tool(visits: list[str]):
    def schedule_visit(reference: str, when: str) -> str:
        """Book a technician. This changes an external system."""
        visits.append(reference)
        del when
        return "booked"

    return prepare_tool(schedule_visit, effect=ToolEffect.SIDE_EFFECT, name=_WRITE_TOOL)


def _script(*responses: str | ChatResponse) -> ScriptedChatClient:
    return ScriptedChatClient(list(responses))


def _model(value: RouteDecision | Draft | Review) -> str:
    return value.model_dump_json()


def _call(name: str, arguments: dict[str, str]) -> ChatResponse:
    return ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id=f"call-{name}",
                        name=name,
                        arguments=arguments,
                    )
                ],
            )
        ],
        response_id="scripted",
    )


def _show_approval(emit: Callable[[str], None], action: str, request: VisitRequest) -> None:
    emit("")
    emit("Approval required")
    emit("")
    emit("Agent: taylor")
    emit(f"Action: {action}")
    emit("Reason: This operation changes an external system.")
    emit("")
    emit("Arguments:")
    for key, value in request.model_dump().items():
        if any(part in key.lower() for part in _HIDDEN_ARGUMENT):
            continue
        emit(f"  {key}: {value}")
    emit("")


def _ask(
    note: Callable[[str], None],
    emit: Callable[[str], None],
    reader: Callable[[str], str],
) -> bool:
    prompt = "Approve? [y/N]: "
    for _ in range(3):
        try:
            answer = reader(prompt).strip().lower()
        except EOFError:
            raise DemoError("No approval decision was entered.") from None
        note(f"{prompt}{answer or 'N'}")
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no", ""}:
            return False
        emit("Enter y or n.")
    raise DemoError("Enter y or n.")


def _pending(store: WorkflowStateStore, request_id: str) -> PendingRequest:
    for row in store.list_pending(status="pending"):
        if row.request_id == request_id:
            return row
    raise DemoError("The approval checkpoint was not stored.")


def _completed_run(audit_log: AuditLog, name: str) -> AuditEvent:
    for event in audit_log.events:
        if (
            event.argument_hash == _AGENT_RUN
            and event.agent_name == name
            and event.outcome == "completed"
        ):
            return event
    raise DemoError(f"Agent '{name}' did not complete.")


def _text(body: Mapping[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or value == "":
        raise DemoError(f"The workflow result did not include {key}.")
    return value


def _tool_result(audit_log: AuditLog, name: str) -> str:
    if _completed_tool(audit_log, name):
        return "allowed"
    return "not recorded"


def _rejection_result(audit_log: AuditLog) -> str:
    if any(
        event.tool_name == _REJECTED_TOOL
        and event.approval == "rejected"
        and event.outcome == "failed"
        for event in audit_log.events
    ):
        return "rejected"
    return "not recorded"


def _completed_tool(audit_log: AuditLog, name: str) -> bool:
    return any(
        event.kind == "tool"
        and event.tool_name == name
        and event.outcome == "completed"
        and event.argument_hash != _AGENT_RUN
        for event in audit_log.events
    )


def _require_effect(approved: bool, status: str, visits: list[str]) -> None:
    if approved and (status != "completed" or len(visits) != 1):
        raise DemoError("The approved side effect did not run once.")
    if not approved and (status != "rejected" or visits):
        raise DemoError("The rejected side effect ran.")


def _step(emit: Callable[[str], None], step: int, title: str, lines: list[str]) -> int:
    step += 1
    emit("")
    emit(f"[{step}] {title}")
    for line in lines:
        emit(line)
    return step


def _block(heading: list[str], extra: list[str]) -> tuple[str, list[str]]:
    return heading[0], [*heading[1:], *extra]


async def _evaluate(
    audit_log: AuditLog,
    *,
    route: str,
    review: Review,
    started: float,
) -> tuple[EvaluationReport, tuple[tuple[str, bool], ...]]:
    approved = any(event.kind == "approval_accepted" for event in audit_log.events)
    case = EvaluationCase(
        name="offline-demo",
        correlation_id=_CORRELATION_ID,
        expectation=Expectation(
            require_valid_output=True,
            output={"accepted": True},
            route=_ROUTE,
            agents=("alex", "taylor", "jordan"),
            tools=(_READ_TOOL, _WRITE_TOOL) if approved else (_READ_TOOL,),
            forbidden_tools=() if approved else (_WRITE_TOOL,),
            approval_actions=(_WRITE_TOOL,),
            no_unapproved_write=True,
            rejected_tools=(_REJECTED_TOOL,),
        ),
    )
    observation = trace_from_audit(audit_log, correlation_id=_CORRELATION_ID).model_copy(
        update={
            "output_valid": True,
            "output": {"accepted": review.accepted},
            "route": route,
            "elapsed_seconds": time.perf_counter() - started,
        }
    )

    async def execute(item: EvaluationCase):
        if item.correlation_id != observation.correlation_id:
            raise DemoError("Evaluation case does not match this run.")
        return observation

    report = await run_evaluation((case,), execute)
    durable = any(
        event.kind in {"checkpoint_created", "workflow_paused"} for event in audit_log.events
    )
    checks = (
        ("deterministic routing", observation.route == _ROUTE),
        ("tool allowlist", _REJECTED_TOOL in observation.tools_rejected),
        (
            "side-effect approval",
            _WRITE_TOOL in observation.approval_requested and not observation.unapproved_writes,
        ),
        ("durable state", durable),
        ("structured output", observation.output_valid),
        (
            "audit trail",
            bool(observation.audit_correlation_ids)
            and all(item == _CORRELATION_ID for item in observation.audit_correlation_ids),
        ),
        ("evaluation", report.failed == 0 and report.total == 1),
    )
    return report, checks


def _store_path(store_path: Path | None) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if store_path is not None:
        return store_path, None
    temporary = tempfile.TemporaryDirectory()
    return Path(temporary.name) / "demo.sqlite", temporary


def _close_store(store: WorkflowStateStore) -> None:
    backend = getattr(store, "_backend", None)
    connection = getattr(backend, "_connection", None)
    if isinstance(connection, sqlite3.Connection):
        connection.close()
