"""Offline tests for the version 2 ledger, runs, directory, and work items."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from agent_framework import FunctionTool
from pydantic import BaseModel

from agentbuildkit.guardrails import ToolEffect, prepare_tool
from agentbuildkit.observability import AuditEvent, AuditLog, argument_hash
from agentbuildkit.state.registry import V2_TABLES
from agentbuildkit.workflows import (
    PLATFORM_STATE_VERSION,
    SUPPORTED_PLATFORM_STATE_VERSIONS,
    CheckpointCompatibilityError,
    HumanApprovalResponse,
    PendingHumanInput,
    RegistryRejected,
    StateMigrationRequired,
    WorkflowStateStore,
    approval_workflow,
    envelope,
    memory_workflow_store,
    resume_approval,
    run_workflow,
    sqlite_workflow_store,
)

SECRET = "acceptance-secret-should-not-be-stored"
TOOL_SECRET = "tool-secret-should-not-be-stored"


class Notice(BaseModel):
    recipient: str


def _tool(sink: list[str]) -> FunctionTool:
    def send_notice(recipient: str) -> str:
        """Send a notice."""
        sink.append(recipient)
        return "sent"

    return prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT)


def _stored_text(path: Path) -> str:
    text = ""
    for item in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if item.exists():
            text += item.read_bytes().decode("utf-8", errors="ignore")
    return text


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[WorkflowStateStore]:
    if request.param == "memory":
        yield memory_workflow_store("org-a")
        return
    yield sqlite_workflow_store(tmp_path / "state.sqlite", "org-a")


def _directory(store: WorkflowStateStore) -> None:
    store.register_organisation("Example org", "2026-10")
    store.register_role("engineer", tools=("draft",))
    store.register_role("release", tools=())
    store.register_repository(
        "app",
        "https://github.com/example/app.git",
        "main",
    )
    store.register_workflow("intake")
    store.register_workflow("delivery")
    store.register_team(
        "engineering",
        roles=("engineer",),
        workflows=("delivery",),
        delegations=("release",),
    )


def _ready_item(store: WorkflowStateStore, *, title: str, work_item_id: str) -> str:
    item = store.open_work_item(
        item_type="slice",
        title=title,
        acceptance=f"acceptance for {title}",
        work_item_id=work_item_id,
    )
    store.transition_work_item(item.work_item_id, "ready")
    return item.work_item_id


def test_constants_keep_version_1_readable() -> None:
    assert PLATFORM_STATE_VERSION == "2"
    assert SUPPORTED_PLATFORM_STATE_VERSIONS == ("1", "2")


def test_directory_fail_closed_rules(store: WorkflowStateStore) -> None:
    _directory(store)
    with pytest.raises(RegistryRejected):
        store.register_organisation("Other", "2")
    with pytest.raises(RegistryRejected):
        store.register_role("engineer")
    with pytest.raises(RegistryRejected):
        store.register_team("quality", roles=("missing",))
    with pytest.raises(RegistryRejected):
        store.register_team("quality", roles=("engineer",), workflows=("missing",))
    with pytest.raises(RegistryRejected):
        store.register_team("quality", roles=("engineer",), delegations=("missing",))
    with pytest.raises(RegistryRejected):
        store.register_repository("other", "https://user:token@github.com/example/app.git", "main")
    with pytest.raises(RegistryRejected):
        store.register_repository("other", "https://token@github.com/example/app.git", "main")
    with pytest.raises(RegistryRejected):
        store.register_repository("other", "file:///tmp/app", "main")
    with pytest.raises(RegistryRejected):
        store.register_repository("other", "http://github.com/example/app.git", "main")
    ssh = store.register_repository("mirror", "git@github.com:example/app.git", "release")
    assert ssh.remote == "git@github.com:example/app.git"
    assert store.get_repository("app").default_branch == "main"
    assert store.get_team(store.list_teams()[0].team_id).workflows == ("delivery",)
    with pytest.raises(RegistryRejected):
        store.start_run("unknown", "corr-1")
    with pytest.raises(RegistryRejected):
        store.get_role("missing")


def test_repository_allowlist_does_not_require_a_network_call(store: WorkflowStateStore) -> None:
    store.register_organisation("Example org", "1")
    record = store.register_repository("app", "https://github.com/example/app", "main")
    assert record.name == "app"
    assert store.list_repositories() == (record,)


def test_ledger_stores_hashes_and_filters(store: WorkflowStateStore) -> None:
    _directory(store)
    run = store.start_run("intake", "corr-ledger", run_id="run-ledger")
    event = AuditEvent(
        correlation_id="corr-ledger",
        agent_name="engineer",
        tool_name="read_notes",
        approval="not_required",
        outcome="completed",
        argument_hash=argument_hash({"token": TOOL_SECRET}),
    )
    stored = store.append_ledger(event, run_id=run.run_id, event_id="event-1")
    assert stored.argument_hash == event.argument_hash
    assert stored.run_id == run.run_id
    assert TOOL_SECRET not in stored.model_dump_json()
    assert store.list_ledger(correlation_id="corr-ledger") == (stored,)
    assert store.list_ledger(run_id="missing") == ()
    rejected = event.model_copy(update={"argument_hash": TOOL_SECRET})
    with pytest.raises(RegistryRejected):
        store.append_ledger(rejected)
    with pytest.raises(RegistryRejected):
        store.append_ledger(event, run_id="missing-run")


def test_audit_sink_writes_once_and_keeps_the_memory_log(store: WorkflowStateStore) -> None:
    log = AuditLog()
    log.add_sink(store)
    log.add_sink(store)
    event = AuditEvent(
        correlation_id="corr-sink",
        agent_name="org-a",
        tool_name="checkpoint.created",
        approval="not_required",
        outcome="completed",
        argument_hash="lifecycle",
        kind="checkpoint_created",
    )
    log.record(event)
    assert log.events == [event]
    ledger = store.list_ledger()
    assert len(ledger) == 1
    assert ledger[0].kind == "checkpoint_created"
    assert ledger[0].argument_hash == "lifecycle"


def test_runs_follow_the_status_machine_and_parent_link(store: WorkflowStateStore) -> None:
    _directory(store)
    parent = store.start_run("intake", "corr-parent", run_id="run-parent")
    child = store.start_run(
        "delivery",
        "corr-child",
        parent_run_id=parent.run_id,
        run_id="run-child",
    )
    assert child.parent_run_id == parent.run_id
    assert child.status == "running"
    with pytest.raises(RegistryRejected):
        store.transition_run(parent.run_id, "running")
    with pytest.raises(RegistryRejected):
        store.transition_run(parent.run_id, "paused")
    with pytest.raises(RegistryRejected):
        store.start_run("intake", "corr-loop", run_id="run-loop", parent_run_id="run-loop")
    with pytest.raises(RegistryRejected):
        store.start_run("intake", "corr-missing", parent_run_id="missing")
    store.transition_run(parent.run_id, "completed")
    with pytest.raises(RegistryRejected):
        store.transition_run(parent.run_id, "failed")
    assert {row.run_id for row in store.list_runs()} == {"run-parent", "run-child"}
    assert store.list_runs(workflow_name="delivery") == (child,)


def test_work_items_are_independent_and_one_mutating_run_holds_each(
    store: WorkflowStateStore,
) -> None:
    _directory(store)
    first = _ready_item(store, title="First slice", work_item_id="work-1")
    second = _ready_item(store, title="Second slice", work_item_id="work-2")
    parent = store.open_work_item(
        item_type="epic",
        title="Epic",
        acceptance="ship the epic",
        work_item_id="epic-1",
    )
    child = store.open_work_item(
        item_type="slice",
        title="Child slice",
        acceptance="ship the child",
        parent_work_item_id=parent.work_item_id,
        work_item_id="work-child",
    )
    assert child.parent_work_item_id == "epic-1"
    with pytest.raises(RegistryRejected):
        store.open_work_item(
            item_type="slice",
            title="Loop",
            acceptance="loop",
            work_item_id="same-item",
            parent_work_item_id="same-item",
        )
    with pytest.raises(RegistryRejected):
        store.open_work_item(
            item_type="slice",
            title="Missing parent",
            acceptance="missing",
            parent_work_item_id="missing",
        )

    running = store.start_run("intake", "corr-1", work_item_id=first, mutating=True, run_id="run-1")
    other = store.start_run(
        "delivery", "corr-2", work_item_id=second, mutating=True, run_id="run-2"
    )
    assert running.status == "running"
    assert other.status == "running"
    assert store.get_work_item(first).active_run_id == "run-1"
    assert store.get_work_item(first).status == "active"
    quiet = store.start_run("intake", "corr-read", work_item_id=first, mutating=False)
    assert quiet.mutating is False
    assert store.get_work_item(first).active_run_id == "run-1"
    with pytest.raises(RegistryRejected):
        store.start_run("delivery", "corr-3", work_item_id=first, mutating=True)
    with pytest.raises(RegistryRejected):
        store.transition_work_item(first, "done")

    draft = store.open_work_item(item_type="slice", title="Draft", acceptance="not ready")
    with pytest.raises(RegistryRejected):
        store.start_run("intake", "corr-draft", work_item_id=draft.work_item_id, mutating=True)
    store.transition_work_item(draft.work_item_id, "cancelled")
    with pytest.raises(RegistryRejected):
        store.start_run("intake", "corr-dead", work_item_id=draft.work_item_id, mutating=False)

    store.transition_run("run-1", "failed")
    held = store.get_work_item(first)
    assert held.status == "blocked"
    assert held.active_run_id is None
    resumed = store.start_run("delivery", "corr-4", work_item_id=first, mutating=True)
    store.transition_run(resumed.run_id, "completed")
    finished = store.get_work_item(first)
    assert finished.status == "active"
    assert finished.active_run_id is None
    store.transition_work_item(first, "done")
    with pytest.raises(RegistryRejected):
        store.transition_work_item(first, "ready")
    with pytest.raises(RegistryRejected):
        store.start_run("intake", "corr-done", work_item_id=first, mutating=True)
    assert store.get_work_item(second).active_run_id == "run-2"


def test_parent_chain_fails_closed_past_the_limit(store: WorkflowStateStore) -> None:
    store.register_organisation("Example org", "1")
    previous: str | None = None
    created = 0
    with pytest.raises(RegistryRejected):
        for index in range(40):
            item = store.open_work_item(
                item_type="slice",
                title=f"Level {index}",
                acceptance=f"level {index}",
                parent_work_item_id=previous,
                work_item_id=f"item-{index}",
            )
            previous = item.work_item_id
            created += 1
    assert created > 16


async def test_paused_run_points_at_one_checkpoint(store: WorkflowStateStore) -> None:
    _directory(store)
    sink: list[str] = []
    workflow = approval_workflow(_tool(sink), AuditLog(), name="intake", storage=store)
    signature = workflow.workflow.graph_signature_hash
    assert isinstance(signature, str) and signature
    store.set_workflow_signature("intake", signature)
    with pytest.raises(RegistryRejected):
        store.set_workflow_signature("intake", "f" * 64)
    store.set_workflow_signature("intake", signature)

    run = store.start_run("intake", "corr-keep", run_id="run-keep")
    paused = await run_workflow(
        workflow,
        envelope("corr-keep", Notice(recipient="unused")),
    )
    assert paused.pending is not None
    bound = await store.bind_checkpoint(run.run_id, paused.pending.checkpoint_id)
    assert bound.checkpoint_id == paused.pending.checkpoint_id
    holding = store.transition_run(run.run_id, "paused")
    assert holding.status == "paused"

    other = store.start_run("delivery", "corr-other", run_id="run-other")
    with pytest.raises(RegistryRejected):
        await store.bind_checkpoint(other.run_id, paused.pending.checkpoint_id)
    store.register_workflow("signed")
    store.set_workflow_signature("signed", "a" * 64)
    signed = store.start_run("signed", "corr-signed")
    with pytest.raises(RegistryRejected):
        await store.bind_checkpoint(signed.run_id, paused.pending.checkpoint_id)

    store.transition_run(run.run_id, "running")
    store.transition_run(run.run_id, "completed")
    with pytest.raises(RegistryRejected):
        await store.bind_checkpoint(run.run_id, paused.pending.checkpoint_id)


def test_ledger_identity_columns_upgrade_old_version_2_files(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    _write_current_v2(path)
    fresh_columns = _ledger_columns(path)
    assert fresh_columns.count("provider") == 1
    assert fresh_columns.count("model") == 1

    _replace_ledger_with_legacy_table(path)
    assert "provider" not in _ledger_columns(path)
    assert "model" not in _ledger_columns(path)

    upgraded = sqlite_workflow_store(path, "org-a")
    assert upgraded.platform_version == PLATFORM_STATE_VERSION
    upgraded_columns = _ledger_columns(path)
    assert upgraded_columns.count("provider") == 1
    assert upgraded_columns.count("model") == 1
    old = upgraded.list_ledger()
    assert len(old) == 1
    assert old[0].event_id == "event-old"
    assert old[0].agent_name == "alex"
    assert old[0].provider is None
    assert old[0].model is None

    secret = "sk-ledger-secret"
    upgraded.append_ledger(
        AuditEvent(
            correlation_id="corr-new",
            agent_name="jordan",
            tool_name="review",
            approval="not_required",
            outcome="completed",
            argument_hash=argument_hash({"api_key": secret}),
            provider="anthropic",
            model="anthropic-model-b",
        ),
        event_id="event-new",
    )
    rendered = _stored_text(path)
    assert secret not in rendered
    assert "anthropic-model-b" in rendered

    reopened = sqlite_workflow_store(path, "org-a")
    opened_again = sqlite_workflow_store(path, "org-a")
    assert reopened.platform_version == PLATFORM_STATE_VERSION
    assert opened_again.platform_version == PLATFORM_STATE_VERSION
    assert _ledger_columns(path).count("provider") == 1
    assert _ledger_columns(path).count("model") == 1
    rows = {row.event_id: row for row in opened_again.list_ledger()}
    assert rows["event-old"].provider is None
    assert rows["event-old"].model is None
    assert rows["event-new"].agent_name == "jordan"
    assert rows["event-new"].provider == "anthropic"
    assert rows["event-new"].model == "anthropic-model-b"
    assert secret not in rows["event-new"].model_dump_json()


def _write_current_v2(path: Path) -> None:
    store = sqlite_workflow_store(path, "org-a")
    assert store.platform_version == PLATFORM_STATE_VERSION


def _ledger_columns(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    names = [str(row[1]) for row in connection.execute("PRAGMA table_info(ledger)")]
    connection.close()
    return names


def _replace_ledger_with_legacy_table(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE ledger_legacy (
            scope TEXT NOT NULL,
            event_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            run_id TEXT,
            agent_name TEXT,
            tool_name TEXT NOT NULL,
            approval TEXT NOT NULL,
            outcome TEXT NOT NULL,
            argument_hash TEXT NOT NULL,
            kind TEXT NOT NULL,
            PRIMARY KEY (scope, event_id)
        );
        INSERT INTO ledger_legacy (
            scope, event_id, recorded_at, correlation_id, run_id, agent_name,
            tool_name, approval, outcome, argument_hash, kind
        ) VALUES (
            'org-a', 'event-old', '2026-01-01T00:00:00+00:00', 'corr-old', NULL,
            'alex', 'read_account', 'not_required', 'completed', 'hash-old', 'tool'
        );
        DROP TABLE ledger;
        ALTER TABLE ledger_legacy RENAME TO ledger;
        """
    )
    connection.commit()
    connection.close()


