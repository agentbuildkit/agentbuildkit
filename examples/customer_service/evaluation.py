"""Offline evaluation cases for the customer-service example."""

import json
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from agent_framework import ChatResponse, Content, Message, SecretString

from agentbuildkit.evaluation import (
    CaseObservation,
    EvaluationCase,
    EvaluationReport,
    Expectation,
    run_evaluation,
    trace_from_audit,
)
from agentbuildkit.observability import AuditLog
from agentbuildkit.teams import TeamBundle
from agentbuildkit.testing import ScriptedChatClient
from agentbuildkit.workflows import memory_workflow_store, sqlite_workflow_store
from customer_service.models import CustomerRequest, FinalOutcome, RouteDecision
from customer_service.service import handle_request, resume_service_action
from customer_service.team import ORGANISATION_SCOPE, Organisation, build_team

LOCAL_MOCK_TOKEN = "local-mock-business-token"
_REVISION_BUNDLE: ContextVar[TeamBundle | None] = ContextVar(
    "customer_revision_bundle", default=None
)


def _scope() -> str:
    bundle = _REVISION_BUNDLE.get()
    return ORGANISATION_SCOPE if bundle is None else bundle.team_id


RAW_MODEL_OUTPUT = "RAW-MODEL-OUTPUT-7f3a"
_REFERENCE = "A-100"
_BILLING = CustomerRequest(reference=_REFERENCE, message="Please explain the latest invoice.")
_SERVICE = CustomerRequest(reference=_REFERENCE, message="The service request is still open.")
_ACCOUNT_RESPONSE = "Northwind's account is current."
_SERVICE_RESPONSE = "The service request is open."
_POOR_RESPONSE = "Whatever."


def organisation_cases() -> tuple[EvaluationCase, ...]:
    """The reference suite. Every case runs without a live model."""
    return (
        _case(
            "billing-routes-to-sam",
            "corr-billing",
            route="sam",
            output={"status": "completed", "specialist": "sam"},
            agents=("alex", "sam", "jordan"),
            forbidden_agents=("taylor",),
            tools=("read_account", "account.read"),
            forbidden_tools=("read_service", "schedule_service", "service.schedule"),
        ),
        _case(
            "service-routes-to-taylor",
            "corr-service",
            route="taylor",
            output={"status": "completed", "specialist": "taylor"},
            agents=("alex", "taylor", "jordan"),
            forbidden_agents=("sam",),
            tools=("read_service", "service.read"),
            forbidden_tools=("read_account", "schedule_service", "service.schedule"),
        ),
        _case(
            "unknown-route-fails-safely",
            "corr-unknown",
            output={"status": "route_rejected", "specialist": ""},
            agents=("alex",),
            forbidden_agents=("sam", "taylor", "jordan"),
            forbidden_tools=("read_account", "read_service", "schedule_service"),
            route_rejected=True,
        ),
        _case(
            "jordan-rejects-poor-response",
            "corr-quality",
            route="taylor",
            output={"status": "rejected", "specialist": "taylor"},
            agents=("alex", "taylor", "jordan"),
            tools=("read_service",),
            forbidden_tools=("schedule_service", "service.schedule"),
        ),
        _case(
            "informational-completes-without-approval",
            "corr-info",
            route="sam",
            output={"status": "completed", "specialist": "sam"},
            agents=("alex", "sam", "jordan"),
            tools=("read_account",),
            forbidden_tools=("schedule_service", "service.schedule"),
        ),
        _case(
            "service-action-pauses-for-approval",
            "corr-pause",
            route="taylor",
            output={"status": "pending_approval", "specialist": "taylor"},
            agents=("alex", "taylor", "jordan"),
            tools=("read_service", "service.read"),
            forbidden_tools=("service.schedule",),
            approvals=("schedule_service",),
        ),
        _case(
            "rejected-service-action-is-never-executed",
            "corr-declined",
            route="taylor",
            output={"status": "rejected"},
            agents=("alex", "taylor", "jordan"),
            forbidden_tools=("service.schedule",),
            approvals=("schedule_service",),
        ),
        _case(
            "approved-service-action-executes-once",
            "corr-approved",
            route="taylor",
            output={"status": "completed"},
            agents=("alex", "taylor", "jordan"),
            tools=("service.schedule", "schedule_service"),
            approvals=("schedule_service",),
        ),
        _case(
            "duplicate-approval-does-not-duplicate",
            "corr-duplicate",
            route="taylor",
            output={"status": "completed"},
            agents=("alex", "taylor", "jordan"),
            tools=("service.schedule",),
            approvals=("schedule_service",),
        ),
        _case(
            "specialist-cannot-use-other-tools",
            "corr-tools",
            route="sam",
            agents=("sam",),
            forbidden_agents=("taylor",),
            forbidden_tools=("read_service", "service.read", "schedule_service"),
            rejected_tools=("read_service",),
        ),
        _case(
            "secrets-stay-out-of-audit-and-evaluation",
            "corr-secrets",
            route="sam",
            output={"status": "completed"},
            secrets=(LOCAL_MOCK_TOKEN, RAW_MODEL_OUTPUT),
        ),
        _case(
            "correlation-survives-the-organisation",
            "corr-whole",
            route="taylor",
            output={"status": "completed"},
            agents=("alex", "taylor", "jordan"),
            tools=("read_service", "service.read", "service.schedule"),
            approvals=("schedule_service",),
        ),
        _case(
            "escalation-does-not-execute",
            "corr-escalate",
            route="taylor",
            output={"status": "escalated", "specialist": "taylor"},
            agents=("alex", "taylor", "jordan"),
            forbidden_tools=("schedule_service", "service.schedule"),
        ),
    )


