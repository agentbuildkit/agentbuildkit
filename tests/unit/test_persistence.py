"""Offline durable approval tests. SQLite uses a temporary file and no network."""

import sqlite3
from pathlib import Path

import pytest
from agent_framework import FunctionTool
from pydantic import BaseModel

from agent_platform.guardrails import ToolEffect, prepare_tool
from agent_platform.observability import AuditLog
from agent_platform.workflows import (
    CheckpointCompatibilityError,
    HumanApprovalResponse,
    PendingHumanInput,
    WorkflowRunError,
    approval_workflow,
    envelope,
    idempotency_key,
    resume_approval,
    run_workflow,
    sqlite_workflow_store,
)

SECRET = "sk-live-persist-secret"
MODEL_OUTPUT = "MODEL-OUTPUT-SHOULD-NOT-BE-STORED"
RECIPIENT = f"{SECRET}|{MODEL_OUTPUT}"


class Notice(BaseModel):
    recipient: str


def _tool(sink: list[str]) -> FunctionTool:
    def send_notice(recipient: str) -> str:
        """Send a notice."""
        sink.append(recipient)
        return "sent"

    return prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT)


def _stored_text(path: Path) -> str:
    parts = [path]
    parts.extend(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm"))
    text = ""
    for item in parts:
        if item.exists():
            text += item.read_bytes().decode("utf-8", errors="ignore")
    return text


async def _pause(
    path: Path,
    sink: list[str],
    log: AuditLog,
    *,
    scope: str = "org-a",
    name: str = "notice",
    correlation_id: str = "corr-keep",
) -> PendingHumanInput:
    store = sqlite_workflow_store(path, scope, log)
    workflow = approval_workflow(_tool(sink), log, name=name, storage=store)
    paused = await run_workflow(
        workflow,
        envelope(correlation_id, Notice(recipient="unused")).model_copy(
            update={"body": {"recipient": RECIPIENT}}
        ),
    )
    assert paused.pending is not None
    return paused.pending


async def test_checkpoint_survives_reconstruction_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    sink: list[str] = []
    first_log = AuditLog()
    pending = await _pause(path, sink, first_log)
    assert sink == []
    stored = _stored_text(path)
    assert SECRET not in stored
    assert MODEL_OUTPUT not in stored
    assert "corr-keep" in stored

    second_log = AuditLog()
    store = sqlite_workflow_store(path, "org-a", second_log)
    found = store.list_pending(status="pending")
    assert len(found) == 1
    assert found[0].workflow_name == "notice"
    assert found[0].run_id == "corr-keep"
    assert found[0].correlation_id == "corr-keep"
    assert found[0].request_id == pending.request_id
    assert found[0].action == "send_notice"
    assert found[0].checkpoint_id == pending.checkpoint_id
    assert found[0].status == "pending"
    assert found[0].created_at

    workflow = approval_workflow(_tool(sink), second_log, name="notice", storage=store)
    resumed = await resume_approval(
        workflow,
        request_id=pending.request_id,
        checkpoint_id=pending.checkpoint_id,
        correlation_id="corr-keep",
        action="send_notice",
        response=HumanApprovalResponse(approved=True),
        arguments={"recipient": RECIPIENT},
    )

    assert sink == [RECIPIENT]
    assert resumed.output is not None
    assert resumed.output.body["status"] == "completed"
    assert resumed.correlation_id == "corr-keep"
    assert {event.correlation_id for event in second_log.events} == {"corr-keep"}
    assert "checkpoint_created" in {event.kind for event in first_log.events}
    assert "workflow_paused" in {event.kind for event in first_log.events}
    assert "workflow_resumed" in {event.kind for event in second_log.events}
    assert "approval_accepted" in {event.kind for event in second_log.events}
    assert "side_effect_attempted" in {event.kind for event in second_log.events}
    assert "side_effect_completed" in {event.kind for event in second_log.events}
    assert SECRET not in "".join(event.model_dump_json() for event in second_log.events)


async def test_rejection_survives_restart_and_blocks_a_later_approval(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    sink: list[str] = []
    pending = await _pause(path, sink, AuditLog())
    store = sqlite_workflow_store(path, "org-a", AuditLog())
    workflow = approval_workflow(_tool(sink), AuditLog(), name="notice", storage=store)
    rejected = await resume_approval(
        workflow,
        request_id=pending.request_id,
        checkpoint_id=pending.checkpoint_id,
        correlation_id="corr-keep",
        action="send_notice",
        response=HumanApprovalResponse(approved=False),
    )
    assert sink == []
    assert rejected.output is not None
    assert rejected.output.body["status"] == "rejected"
    assert store.list_pending(status="rejected")[0].request_id == pending.request_id

    again = sqlite_workflow_store(path, "org-a", AuditLog())
    rebuilt = approval_workflow(_tool(sink), AuditLog(), name="notice", storage=again)
    repeated = await resume_approval(
        rebuilt,
        request_id=pending.request_id,
        checkpoint_id=pending.checkpoint_id,
        correlation_id="corr-keep",
        action="send_notice",
        response=HumanApprovalResponse(approved=True),
        arguments={"recipient": RECIPIENT},
    )

    assert sink == []
    assert repeated.output is not None
    assert repeated.output.body["status"] == "rejected"


async def test_duplicate_approval_and_resume_run_the_side_effect_once(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    sink: list[str] = []
    pending = await _pause(path, sink, AuditLog())

    async def approve() -> None:
        store = sqlite_workflow_store(path, "org-a", AuditLog())
        workflow = approval_workflow(_tool(sink), AuditLog(), name="notice", storage=store)
        result = await resume_approval(
            workflow,
            request_id=pending.request_id,
            checkpoint_id=pending.checkpoint_id,
            correlation_id="corr-keep",
            action="send_notice",
            response=HumanApprovalResponse(approved=True),
            arguments={"recipient": RECIPIENT},
        )
        assert result.output is not None
        assert result.output.body["status"] == "completed"

    await approve()
    await approve()
    assert sink == [RECIPIENT]


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": "other-request"},
        {"checkpoint_id": "other-checkpoint"},
        {"correlation_id": "other-correlation"},
        {"action": "other-action"},
    ],
)
async def test_mismatched_approval_does_not_run(tmp_path: Path, override: dict[str, str]) -> None:
    path = tmp_path / "state.sqlite"
    sink: list[str] = []
    pending = await _pause(path, sink, AuditLog())
    store = sqlite_workflow_store(path, "org-a", AuditLog())
    workflow = approval_workflow(_tool(sink), AuditLog(), name="notice", storage=store)
    values = {
        "request_id": pending.request_id,
        "checkpoint_id": pending.checkpoint_id,
        "correlation_id": "corr-keep",
        "action": "send_notice",
    }
    values.update(override)

    with pytest.raises(WorkflowRunError) as caught:
        await resume_approval(
            workflow,
            request_id=values["request_id"],
            checkpoint_id=values["checkpoint_id"],
            correlation_id=values["correlation_id"],
            action=values["action"],
            response=HumanApprovalResponse(approved=True),
            arguments={"recipient": SECRET},
        )

    assert sink == []
    assert SECRET not in str(caught.value)
    assert "other-correlation" not in str(caught.value)


async def test_another_scope_cannot_see_or_resume_the_run(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    sink: list[str] = []
    pending = await _pause(path, sink, AuditLog(), scope="org-a")
    other = sqlite_workflow_store(path, "org-b", AuditLog())
    assert other.list_pending() == ()
    workflow = approval_workflow(_tool(sink), AuditLog(), name="notice", storage=other)

    with pytest.raises(WorkflowRunError):
        await resume_approval(
            workflow,
            request_id=pending.request_id,
            checkpoint_id=pending.checkpoint_id,
            correlation_id="corr-keep",
            action="send_notice",
            response=HumanApprovalResponse(approved=True),
            arguments={"recipient": SECRET},
        )

    assert sink == []


async def test_wrong_workflow_name_does_not_resume(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    sink: list[str] = []
    pending = await _pause(path, sink, AuditLog(), name="notice")
    store = sqlite_workflow_store(path, "org-a", AuditLog())
    workflow = approval_workflow(_tool(sink), AuditLog(), name="other-notice", storage=store)

    with pytest.raises(WorkflowRunError):
        await resume_approval(
            workflow,
            request_id=pending.request_id,
            checkpoint_id=pending.checkpoint_id,
            correlation_id="corr-keep",
            action="send_notice",
            response=HumanApprovalResponse(approved=True),
            arguments={"recipient": RECIPIENT},
        )

    assert sink == []


async def test_incompatible_or_corrupt_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    pending = await _pause(path, [], AuditLog())
    connection = sqlite3.connect(path)
    connection.execute("UPDATE checkpoints SET payload = ?", ("{",))
    connection.commit()
    connection.close()
    store = sqlite_workflow_store(path, "org-a")

    with pytest.raises(CheckpointCompatibilityError) as caught:
        await store.load(pending.checkpoint_id)

    assert SECRET not in str(caught.value)
    assert "{" not in str(caught.value)

    versioned = tmp_path / "versioned.sqlite"
    await _pause(versioned, [], AuditLog())
    connection = sqlite3.connect(versioned)
    connection.execute("UPDATE schema_meta SET platform_version = ?", ("0",))
    connection.commit()
    connection.close()
    with pytest.raises(CheckpointCompatibilityError):
        sqlite_workflow_store(versioned, "org-a")


def test_idempotency_key_depends_on_stable_action_identity() -> None:
    original = idempotency_key("notice", "corr-keep", "request-1", "send_notice")
    assert original == idempotency_key("notice", "corr-keep", "request-1", "send_notice")
    assert original != idempotency_key("notice", "corr-other", "request-1", "send_notice")
    assert original != idempotency_key("other", "corr-keep", "request-1", "send_notice")
