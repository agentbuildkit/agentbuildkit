"""Offline checks for named gates and bounded grants."""

from pathlib import Path

import pytest

from agentbuildkit.guardrails import (
    ApprovalDecision,
    RepeatedGrantUse,
    ToolEffect,
    execute_tool,
    prepare_tool,
)
from agentbuildkit.observability import AuditLog, argument_hash
from agentbuildkit.state import registry as registry_module
from agentbuildkit.state.models import GateRecord, RegistryRejected, WorkItemRecord
from agentbuildkit.workflows import memory_workflow_store, sqlite_workflow_store
from agentbuildkit.workflows.persistence import WorkflowStateStore

SECRET = "grant-secret-must-not-be-stored"


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> WorkflowStateStore:
    if request.param == "memory":
        created = memory_workflow_store("org-a")
    else:
        created = sqlite_workflow_store(tmp_path / "state.sqlite", "org-a")
    created.register_organisation("Example org", "1")
    created.register_tool_permission("edit_code", "implementation")
    created.register_tool_permission("open_pull_request", "implementation")
    created.register_tool_permission("merge_pull_request", "merge")
    created.register_tool_permission("publish_release", "release")
    created.register_role(
        "implementer",
        ["edit_code", "open_pull_request"],
        permission_class="implementation",
    )
    created.register_role("merger", ["merge_pull_request"], permission_class="merge")
    created.register_role("releaser", ["publish_release"], permission_class="release")
    created.register_repository("app", "https://example.com/app.git", "main")
    return created


def test_gate_rejection_issues_no_grant(store: WorkflowStateStore) -> None:
    item = _draft(store)
    gate = _open(store, item.work_item_id, role="implementer", tools=["edit_code"])
    rejected, grant = store.decide_gate(gate.gate_id, approved=False)

    assert rejected.decision == "rejected"
    assert grant is None
    assert store.list_ledger()[0].kind == "approval_rejected"
    with pytest.raises(RegistryRejected):
        store.decide_gate(gate.gate_id, approved=True)
    again, still_none = store.decide_gate(gate.gate_id, approved=False)
    assert again.decision == "rejected"
    assert still_none is None
    assert len(store.list_ledger()) == 1


def test_grant_copies_the_gate_and_cannot_be_widened(store: WorkflowStateStore) -> None:
    item = _draft(store)
    arguments = _arguments()
    gate = _open(
        store,
        item.work_item_id,
        role="implementer",
        tools=["edit_code"],
        digest=argument_hash(arguments),
    )
    approved, grant = store.decide_gate(
        gate.gate_id,
        approved=True,
        repository="app",
        branch_prefix="feature/org",
        expires_at="2099-01-01T00:00:00+00:00",
    )

    assert approved.decision == "approved"
    assert grant is not None
    assert grant.tools == ("edit_code",)
    assert grant.role == "implementer"
    assert grant.permission_class == "implementation"
    assert grant.work_item_id == item.work_item_id
    assert grant.repository == "app"
    assert grant.branch_prefix == "feature/org"
    assert grant.authorised_status == "draft"
    assert grant.argument_hash == argument_hash(arguments)
    assert store.get_grant(grant.grant_id).tools == ("edit_code",)
    repeated, same = store.decide_gate(
        gate.gate_id,
        approved=True,
        repository="app",
        branch_prefix="feature/org",
    )
    assert repeated.gate_id == gate.gate_id
    assert same is not None
    assert same.grant_id == grant.grant_id


