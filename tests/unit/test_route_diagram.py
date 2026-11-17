"""Run-page routes follow the selected team's registered diagram."""

import re

from agentbuildkit.workflows.diagram import WorkflowDiagram, build_route
from agentbuildkit_templates.customer_service.dashboard_model import AuditItem, Run
from agentbuildkit_templates.customer_service.dashboard_view import Snapshot, render_page
from agentbuildkit_templates.customer_service.models import FinalOutcome, OutcomeStatus
from agentbuildkit_templates.customer_service.team import CUSTOMER_DIAGRAM
from agentbuildkit_templates.operations_reporting.team import REPORTING_DIAGRAM

_OPS = "corr-ops"
_CUSTOMER = "corr-customer"


def _event(
    correlation_id: str,
    agent: str,
    *,
    tool: str | None = None,
    kind: str = "tool",
    outcome: str = "completed",
    argument_hash: str = "agent-run",
    approval: str = "not_required",
) -> AuditItem:
    return AuditItem(
        correlation_id=correlation_id,
        agent=agent,
        tool=tool or agent,
        kind=kind,
        approval=approval,
        outcome=outcome,
        argument_hash=argument_hash,
        provider="openai",
        model="gpt-4.1-mini",
        recorded_at=None,
        event_id=None,
        run_id=None,
        durable=False,
    )


def _run(
    correlation_id: str,
    reference: str,
    specialist: str,
    status: OutcomeStatus = "completed",
) -> Run:
    outcome = FinalOutcome(
        reference=reference,
        status=status,
        specialist=specialist,
        response="Recorded outcome",
        correlation_id=correlation_id,
    )
    return Run(
        correlation_id=correlation_id,
        reference=reference,
        specialist=specialist,
        state="completed",
        response=outcome.response,
        history=(outcome,),
        pause=None,
    )


def _page(
    diagram: WorkflowDiagram,
    run: Run,
    audit: tuple[AuditItem, ...],
    **fields: object,
) -> str:
    snap = Snapshot(
        team_name=str(fields.get("team_name", "operations-reporting")),
        agents=(),
        runs=(run,),
        approvals=(),
        audit=audit,
        report=None,
        error="",
        draft=None,
        persistent=False,
        team_id=str(fields.get("team_id", "operations-reporting")),
        title=str(fields.get("title", "Operations Reporting")),
        form=str(fields.get("form", "operations")),
        subject_label=str(fields.get("subject_label", "As of")),
        sheet_agent=str(fields.get("sheet_agent", "analyst")),
        approvals_enabled=bool(fields.get("approvals_enabled", False)),
        diagram=diagram,
        actor_label=str(fields.get("actor_label", "Handled by")),
    )
    return render_page(snap, f"/runs/{run.correlation_id}").body


def _captions(page: str) -> dict[str, str]:
    pattern = r"<b>(?:<span class=\"mono\">)?([^<]+)(?:</span>)?</b><small>([^<]*)</small>"
    return dict(re.findall(pattern, page))


def test_operations_route_uses_only_its_registered_stages() -> None:
    audit = (
        _event(_OPS, "analyst"),
        _event(_OPS, "analyst", tool="read_jobs", argument_hash="ab" * 32),
        _event(_OPS, "reviewer"),
    )
    route = build_route(
        REPORTING_DIAGRAM,
        audit,
        correlation_id=_OPS,
        reference="2026-11-16",
    )
    assert [node.name for node in route.main] == ["Request", "Analyst", "Reviewer", "Report"]
    assert route.branches == ()
    assert [node.caption for node in route.main] == [
        "2026-11-16",
        "read_jobs",
        "Ran",
        "Produced",
    ]
    assert [node.state for node in route.main] == ["recorded", "recorded", "recorded", "recorded"]


def test_a_completed_outcome_does_not_record_stages_without_audit() -> None:
    route = build_route(
        REPORTING_DIAGRAM,
        (),
        correlation_id=_OPS,
        reference="2026-11-16",
    )
    assert [node.state for node in route.main] == ["recorded", "idle", "idle", "idle"]
    assert [node.caption for node in route.main[1:]] == [
        "Not recorded",
        "Not recorded",
        "Not recorded",
    ]


def test_operations_run_page_does_not_show_customer_service_agents() -> None:
    page = _page(
        REPORTING_DIAGRAM,
        _run(_OPS, "2026-11-16", "reviewer"),
        (
            _event(_OPS, "analyst"),
            _event(_OPS, "analyst", tool="read_jobs", argument_hash="ab" * 32),
            _event(_OPS, "reviewer"),
        ),
    )
    captions = _captions(page)
    assert 'class="trace cols-4"' in page
    assert list(captions) == ["Request", "Analyst", "Reviewer", "Report"]
    assert captions["Analyst"] == "read_jobs"
    assert captions["Report"] == "Produced"
    assert "Handled by" in page
    for name in ("Alex", "Sam", "Taylor", "Jordan", "You", "schedule_service", "Specialist"):
        assert name not in page
    team = render_page(
        Snapshot(
            team_name="operations-reporting",
            agents=(),
            runs=(),
            approvals=(),
            audit=(),
            report=None,
            error="",
            draft=None,
            persistent=False,
            title="Operations Reporting",
            form="operations",
            sheet_agent="analyst",
            diagram=REPORTING_DIAGRAM,
            approvals_enabled=False,
        ),
        "/team",
    ).body
    assert "Analyst reads the jobs" in team
    assert "Alex classifies" not in team
    connections = render_page(
        Snapshot(
            team_name="operations-reporting",
            agents=(),
            runs=(),
            approvals=(),
            audit=(),
            report=None,
            error="",
            draft=None,
            persistent=False,
            title="Operations Reporting",
            form="operations",
            sheet_agent="analyst",
            diagram=REPORTING_DIAGRAM,
        ),
        "/connections",
    ).body
    assert "Analyst's page" in connections
    assert "Sam's page" not in connections


def test_customer_route_keeps_its_branch_and_approval_stages() -> None:
    audit = (
        _event(_CUSTOMER, "alex"),
        _event(_CUSTOMER, "sam"),
        _event(_CUSTOMER, "sam", tool="read_account", argument_hash="cd" * 32),
        _event(_CUSTOMER, "jordan"),
    )
    route = build_route(
        CUSTOMER_DIAGRAM,
        audit,
        correlation_id=_CUSTOMER,
        reference="A-100",
    )
    assert [node.name for node in route.main] == [
        "Request",
        "Alex",
        "Sam",
        "Jordan",
        "You",
        "schedule_service",
    ]
    assert route.branches[0][1].name == "Taylor"
    assert route.branches[0][1].caption == "Configured, not used"
    assert route.branches[0][1].state == "idle"
    assert route.main[2].caption == "read_account"
    assert route.main[3].caption == "Ran"
    assert route.main[4].state == "idle"
    assert route.main[5].state == "idle"
    page = _page(
        CUSTOMER_DIAGRAM,
        _run(_CUSTOMER, "A-100", "sam"),
        audit,
        team_name="customer-service",
        team_id="customer-service",
        title="Customer Service",
        form="customer",
        subject_label="Customer",
        sheet_agent="sam",
        approvals_enabled=True,
        actor_label="Specialist",
    )
    assert 'class="trace cols-6"' in page
    assert _captions(page)["Taylor"] == "Configured, not used"
    assert "Specialist" in page
    assert "Approved the draft" not in page
