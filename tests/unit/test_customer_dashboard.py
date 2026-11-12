"""The local dashboard uses the customer-service application service."""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from agent_framework import SecretString

from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.observability import AuditLog
from agentbuildkit.workflows import WorkflowRunError, memory_workflow_store, sqlite_workflow_store
from customer_service.dashboard import DashboardBinding, DashboardService, serve
from customer_service.evaluation import LOCAL_MOCK_TOKEN, scripted_clients
from customer_service.models import FinalOutcome
from customer_service.recovery import SqliteActionRecovery
from customer_service.team import ORGANISATION_SCOPE, build_team


def _silent(**_kwargs: object) -> None:
    return None


async def _report() -> EvaluationReport:
    return EvaluationReport(
        total=1,
        passed=1,
        failed=0,
        pass_rate=1.0,
        elapsed_seconds=0.0,
        failures_by_assertion={},
        cases=(),
    )


def _service(case: str, actions: list[str] | None = None) -> tuple[DashboardService, list[str]]:
    recorded = actions if actions is not None else []
    audit_log = AuditLog()
    organisation = build_team(
        clients=scripted_clients(case),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=audit_log,
        actions=recorded,
        configurator=_silent,
    )
    service = DashboardService(
        organisation,
        memory_workflow_store(ORGANISATION_SCOPE, audit_log),
        evaluate=_report,
    )
    return service, recorded


async def test_dashboard_runs_a_read_and_shows_audit() -> None:
    service, _actions = _service("billing-routes-to-sam")
    try:
        outcome = await service.submit("A-100", "Please explain the latest invoice.")
        await service.run_evaluation()
        page = service.render()
    finally:
        await service.organisation.aclose()

    assert outcome.status == "completed"
    assert "alex" in page
    assert "1 of 1 passed." in page
    assert LOCAL_MOCK_TOKEN not in page
    assert "RAW-MODEL-OUTPUT" not in page
    service.outcomes.append(
        FinalOutcome(
            reference="A-100",
            status="completed",
            specialist="sam",
            response="<script>alert(1)</script>",
            correlation_id="escape",
        )
    )
    escaped = service.render()
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


async def test_dashboard_reject_does_not_run_the_side_effect() -> None:
    service, actions = _service("service-action-pauses-for-approval")
    try:
        paused = await service.submit("A-100", "The service request is still open.")
        request_id = service.store.list_pending(status="pending")[0].request_id
        outcome = await service.decide(request_id, approved=False)
    finally:
        await service.organisation.aclose()

    assert paused.status == "pending_approval"
    assert outcome.status == "rejected"
    assert actions == []


def _durable(
    case: str,
    actions: list[str],
    workflow: Path,
    recovery: Path,
) -> DashboardService:
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
        sqlite_workflow_store(workflow, ORGANISATION_SCOPE, audit_log),
        recovery=SqliteActionRecovery(recovery),
        evaluate=_report,
    )


