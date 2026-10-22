"""Offline external execution checks. No live Cursor calls."""

import inspect
import json
import sqlite3
from pathlib import Path
from typing import Literal

import httpx
import pytest
from agent_framework import SecretString
from pydantic import BaseModel

from agent_platform.config import load_cursor_settings, redacted_settings
from agent_platform.external_execution import (
    TOOL_DELEGATE_IMPLEMENTATION,
    TOOL_FOLLOW_UP_IMPLEMENTATION,
    ExecutionConflict,
    ExecutionContactError,
    ExecutionMalformed,
    ExecutionRejected,
    ExecutionService,
    ExternalExecutionRecord,
    FakeExecutionProvider,
    ProviderBranch,
    ProviderObservation,
    delegation_arguments,
    follow_up_arguments,
    register_execution_tool_permissions,
    render_assignment,
)
from agent_platform.guardrails import RepeatedGrantUse, ToolEffect, prepare_tool
from agent_platform.integrations import (
    CURSOR_API_BASE_URL,
    CursorCredentials,
    CursorExecutionProvider,
    HttpClient,
    cursor_header_provider,
    cursor_http_client,
)
from agent_platform.observability import AuditLog, argument_hash
from agent_platform.state.models import RegistryRejected
from agent_platform.workflows import (
    PendingHumanInput,
    WorkflowStateStore,
    approval_workflow,
    envelope,
    memory_workflow_store,
    run_workflow,
    sqlite_workflow_store,
)

BRANCH = "feature/org/1"
OTHER = "feature/org/2"
OUTSIDE = "feature/other/1"
SHA = "a" * 40
API_KEY = "cursor_secret_9f3a"
ACCEPTANCE = "acceptance-secret-9f3a"
AGENT = "bc-00000000-0000-4000-8000-000000000001"
RUN = "run-00000000-0000-4000-8000-000000000001"
REPO_URL = "https://github.com/acme/app"


class Notice(BaseModel):
    recipient: str


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> WorkflowStateStore:
    if request.param == "memory":
        created = memory_workflow_store("org-a")
    else:
        created = sqlite_workflow_store(tmp_path / "state.sqlite", "org-a")
    created.register_organisation("Example org", "1")
    register_execution_tool_permissions(created)
    created.register_tool_permission("implementation.note", "implementation")
    created.register_tool_permission("ship.merge", "merge")
    created.register_role(
        "implementer",
        [TOOL_DELEGATE_IMPLEMENTATION, TOOL_FOLLOW_UP_IMPLEMENTATION, "implementation.note"],
    )
    created.register_role("merger", ["ship.merge"], permission_class="merge")
    created.register_repository("app", "https://github.com/acme/app.git", "main")
    created.register_workflow("delivery")
    return created


async def test_valid_delegation_records_the_binding_and_assignment(
    store: WorkflowStateStore,
) -> None:
    binding = _prepared(store)
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")

    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )

    assert not isinstance(created, RepeatedGrantUse)
    assert created.provider == "fake"
    assert created.organisation == "Example org"
    assert created.work_item_id == "work-1"
    assert created.run_id == "run-1"
    assert created.repository == "app"
    assert created.branch == BRANCH
    assert created.grant_id == grant.grant_id
    assert created.status == "running"
    assert created.external_id.startswith("bc-")
    assert created.provider_run_id is not None
    assignment = provider.starts[0]
    assert assignment.schema_version == "1"
    assert assignment.authorised_branch == BRANCH
    assert assignment.repository_url == REPO_URL
    assert assignment.acceptance_source == "github-issue:7"
    assert assignment.prohibitions[0].startswith("Do not merge")
    assert ACCEPTANCE not in assignment.model_dump_json()
    assert grant.grant_id not in assignment.model_dump_json()
    assert "Do not merge to the default branch" in render_assignment(assignment)
    signals = [row for row in store.list_signals() if row.name == "implementation_started"]
    assert signals[0].outcome_status == "started"
    assert signals[0].subject_id == created.provider_run_id


async def test_missing_grant_does_not_start(store: WorkflowStateStore) -> None:
    _prepared(store)
    provider, service = _service(store)

    with pytest.raises(RegistryRejected):
        await service.delegate_implementation(
            work_item_id="work-1",
            grant_id="missing-grant",
            attempt_id="attempt-1",
            correlation_id="corr-exec",
        )

    assert provider.starts == []


