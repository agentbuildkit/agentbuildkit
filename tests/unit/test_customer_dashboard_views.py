"""Dashboard pages show recorded state honestly and never change execution."""

from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import pytest
from agent_framework import SecretString

from agentbuildkit.evaluation import AssertionResult, CaseResult, EvaluationReport
from agentbuildkit.observability import AuditEvent, AuditLog
from agentbuildkit.workflows import (
    LedgerRecord,
    PendingRequest,
    WorkflowRunError,
    memory_workflow_store,
    sqlite_workflow_store,
)
from customer_service.dashboard import DashboardBinding, DashboardService, serve
from customer_service.dashboard_model import (
    ALREADY_HANDLED,
    MISSING_ARGUMENTS,
    UNRESOLVED_RESPONSE,
    Pause,
    build_runs,
    merge_audit,
    outcome_state,
    pause_state,
)
from customer_service.evaluation import LOCAL_MOCK_TOKEN, scripted_clients
from customer_service.models import FinalOutcome, OutcomeStatus
from customer_service.recovery import SqliteActionRecovery
from customer_service.team import ORGANISATION_SCOPE, build_team


def _flat(text: str) -> str:
    """Collapse whitespace, so wrapped HTML text compares as it reads."""
    return " ".join(text.split())


PAGES = ("/", "/runs", "/approvals", "/audit", "/evaluation", "/team", "/?new=1")


def _silent(**_kwargs: object) -> None:
    return None


async def _report() -> EvaluationReport:
    return EvaluationReport(
        total=2,
        passed=1,
        failed=1,
        pass_rate=0.5,
        elapsed_seconds=0.25,
        failures_by_assertion={"tool_forbidden": 1},
        cases=(
            CaseResult(
                name="billing-routes-to-sam",
                passed=True,
                assertions=(AssertionResult(name="route", passed=True, message="Routed."),),
                elapsed_seconds=0.1,
                tool_call_count=2,
                correlation_id="corr-billing",
            ),
            CaseResult(
                name="jordan-rejects-poor-response",
                passed=False,
                assertions=(
                    AssertionResult(
                        name="tool_forbidden",
                        passed=False,
                        message="A forbidden tool was called.",
                    ),
                ),
                elapsed_seconds=0.1,
                tool_call_count=3,
                correlation_id="corr-quality",
            ),
        ),
    )


def _memory(case: str, actions: list[str] | None = None) -> DashboardService:
    audit_log = AuditLog()
    organisation = build_team(
        clients=scripted_clients(case),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=audit_log,
        actions=actions if actions is not None else [],
        configurator=_silent,
    )
    return DashboardService(
        organisation,
        memory_workflow_store(ORGANISATION_SCOPE, audit_log),
        evaluate=_report,
    )


def _durable(case: str, actions: list[str], directory: Path) -> DashboardService:
    audit_log = AuditLog()
    organisation = build_team(
        clients=scripted_clients(case),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=audit_log,
        actions=actions,
        configurator=_silent,
    )
    return DashboardService(
        organisation,
        sqlite_workflow_store(directory / "workflows.sqlite", ORGANISATION_SCOPE, audit_log),
        recovery=SqliteActionRecovery(directory / "action-recovery.sqlite"),
        evaluate=_report,
    )


def _outcome(status: OutcomeStatus, response: str, cid: str = "c1") -> FinalOutcome:
    return FinalOutcome(
        reference="A-100",
        status=status,
        specialist="taylor",
        response=response,
        correlation_id=cid,
    )


def _pending(status: str = "pending") -> PendingRequest:
    return PendingRequest.model_validate(
        {
            "scope": ORGANISATION_SCOPE,
            "workflow_name": "service-action",
            "run_id": "run-1",
            "correlation_id": "c1",
            "request_id": "r1",
            "action": "schedule_service",
            "checkpoint_id": "k1",
            "created_at": "2026-11-12T19:14:07+00:00",
            "status": status,
        }
    )