def test_sqlite_ledger_and_runs_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = sqlite_workflow_store(path, "org-a")
    assert store.platform_version == "2"
    _directory(store)
    store.append_ledger(
        AuditEvent(
            correlation_id="corr-keep",
            agent_name="engineer",
            tool_name="read_notes",
            approval="not_required",
            outcome="completed",
            argument_hash=argument_hash({"token": TOOL_SECRET}),
        ),
        event_id="event-keep",
    )
    store.start_run("intake", "corr-keep", run_id="run-keep")
    item = store.open_work_item(
        item_type="slice",
        title="Durable slice",
        acceptance=SECRET,
        work_item_id="work-keep",
    )
    assert SECRET not in _stored_text(path)
    assert TOOL_SECRET not in _stored_text(path)
    assert item.acceptance_hash == argument_hash({"acceptance": SECRET})

    reopened = sqlite_workflow_store(path, "org-b")
    assert reopened.list_runs() == ()
    with pytest.raises(RegistryRejected):
        reopened.get_organisation()
    same = sqlite_workflow_store(path, "org-a")
    assert same.platform_version == "2"
    assert same.get_run("run-keep").workflow_name == "intake"
    assert same.list_ledger()[0].event_id == "event-keep"
    assert same.get_work_item("work-keep").title == "Durable slice"
    assert same.migrate_platform_state() == "2"
    assert same.get_organisation().name == "Example org"


