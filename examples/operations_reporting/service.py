"""Run the overdue report. Tool permission stays in the saved assignment."""

from agentbuildkit.integrations.composio import ComposioError
from agentbuildkit.workflows import (
    WorkflowStateStore,
    envelope,
    run_workflow,
    sequential_workflow,
)
from customer_service.models import FinalOutcome
from operations_reporting.jobs import mentioned_job_ids
from operations_reporting.models import JobsRequest, ReviewedReport
from operations_reporting.sheets import JobWindow
from operations_reporting.team import WORKFLOW_NAME, ReportingTeam

CONNECTED_FAILURE = "The connected spreadsheet could not be read. Demo jobs were not used."
_UNSUPPORTED = "Not supported by the supplied records."
_SUPPORTED = "Supported by the supplied records."


async def handle_report(
    team: ReportingTeam,
    request: JobsRequest,
    *,
    correlation_id: str,
    store: WorkflowStateStore,
    window: JobWindow,
) -> FinalOutcome:
    """Summarise overdue jobs and keep the result only when the records support it."""
    if store.scope != window.desk.team_id:
        raise RuntimeError("The report store does not match the team.")
    workflow = sequential_workflow(
        (team.analyst, team.reviewer),
        name=WORKFLOW_NAME,
    )
    try:
        result = await run_workflow(workflow, envelope(correlation_id, request))
    except (ComposioError, ValueError):
        return _failed(request.as_of, correlation_id)
    except Exception:
        if window.failed or window.desk.uses_sheet(window.desk.agent_name):
            return _failed(request.as_of, correlation_id)
        raise
    if window.failed:
        return _failed(request.as_of, correlation_id)
    if result.output is None:
        return _failed(request.as_of, correlation_id)
    try:
        report = ReviewedReport.model_validate(result.output.body)
    except Exception:
        return _failed(request.as_of, correlation_id)
    supported = _supported(window, report)
    verdict = _SUPPORTED if supported else _UNSUPPORTED
    text = f"{verdict}\n{report.summary}\nRecords:\n{report.records}"
    return FinalOutcome(
        reference=request.as_of,
        status="completed" if supported else "rejected",
        specialist="reviewer",
        response=text,
        correlation_id=correlation_id,
    )


def _supported(window: JobWindow, report: ReviewedReport) -> bool:
    if not window.called or window.failed:
        return False
    claimed = mentioned_job_ids(report.summary)
    expected = set(window.last_ids)
    return report.supported and claimed == expected


def _failed(as_of: str, correlation_id: str) -> FinalOutcome:
    return FinalOutcome(
        reference=as_of,
        status="rejected",
        specialist="analyst",
        response=CONNECTED_FAILURE,
        correlation_id=correlation_id,
    )


def report_is_closed(response: str) -> bool:
    """True when the visible report states whether the records support it."""
    return _UNSUPPORTED in response or _SUPPORTED in response or CONNECTED_FAILURE in response