def test_outcomes_keep_distinct_names() -> None:
    assert outcome_state(_outcome("completed", "done")) == "completed"
    assert outcome_state(_outcome("pending_approval", "wait")) == "waiting"
    assert outcome_state(_outcome("escalated", "x")) == "escalated"
    assert outcome_state(_outcome("route_rejected", "x")) == "not_routed"
    assert outcome_state(_outcome("rejected", UNRESOLVED_RESPONSE)) == "unresolved"
    assert outcome_state(_outcome("rejected", MISSING_ARGUMENTS)) == "not_run"
    assert outcome_state(_outcome("rejected", ALREADY_HANDLED)) == "declined"
    assert outcome_state(_outcome("rejected", "The service action was not approved.")) == (
        "declined"
    )
    assert outcome_state(_outcome("rejected", "The proposed response was not accepted.")) == (
        "review_rejected"
    )


def test_store_state_separates_failed_from_unresolved() -> None:
    def pause(status: str, effect: str | None) -> Pause:
        return Pause(request=_pending(status), effect=effect, reference="A-100", arguments=None)

    assert pause_state(pause("pending", None)) == "waiting"
    assert pause_state(pause("pending", "started")) == "unresolved"
    assert pause_state(pause("failed", "failed")) == "failed"
    assert pause_state(pause("failed", "started")) == "unresolved"
    assert pause_state(pause("completed", "completed")) == "completed"
    assert pause_state(pause("rejected", "rejected")) == "declined"


def test_runs_group_by_correlation_and_keep_history() -> None:
    outcomes = [
        _outcome("pending_approval", "The service action is waiting for approval.", "a"),
        _outcome("completed", "Answered.", "b"),
        _outcome("completed", "The service action was completed.", "a"),
    ]
    runs = build_runs(outcomes, {})
    assert [run.correlation_id for run in runs] == ["a", "b"]
    assert runs[0].state == "completed"
    assert [item.status for item in runs[0].history] == ["pending_approval", "completed"]


def test_stale_outcome_defers_to_the_store() -> None:
    outcomes = [_outcome("pending_approval", "The service action is waiting for approval.")]
    pause = Pause(request=_pending("failed"), effect="failed", reference="A-100", arguments=None)
    run = build_runs(outcomes, {"c1": pause})[0]
    assert run.state == "failed"
    assert "error" in run.response


def test_audit_merge_keeps_repeats_and_uses_ledger_times() -> None:
    event = AuditEvent(
        correlation_id="c1",
        agent_name=None,
        tool_name="schedule_service",
        approval="pending",
        outcome="pending_approval",
        argument_hash="a" * 64,
        kind="workflow_paused",
    )
    agent_run = AuditEvent(
        correlation_id="c1",
        agent_name="alex",
        tool_name="alex",
        approval="not_required",
        outcome="completed",
        argument_hash="agent-run",
    )
    ledger = (
        LedgerRecord(
            scope=ORGANISATION_SCOPE,
            event_id="e1",
            recorded_at="2026-11-12T19:14:07+00:00",
            correlation_id="c1",
            tool_name="schedule_service",
            approval="pending",
            outcome="pending_approval",
            argument_hash="a" * 64,
            kind="workflow_paused",
        ),
    )
    items = merge_audit(ledger, [agent_run, event, event])
    assert [item.kind for item in items] == ["tool", "workflow_paused", "workflow_paused"]
    assert items[0].is_agent_run and items[0].recorded_at is None
    assert [item.event_id for item in items[1:]] == ["e1", None]


