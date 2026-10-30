"""Offline GitHub binding checks. No live GitHub calls."""

import inspect
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from agent_framework import SecretString
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel

from agentbuildkit.guardrails import RepeatedGrantUse, ToolEffect, prepare_tool
from agentbuildkit.integrations import (
    TOOL_CREATE_BRANCH,
    TOOL_CREATE_COMMENT,
    TOOL_CREATE_PULL_REQUEST,
    TOOL_CREATE_RELEASE,
    TOOL_CREATE_REVIEW,
    TOOL_MERGE_PULL_REQUEST,
    TOOL_READ_CHECK,
    TOOL_READ_ISSUE,
    TOOL_READ_PULL_REQUEST,
    GithubAppAuthorizer,
    GithubAppCredentials,
    GithubCredentialError,
    GithubError,
    GithubService,
    HttpClient,
    HttpPayloadError,
    branch_arguments,
    comment_arguments,
    github_http_client,
    github_tool_permission,
    register_github_tool_permissions,
    release_arguments,
    review_arguments,
)
from agentbuildkit.observability import AuditLog, argument_hash
from agentbuildkit.state.models import GrantRecord, RegistryRejected
from agentbuildkit.workflows import (
    PendingHumanInput,
    WorkflowStateStore,
    approval_workflow,
    envelope,
    memory_workflow_store,
    run_workflow,
    sqlite_workflow_store,
)

TOKEN = "ghs_installation_token_9f3a"
ISSUE_TITLE = "issue-title-secret-9f3a"
ISSUE_BODY = "issue-body-secret-9f3a"
COMMENT_BODY = "comment-body-secret-9f3a"
REVIEW_BODY = "review-body-secret-9f3a"
BASE_SHA = "a" * 40
MERGE_SHA = "b" * 40
BRANCH = "feature/org/1"
OUTSIDE = "feature/other/1"


class Notice(BaseModel):
    recipient: str


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> WorkflowStateStore:
    if request.param == "memory":
        created = memory_workflow_store("org-a")
    else:
        created = sqlite_workflow_store(tmp_path / "state.sqlite", "org-a")
    created.register_organisation("Example org", "1")
    register_github_tool_permissions(created)
    created.register_role("reader", [TOOL_READ_ISSUE, TOOL_READ_PULL_REQUEST, TOOL_READ_CHECK])
    created.register_role(
        "implementer",
        [
            TOOL_CREATE_BRANCH,
            TOOL_CREATE_PULL_REQUEST,
            TOOL_CREATE_COMMENT,
            TOOL_CREATE_REVIEW,
        ],
        permission_class="implementation",
    )
    created.register_role("merger", [TOOL_MERGE_PULL_REQUEST], permission_class="merge")
    created.register_role("releaser", [TOOL_CREATE_RELEASE], permission_class="release")
    created.register_repository("app", "https://github.com/acme/app.git", "main")
    created.register_workflow("delivery")
    return created


def test_unknown_github_tool_fails_closed() -> None:
    with pytest.raises(RegistryRejected):
        github_tool_permission("github.delete_repository")


def test_binding_requires_an_allowlisted_github_repository(store: WorkflowStateStore) -> None:
    item = _item(store)
    with pytest.raises(RegistryRejected):
        store.bind_github_repository(repository="missing", work_item_id=item, issue_number=7)
    store.register_repository("mirror", "https://example.com/app.git", "main")
    with pytest.raises(RegistryRejected):
        store.bind_github_repository(repository="mirror", work_item_id=item, issue_number=7)
    store.register_repository("git", "git@github.com:acme/app.git", "main")
    bound = store.bind_github_repository(
        repository="git", work_item_id=item, issue_number=7, binding_id="bind-git"
    )
    assert bound.repository == "git"
    assert bound.issue_number == 7


def test_default_and_protected_branches_cannot_be_recorded(store: WorkflowStateStore) -> None:
    binding = _binding(store)
    with pytest.raises(RegistryRejected):
        store.record_github_ref(binding, ref="main", sha=BASE_SHA)
    store.protect_branch("app", "release")
    with pytest.raises(RegistryRejected):
        store.record_github_ref(binding, ref="release", sha=BASE_SHA)
    with pytest.raises(RegistryRejected):
        store.record_github_ref(binding, ref="refs/heads/feature", sha=BASE_SHA)
    assert store.protected_branches("app") == ("main", "release")