async def run_organisation_evaluation() -> EvaluationReport:
    """Run the reference suite and return the platform report."""
    return await run_evaluation(organisation_cases(), execute_case)


async def run_bundle_evaluation(bundle: TeamBundle) -> EvaluationReport:
    """Run the same scripted suite against one candidate revision."""
    token = _REVISION_BUNDLE.set(bundle)
    try:
        return await run_organisation_evaluation()
    finally:
        _REVISION_BUNDLE.reset(token)


async def execute_case(case: EvaluationCase) -> CaseObservation:
    """Run one organisation case and describe it for the platform runner."""
    started = time.perf_counter()
    if case.name == "specialist-cannot-use-other-tools":
        observation = await _foreign_tool(case)
    elif case.name in {
        "approved-service-action-executes-once",
        "duplicate-approval-does-not-duplicate",
        "correlation-survives-the-organisation",
    }:
        observation = await _approved(case, duplicate=case.name.startswith("duplicate"))
    elif case.name == "rejected-service-action-is-never-executed":
        observation = await _declined(case)
    else:
        observation = await _direct(case)
    return observation.model_copy(update={"elapsed_seconds": time.perf_counter() - started})


async def _direct(case: EvaluationCase) -> CaseObservation:
    actions: list[str] = []
    audit_log = AuditLog()
    organisation = _organisation(scripted_clients(case.name), audit_log, actions)
    try:
        result = await handle_request(
            organisation,
            _request(case.name),
            correlation_id=case.correlation_id,
            store=memory_workflow_store(_scope(), audit_log),
        )
        _require_action_count(actions)
        return _observe(
            case,
            result.outcome,
            [audit_log],
            route=result.outcome.specialist or None,
            route_rejected=result.outcome.status == "route_rejected",
        )
    finally:
        await organisation.aclose()


async def _approved(case: EvaluationCase, *, duplicate: bool) -> CaseObservation:
    actions: list[str] = []
    first_log = AuditLog()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "organisation.sqlite"
        organisation = _organisation(scripted_clients(case.name), first_log, actions)
        try:
            store = sqlite_workflow_store(path, _scope(), first_log)
            result = await handle_request(
                organisation,
                _SERVICE,
                correlation_id=case.correlation_id,
                store=store,
            )
            if result.outcome.status != "pending_approval" or actions:
                raise RuntimeError("The service action ran before approval.")
            pending = store.list_pending(status="pending")
            if len(pending) != 1:
                raise RuntimeError("The paused action was not stored.")
        finally:
            await organisation.aclose()
        second_log = AuditLog()
        resumed = _organisation(_clients(), second_log, actions)
        try:
            restored = sqlite_workflow_store(path, _scope(), second_log)
            outcome = await resume_service_action(
                resumed,
                restored,
                pending[0],
                reference=_REFERENCE,
                approved=True,
                arguments=_action_arguments(),
            )
            if duplicate:
                outcome = await resume_service_action(
                    resumed,
                    restored,
                    pending[0],
                    reference=_REFERENCE,
                    approved=True,
                    arguments=_action_arguments(),
                )
            if actions != [f"{_REFERENCE}:schedule_visit"]:
                raise RuntimeError("The service action did not run once.")
        finally:
            await resumed.aclose()
    return _observe(case, outcome, [first_log, second_log], route="taylor")