async def test_version_1_file_resumes_and_migrates_without_rewriting_payloads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite"
    sink: list[str] = []
    pending = await _pause(path, sink)
    _rewrite_payload_version(path, "1")
    _drop_v2_tables(path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_meta SET platform_version = '1'")
    connection.commit()
    names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    connection.close()
    assert "runs" not in names

    store = sqlite_workflow_store(path, "org-a")
    assert store.platform_version == "1"
    with pytest.raises(StateMigrationRequired):
        store.start_run("notice", "corr-keep")
    with pytest.raises(StateMigrationRequired):
        store.register_organisation("Example", "1")
    with pytest.raises(StateMigrationRequired):
        store.append_ledger(
            AuditEvent(
                correlation_id="corr-keep",
                agent_name=None,
                tool_name="read_notes",
                approval="not_required",
                outcome="completed",
                argument_hash=argument_hash({}),
            )
        )

    workflow = approval_workflow(_tool(sink), AuditLog(), name="notice", storage=store)
    resumed = await resume_approval(
        workflow,
        request_id=pending.request_id,
        checkpoint_id=pending.checkpoint_id,
        correlation_id="corr-keep",
        action="send_notice",
        response=HumanApprovalResponse(approved=True),
        arguments={"recipient": "person"},
    )
    assert sink == ["person"]
    assert resumed.output is not None
    assert resumed.output.body["status"] == "completed"
    assert _payload_version(path, pending.checkpoint_id) == "1"

    assert store.migrate_platform_state() == "2"
    assert store.platform_version == "2"
    assert _payload_version(path, pending.checkpoint_id) == "1"
    store.register_organisation("Example org", "1")
    store.register_workflow("notice")
    run = store.start_run("notice", "corr-keep")
    assert run.workflow_name == "notice"
    assert store.list_pending(status="completed")


async def test_unknown_or_incomplete_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    await _pause(path, [])
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_meta SET platform_version = '3'")
    connection.commit()
    connection.close()
    with pytest.raises(CheckpointCompatibilityError):
        sqlite_workflow_store(path, "org-a")

    broken = tmp_path / "broken.sqlite"
    await _pause(broken, [])
    connection = sqlite3.connect(broken)
    connection.execute("DROP TABLE runs")
    connection.commit()
    connection.close()
    with pytest.raises(CheckpointCompatibilityError):
        sqlite_workflow_store(broken, "org-a")

    legacy = tmp_path / "legacy.sqlite"
    pending = await _pause(legacy, [])
    _rewrite_payload_version(legacy, "1")
    store = sqlite_workflow_store(legacy, "org-a")
    found = store.list_pending(status="pending")
    assert len(found) == 1
    loaded = await store.load(pending.checkpoint_id)
    assert loaded.workflow_name == "notice"


def test_corrupt_parent_cycle_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = sqlite_workflow_store(path, "org-a")
    store.register_organisation("Example org", "1")
    store.open_work_item(item_type="slice", title="A", acceptance="a", work_item_id="work-a")
    store.open_work_item(
        item_type="slice",
        title="B",
        acceptance="b",
        parent_work_item_id="work-a",
        work_item_id="work-b",
    )
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE work_items SET parent_work_item_id = 'work-b' WHERE work_item_id = 'work-a'"
    )
    connection.commit()
    connection.close()
    reopened = sqlite_workflow_store(path, "org-a")
    with pytest.raises(RegistryRejected):
        reopened.open_work_item(
            item_type="slice",
            title="C",
            acceptance="c",
            parent_work_item_id="work-a",
        )