async def test_allowed_repository_reads_an_issue_without_storing_its_text(
    store: WorkflowStateStore,
    tmp_path: Path,
) -> None:
    binding = _binding(store, run=True)
    calls, service = _service(
        store,
        [_json(200, {"number": 7, "state": "open", "title": ISSUE_TITLE, "body": ISSUE_BODY})],
    )

    issue = await service.read_issue(binding_id=binding, role="reader", correlation_id="corr-issue")

    assert calls[0].url.host == "api.github.com"
    assert calls[0].url.path == "/repos/acme/app/issues/7"
    assert issue.number == 7
    assert issue.state == "open"
    assert ISSUE_TITLE not in issue.model_dump_json()
    assert ISSUE_BODY not in issue.model_dump_json()
    stored = store.get_github_binding(binding)
    assert stored.issue_content_hash == argument_hash({"body": ISSUE_BODY, "title": ISSUE_TITLE})
    assert _surface(store, tmp_path).count(ISSUE_TITLE) == 0
    assert _surface(store, tmp_path).count(ISSUE_BODY) == 0


async def test_role_without_the_read_tool_does_not_call_github(store: WorkflowStateStore) -> None:
    binding = _binding(store)
    store.register_role("blank", [])
    calls, service = _service(
        store, [_json(200, {"number": 7, "state": "open", "title": "t", "body": ""})]
    )

    with pytest.raises(RegistryRejected):
        await service.read_issue(binding_id=binding, role="blank", correlation_id="corr-role")

    assert calls == []


async def test_branch_creation_records_the_ref_and_rejects_scope_violations(
    store: WorkflowStateStore,
) -> None:
    binding = _binding(store, run=True)
    item = store.get_github_binding(binding).work_item_id
    calls, service = _service(
        store, [_ref("refs/heads/main", BASE_SHA), _ref(f"refs/heads/{BRANCH}", BASE_SHA)]
    )
    arguments = branch_arguments(repository="app", branch=BRANCH, binding_id=binding)
    grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=arguments,
        prefix="feature/org",
        item=item,
    )

    created = await service.create_branch(
        binding_id=binding, branch=BRANCH, grant_id=grant.grant_id, correlation_id="corr-branch"
    )

    assert not isinstance(created, RepeatedGrantUse)
    assert created.ref == BRANCH
    assert created.sha == BASE_SHA
    assert calls[0].method == "GET"
    assert calls[1].method == "POST"
    assert calls[1].url.path == "/repos/acme/app/git/refs"
    replay = await service.create_branch(
        binding_id=binding, branch=BRANCH, grant_id=grant.grant_id, correlation_id="corr-branch"
    )
    assert isinstance(replay, RepeatedGrantUse)
    assert len(calls) == 2
    signals = [
        row
        for row in store.list_signals(target_run_id="run-1")
        if row.name == "github_branch_created"
    ]
    assert signals[0].subject_id == BASE_SHA
    assert signals[0].outcome_status == "created"


async def test_branch_prefix_and_protected_branches_do_not_call_github(
    store: WorkflowStateStore,
) -> None:
    binding = _binding(store)
    item = store.get_github_binding(binding).work_item_id
    calls, service = _service(store, [_ref("refs/heads/main", BASE_SHA)])
    outside = branch_arguments(repository="app", branch=OUTSIDE, binding_id=binding)
    outside_grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=outside,
        prefix="feature/org",
        item=item,
        name="outside",
    )
    with pytest.raises(RegistryRejected):
        await service.create_branch(
            binding_id=binding,
            branch=OUTSIDE,
            grant_id=outside_grant.grant_id,
            correlation_id="corr-prefix",
        )
    protected = branch_arguments(repository="app", branch="main", binding_id=binding)
    protected_grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=protected,
        prefix="main",
        item=item,
        name="protected",
    )
    with pytest.raises(RegistryRejected):
        await service.create_branch(
            binding_id=binding,
            branch="main",
            grant_id=protected_grant.grant_id,
            correlation_id="corr-protected",
        )

    assert calls == []