def _stored_text(path: Path) -> str:
    parts = [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
    text = ""
    for item in parts:
        if item.exists():
            text += item.read_bytes().decode("utf-8", errors="ignore")
    return text


async def _pause(
    tmp_path: Path,
    actions: list[str],
) -> tuple[Path, Path, str]:
    workflow = tmp_path / "workflows.sqlite"
    recovery = tmp_path / "action-recovery.sqlite"
    service = _durable("service-action-pauses-for-approval", actions, workflow, recovery)
    try:
        await service.submit("A-100", "The service request is still open.")
        request_id = service.store.list_pending(status="pending")[0].request_id
    finally:
        await service.organisation.aclose()
    return workflow, recovery, request_id


async def test_pending_approval_survives_a_fresh_dashboard(tmp_path: Path) -> None:
    actions: list[str] = []
    workflow, recovery, request_id = await _pause(tmp_path, actions)
    assert "schedule_visit" not in _stored_text(workflow)
    assert LOCAL_MOCK_TOKEN not in _stored_text(workflow)
    assert "schedule_visit" in _stored_text(recovery)
    assert LOCAL_MOCK_TOKEN not in _stored_text(recovery)

    service = _durable("approved-service-action-executes-once", actions, workflow, recovery)
    try:
        page = service.render()
        outcome = await service.decide(request_id, approved=True)
        repeated = await service.decide(request_id, approved=True)
    finally:
        await service.organisation.aclose()

    assert "A-100" in page
    assert "pending_approval" in page
    assert "workflow_paused" in page
    assert LOCAL_MOCK_TOKEN not in page
    assert outcome.status == "completed"
    assert outcome.reference == "A-100"
    assert repeated.response == "The service action was already handled."
    assert actions == ["A-100:schedule_visit"]


async def test_missing_recovered_arguments_are_not_executed(tmp_path: Path) -> None:
    actions: list[str] = []
    workflow, _recovery, request_id = await _pause(tmp_path, actions)
    service = _durable(
        "approved-service-action-executes-once",
        actions,
        workflow,
        tmp_path / "empty-recovery.sqlite",
    )
    try:
        visible = service.render()
        refused = await service.decide(request_id, approved=True)
        rejected = await service.decide(request_id, approved=False)
    finally:
        await service.organisation.aclose()

    assert "arguments were not recovered" in visible
    assert "were not recovered" in refused.response
    assert rejected.response == "The service action was not approved."
    assert actions == []


async def test_rejection_after_restart_executes_nothing(tmp_path: Path) -> None:
    actions: list[str] = []
    workflow, recovery, request_id = await _pause(tmp_path, actions)
    service = _durable("service-action-pauses-for-approval", actions, workflow, recovery)
    try:
        outcome = await service.decide(request_id, approved=False)
        repeated = await service.decide(request_id, approved=True)
    finally:
        await service.organisation.aclose()

    assert outcome.status == "rejected"
    assert outcome.reference == "A-100"
    assert repeated.response == "The service action was already handled."
    assert actions == []


async def test_concurrent_approval_executes_one_action(tmp_path: Path) -> None:
    actions: list[str] = []
    workflow, recovery, request_id = await _pause(tmp_path, actions)
    first = _durable("approved-service-action-executes-once", actions, workflow, recovery)
    second = _durable("approved-service-action-executes-once", actions, workflow, recovery)
    try:
        results = await asyncio.gather(
            first.decide(request_id, approved=True),
            second.decide(request_id, approved=True),
        )
    finally:
        await first.organisation.aclose()
        await second.organisation.aclose()

    assert actions == ["A-100:schedule_visit"]
    assert any(item.status == "completed" for item in results)
    assert any("already" in item.response or "unresolved" in item.response for item in results)


async def test_completed_and_rejected_stay_closed_after_restart(tmp_path: Path) -> None:
    approved_actions: list[str] = []
    workflow, recovery, request_id = await _pause(tmp_path, approved_actions)
    approving = _durable(
        "approved-service-action-executes-once",
        approved_actions,
        workflow,
        recovery,
    )
    try:
        await approving.decide(request_id, approved=True)
    finally:
        await approving.organisation.aclose()
    reopened = _durable(
        "approved-service-action-executes-once",
        approved_actions,
        workflow,
        recovery,
    )
    try:
        page = reopened.render()
        outcome = await reopened.decide(request_id, approved=True)
    finally:
        await reopened.organisation.aclose()

    assert "completed" in page
    assert "workflow_paused" in page
    assert outcome.response == "The service action was already handled."
    assert approved_actions == ["A-100:schedule_visit"]

    rejected_actions: list[str] = []
    rejected_workflow, rejected_recovery, rejected_id = await _pause(
        tmp_path / "rejected",
        rejected_actions,
    )
    declining = _durable(
        "service-action-pauses-for-approval",
        rejected_actions,
        rejected_workflow,
        rejected_recovery,
    )
    try:
        await declining.decide(rejected_id, approved=False)
    finally:
        await declining.organisation.aclose()
    reopened_rejection = _durable(
        "service-action-pauses-for-approval",
        rejected_actions,
        rejected_workflow,
        rejected_recovery,
    )
    try:
        rejection = await reopened_rejection.decide(rejected_id, approved=True)
    finally:
        await reopened_rejection.organisation.aclose()

    assert rejection.response == "The service action was already handled."
    assert rejected_actions == []


async def test_interrupted_execution_is_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []
    workflow, recovery, request_id = await _pause(tmp_path, actions)

    def fail_before_action(system: object, request: object) -> object:
        del system, request
        raise RuntimeError("interrupted before the business call")

    monkeypatch.setattr("customer_service.business._schedule", fail_before_action)
    failed = _durable("approved-service-action-executes-once", actions, workflow, recovery)
    try:
        with pytest.raises(WorkflowRunError):
            await failed.decide(request_id, approved=True)
    finally:
        await failed.organisation.aclose()
    monkeypatch.undo()

    retry = _durable("approved-service-action-executes-once", actions, workflow, recovery)
    try:
        outcome = await retry.decide(request_id, approved=True)
    finally:
        await retry.organisation.aclose()

    assert actions == []
    assert outcome.response == "The service action is unresolved and will not be repeated."


async def test_interrupted_completion_is_not_replayed(tmp_path: Path) -> None:
    actions: list[str] = []
    workflow, recovery, request_id = await _pause(tmp_path, actions)
    service = _durable("approved-service-action-executes-once", actions, workflow, recovery)

    def fail_completion(key: str, status: str) -> None:
        del key, status
        raise RuntimeError("interrupted before completion was recorded")

    service.store.finish_effect = fail_completion  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await service.decide(request_id, approved=True)
    finally:
        await service.organisation.aclose()

    retry = _durable("approved-service-action-executes-once", actions, workflow, recovery)
    try:
        outcome = await retry.decide(request_id, approved=True)
        page = retry.render()
    finally:
        await retry.organisation.aclose()

    assert actions == ["A-100:schedule_visit"]
    assert outcome.response == "The service action is unresolved and will not be repeated."
    assert "unresolved" in page
    assert LOCAL_MOCK_TOKEN not in page


def _http(
    binding: DashboardBinding,
    method: str,
    path: str,
    body: bytes | None = None,
) -> str:
    host, port = binding.server_address
    request = Request(f"http://{host}:{port}{path}", data=body, method=method)
    with urlopen(request, timeout=10) as response:
        return response.read().decode()


def _request_ids(page: str) -> list[str]:
    return re.findall(r"name='request_id' value='([^']+)'", page)


def test_dashboard_http_posts_a_run() -> None:
    service, _actions = _service("informational-completes-without-approval")
    binding = serve(service, port=0)
    try:
        host, port = binding.server_address
        request = Request(
            f"http://{host}:{port}/run",
            data=b"reference=A-100&message=Please+explain+the+latest+invoice.",
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            page = response.read().decode()
    finally:
        binding.shutdown()

    assert "completed" in page
    assert LOCAL_MOCK_TOKEN not in page


def test_http_disk_store_stays_on_one_thread(tmp_path: Path) -> None:
    """Browser threads must not touch SQLite connections opened on another thread."""
    workflow = tmp_path / "workflows.sqlite"
    recovery = tmp_path / "action-recovery.sqlite"
    actions: list[str] = []

    def open_service(case: str) -> DashboardService:
        return _durable(case, actions, workflow, recovery)

    first = serve(lambda: open_service("service-action-pauses-for-approval"), port=0)
    try:
        page = _http(first, "GET", "/")
        assert "customer-service" in page
        assert "SQLite" not in page
        paused = _http(
            first,
            "POST",
            "/run",
            b"reference=A-100&message=The+service+request+is+still+open.",
        )
        request_ids = _request_ids(paused)
        assert request_ids
        rejected = _http(
            first,
            "POST",
            "/decide",
            f"request_id={request_ids[0]}&decision=reject".encode(),
        )
        assert "was not approved" in rejected
        assert actions == []
    finally:
        first.shutdown()

    pages: list[str] = []
    approved_id = ""
    second = serve(lambda: open_service("approved-service-action-executes-once"), port=0)
    try:
        restored = _http(second, "GET", "/")
        assert "rejected" in restored
        assert "workflow_paused" in restored
        assert LOCAL_MOCK_TOKEN not in restored
        paused = _http(
            second,
            "POST",
            "/run",
            b"reference=A-100&message=The+service+request+is+still+open.",
        )
        request_ids = _request_ids(paused)
        assert len(request_ids) == 1
        approved_id = request_ids[0]
        body = f"request_id={approved_id}&decision=approve".encode()

        def post_decision(_item: int) -> str:
            return _http(second, "POST", "/decide", body)

        def read_page(_item: int) -> str:
            return _http(second, "GET", "/")

        with ThreadPoolExecutor(max_workers=4) as pool:
            pages.extend(pool.map(post_decision, range(4)))
            pages.extend(pool.map(read_page, range(2)))
    finally:
        second.shutdown()

    assert actions == ["A-100:schedule_visit"]
    assert any("completed" in page for page in pages)
    assert any("already" in page or "unresolved" in page for page in pages)
    assert all(LOCAL_MOCK_TOKEN not in page for page in pages)
    assert all("SQLite objects created in a thread" not in page for page in pages)

    third = serve(lambda: open_service("approved-service-action-executes-once"), port=0)
    try:
        reopened = _http(third, "GET", "/")
        closed = _http(
            third,
            "POST",
            "/decide",
            f"request_id={approved_id}&decision=approve".encode(),
        )
    finally:
        third.shutdown()

    assert "completed" in reopened
    assert "already handled" in closed
    assert actions == ["A-100:schedule_visit"]


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "invalid_api_key", "authentication failed"),
        (429, "insufficient_quota", "quota is exhausted"),
        (429, "rate_limit_exceeded", "limited this request"),
        (400, "invalid_request", "request format"),
    ],
)
def test_provider_errors_are_useful_without_exposing_payloads(
    status: int, code: str, expected: str
) -> None:
    import httpx2 as httpx
    from openai import AuthenticationError, BadRequestError, RateLimitError

    from customer_service.dashboard import describe_error

    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    error_type = {401: AuthenticationError, 429: RateLimitError, 400: BadRequestError}[status]
    cause = error_type("secret-key-and-prompt", response=response, body={"code": code})
    wrapper = RuntimeError("private-wrapper-message")
    wrapper.__cause__ = cause
    notice = describe_error(wrapper)
    assert expected in notice
    assert "secret-key-and-prompt" not in notice
    assert "private-wrapper-message" not in notice


def test_unknown_and_cyclic_errors_do_not_expose_details() -> None:
    from customer_service.dashboard import describe_error

    error = RuntimeError("private-data")
    error.__cause__ = error
    assert "unclassified" in describe_error(error)
    assert "private-data" not in describe_error(error)