async def _declined(case: EvaluationCase) -> CaseObservation:
    actions: list[str] = []
    first_log = AuditLog()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "organisation.sqlite"
        organisation = _organisation(scripted_clients(case.name), first_log, actions)
        try:
            store = sqlite_workflow_store(path, _scope(), first_log)
            result = await handle_request(
                organisation,
                _SERVICE,
                correlation_id=case.correlation_id,
                store=store,
            )
            if result.outcome.status != "pending_approval":
                raise RuntimeError("The service action did not pause.")
            pending = store.list_pending(status="pending")
        finally:
            await organisation.aclose()
        second_log = AuditLog()
        resumed = _organisation(_clients(), second_log, actions)
        try:
            restored = sqlite_workflow_store(path, _scope(), second_log)
            outcome = await resume_service_action(
                resumed,
                restored,
                pending[0],
                reference=_REFERENCE,
                approved=False,
            )
            outcome = await resume_service_action(
                resumed,
                restored,
                pending[0],
                reference=_REFERENCE,
                approved=True,
                arguments=_action_arguments(),
            )
            if actions or outcome.status != "rejected":
                raise RuntimeError("A declined service action ran.")
        finally:
            await resumed.aclose()
    return _observe(case, outcome, [first_log, second_log], route="taylor")


async def _foreign_tool(case: EvaluationCase) -> CaseObservation:
    audit_log = AuditLog()
    organisation = _organisation(scripted_clients(case.name), audit_log, [])
    try:
        await organisation.sam.run(
            RouteDecision(
                specialist="sam",
                category="billing",
                reference=_REFERENCE,
                message="Read the service record.",
            ),
            correlation_id=case.correlation_id,
        )
        if organisation.business.service_reads or organisation.business.actions:
            raise RuntimeError("Sam used a tool that is not assigned.")
        outcome = FinalOutcome(
            reference=_REFERENCE,
            status="rejected",
            specialist="sam",
            response="The specialist could not use that tool.",
            correlation_id=case.correlation_id,
        )
        return _observe(case, outcome, [audit_log], route="sam")
    finally:
        await organisation.aclose()


def _observe(
    case: EvaluationCase,
    outcome: FinalOutcome,
    logs: Sequence[AuditLog],
    *,
    route: str | None,
    route_rejected: bool = False,
) -> CaseObservation:
    merged = AuditLog()
    for audit_log in logs:
        for event in audit_log.events:
            merged.record(event)
    rendered = json.dumps(
        [event.model_dump() for event in merged.events],
        sort_keys=True,
        default=str,
    )
    trace = trace_from_audit(merged, correlation_id=case.correlation_id)
    return trace.model_copy(
        update={
            "output_valid": True,
            "output": outcome.model_dump(),
            "route": route,
            "route_rejected": route_rejected,
            "captured_text": rendered,
        }
    )


def _organisation(
    clients: Mapping[str, ScriptedChatClient],
    audit_log: AuditLog,
    actions: list[str],
) -> Organisation:
    return build_team(
        clients=clients,
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=audit_log,
        actions=actions,
        configurator=_silent,
        bundle=_REVISION_BUNDLE.get(),
    )


def _silent(**_kwargs: Any) -> None:
    return None


def _request(name: str) -> CustomerRequest:
    if name in {"service-routes-to-taylor", "service-action-pauses-for-approval"}:
        return _SERVICE
    if name in {"jordan-rejects-poor-response", "escalation-does-not-execute"}:
        return _SERVICE
    return _BILLING


def _require_action_count(actions: list[str]) -> None:
    if actions:
        raise RuntimeError("A service action ran without approval.")


def _action_arguments() -> dict[str, str]:
    return {"reference": _REFERENCE, "action": "schedule_visit"}