async def test_pull_request_creation_is_once_and_a_second_grant_does_not_write(
    store: WorkflowStateStore,
) -> None:
    binding = _binding(store, run=True)
    await _branch(store, binding)
    calls, service = _service(store, [_pull(4, BRANCH, BASE_SHA)])
    item = store.get_github_binding(binding).work_item_id
    arguments = branch_arguments(repository="app", branch=BRANCH, binding_id=binding)
    grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_PULL_REQUEST],
        arguments=arguments,
        prefix="feature/org",
        item=item,
    )
    second = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_PULL_REQUEST],
        arguments=arguments,
        prefix="feature/org",
        item=item,
        name="second-pr",
    )

    opened = await service.create_pull_request(
        binding_id=binding, branch=BRANCH, grant_id=grant.grant_id, correlation_id="corr-pr"
    )
    with pytest.raises(RegistryRejected):
        await service.create_pull_request(
            binding_id=binding, branch=BRANCH, grant_id=second.grant_id, correlation_id="corr-pr"
        )

    assert not isinstance(opened, RepeatedGrantUse)
    assert opened.number == 4
    assert opened.base_ref == "main"
    assert len(calls) == 1
    assert calls[0].url.path == "/repos/acme/app/pulls"
    signal = store.list_signals(target_run_id="run-1")[-1]
    assert signal.name == "github_pull_request_opened"
    assert signal.subject_id == "4"


async def test_check_status_is_a_read_and_emits_a_signal(store: WorkflowStateStore) -> None:
    binding = _binding(store, run=True)
    await _branch(store, binding)
    calls, service = _service(
        store,
        [
            _json(
                200,
                {
                    "check_runs": [
                        {
                            "id": 99,
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": BASE_SHA,
                        }
                    ]
                },
            )
        ],
    )

    checks = await service.read_checks(
        binding_id=binding, role="reader", ref=BRANCH, correlation_id="corr-check"
    )

    assert calls[0].url.path == f"/repos/acme/app/commits/{BASE_SHA}/check-runs"
    assert checks.checks[0].check_id == "99"
    assert checks.checks[0].conclusion == "success"
    assert store.list_github_checks(binding)[0].conclusion == "success"
    signal = [
        row
        for row in store.list_signals(target_run_id="run-1")
        if row.name == "github_check_completed"
    ]
    assert signal[0].outcome_status == "success"
    assert signal[0].subject_id == "99"


async def test_comment_and_review_require_an_implementation_grant(
    store: WorkflowStateStore,
    tmp_path: Path,
) -> None:
    binding = _binding(store, run=True)
    await _branch(store, binding)
    await _pull_request(store, binding)
    calls, service = _service(store, [_json(201, {"id": 15}), _json(201, {"id": 16})])
    item = store.get_github_binding(binding).work_item_id
    comment_args = comment_arguments(
        repository="app", branch=BRANCH, binding_id=binding, body=COMMENT_BODY
    )
    review_args = review_arguments(
        repository="app",
        branch=BRANCH,
        binding_id=binding,
        body=REVIEW_BODY,
        event="comment",
    )
    comment_grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_COMMENT],
        arguments=comment_args,
        prefix="feature/org",
        item=item,
        name="comment",
    )
    review_grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_REVIEW],
        arguments=review_args,
        prefix="feature/org",
        item=item,
        name="review",
    )
    merge_grant = _grant(
        store,
        role="merger",
        tools=[TOOL_MERGE_PULL_REQUEST],
        arguments=branch_arguments(repository="app", branch=BRANCH, binding_id=binding),
        prefix="feature/org",
        item=item,
        name="merge-for-comment",
    )

    comment_id = await service.create_comment(
        binding_id=binding,
        branch=BRANCH,
        body=COMMENT_BODY,
        grant_id=comment_grant.grant_id,
        correlation_id="corr-comment",
    )
    review_id = await service.create_review(
        binding_id=binding,
        branch=BRANCH,
        body=REVIEW_BODY,
        event="comment",
        grant_id=review_grant.grant_id,
        correlation_id="corr-review",
    )
    with pytest.raises(RegistryRejected):
        await service.create_comment(
            binding_id=binding,
            branch=BRANCH,
            body=COMMENT_BODY,
            grant_id=merge_grant.grant_id,
            correlation_id="corr-comment",
        )

    assert comment_id == "15"
    assert review_id == "16"
    assert len(calls) == 2
    surface = _surface(store, tmp_path)
    assert COMMENT_BODY not in surface
    assert REVIEW_BODY not in surface
    assert TOKEN not in surface