async def test_every_page_renders_without_secrets() -> None:
    service = _memory("service-action-pauses-for-approval")
    try:
        await service.submit("A-100", "The service request is still open.")
        await service.run_evaluation()
        correlation = service.outcomes[-1].correlation_id
        pages = [service.page(path.split("?")[0], _query(path)) for path in PAGES]
        bodies = [_flat(page.body) for page in pages]
        detail = service.page(f"/runs/{correlation}")
        missing = service.page("/runs/not-a-run")
        invalid = service.page("/runs/<script>")
    finally:
        await service.organisation.aclose()

    for page in (*pages, detail):
        assert page.status == 200
        assert LOCAL_MOCK_TOKEN not in page.body
        assert "RAW-MODEL-OUTPUT" not in page.body
    assert missing.status == 404
    assert invalid.status == 404
    detail_text = _flat(detail.body)
    assert "Waiting for approval" in detail_text
    assert "name='request_id'" in detail_text
    assert "Recorded in this run" in detail_text
    evaluation = bodies[PAGES.index("/evaluation")]
    assert "Scripted offline evaluation." in evaluation
    assert "1 of 2 passed." in evaluation
    assert "A forbidden tool was called." in evaluation
    assert "Demo data" in bodies[0]
    assert "New request" in bodies[-1]
    assert "exactly-once" not in bodies[0]


def _query(path: str) -> dict[str, str]:
    if "?" not in path:
        return {}
    key, value = path.split("?", 1)[1].split("=", 1)
    return {key: value}


async def test_rejected_approval_reads_as_declined() -> None:
    service = _memory("service-action-pauses-for-approval")
    try:
        await service.submit("A-100", "The service request is still open.")
        request_id = service.store.list_pending(status="pending")[0].request_id
        outcome = await service.decide(request_id, approved=False)
        detail = _flat(service.render(f"/runs/{outcome.correlation_id}"))
        approvals = _flat(service.render("/approvals"))
    finally:
        await service.organisation.aclose()

    assert "Declined" in detail
    assert "Rejected. Nothing was sent." in detail
    assert "Nothing waiting for approval" in approvals
    assert "name='request_id'" not in detail


async def test_failed_action_is_not_shown_as_unresolved_or_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []
    first = _durable("service-action-pauses-for-approval", actions, tmp_path)
    try:
        await first.submit("A-100", "The service request is still open.")
        request_id = first.store.list_pending(status="pending")[0].request_id
    finally:
        await first.organisation.aclose()

    def fail(system: object, request: object) -> object:
        del system, request
        raise RuntimeError("private failure detail")

    monkeypatch.setattr("customer_service.business._schedule", fail)
    second = _durable("approved-service-action-executes-once", actions, tmp_path)
    try:
        with pytest.raises(WorkflowRunError):
            await second.decide(request_id, approved=True)
        correlation = second.store.list_pending()[0].correlation_id
        overview = _flat(second.render())
        detail = _flat(second.render(f"/runs/{correlation}"))
    finally:
        await second.organisation.aclose()

    assert actions == []
    assert "Service action failed for A-100" in overview
    assert "ended with an error" in detail
    assert "Waiting for approval</span></div><p>" not in detail
    assert "private failure detail" not in detail
    assert "name='request_id'" not in detail


async def test_unresolved_action_explains_what_to_check(tmp_path: Path) -> None:
    actions: list[str] = []
    first = _durable("service-action-pauses-for-approval", actions, tmp_path)
    try:
        await first.submit("A-100", "The service request is still open.")
        request_id = first.store.list_pending(status="pending")[0].request_id
    finally:
        await first.organisation.aclose()
    second = _durable("approved-service-action-executes-once", actions, tmp_path)

    def stop_before_completion(key: str, status: str) -> None:
        del key, status
        raise RuntimeError("stopped")

    second.store.finish_effect = stop_before_completion  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await second.decide(request_id, approved=True)
    finally:
        await second.organisation.aclose()

    third = _durable("approved-service-action-executes-once", actions, tmp_path)
    try:
        correlation = third.store.list_pending()[0].correlation_id
        overview = _flat(third.render())
        detail = _flat(third.render(f"/runs/{correlation}"))
    finally:
        await third.organisation.aclose()

    assert actions == ["A-100:schedule_visit"]
    assert "Unknown outcome for schedule_visit on A-100" in overview
    assert "is unknown" in detail
    assert "Check account A-100" in detail
    assert "Started, not confirmed" in detail
    assert "the workflow store did not" in detail
    assert "name='request_id'" not in detail