async def test_wrong_permission_class_does_not_start(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    provider, service = _service(store)
    with pytest.raises(RegistryRejected):
        store.open_gate(
            "merge-gate",
            role="merger",
            work_item_id="work-1",
            stage="delivery",
            tools=[TOOL_DELEGATE_IMPLEMENTATION],
            correlation_id="corr-exec",
            argument_hash=argument_hash({}),
        )
    grant = _grant(
        store,
        binding,
        attempt_id="attempt-note",
        tools=["implementation.note"],
        name="note",
    )

    with pytest.raises(RegistryRejected):
        await service.delegate_implementation(
            work_item_id="work-1",
            grant_id=grant.grant_id,
            attempt_id="attempt-note",
            correlation_id="corr-exec",
        )

    assert provider.starts == []


async def test_unknown_repository_and_missing_binding_fail_closed(
    store: WorkflowStateStore,
) -> None:
    _item(store, run=True)
    provider, service = _service(store)
    gate = store.open_gate(
        "ship",
        role="implementer",
        work_item_id="work-1",
        stage="delivery",
        tools=[TOOL_DELEGATE_IMPLEMENTATION],
        correlation_id="corr-exec",
        argument_hash=argument_hash({}),
    )
    with pytest.raises(RegistryRejected):
        store.decide_gate(gate.gate_id, approved=True, repository="missing", branch_prefix=BRANCH)
    _approved, grant = store.decide_gate(
        gate.gate_id, approved=True, repository="app", branch_prefix="feature/org"
    )
    assert grant is not None

    with pytest.raises(RegistryRejected):
        await service.delegate_implementation(
            work_item_id="work-1",
            grant_id=grant.grant_id,
            attempt_id="attempt-1",
            correlation_id="corr-exec",
        )

    assert provider.starts == []


async def test_branch_outside_the_grant_or_on_a_protected_branch_does_not_start(
    store: WorkflowStateStore,
) -> None:
    binding = _prepared(store, ref=OUTSIDE)
    provider, service = _service(store)
    outside = _grant(store, binding, attempt_id="attempt-out", prefix="feature/org", name="outside")
    with pytest.raises(RegistryRejected):
        await service.delegate_implementation(
            work_item_id="work-1",
            grant_id=outside.grant_id,
            attempt_id="attempt-out",
            correlation_id="corr-exec",
        )
    with pytest.raises(RegistryRejected):
        store.record_github_ref(binding, ref="main", sha=SHA)
    assert provider.starts == []


async def test_two_authorised_branches_fail_closed(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    store.record_github_ref(binding, ref=OTHER, sha=SHA)
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")

    with pytest.raises(RegistryRejected):
        await service.delegate_implementation(
            work_item_id="work-1",
            grant_id=grant.grant_id,
            attempt_id="attempt-1",
            correlation_id="corr-exec",
        )

    assert provider.starts == []


async def test_duplicate_delegation_does_not_start_again(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    kwargs = {
        "work_item_id": "work-1",
        "grant_id": grant.grant_id,
        "attempt_id": "attempt-1",
        "correlation_id": "corr-exec",
    }

    created = await service.delegate_implementation(**kwargs)
    replay = await service.delegate_implementation(**kwargs)

    assert not isinstance(created, RepeatedGrantUse)
    assert isinstance(replay, RepeatedGrantUse)
    assert len(provider.starts) == 1
    assert len(store.list_external_executions("work-1")) == 1


async def test_startup_failure_can_retry_the_same_attempt(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    provider, service = _service(store)
    provider.start_errors.append(ExecutionRejected("rejected"))
    grant = _grant(store, binding, attempt_id="attempt-1")
    kwargs = {
        "work_item_id": "work-1",
        "grant_id": grant.grant_id,
        "attempt_id": "attempt-1",
        "correlation_id": "corr-exec",
    }

    with pytest.raises(ExecutionRejected):
        await service.delegate_implementation(**kwargs)
    failed = store.list_external_executions("work-1")[0]
    assert failed.status == "startup_failed"
    assert provider.starts == []

    created = await service.delegate_implementation(**kwargs)

    assert not isinstance(created, RepeatedGrantUse)
    assert created.status == "running"
    assert created.external_id == failed.external_id
    assert len(provider.starts) == 1
    assert len(store.list_external_executions("work-1")) == 1


async def test_lost_contact_does_not_start_another_execution(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    provider, service = _service(store)
    provider.start_errors.append(ExecutionContactError("lost"))
    provider.inspect_errors.append(ExecutionContactError("lost"))
    grant = _grant(store, binding, attempt_id="attempt-1")
    kwargs = {
        "work_item_id": "work-1",
        "grant_id": grant.grant_id,
        "attempt_id": "attempt-1",
        "correlation_id": "corr-exec",
    }

    with pytest.raises(ExecutionContactError):
        await service.delegate_implementation(**kwargs)
    assert store.list_external_executions("work-1")[0].status == "lost_contact"

    replay = await service.delegate_implementation(**kwargs)

    assert isinstance(replay, RepeatedGrantUse)
    assert provider.starts == []


async def test_completion_and_failure_signals_resume_once(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    await _checkpoint(store)
    store.pause_for_signal("run-1")
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    assert not isinstance(created, RepeatedGrantUse)
    provider.inspect_results.append(_observation(created, "completed"))

    completed = await service.observe_execution(created.execution_id, correlation_id="corr-exec")
    again = await service.observe_execution(created.execution_id, correlation_id="corr-exec")
    signal = _signal(store, "implementation_completed")
    resumed = store.take_execution_signal("run-1", signal.signal_id)
    repeated = store.take_execution_signal("run-1", signal.signal_id)

    assert completed.status == "completed"
    assert again.execution_id == completed.execution_id
    assert len([row for row in store.list_signals() if row.name == "implementation_completed"]) == 1
    assert resumed.consumed_at is not None
    assert repeated.signal_id == resumed.signal_id
    assert store.get_run("run-1").status == "running"
    assert len(provider.inspect_results) == 0


async def test_failure_signal_and_wrong_branch_do_not_complete(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    await _checkpoint(store)
    store.pause_for_signal("run-1")
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    assert not isinstance(created, RepeatedGrantUse)
    provider.inspect_results.append(
        _observation(created, "completed", branch="cursor/generated", pull_request=True)
    )

    failed = await service.observe_execution(created.execution_id, correlation_id="corr-exec")
    signal = _signal(store, "implementation_failed")
    store.take_execution_signal("run-1", signal.signal_id)

    assert failed.status == "failed"
    assert signal.outcome_status == "failed"
    assert store.get_run("run-1").status == "running"
    assert [row.name for row in store.list_signals()].count("implementation_completed") == 0
    assert provider.starts[0].authorised_branch == BRANCH


async def test_provider_failure_resumes_the_paused_run(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    await _checkpoint(store)
    store.pause_for_signal("run-1")
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    assert not isinstance(created, RepeatedGrantUse)
    provider.inspect_results.append(_observation(created, "failed"))

    failed = await service.observe_execution(created.execution_id, correlation_id="corr-exec")
    signal = _signal(store, "implementation_failed")
    store.take_execution_signal("run-1", signal.signal_id)

    assert failed.status == "failed"
    assert signal.outcome_status == "failed"
    assert store.get_run("run-1").status == "running"


async def test_unknown_external_id_fails_closed(store: WorkflowStateStore) -> None:
    _prepared(store)
    provider, service = _service(store)

    with pytest.raises(RegistryRejected):
        service.get_by_external_id(AGENT)
    with pytest.raises(RegistryRejected):
        store.get_external_execution("exec-missing")

    assert provider.starts == []


async def test_resume_after_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = sqlite_workflow_store(path, "org-a")
    _register(store)
    binding = _prepared(store)
    await _checkpoint(store)
    store.pause_for_signal("run-1")
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    assert not isinstance(created, RepeatedGrantUse)

    reopened = sqlite_workflow_store(path, "org-a")
    restored = reopened.get_external_execution(created.execution_id)
    follow = FakeExecutionProvider()
    follow.inspect_results.append(_observation(restored, "completed"))
    resumed_service = ExecutionService(store=reopened, provider=follow, audit_log=AuditLog())
    observed = await resumed_service.observe_execution(
        restored.execution_id, correlation_id="corr-exec"
    )
    signal = _signal(reopened, "implementation_completed")
    reopened.take_execution_signal("run-1", signal.signal_id)
    reopened.take_execution_signal("run-1", signal.signal_id)

    assert observed.status == "completed"
    assert reopened.get_run("run-1").status == "running"
    assert len(reopened.list_runs(work_item_id="work-1")) == 1
    assert provider.starts[0].repository_url == REPO_URL


async def test_started_failure_does_not_launch_another_agent(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    assert not isinstance(created, RepeatedGrantUse)
    provider.inspect_results.append(_observation(created, "failed"))
    await service.observe_execution(created.execution_id, correlation_id="corr-exec")

    replay = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    follow_grant = _grant(
        store,
        binding,
        attempt_id="attempt-2",
        tool=TOOL_FOLLOW_UP_IMPLEMENTATION,
        execution_id=created.execution_id,
        name="follow",
    )
    followed = await service.follow_up_implementation(
        execution_id=created.execution_id,
        grant_id=follow_grant.grant_id,
        attempt_id="attempt-2",
        correlation_id="corr-exec",
    )

    assert isinstance(replay, RepeatedGrantUse)
    assert not isinstance(followed, RepeatedGrantUse)
    assert len(provider.starts) == 1
    assert len(provider.follow_ups) == 1
    assert followed.external_id == created.external_id
    assert followed.status == "running"
    assert followed.provider_run_id != created.provider_run_id


async def test_malformed_observation_does_not_complete(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    assert not isinstance(created, RepeatedGrantUse)
    provider.inspect_errors.append(ExecutionMalformed("bad"))

    with pytest.raises(ExecutionMalformed):
        await service.observe_execution(created.execution_id, correlation_id="corr-exec")

    assert store.get_external_execution(created.execution_id).status == "lost_contact"
    assert [row.name for row in store.list_signals()].count("implementation_completed") == 0


async def test_a_different_provider_cannot_read_the_binding(store: WorkflowStateStore) -> None:
    binding = _prepared(store)
    _provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    assert not isinstance(created, RepeatedGrantUse)
    other = ExecutionService(
        store=store, provider=FakeExecutionProvider(name="other"), audit_log=AuditLog()
    )

    with pytest.raises(RegistryRejected):
        other.get_by_external_id(created.external_id)


def test_delegate_signature_does_not_accept_scope_from_the_model() -> None:
    parameters = set(inspect.signature(ExecutionService.delegate_implementation).parameters)
    assert parameters.isdisjoint(
        {"repository", "branch", "prompt", "token", "url", "remote", "authorization", "api_key"}
    )


async def test_secrets_and_acceptance_text_are_not_stored(
    store: WorkflowStateStore, tmp_path: Path
) -> None:
    binding = _prepared(store)
    store.register_execution_policy(
        "app",
        constraints=("keep the typed boundary",),
        validation_commands=("uv run pytest",),
    )
    provider, service = _service(store)
    grant = _grant(store, binding, attempt_id="attempt-1")
    created = await service.delegate_implementation(
        work_item_id="work-1",
        grant_id=grant.grant_id,
        attempt_id="attempt-1",
        correlation_id="corr-exec",
    )
    assert not isinstance(created, RepeatedGrantUse)
    surface = _surface(store, tmp_path, "work-1")

    assert provider.starts[0].constraints == ("keep the typed boundary",)
    assert ACCEPTANCE not in surface
    assert "Do not merge to the default branch" not in surface
    assert API_KEY not in surface


def test_cursor_settings_keep_the_api_key_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"CURSOR_API_KEY={API_KEY}\n", encoding="utf-8")

    settings = load_cursor_settings(env_file_path=str(env_file))

    assert isinstance(settings.get("api_key"), SecretString)
    assert API_KEY not in str(redacted_settings(settings))


async def test_cursor_request_stays_on_the_authorised_branch_and_hides_the_key() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "agent": {"id": AGENT, "latestRunId": RUN},
                "run": {"id": RUN, "agentId": AGENT, "status": "CREATING"},
            },
        )

    audit = AuditLog()
    credentials = CursorCredentials(SecretString(API_KEY))
    http = cursor_http_client(
        audit_log=audit,
        header_provider=cursor_header_provider(credentials),
        transport=httpx.MockTransport(handler),
    )
    provider = CursorExecutionProvider(credentials=credentials, http=http)
    assignment = _assignment()

    started = await provider.start(assignment, external_id=AGENT, correlation_id="corr-exec")
    body = json.loads(calls[0].content.decode())

    assert started.provider_run_id == RUN
    assert calls[0].url.host == "api.cursor.com"
    assert body["autoCreatePR"] is False
    assert body["workOnCurrentBranch"] is True
    assert body["repos"] == [{"url": REPO_URL, "startingRef": BRANCH}]
    assert body["agentId"] == AGENT
    assert "prUrl" not in json.dumps(body)
    assert "envVars" not in body
    assert API_KEY not in calls[0].content.decode()
    assert API_KEY not in "".join(event.model_dump_json() for event in audit.events)
    assert calls[0].headers["authorization"].startswith("Basic ")


async def test_cursor_malformed_and_conflicting_responses_fail_closed() -> None:
    responses = [
        httpx.Response(200, json={"agent": {"id": AGENT}}),
        httpx.Response(409, json={"error": "agent_id_conflict"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return responses.pop(0)

    credentials = CursorCredentials(SecretString(API_KEY))
    http = cursor_http_client(
        audit_log=AuditLog(),
        header_provider=cursor_header_provider(credentials),
        transport=httpx.MockTransport(handler),
    )
    provider = CursorExecutionProvider(credentials=credentials, http=http)
    assignment = _assignment()

    with pytest.raises(ExecutionMalformed):
        await provider.start(assignment, external_id=AGENT, correlation_id="corr-exec")
    with pytest.raises(ExecutionRejected):
        CursorExecutionProvider(
            credentials=credentials,
            http=HttpClient(
                name="cursor",
                base_url="https://example.invalid",
                audit_log=AuditLog(),
            ),
        )
    with pytest.raises(ExecutionRejected):
        await provider.start(
            assignment.model_copy(update={"objective": API_KEY}),
            external_id=AGENT,
            correlation_id="corr-exec",
        )
    with pytest.raises(ExecutionConflict):
        await provider.start(assignment, external_id=AGENT, correlation_id="corr-exec")
    assert str(http.base_url) == CURSOR_API_BASE_URL


async def test_cursor_cancel_uses_the_run_endpoint() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"id": RUN})
        return httpx.Response(
            200,
            json={
                "id": RUN,
                "agentId": AGENT,
                "status": "CANCELLED",
                "git": {"branches": [{"repoUrl": "github.com/acme/app", "branch": BRANCH}]},
            },
        )

    credentials = CursorCredentials(SecretString(API_KEY))
    http = cursor_http_client(
        audit_log=AuditLog(),
        header_provider=cursor_header_provider(credentials),
        transport=httpx.MockTransport(handler),
    )
    provider = CursorExecutionProvider(credentials=credentials, http=http)

    await provider.cancel(external_id=AGENT, provider_run_id=RUN, correlation_id="corr-exec")
    observed = await provider.inspect(
        external_id=AGENT, provider_run_id=RUN, correlation_id="corr-exec"
    )

    assert calls[0].method == "POST"
    assert calls[0].url.path == f"/v1/agents/{AGENT}/runs/{RUN}/cancel"
    assert observed.phase == "cancelled"
    assert observed.branches[0].branch == BRANCH
    assert API_KEY not in observed.model_dump_json()


async def test_existing_version_2_file_gains_execution_tables(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    created = sqlite_workflow_store(path, "org-a")
    _register(created)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE external_executions")
    connection.execute("DROP TABLE execution_policies")
    connection.commit()
    connection.close()

    reopened = sqlite_workflow_store(path, "org-a")
    policy = reopened.register_execution_policy(
        "app", constraints=("keep the boundary",), validation_commands=("uv run pytest",)
    )

    assert policy.repository == "app"
    assert policy.constraints == ("keep the boundary",)


def _register(store: WorkflowStateStore) -> None:
    store.register_organisation("Example org", "1")
    register_execution_tool_permissions(store)
    store.register_role(
        "implementer",
        [TOOL_DELEGATE_IMPLEMENTATION, TOOL_FOLLOW_UP_IMPLEMENTATION],
    )
    store.register_repository("app", "https://github.com/acme/app.git", "main")
    store.register_workflow("delivery")


def _item(store: WorkflowStateStore, *, run: bool) -> str:
    item = store.open_work_item(
        item_type="slice",
        title="Ship the slice",
        acceptance=ACCEPTANCE,
        work_item_id="work-1",
    )
    store.transition_work_item(item.work_item_id, "ready")
    if run:
        store.start_run(
            "delivery", "corr-exec", work_item_id=item.work_item_id, mutating=True, run_id="run-1"
        )
    return item.work_item_id


def _prepared(store: WorkflowStateStore, *, ref: str = BRANCH) -> str:
    item = _item(store, run=True)
    binding = store.bind_github_repository(
        repository="app",
        work_item_id=item,
        issue_number=7,
        run_id="run-1",
        binding_id="bind-1",
    )
    store.record_github_ref(binding.binding_id, ref=ref, sha=SHA)
    return binding.binding_id


def _grant(
    store: WorkflowStateStore,
    binding: str,
    *,
    attempt_id: str,
    prefix: str = "feature/org",
    tools: list[str] | None = None,
    tool: str = TOOL_DELEGATE_IMPLEMENTATION,
    execution_id: str | None = None,
    name: str = "ship",
):
    selected = tools if tools is not None else [tool]
    if execution_id is None:
        arguments = delegation_arguments(
            work_item_id="work-1",
            attempt_id=attempt_id,
            repository="app",
            branch=BRANCH if prefix == "feature/org" else OUTSIDE,
            binding_id=binding,
        )
    else:
        arguments = follow_up_arguments(
            work_item_id="work-1",
            attempt_id=attempt_id,
            repository="app",
            branch=BRANCH,
            execution_id=execution_id,
        )
    gate = store.open_gate(
        name,
        role="implementer",
        work_item_id="work-1",
        stage="delivery",
        tools=selected,
        correlation_id="corr-exec",
        argument_hash=argument_hash(arguments),
    )
    _approved, grant = store.decide_gate(
        gate.gate_id, approved=True, repository="app", branch_prefix=prefix
    )
    assert grant is not None
    return grant


def _service(
    store: WorkflowStateStore, provider: FakeExecutionProvider | None = None
) -> tuple[FakeExecutionProvider, ExecutionService]:
    selected = provider or FakeExecutionProvider()
    audit = AuditLog()
    audit.add_sink(store)
    return selected, ExecutionService(store=store, provider=selected, audit_log=audit)


def _observation(
    record: ExternalExecutionRecord,
    phase: Literal["creating", "running", "completed", "failed", "cancelled", "expired"],
    *,
    branch: str = BRANCH,
    pull_request: bool = False,
) -> ProviderObservation:
    provider_run_id = record.provider_run_id
    assert provider_run_id is not None
    return ProviderObservation(
        external_id=record.external_id,
        provider_run_id=provider_run_id,
        phase=phase,
        branches=(ProviderBranch(repository_url=REPO_URL, branch=branch),),
        pull_request_opened=pull_request,
    )


def _signal(store: WorkflowStateStore, name: str):
    found = [row for row in store.list_signals(target_run_id="run-1") if row.name == name]
    assert found
    return found[-1]


def _assignment():
    from agent_platform.external_execution import ImplementationAssignment

    return ImplementationAssignment(
        work_item_id="work-1",
        objective="Ship the slice",
        acceptance_source="github-issue:7",
        repository_url=REPO_URL,
        authorised_branch=BRANCH,
        constraints=(),
        validation_commands=("uv run pytest",),
        prohibitions=("Do not merge to the default branch or any protected branch.",),
        completion_output="Stop on the authorised branch.",
    )


def _surface(store: WorkflowStateStore, tmp_path: Path, work_item_id: str) -> str:
    chunks = [event.model_dump_json() for event in store.list_ledger()]
    chunks.extend(row.model_dump_json() for row in store.list_signals())
    chunks.extend(row.model_dump_json() for row in store.list_external_executions(work_item_id))
    for path in tmp_path.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


async def _checkpoint(store: WorkflowStateStore) -> PendingHumanInput:
    def send_notice(recipient: str) -> str:
        return recipient

    tool = prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT)
    waiting = approval_workflow(tool, AuditLog(), name="delivery", storage=store)
    paused = await run_workflow(waiting, envelope("corr-exec", Notice(recipient="person")))
    assert paused.pending is not None
    await store.bind_checkpoint("run-1", paused.pending.checkpoint_id)
    return paused.pending