async def test_merge_requires_a_merge_grant_and_records_the_merge_sha(
    store: WorkflowStateStore,
) -> None:
    binding = _binding(store, run=True)
    await _branch(store, binding)
    await _pull_request(store, binding)
    calls, service = _service(store, [_json(200, {"sha": MERGE_SHA, "merged": True})])
    item = store.get_github_binding(binding).work_item_id
    arguments = branch_arguments(repository="app", branch=BRANCH, binding_id=binding)
    implementation = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=arguments,
        prefix="feature/org",
        item=item,
        name="impl-merge",
    )
    with pytest.raises(RegistryRejected):
        await service.merge_pull_request(
            binding_id=binding,
            branch=BRANCH,
            grant_id=implementation.grant_id,
            correlation_id="corr-merge",
        )
    assert calls == []
    merger = _grant(
        store,
        role="merger",
        tools=[TOOL_MERGE_PULL_REQUEST],
        arguments=arguments,
        prefix="feature/org",
        item=item,
        name="merger",
    )

    merged = await service.merge_pull_request(
        binding_id=binding, branch=BRANCH, grant_id=merger.grant_id, correlation_id="corr-merge"
    )

    assert not isinstance(merged, RepeatedGrantUse)
    assert merged.state == "merged"
    assert merged.merge_sha == MERGE_SHA
    assert calls[0].method == "PUT"
    assert calls[0].url.path == "/repos/acme/app/pulls/4/merge"
    signal = [
        row
        for row in store.list_signals(target_run_id="run-1")
        if row.name == "github_pull_request_merged"
    ]
    assert signal[-1].outcome_status == "merged"


async def test_release_requires_a_release_grant(
    store: WorkflowStateStore,
) -> None:
    binding = _binding(store, run=True)
    await _branch(store, binding)
    calls, service = _service(store, [_json(201, {"id": 21, "tag_name": "v1.0.0"})])
    item = store.get_github_binding(binding).work_item_id
    arguments = release_arguments(
        repository="app", binding_id=binding, tag="v1.0.0", target_sha=BASE_SHA
    )
    implementation = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=branch_arguments(repository="app", branch=BRANCH, binding_id=binding),
        prefix="feature/org",
        item=item,
        name="impl-release",
    )
    with pytest.raises(RegistryRejected):
        await service.create_release(
            binding_id=binding,
            tag="v1.0.0",
            target_sha=BASE_SHA,
            grant_id=implementation.grant_id,
            correlation_id="corr-release",
        )
    assert calls == []
    releaser = _grant(
        store,
        role="releaser",
        tools=[TOOL_CREATE_RELEASE],
        arguments=arguments,
        prefix=None,
        item=item,
        name="releaser",
    )

    release = await service.create_release(
        binding_id=binding,
        tag="v1.0.0",
        target_sha=BASE_SHA,
        grant_id=releaser.grant_id,
        correlation_id="corr-release",
    )

    assert not isinstance(release, RepeatedGrantUse)
    assert release.release_id == "21"
    assert release.target_sha == BASE_SHA
    assert calls[0].url.path == "/repos/acme/app/releases"
    signal = [
        row
        for row in store.list_signals(target_run_id="run-1")
        if row.name == "github_release_published"
    ]
    assert signal[0].subject_id == "21"


async def test_github_api_failure_is_not_retried_or_replayed(store: WorkflowStateStore) -> None:
    binding = _binding(store)
    item = store.get_github_binding(binding).work_item_id
    calls, service = _service(
        store,
        [_ref("refs/heads/main", BASE_SHA), httpx.Response(500, json={"token": TOKEN})],
    )
    arguments = branch_arguments(repository="app", branch=BRANCH, binding_id=binding)
    grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=arguments,
        prefix="feature/org",
        item=item,
    )

    with pytest.raises(Exception) as caught:
        await service.create_branch(
            binding_id=binding, branch=BRANCH, grant_id=grant.grant_id, correlation_id="corr-fail"
        )
    replay = await service.create_branch(
        binding_id=binding, branch=BRANCH, grant_id=grant.grant_id, correlation_id="corr-fail"
    )

    assert TOKEN not in str(caught.value)
    assert isinstance(replay, RepeatedGrantUse)
    assert len(calls) == 2
    assert store.find_github_ref(binding, BRANCH) is None
    assert store.list_signals() == ()


