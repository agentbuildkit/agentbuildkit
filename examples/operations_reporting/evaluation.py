"""Offline evaluation for the overdue-jobs report."""

import json
from contextvars import ContextVar

from agent_framework import ChatResponse, Content, Message

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
from agentbuildkit.workflows import memory_workflow_store
from customer_service.connections import MemoryConnectionStore
from operations_reporting.models import JobsRequest
from operations_reporting.service import handle_report
from operations_reporting.sheets import JobWindow
from operations_reporting.team import TEAM_ID, build_team

_AS_OF = "2026-11-16"
_REVISION_BUNDLE: ContextVar[TeamBundle | None] = ContextVar(
    "reporting_revision_bundle", default=None
)


def reporting_cases() -> tuple[EvaluationCase, ...]:
    """Two scripted cases. Neither contacts a model or a spreadsheet."""
    return (
        EvaluationCase(
            name="overdue-report-is-supported",
            correlation_id="corr-overdue",
            expectation=Expectation(
                output={"status": "completed", "specialist": "reviewer"},
                agents=("analyst", "reviewer"),
                tools=("read_jobs",),
                forbidden_tools=("schedule_service", "GOOGLESHEETS_BATCH_GET"),
            ),
        ),
        EvaluationCase(
            name="invented-job-is-not-supported",
            correlation_id="corr-invented",
            expectation=Expectation(
                output={"status": "rejected", "specialist": "reviewer"},
                agents=("analyst", "reviewer"),
                tools=("read_jobs",),
            ),
        ),
    )


async def run_reporting_evaluation() -> EvaluationReport:
    """Run the reporting suite and return the platform report."""
    return await run_evaluation(reporting_cases(), _execute)


async def run_bundle_evaluation(bundle: TeamBundle) -> EvaluationReport:
    """Run the same scripted suite against one candidate revision."""
    token = _REVISION_BUNDLE.set(bundle)
    try:
        return await run_reporting_evaluation()
    finally:
        _REVISION_BUNDLE.reset(token)


async def _execute(case: EvaluationCase) -> CaseObservation:
    log = AuditLog()
    bundle = _REVISION_BUNDLE.get()
    scope = TEAM_ID if bundle is None else bundle.team_id
    window = JobWindow(MemoryConnectionStore(), None, team_id=scope)
    invented = case.name == "invented-job-is-not-supported"
    summary = "J-100 and J-999 are overdue." if invented else "J-100 is overdue."
    team = build_team(
        bundle=_REVISION_BUNDLE.get(),
        clients={
            "analyst": ScriptedChatClient(
                [
                    _call("read_jobs", {"as_of": _AS_OF}),
                    json.dumps(
                        {
                            "as_of": _AS_OF,
                            "records": (
                                "job_id, customer, due_date, status\n"
                                "J-100, Northwind, 2026-11-01, open"
                            ),
                            "summary": summary,
                        }
                    ),
                ]
            ),
            "reviewer": ScriptedChatClient(
                [
                    json.dumps(
                        {
                            "as_of": _AS_OF,
                            "records": "J-100",
                            "summary": summary,
                            "supported": True,
                            "notes": "",
                        }
                    )
                ]
            ),
        },
        audit_log=log,
        read_jobs=window.tool(),
    )
    try:
        outcome = await handle_report(
            team,
            JobsRequest(as_of=_AS_OF),
            correlation_id=case.correlation_id,
            store=memory_workflow_store(scope, log),
            window=window,
        )
    finally:
        await team.aclose()
    observation = trace_from_audit(log, correlation_id=case.correlation_id)
    return observation.model_copy(
        update={
            "output_valid": True,
            "output": {"status": outcome.status, "specialist": outcome.specialist},
        }
    )


def _call(name: str, arguments: dict[str, object]) -> ChatResponse:
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