async def test_execute_tool_checks_grant_scope_expiry_and_replay(
    store: WorkflowStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": "2026-01-01T00:00:00+00:00"}
    monkeypatch.setattr(registry_module, "_now", lambda: clock["now"])
    item = _draft(store)
    arguments = _arguments()
    gate = _open(
        store,
        item.work_item_id,
        role="implementer",
        tools=["edit_code"],
        digest=argument_hash(arguments),
    )
    _approved, grant = store.decide_gate(
        gate.gate_id,
        approved=True,
        repository="app",
        branch_prefix="feature/org",
        expires_at="2026-01-02T00:00:00+00:00",
    )
    assert grant is not None
    sink: list[str] = []
    tool = prepare_tool(_writer(sink), effect=ToolEffect.SIDE_EFFECT, name="edit_code")
    audit_log = AuditLog()

    await execute_tool(
        tool,
        arguments,
        audit_log=audit_log,
        correlation_id="corr-grant",
        grant_id=grant.grant_id,
        grants=store,
    )
    replay = await execute_tool(
        tool,
        arguments,
        audit_log=audit_log,
        correlation_id="corr-grant",
        grant_id=grant.grant_id,
        grants=store,
    )

    assert sink == ["applied"]
    assert isinstance(replay, RepeatedGrantUse)
    outside = prepare_tool(_writer(sink), effect=ToolEffect.SIDE_EFFECT, name="open_pull_request")
    with pytest.raises(RegistryRejected):
        await execute_tool(
            outside,
            arguments,
            audit_log=audit_log,
            correlation_id="corr-grant",
            grant_id=grant.grant_id,
            grants=store,
        )
    wrong_branch = dict(arguments)
    wrong_branch["branch"] = "feature/other"
    wrong_gate = _open(
        store,
        item.work_item_id,
        role="implementer",
        tools=["edit_code"],
        digest=argument_hash(wrong_branch),
        name="wrong-branch",
    )
    _wrong, wrong_grant = store.decide_gate(
        wrong_gate.gate_id,
        approved=True,
        repository="app",
        branch_prefix="feature/org",
        expires_at="2026-01-02T00:00:00+00:00",
    )
    assert wrong_grant is not None
    with pytest.raises(RegistryRejected):
        await execute_tool(
            tool,
            wrong_branch,
            audit_log=audit_log,
            correlation_id="corr-grant",
            grant_id=wrong_grant.grant_id,
            grants=store,
        )
    clock["now"] = "2026-01-03T00:00:00+00:00"
    with pytest.raises(RegistryRejected):
        await execute_tool(
            tool,
            arguments,
            audit_log=audit_log,
            correlation_id="corr-grant",
            grant_id=grant.grant_id,
            grants=store,
        )
    assert sink == ["applied"]
    assert store.get_grant(grant.grant_id).status == "expired"
    assert any(event.kind == "grant_used" for event in store.list_ledger())


async def test_grant_dies_when_the_work_item_leaves_its_state(store: WorkflowStateStore) -> None:
    item = _draft(store)
    arguments = _arguments()
    gate = _open(
        store,
        item.work_item_id,
        role="implementer",
        tools=["edit_code"],
        digest=argument_hash(arguments),
    )
    _approved, grant = store.decide_gate(
        gate.gate_id,
        approved=True,
        repository="app",
        branch_prefix="feature/org",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert grant is not None
    store.transition_work_item(item.work_item_id, "ready")
    sink: list[str] = []
    tool = prepare_tool(_writer(sink), effect=ToolEffect.SIDE_EFFECT, name="edit_code")

    with pytest.raises(RegistryRejected):
        await execute_tool(
            tool,
            arguments,
            audit_log=AuditLog(),
            correlation_id="corr-grant",
            grant_id=grant.grant_id,
            grants=store,
        )

    assert sink == []
    assert store.get_grant(grant.grant_id).status == "invalidated"


def test_merge_and_release_stay_distinct_from_implementation(store: WorkflowStateStore) -> None:
    item = _draft(store)
    with pytest.raises(RegistryRejected):
        _open(store, item.work_item_id, role="implementer", tools=["merge_pull_request"])
    with pytest.raises(RegistryRejected):
        _open(store, item.work_item_id, role="merger", tools=["publish_release"])
    with pytest.raises(RegistryRejected):
        _open(
            store,
            item.work_item_id,
            role="releaser",
            tools=["publish_release", "edit_code"],
        )

    merge_arguments = {"repository": "app", "branch": "release/1", "change": "merge"}
    merge_gate = _open(
        store,
        item.work_item_id,
        role="merger",
        tools=["merge_pull_request"],
        digest=argument_hash(merge_arguments),
        name="merge",
        stage="merge",
    )
    _approved, grant = store.decide_gate(
        merge_gate.gate_id,
        approved=True,
        repository="app",
        branch_prefix="release",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert grant is not None
    assert grant.permission_class == "merge"
    assert "publish_release" not in grant.tools
    assert "edit_code" not in grant.tools


async def test_version_1_approval_still_runs_without_a_grant() -> None:
    sink: list[str] = []

    def send_notice(change: str) -> str:
        sink.append(change)
        return change

    tool = prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT, name="send_notice")
    result = await execute_tool(
        tool,
        {"change": "notify"},
        audit_log=AuditLog(),
        correlation_id="corr-v1",
        decision=ApprovalDecision("send_notice", approved=True),
    )

    assert sink == ["notify"]
    assert result[0].text == "notify"


def test_grant_ledger_omits_argument_values(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = sqlite_workflow_store(path, "org-a")
    store.register_organisation("Example org", "1")
    store.register_tool_permission("edit_code", "implementation")
    store.register_role("implementer", ["edit_code"], permission_class="implementation")
    store.register_repository("app", "https://example.com/app.git", "main")
    item = _draft(store)
    arguments = _arguments()
    gate = _open(
        store,
        item.work_item_id,
        role="implementer",
        tools=["edit_code"],
        digest=argument_hash(arguments),
    )
    store.decide_gate(
        gate.gate_id,
        approved=True,
        repository="app",
        branch_prefix="feature/org",
        expires_at="2099-01-01T00:00:00+00:00",
    )

    stored = path.read_text(errors="ignore")
    assert SECRET not in stored


def _draft(store: WorkflowStateStore) -> WorkItemRecord:
    return store.open_work_item(item_type="slice", title="Slice", acceptance="ship the slice")


def _arguments(change: str = "1") -> dict[str, str]:
    return {
        "repository": "app",
        "branch": "feature/org/slice",
        "change": change,
        "token": SECRET,
    }


def _open(
    store: WorkflowStateStore,
    work_item_id: str,
    *,
    role: str,
    tools: list[str],
    digest: str | None = None,
    name: str = "implement",
    stage: str = "build",
) -> GateRecord:
    return store.open_gate(
        name,
        role=role,
        work_item_id=work_item_id,
        stage=stage,
        tools=tools,
        correlation_id="corr-gate",
        argument_hash=digest or argument_hash({"change": "pending"}),
    )


def _writer(sink: list[str]):
    def edit_code(change: str, repository: str, branch: str, token: str) -> str:
        del change, repository, branch, token
        sink.append("applied")
        return "applied"

    return edit_code