async def test_malformed_and_unexpected_github_payloads_fail_closed(
    store: WorkflowStateStore,
) -> None:
    binding = _binding(store)
    item = store.get_github_binding(binding).work_item_id
    calls, service = _service(
        store, [_json(200, {"number": 7, "state": "draft", "title": ISSUE_TITLE})]
    )
    with pytest.raises(HttpPayloadError) as issue_error:
        await service.read_issue(binding_id=binding, role="reader", correlation_id="corr-bad-issue")
    assert ISSUE_TITLE not in str(issue_error.value)

    arguments = branch_arguments(repository="app", branch=BRANCH, binding_id=binding)
    grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=arguments,
        prefix="feature/org",
        item=item,
    )
    _replace(
        service,
        [
            _ref("refs/heads/main", BASE_SHA),
            _json(200, {"ref": f"refs/heads/{BRANCH}", "object": {"sha": "nope"}}),
        ],
    )
    with pytest.raises(HttpPayloadError) as branch_error:
        await service.create_branch(
            binding_id=binding,
            branch=BRANCH,
            grant_id=grant.grant_id,
            correlation_id="corr-bad-branch",
        )
    assert "nope" not in str(branch_error.value)
    assert store.find_github_ref(binding, BRANCH) is None

    await _branch(store, binding)
    _replace(
        service,
        [
            _json(
                200,
                {
                    "check_runs": [
                        {
                            "id": 5,
                            "status": "completed",
                            "conclusion": "exploded",
                            "head_sha": BASE_SHA,
                        }
                    ]
                },
            )
        ],
    )
    with pytest.raises(HttpPayloadError):
        await service.read_checks(
            binding_id=binding, role="reader", ref=BRANCH, correlation_id="corr-bad-check"
        )
    assert store.list_github_checks(binding) == ()
    assert len(calls) >= 1


async def test_credentials_and_headers_stay_out_of_audit_and_ledger(
    store: WorkflowStateStore,
    tmp_path: Path,
) -> None:
    private_key = _private_key()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00+00:00"}
            )
        return httpx.Response(500, json={"authorization": TOKEN, "key": private_key})

    audit = AuditLog()
    audit.add_sink(store)
    authorizer = GithubAppAuthorizer(
        GithubAppCredentials(
            app_id="7", installation_id="42", private_key=SecretString(private_key)
        ),
        transport=httpx.MockTransport(handler),
    )
    http = github_http_client(
        audit_log=audit,
        header_provider=authorizer,
        transport=httpx.MockTransport(handler),
    )
    service = GithubService(store=store, audit_log=audit, http=http)
    binding = _binding(store)

    with pytest.raises(Exception) as caught:
        await service.read_issue(binding_id=binding, role="reader", correlation_id="corr-secret")

    rendered = (
        str(caught.value)
        + _surface(store, tmp_path)
        + "".join(event.model_dump_json() for event in audit.events)
    )
    assert TOKEN not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered
    assert calls[0].url.path == "/app/installations/42/access_tokens"
    assert calls[1].headers["authorization"] == f"Bearer {TOKEN}"
    assert "BEGIN PRIVATE KEY" not in calls[0].content.decode()
    assert calls[1].url.host == "api.github.com"