def scripted_clients(name: str) -> dict[str, ScriptedChatClient]:
    if name == "unknown-route-fails-safely":
        return _clients(alex=[_decision("outsider", "unknown")])
    if name == "specialist-cannot-use-other-tools":
        return _clients(
            sam=[
                _call("read_service", {"reference": _REFERENCE}),
                _proposal("sam", _ACCOUNT_RESPONSE, needs_action=False),
            ]
        )
    if name in {"service-routes-to-taylor"}:
        return _service_reply(needs_action=False)
    if name in {
        "service-action-pauses-for-approval",
        "rejected-service-action-is-never-executed",
        "approved-service-action-executes-once",
        "duplicate-approval-does-not-duplicate",
        "correlation-survives-the-organisation",
    }:
        return _service_reply(needs_action=True)
    if name == "jordan-rejects-poor-response":
        return _service_reply(needs_action=True, response=_POOR_RESPONSE, verdict="reject")
    if name == "escalation-does-not-execute":
        return _service_reply(needs_action=True, verdict="escalate")
    message = RAW_MODEL_OUTPUT if name == "secrets-stay-out-of-audit-and-evaluation" else "billing"
    return _billing_reply(message=message)


def _billing_reply(*, message: str) -> dict[str, ScriptedChatClient]:
    return _clients(
        alex=[_decision("sam", "billing", message=message)],
        sam=[
            _call("read_account", {"reference": _REFERENCE}),
            _proposal("sam", _ACCOUNT_RESPONSE, needs_action=False),
        ],
        jordan=[_quality("approve", _ACCOUNT_RESPONSE)],
    )


def _service_reply(
    *,
    needs_action: bool,
    response: str = _SERVICE_RESPONSE,
    verdict: str = "approve",
) -> dict[str, ScriptedChatClient]:
    action = "schedule_visit" if needs_action else None
    return _clients(
        alex=[_decision("taylor", "service")],
        taylor=[
            _call("read_service", {"reference": _REFERENCE}),
            _proposal("taylor", response, needs_action=needs_action, action=action),
        ],
        jordan=[_quality(verdict, response)],
    )


def _clients(
    *,
    alex: Sequence[str | ChatResponse] = (),
    sam: Sequence[str | ChatResponse] = (),
    taylor: Sequence[str | ChatResponse] = (),
    jordan: Sequence[str | ChatResponse] = (),
) -> dict[str, ScriptedChatClient]:
    return {
        "alex": ScriptedChatClient(alex),
        "sam": ScriptedChatClient(sam),
        "taylor": ScriptedChatClient(taylor),
        "jordan": ScriptedChatClient(jordan),
    }


def _decision(specialist: str, category: str, *, message: str = "classified") -> str:
    return json.dumps(
        {
            "specialist": specialist,
            "category": category,
            "reference": _REFERENCE,
            "message": message,
        }
    )


def _proposal(
    specialist: str,
    response: str,
    *,
    needs_action: bool,
    action: str | None = None,
) -> str:
    return json.dumps(
        {
            "specialist": specialist,
            "reference": _REFERENCE,
            "response": response,
            "needs_action": needs_action,
            "action": action,
        }
    )


def _quality(verdict: str, response: str) -> str:
    return json.dumps({"verdict": verdict, "reference": _REFERENCE, "response": response})


def _call(name: str, arguments: dict[str, Any]) -> ChatResponse:
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


def _case(
    name: str,
    correlation_id: str,
    *,
    route: str | None = None,
    output: dict[str, Any] | None = None,
    agents: tuple[str, ...] = (),
    forbidden_agents: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    forbidden_tools: tuple[str, ...] = (),
    approvals: tuple[str, ...] = (),
    rejected_tools: tuple[str, ...] = (),
    route_rejected: bool = False,
    secrets: tuple[str, ...] = (),
) -> EvaluationCase:
    return EvaluationCase(
        name=name,
        correlation_id=correlation_id,
        expectation=Expectation(
            require_valid_output=True,
            output=output,
            route=route,
            agents=agents,
            forbidden_agents=forbidden_agents,
            tools=tools,
            forbidden_tools=forbidden_tools,
            approval_actions=approvals,
            no_unapproved_write=True,
            expect_route_rejected=route_rejected,
            rejected_tools=rejected_tools,
            secrets_absent=secrets,
        ),
    )