async def test_missing_arguments_disable_approval_only(tmp_path: Path) -> None:
    actions: list[str] = []
    first = _durable("service-action-pauses-for-approval", actions, tmp_path)
    try:
        await first.submit("A-100", "The service request is still open.")
    finally:
        await first.organisation.aclose()
    (tmp_path / "action-recovery.sqlite").rename(tmp_path / "moved.sqlite")
    second = _durable("approved-service-action-executes-once", actions, tmp_path)
    try:
        approvals = _flat(second.render("/approvals"))
    finally:
        await second.organisation.aclose()

    assert "Can&#x27;t approve" in approvals
    assert '<button class="btn" type="button" disabled>Approve</button>' in approvals
    assert 'value="reject">Reject</button>' in approvals
    assert 'value="approve"' not in approvals


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def _url(binding: DashboardBinding, path: str) -> str:
    host, port = binding.server_address
    return f"http://{host}:{port}{path}"


def test_static_files_are_allowlisted_and_pages_carry_headers() -> None:
    service = _memory("informational-completes-without-approval")
    binding = serve(service, port=0)
    try:
        with urlopen(_url(binding, "/static/dashboard.css"), timeout=10) as response:
            css = response.read().decode()
            css_type = response.headers["Content-Type"]
        with urlopen(_url(binding, "/static/fonts/geist-mono-latin-400-normal.woff2")) as font:
            font_type = font.headers["Content-Type"]
        with urlopen(_url(binding, "/"), timeout=10) as page:
            policy = page.headers["Content-Security-Policy"]
            nosniff = page.headers["X-Content-Type-Options"]
        codes: list[int] = []
        for path in ("/static/../dashboard.py", "/static/fonts/OFL-GeistMono.txt", "/nope"):
            with pytest.raises(HTTPError) as caught:
                urlopen(_url(binding, path), timeout=10)
            codes.append(caught.value.code)
    finally:
        binding.shutdown()

    assert "Instrument Sans" in css
    assert css_type.startswith("text/css")
    assert font_type == "font/woff2"
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert nosniff == "nosniff"
    assert codes == [404, 404, 404]


def test_failed_request_keeps_the_draft_without_raw_errors() -> None:
    service = _memory("informational-completes-without-approval")

    async def broken(reference: str, message: str) -> FinalOutcome:
        del reference, message
        raise RuntimeError("private-provider-payload")

    service.submit = broken  # type: ignore[method-assign]
    binding = serve(service, port=0)
    try:
        opener = build_opener(_NoRedirect)
        request = Request(
            _url(binding, "/run"),
            data=b"reference=A-100&message=Please+book+a+visit.",
            method="POST",
        )
        with pytest.raises(HTTPError) as caught:
            opener.open(request, timeout=10)
        location = caught.value.headers["Location"]
        with urlopen(_url(binding, location), timeout=10) as response:
            page = _flat(response.read().decode())
    finally:
        binding.shutdown()

    assert caught.value.code == 303
    assert location == "/?new=1"
    assert "The request didn't run." in page
    assert "unclassified" in page
    assert "Please book a visit." in page
    assert "private-provider-payload" not in page


def test_unknown_approval_is_reported_plainly() -> None:
    service = _memory("informational-completes-without-approval")
    binding = serve(service, port=0)
    try:
        request = Request(
            _url(binding, "/decide"),
            data=b"request_id=missing&decision=approve",
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            page = _flat(response.read().decode())
            final = response.geturl()
    finally:
        binding.shutdown()

    assert final.endswith("/approvals")
    assert "could not be found" in page