async def test_service_rejects_a_client_that_is_not_the_github_api(
    store: WorkflowStateStore,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    http = HttpClient(
        name="github",
        base_url="https://evil.example",
        audit_log=AuditLog(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GithubError):
        GithubService(store=store, audit_log=AuditLog(), http=http)
    parameters = {
        name
        for method in (
            GithubService.read_issue,
            GithubService.create_branch,
            GithubService.create_pull_request,
            GithubService.merge_pull_request,
            GithubService.create_release,
        )
        for name in inspect.signature(method).parameters
    }
    assert parameters.isdisjoint({"url", "base_url", "token", "authorization", "remote"})
    assert calls == []


async def test_github_signal_resumes_a_paused_run(store: WorkflowStateStore) -> None:
    _binding(store, run=True)
    paused = await _checkpoint(store)
    store.pause_for_signal("run-1")
    assert store.get_run("run-1").status == "paused"
    binding = store.get_github_binding("bind-1").binding_id
    _calls, service = _service(
        store, [_ref("refs/heads/main", BASE_SHA), _ref(f"refs/heads/{BRANCH}", BASE_SHA)]
    )
    item = store.get_work_item(store.get_github_binding(binding).work_item_id).work_item_id
    grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=branch_arguments(repository="app", branch=BRANCH, binding_id=binding),
        prefix="feature/org",
        item=item,
    )

    await service.create_branch(
        binding_id=binding, branch=BRANCH, grant_id=grant.grant_id, correlation_id="corr-signal"
    )
    signal = store.list_signals(target_run_id="run-1")[-1]
    resumed = store.take_github_signal("run-1", signal.signal_id)
    again = store.take_github_signal("run-1", signal.signal_id)

    assert resumed.consumed_at is not None
    assert again.signal_id == resumed.signal_id
    assert store.get_run("run-1").status == "running"
    assert paused.request_id


async def test_existing_version_2_file_gains_github_tables(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    created = sqlite_workflow_store(path, "org-a")
    created.register_organisation("Example org", "1")
    created.register_repository("app", "https://github.com/acme/app.git", "main")
    created.register_workflow("delivery")
    item = _item(created)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE github_bindings")
    connection.executescript(
        """
        ALTER TABLE signals RENAME TO signals_old;
        CREATE TABLE signals (
            scope TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            name TEXT NOT NULL,
            target_run_id TEXT NOT NULL,
            source_run_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            outcome_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT,
            PRIMARY KEY (scope, signal_id)
        );
        INSERT INTO signals (
            scope, signal_id, name, target_run_id, source_run_id, correlation_id,
            outcome_status, created_at, consumed_at
        )
        SELECT
            scope, signal_id, name, target_run_id, source_run_id, correlation_id,
            outcome_status, created_at, consumed_at
        FROM signals_old;
        DROP TABLE signals_old;
        """
    )
    connection.commit()
    connection.close()

    reopened = sqlite_workflow_store(path, "org-a")
    bound = reopened.bind_github_repository(
        repository="app", work_item_id=item, issue_number=7, binding_id="bind-migrated"
    )

    assert bound.issue_number == 7
    assert reopened.list_signals() == ()


def test_github_settings_keep_the_private_key_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentbuildkit.config import load_github_settings, redacted_settings

    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_INSTALLATION_ID", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GITHUB_APP_ID=7\nGITHUB_INSTALLATION_ID=42\nGITHUB_PRIVATE_KEY=pem-secret-9f3a\n",
        encoding="utf-8",
    )

    settings = load_github_settings(env_file_path=str(env_file))

    assert settings.get("app_id") == "7"
    assert isinstance(settings.get("private_key"), SecretString)
    rendered = str(redacted_settings(settings))
    assert "pem-secret-9f3a" not in rendered
    with pytest.raises(GithubCredentialError):
        GithubAppAuthorizer(
            GithubAppCredentials(
                app_id="app/../42",
                installation_id="42",
                private_key=SecretString("unused"),
            )
        )()


def _item(store: WorkflowStateStore) -> str:
    item = store.open_work_item(
        item_type="slice",
        title="Ship the slice",
        acceptance="acceptance stays hashed",
        work_item_id="work-1",
    )
    return item.work_item_id


def _binding(store: WorkflowStateStore, *, run: bool = False) -> str:
    item = _item(store)
    run_id = None
    if run:
        store.start_run("delivery", "corr-github", work_item_id=item, run_id="run-1")
        run_id = "run-1"
    bound = store.bind_github_repository(
        repository="app",
        work_item_id=item,
        issue_number=7,
        run_id=run_id,
        binding_id="bind-1",
    )
    return bound.binding_id


def _grant(
    store: WorkflowStateStore,
    *,
    role: str,
    tools: list[str],
    arguments: dict[str, str],
    prefix: str | None,
    item: str,
    name: str = "ship",
) -> GrantRecord:
    gate = store.open_gate(
        name,
        role=role,
        work_item_id=item,
        stage="delivery",
        tools=tools,
        correlation_id="corr-github",
        argument_hash=argument_hash(arguments),
    )
    _approved, grant = store.decide_gate(
        gate.gate_id,
        approved=True,
        repository="app",
        branch_prefix=prefix,
    )
    assert grant is not None
    return grant


async def _branch(store: WorkflowStateStore, binding: str) -> None:
    _calls, service = _service(
        store, [_ref("refs/heads/main", BASE_SHA), _ref(f"refs/heads/{BRANCH}", BASE_SHA)]
    )
    item = store.get_github_binding(binding).work_item_id
    grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_BRANCH],
        arguments=branch_arguments(repository="app", branch=BRANCH, binding_id=binding),
        prefix="feature/org",
        item=item,
        name=f"branch-{binding}",
    )
    await service.create_branch(
        binding_id=binding, branch=BRANCH, grant_id=grant.grant_id, correlation_id="corr-setup"
    )