async def _pause(path: Path, sink: list[str]) -> PendingHumanInput:
    store = sqlite_workflow_store(path, "org-a")
    workflow = approval_workflow(_tool(sink), AuditLog(), name="notice", storage=store)
    paused = await run_workflow(workflow, envelope("corr-keep", Notice(recipient="person")))
    assert paused.pending is not None
    pending: PendingHumanInput = paused.pending
    return pending


def _rewrite_payload_version(path: Path, version: str) -> None:
    connection = sqlite3.connect(path)
    rows = connection.execute("SELECT checkpoint_id, payload FROM checkpoints").fetchall()
    for checkpoint_id, payload in rows:
        body = json.loads(str(payload))
        body["platform_version"] = version
        connection.execute(
            "UPDATE checkpoints SET payload = ? WHERE checkpoint_id = ?",
            (json.dumps(body), checkpoint_id),
        )
    connection.commit()
    connection.close()


def _payload_version(path: Path, checkpoint_id: str) -> str:
    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT payload FROM checkpoints WHERE checkpoint_id = ?",
        (checkpoint_id,),
    ).fetchone()
    connection.close()
    assert row is not None
    value = json.loads(str(row[0]))["platform_version"]
    assert isinstance(value, str)
    return value


def _drop_v2_tables(path: Path) -> None:
    connection = sqlite3.connect(path)
    for name in V2_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {name}")
    connection.commit()
    connection.close()