async def _pull_request(store: WorkflowStateStore, binding: str) -> None:
    _calls, service = _service(store, [_pull(4, BRANCH, BASE_SHA)])
    item = store.get_github_binding(binding).work_item_id
    grant = _grant(
        store,
        role="implementer",
        tools=[TOOL_CREATE_PULL_REQUEST],
        arguments=branch_arguments(repository="app", branch=BRANCH, binding_id=binding),
        prefix="feature/org",
        item=item,
        name=f"pr-{binding}",
    )
    await service.create_pull_request(
        binding_id=binding, branch=BRANCH, grant_id=grant.grant_id, correlation_id="corr-setup"
    )


def _service(
    store: WorkflowStateStore,
    responses: list[httpx.Response],
) -> tuple[list[httpx.Request], GithubService]:
    calls, handler = _capture(responses)
    audit = AuditLog()
    audit.add_sink(store)
    http = github_http_client(
        audit_log=audit,
        header_provider=lambda: {"Authorization": SecretString(f"Bearer {TOKEN}")},
        transport=httpx.MockTransport(handler),
    )
    return calls, GithubService(store=store, audit_log=audit, http=http)


def _replace(service: GithubService, responses: list[httpx.Response]) -> None:
    calls, handler = _capture(responses)
    http = github_http_client(
        audit_log=service._audit_log,  # type: ignore[attr-defined]
        header_provider=lambda: {"Authorization": SecretString(f"Bearer {TOKEN}")},
        transport=httpx.MockTransport(handler),
    )
    service._http = http  # type: ignore[attr-defined]
    del calls


def _capture(
    responses: list[httpx.Response],
) -> tuple[list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    return calls, handler


def _json(status: int, payload: object) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _ref(name: str, sha: str) -> httpx.Response:
    return _json(200, {"ref": name, "object": {"sha": sha, "type": "commit"}})


def _pull(number: int, head: str, sha: str) -> httpx.Response:
    return _json(
        201,
        {
            "number": number,
            "state": "open",
            "merged": False,
            "title": "ignored-title",
            "head": {"ref": head, "sha": sha},
            "base": {"ref": "main", "sha": sha},
        },
    )


def _surface(store: WorkflowStateStore, tmp_path: Path) -> str:
    chunks = [event.model_dump_json() for event in store.list_ledger()]
    chunks.extend(signal.model_dump_json() for signal in store.list_signals())
    for path in tmp_path.rglob("*"):
        if path.is_file():
            chunks.append(path.read_bytes().decode("utf-8", errors="ignore"))
    return "\n".join(chunks)


def _private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem.decode("ascii")


async def _checkpoint(store: WorkflowStateStore) -> PendingHumanInput:
    def send_notice(recipient: str) -> str:
        return recipient

    tool = prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT)
    waiting = approval_workflow(tool, AuditLog(), name="delivery", storage=store)
    paused = await run_workflow(waiting, envelope("corr-github", Notice(recipient="person")))
    assert paused.pending is not None
    await store.bind_checkpoint("run-1", paused.pending.checkpoint_id)
    return paused.pending
