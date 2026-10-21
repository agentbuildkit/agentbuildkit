"""Durable workflow pause state.

SQLite is the first durable backend. A memory backend covers local development.
PostgreSQL can implement the same table operations later. Workflow definitions
do not name a database.

The guarantee for an approved side effect is at-most-once for one idempotency
key inside this store. The key is the workflow name, correlation id, request
id, and action. A repeated approval or resume does not call the tool again.
If the process stops after the key is reserved and before the outcome is
recorded, a later resume also does not call the tool. This store cannot promise
exactly-once delivery to an external system.

Checkpoints store hashes, identifiers, and approval metadata. They do not store
argument values, prompts, model responses, or credentials. Resume rebuilds the
workflow from the organisation's definition and supplies the arguments again.
Those arguments must hash to the stored value. Python functions are not stored.

New files are platform state version 2. Version 1 files still open for
checkpoints, pending approvals, and side-effect reservations. Ledger, run,
directory, and work-item calls on a version 1 file fail closed until
``migrate_platform_state`` creates the version 2 tables and bumps the schema
version. Checkpoint payloads written by version 1 remain readable. Unknown
schema or payload versions are rejected.
"""

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from agent_framework import WorkflowCheckpoint, WorkflowEvent
from pydantic import BaseModel, ConfigDict

from agent_platform.observability import AuditEvent, AuditLog, argument_hash
from agent_platform.state.api import PlatformStateMixin
from agent_platform.state.models import (
    PLATFORM_STATE_VERSION,
    SUPPORTED_PLATFORM_STATE_VERSIONS,
    CheckpointCompatibilityError,
    RunRecord,
)
from agent_platform.state.registry import (
    V2_DDL,
    MemoryRegistry,
    PlatformRegistry,
    SqliteRegistry,
    v2_schema_ready,
)
from agent_platform.workflows.runtime import (
    ApprovalGate,
    CompiledWorkflow,
    HumanApprovalRequest,
    HumanApprovalResponse,
    WorkflowEnvelope,
    WorkflowRun,
    WorkflowRunError,
    run_workflow,
)

_FRAMEWORK_CHECKPOINT_VERSION = "1.0"
_LIFECYCLE_HASH = "lifecycle"
PendingStatus = Literal["pending", "rejected", "completed", "failed"]
EffectStatus = Literal["started", "completed", "failed", "rejected"]


class PendingRequest(BaseModel):
    """Safe metadata for one paused approval. Argument values are not included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    workflow_name: str
    run_id: str
    correlation_id: str
    request_id: str
    action: str
    checkpoint_id: str
    created_at: str
    status: PendingStatus


class _EffectRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EffectStatus


class _Backend(Protocol):
    def save_checkpoint(
        self,
        *,
        scope: str,
        checkpoint_id: str,
        workflow_name: str,
        graph_signature_hash: str,
        timestamp: str,
        payload: str,
    ) -> None: ...

    def load_checkpoint(self, *, scope: str, checkpoint_id: str) -> str | None: ...

    def list_checkpoint_payloads(self, *, scope: str, workflow_name: str) -> list[str]: ...

    def delete_checkpoint(self, *, scope: str, checkpoint_id: str) -> bool: ...

    def save_pending(self, row: PendingRequest, *, argument_hash: str) -> None: ...

    def get_pending(self, *, scope: str, request_id: str) -> tuple[PendingRequest, str] | None: ...

    def list_pending(self, *, scope: str, status: PendingStatus | None) -> list[PendingRequest]: ...

    def mark_pending(self, *, scope: str, request_id: str, status: PendingStatus) -> None: ...

    def claim_effect(
        self,
        *,
        scope: str,
        key: str,
        workflow_name: str,
        correlation_id: str,
        request_id: str,
        action: str,
        status: EffectStatus,
        created_at: str,
    ) -> _EffectRow | None: ...

    def get_effect(self, *, scope: str, key: str) -> _EffectRow | None: ...

    def finish_effect(self, *, scope: str, key: str, status: EffectStatus) -> None: ...

    def platform_version(self) -> str: ...

    def platform_registry(self) -> PlatformRegistry | None: ...

    def upgrade_platform_state(self) -> PlatformRegistry: ...


class WorkflowStateStore(PlatformStateMixin):
    """Checkpoint storage plus pending approvals and side-effect reservations."""

    def __init__(self, backend: _Backend, *, scope: str, audit_log: AuditLog | None = None) -> None:
        if not scope:
            raise ValueError("Workflow state needs a scope.")
        self.scope = scope
        self._backend = backend
        self._audit_log = audit_log
        self._registry = backend.platform_registry()
        self._seen_checkpoints: set[str] = set()
        self._seen_pauses: set[str] = set()

    @property
    def platform_version(self) -> str:
        """Schema version of this store. New stores are version 2."""
        return self._backend.platform_version()

    def migrate_platform_state(self) -> str:
        """Create version 2 tables when the file is version 1. Return the version.

        Checkpoint payloads, pending approvals, and side-effect rows are left
        as they were. A version 2 store is unchanged.
        """
        self._registry = self._backend.upgrade_platform_state()
        return self.platform_version

    async def bind_checkpoint(self, run_id: str, checkpoint_id: str) -> RunRecord:
        """Point a run at an existing checkpoint for the same workflow."""
        checkpoint = await self.load(checkpoint_id)
        return self._require_registry().bind_checkpoint(
            self.scope,
            run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            workflow_name=checkpoint.workflow_name,
            graph_signature_hash=checkpoint.graph_signature_hash,
        )

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        """Persist a scrubbed checkpoint and any pending approval metadata."""
        payload, requests = _scrub(checkpoint)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self._backend.save_checkpoint(
            scope=self.scope,
            checkpoint_id=checkpoint.checkpoint_id,
            workflow_name=checkpoint.workflow_name,
            graph_signature_hash=checkpoint.graph_signature_hash,
            timestamp=checkpoint.timestamp,
            payload=encoded,
        )
        if requests and checkpoint.checkpoint_id not in self._seen_checkpoints:
            self.record_lifecycle(
                correlation_id=requests[0][0].correlation_id,
                tool_name="checkpoint.created",
                approval="not_required",
                outcome="completed",
                kind="checkpoint_created",
            )
        self._seen_checkpoints.add(checkpoint.checkpoint_id)
        for pending, digest in requests:
            scoped = pending.model_copy(update={"scope": self.scope})
            self._backend.save_pending(scoped, argument_hash=digest)
            if scoped.request_id in self._seen_pauses:
                continue
            self.record_lifecycle(
                correlation_id=scoped.correlation_id,
                tool_name=scoped.action,
                approval="pending",
                outcome="pending_approval",
                kind="workflow_paused",
            )
            self._seen_pauses.add(scoped.request_id)
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        """Rebuild a checkpoint from stored metadata."""
        encoded = self._backend.load_checkpoint(scope=self.scope, checkpoint_id=checkpoint_id)
        if encoded is None:
            raise CheckpointCompatibilityError()
        return _restore_checkpoint(encoded)

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        """Return scrubbed checkpoints for one workflow in this scope."""
        payloads = self._backend.list_checkpoint_payloads(
            scope=self.scope,
            workflow_name=workflow_name,
        )
        return [_restore_checkpoint(payload) for payload in payloads]

    async def delete(self, checkpoint_id: str) -> bool:
        """Delete one checkpoint. Pending approval rows stay in place."""
        return self._backend.delete_checkpoint(scope=self.scope, checkpoint_id=checkpoint_id)

    async def get_latest(self, *, workflow_name: str) -> WorkflowCheckpoint | None:
        """Return the newest scrubbed checkpoint for a workflow."""
        checkpoints = await self.list_checkpoints(workflow_name=workflow_name)
        if not checkpoints:
            return None
        return max(checkpoints, key=lambda checkpoint: checkpoint.timestamp)

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        """Return checkpoint ids for a workflow in this scope."""
        checkpoints = await self.list_checkpoints(workflow_name=workflow_name)
        return [checkpoint.checkpoint_id for checkpoint in checkpoints]

    def list_pending(self, *, status: PendingStatus | None = None) -> tuple[PendingRequest, ...]:
        """Return paused requests for this scope."""
        return tuple(self._backend.list_pending(scope=self.scope, status=status))

    def pending_for(self, request_id: str) -> tuple[PendingRequest, str] | None:
        """Return one pending request and its argument hash."""
        return self._backend.get_pending(scope=self.scope, request_id=request_id)

    def effect_for(self, key: str) -> _EffectRow | None:
        """Return the reservation for one idempotency key."""
        return self._backend.get_effect(scope=self.scope, key=key)

    def claim_effect(
        self,
        *,
        key: str,
        workflow_name: str,
        correlation_id: str,
        request_id: str,
        action: str,
        status: EffectStatus,
    ) -> _EffectRow | None:
        """Reserve a side effect. Return the existing row when the key is already used."""
        return self._backend.claim_effect(
            scope=self.scope,
            key=key,
            workflow_name=workflow_name,
            correlation_id=correlation_id,
            request_id=request_id,
            action=action,
            status=status,
            created_at=_now(),
        )

    def finish_effect(self, key: str, status: EffectStatus) -> None:
        """Record the outcome of a reserved side effect."""
        self._backend.finish_effect(scope=self.scope, key=key, status=status)

    def mark_pending(self, request_id: str, status: PendingStatus) -> None:
        """Update the visible status of a paused request."""
        self._backend.mark_pending(scope=self.scope, request_id=request_id, status=status)

    def record_lifecycle(
        self,
        *,
        correlation_id: str,
        tool_name: str,
        approval: Literal["not_required", "pending", "approved", "rejected"],
        outcome: Literal["completed", "pending_approval", "rejected", "failed"],
        kind: Literal[
            "checkpoint_created",
            "workflow_paused",
            "workflow_resumed",
            "approval_accepted",
            "approval_rejected",
        ],
    ) -> None:
        if self._audit_log is None:
            return
        self._audit_log.record(
            AuditEvent(
                correlation_id=correlation_id,
                agent_name=self.scope,
                tool_name=tool_name,
                approval=approval,
                outcome=outcome,
                argument_hash=_LIFECYCLE_HASH,
                kind=kind,
            )
        )


class _MemoryBackend:
    def __init__(self) -> None:
        self.checkpoints: dict[tuple[str, str], str] = {}
        self.pending: dict[tuple[str, str], tuple[PendingRequest, str]] = {}
        self.effects: dict[tuple[str, str], _EffectRow] = {}
        self._registry = MemoryRegistry()

    def platform_version(self) -> str:
        return PLATFORM_STATE_VERSION

    def platform_registry(self) -> PlatformRegistry | None:
        return self._registry

    def upgrade_platform_state(self) -> PlatformRegistry:
        return self._registry

    def save_checkpoint(
        self,
        *,
        scope: str,
        checkpoint_id: str,
        workflow_name: str,
        graph_signature_hash: str,
        timestamp: str,
        payload: str,
    ) -> None:
        del workflow_name, graph_signature_hash, timestamp
        self.checkpoints[(scope, checkpoint_id)] = payload

    def load_checkpoint(self, *, scope: str, checkpoint_id: str) -> str | None:
        return self.checkpoints.get((scope, checkpoint_id))

    def list_checkpoint_payloads(self, *, scope: str, workflow_name: str) -> list[str]:
        found: list[str] = []
        for (item_scope, _), payload in self.checkpoints.items():
            if item_scope != scope:
                continue
            decoded = _object(payload)
            if decoded.get("workflow_name") == workflow_name:
                found.append(payload)
        return found

    def delete_checkpoint(self, *, scope: str, checkpoint_id: str) -> bool:
        return self.checkpoints.pop((scope, checkpoint_id), None) is not None

    def save_pending(self, row: PendingRequest, *, argument_hash: str) -> None:
        key = (row.scope, row.request_id)
        current = self.pending.get(key)
        if current is None:
            self.pending[key] = (row, argument_hash)
            return
        if current[0].status != "pending":
            return
        self.pending[key] = (row, argument_hash)

    def get_pending(self, *, scope: str, request_id: str) -> tuple[PendingRequest, str] | None:
        return self.pending.get((scope, request_id))

    def list_pending(self, *, scope: str, status: PendingStatus | None) -> list[PendingRequest]:
        rows = [item[0] for key, item in self.pending.items() if key[0] == scope]
        if status is None:
            return rows
        return [item for item in rows if item.status == status]

    def mark_pending(self, *, scope: str, request_id: str, status: PendingStatus) -> None:
        current = self.pending.get((scope, request_id))
        if current is None:
            return
        updated = current[0].model_copy(update={"status": status})
        self.pending[(scope, request_id)] = (updated, current[1])

    def claim_effect(
        self,
        *,
        scope: str,
        key: str,
        workflow_name: str,
        correlation_id: str,
        request_id: str,
        action: str,
        status: EffectStatus,
        created_at: str,
    ) -> _EffectRow | None:
        del workflow_name, correlation_id, request_id, action, created_at
        existing = self.effects.get((scope, key))
        if existing is not None:
            return existing
        self.effects[(scope, key)] = _EffectRow(status=status)
        return None

    def get_effect(self, *, scope: str, key: str) -> _EffectRow | None:
        return self.effects.get((scope, key))

    def finish_effect(self, *, scope: str, key: str, status: EffectStatus) -> None:
        if (scope, key) in self.effects:
            self.effects[(scope, key)] = _EffectRow(status=status)


class _SqliteBackend:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._version = ""
        self._registry: SqliteRegistry | None = None
        self._prepare()

    def platform_version(self) -> str:
        return self._version

    def platform_registry(self) -> PlatformRegistry | None:
        return self._registry

    def upgrade_platform_state(self) -> PlatformRegistry:
        if self._version == PLATFORM_STATE_VERSION and self._registry is not None:
            return self._registry
        if self._version != "1":
            raise CheckpointCompatibilityError()
        self._connection.executescript(V2_DDL)
        self._connection.execute(
            "UPDATE schema_meta SET platform_version = ? WHERE id = 1",
            (PLATFORM_STATE_VERSION,),
        )
        self._commit()
        if not v2_schema_ready(self._connection):
            raise CheckpointCompatibilityError()
        self._version = PLATFORM_STATE_VERSION
        self._registry = SqliteRegistry(self._connection, self._commit)
        return self._registry

    def _prepare(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                platform_version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                scope TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                graph_signature_hash TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (scope, checkpoint_id)
            );
            CREATE TABLE IF NOT EXISTS pending_requests (
                scope TEXT NOT NULL,
                request_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                action TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                argument_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (scope, request_id)
            );
            CREATE TABLE IF NOT EXISTS side_effects (
                scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (scope, idempotency_key)
            );
            """
        )
        row = self._connection.execute(
            "SELECT platform_version FROM schema_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            self._connection.executescript(V2_DDL)
            self._connection.execute(
                "INSERT INTO schema_meta (id, platform_version) VALUES (1, ?)",
                (PLATFORM_STATE_VERSION,),
            )
            self._version = PLATFORM_STATE_VERSION
        elif row["platform_version"] == "1":
            self._version = "1"
        elif row["platform_version"] == PLATFORM_STATE_VERSION:
            if not v2_schema_ready(self._connection):
                raise CheckpointCompatibilityError()
            self._version = PLATFORM_STATE_VERSION
        else:
            raise CheckpointCompatibilityError()
        if self._version == PLATFORM_STATE_VERSION:
            self._registry = SqliteRegistry(self._connection, self._commit)
        self._connection.commit()
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def save_checkpoint(
        self,
        *,
        scope: str,
        checkpoint_id: str,
        workflow_name: str,
        graph_signature_hash: str,
        timestamp: str,
        payload: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO checkpoints (
                scope, checkpoint_id, workflow_name, graph_signature_hash, timestamp, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, checkpoint_id) DO UPDATE SET
                payload = excluded.payload,
                timestamp = excluded.timestamp
            """,
            (scope, checkpoint_id, workflow_name, graph_signature_hash, timestamp, payload),
        )
        self._commit()

    def load_checkpoint(self, *, scope: str, checkpoint_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT payload FROM checkpoints WHERE scope = ? AND checkpoint_id = ?",
            (scope, checkpoint_id),
        ).fetchone()
        if row is None:
            return None
        return str(row["payload"])

    def list_checkpoint_payloads(self, *, scope: str, workflow_name: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT payload FROM checkpoints WHERE scope = ? AND workflow_name = ?",
            (scope, workflow_name),
        ).fetchall()
        return [str(row["payload"]) for row in rows]

    def delete_checkpoint(self, *, scope: str, checkpoint_id: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM checkpoints WHERE scope = ? AND checkpoint_id = ?",
            (scope, checkpoint_id),
        )
        self._commit()
        return cursor.rowcount > 0

    def save_pending(self, row: PendingRequest, *, argument_hash: str) -> None:
        current = self.get_pending(scope=row.scope, request_id=row.request_id)
        if current is not None and current[0].status != "pending":
            return
        self._connection.execute(
            """
            INSERT INTO pending_requests (
                scope, request_id, workflow_name, run_id, correlation_id, action,
                checkpoint_id, argument_hash, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, request_id) DO UPDATE SET
                checkpoint_id = excluded.checkpoint_id,
                argument_hash = excluded.argument_hash
            WHERE pending_requests.status = 'pending'
            """,
            (
                row.scope,
                row.request_id,
                row.workflow_name,
                row.run_id,
                row.correlation_id,
                row.action,
                row.checkpoint_id,
                argument_hash,
                row.created_at,
                row.status,
            ),
        )
        self._commit()

    def get_pending(self, *, scope: str, request_id: str) -> tuple[PendingRequest, str] | None:
        row = self._connection.execute(
            "SELECT * FROM pending_requests WHERE scope = ? AND request_id = ?",
            (scope, request_id),
        ).fetchone()
        if row is None:
            return None
        return _pending_from_row(row), str(row["argument_hash"])

    def list_pending(self, *, scope: str, status: PendingStatus | None) -> list[PendingRequest]:
        if status is None:
            rows = self._connection.execute(
                "SELECT * FROM pending_requests WHERE scope = ?",
                (scope,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM pending_requests WHERE scope = ? AND status = ?",
                (scope, status),
            ).fetchall()
        return [_pending_from_row(row) for row in rows]

    def mark_pending(self, *, scope: str, request_id: str, status: PendingStatus) -> None:
        self._connection.execute(
            "UPDATE pending_requests SET status = ? WHERE scope = ? AND request_id = ?",
            (status, scope, request_id),
        )
        self._commit()

    def claim_effect(
        self,
        *,
        scope: str,
        key: str,
        workflow_name: str,
        correlation_id: str,
        request_id: str,
        action: str,
        status: EffectStatus,
        created_at: str,
    ) -> _EffectRow | None:
        existing = self.get_effect(scope=scope, key=key)
        if existing is not None:
            return existing
        try:
            self._connection.execute(
                """
                INSERT INTO side_effects (
                    scope, idempotency_key, workflow_name, correlation_id, request_id,
                    action, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (scope, key, workflow_name, correlation_id, request_id, action, status, created_at),
            )
            self._commit()
        except sqlite3.IntegrityError:
            self._connection.rollback()
            return self.get_effect(scope=scope, key=key)
        return None

    def get_effect(self, *, scope: str, key: str) -> _EffectRow | None:
        row = self._connection.execute(
            "SELECT status FROM side_effects WHERE scope = ? AND idempotency_key = ?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        return _EffectRow(status=_effect_status(str(row["status"])))

    def finish_effect(self, *, scope: str, key: str, status: EffectStatus) -> None:
        self._connection.execute(
            "UPDATE side_effects SET status = ? WHERE scope = ? AND idempotency_key = ?",
            (status, scope, key),
        )
        self._commit()

    def _commit(self) -> None:
        self._connection.commit()
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def memory_workflow_store(scope: str, audit_log: AuditLog | None = None) -> WorkflowStateStore:
    """In-memory state for tests and single-process development."""
    return WorkflowStateStore(_MemoryBackend(), scope=scope, audit_log=audit_log)


def sqlite_workflow_store(
    path: str | Path,
    scope: str,
    audit_log: AuditLog | None = None,
) -> WorkflowStateStore:
    """SQLite state that survives a process restart on one machine."""
    return WorkflowStateStore(_SqliteBackend(Path(path)), scope=scope, audit_log=audit_log)


def idempotency_key(workflow_name: str, correlation_id: str, request_id: str, action: str) -> str:
    """Hash the stable identity of one paused action."""
    material = "\n".join((workflow_name, correlation_id, request_id, action))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def resume_approval(
    compiled: CompiledWorkflow,
    *,
    request_id: str,
    checkpoint_id: str,
    correlation_id: str,
    action: str,
    response: HumanApprovalResponse,
    arguments: Mapping[str, Any] | None = None,
) -> WorkflowRun:
    """Resume one stored approval. A mismatched identity does not run the action."""
    store = compiled.checkpoints
    if not isinstance(store, WorkflowStateStore):
        raise WorkflowRunError("Approval does not match the pending action.")
    found = store.pending_for(request_id)
    if found is None:
        raise WorkflowRunError("Approval does not match the pending action.")
    pending, digest = found
    if (
        pending.workflow_name != compiled.name
        or pending.correlation_id != correlation_id
        or pending.action != action
        or pending.checkpoint_id != checkpoint_id
    ):
        raise WorkflowRunError("Approval does not match the pending action.")
    if pending.status == "rejected":
        return _finished(correlation_id, "rejected")
    key = idempotency_key(compiled.name, correlation_id, request_id, action)
    existing = store.effect_for(key)
    if existing is not None:
        return _finished(correlation_id, _public_status(existing.status))
    checkpoint = await store.load(checkpoint_id)
    if (
        checkpoint.workflow_name != compiled.name
        or checkpoint.graph_signature_hash != compiled.workflow.graph_signature_hash
    ):
        raise CheckpointCompatibilityError()
    if response.approved:
        if arguments is None:
            raise WorkflowRunError("Approval arguments must be supplied for this run.")
        supplied = _copy(arguments)
        if argument_hash(supplied) != digest:
            raise WorkflowRunError("Approval arguments do not match the paused action.")
        claimed = store.claim_effect(
            key=key,
            workflow_name=compiled.name,
            correlation_id=correlation_id,
            request_id=request_id,
            action=action,
            status="started",
        )
        if claimed is not None:
            return _finished(correlation_id, _public_status(claimed.status))
        _stage(compiled, correlation_id, supplied)
        store.record_lifecycle(
            correlation_id=correlation_id,
            tool_name=action,
            approval="approved",
            outcome="completed",
            kind="approval_accepted",
        )
        store.record_lifecycle(
            correlation_id=correlation_id,
            tool_name=compiled.name,
            approval="approved",
            outcome="completed",
            kind="workflow_resumed",
        )
        try:
            result = await run_workflow(
                compiled,
                responses={request_id: response},
                checkpoint_id=checkpoint_id,
            )
        except Exception:
            store.finish_effect(key, "failed")
            store.mark_pending(request_id, "failed")
            raise WorkflowRunError("Side effect could not be completed.") from None
        store.finish_effect(key, "completed")
        store.mark_pending(request_id, "completed")
        return result
    claimed = store.claim_effect(
        key=key,
        workflow_name=compiled.name,
        correlation_id=correlation_id,
        request_id=request_id,
        action=action,
        status="rejected",
    )
    if claimed is not None:
        return _finished(correlation_id, _public_status(claimed.status))
    store.record_lifecycle(
        correlation_id=correlation_id,
        tool_name=action,
        approval="rejected",
        outcome="rejected",
        kind="approval_rejected",
    )
    store.record_lifecycle(
        correlation_id=correlation_id,
        tool_name=compiled.name,
        approval="rejected",
        outcome="rejected",
        kind="workflow_resumed",
    )
    result = await run_workflow(
        compiled,
        responses={request_id: response},
        checkpoint_id=checkpoint_id,
    )
    store.mark_pending(request_id, "rejected")
    return result


def _scrub(
    checkpoint: WorkflowCheckpoint,
) -> tuple[dict[str, Any], list[tuple[PendingRequest, str]]]:
    executor_state: dict[str, Any] = {}
    raw_state = checkpoint.state.get("_executor_state", {})
    requests: list[tuple[PendingRequest, str]] = []
    raw_executors = _as_map(raw_state)
    if raw_executors is not None:
        for executor_id, snapshot in raw_executors.items():
            snapshot_map = _as_map(snapshot)
            if snapshot_map is None:
                continue
            pending_map = _as_map(snapshot_map.get("pending", {}))
            safe_pending: dict[str, dict[str, str]] = {}
            if pending_map is not None:
                for correlation_id, body in pending_map.items():
                    body_map = _as_map(body)
                    if body_map is None:
                        continue
                    digest = body_map.get("argument_hash")
                    if not isinstance(digest, str):
                        digest = argument_hash(body_map)
                    safe_pending[correlation_id] = {"argument_hash": digest}
            executor_state[executor_id] = {"pending": safe_pending}
    safe_requests: list[dict[str, str]] = []
    for request_id, event in checkpoint.pending_request_info_events.items():
        data = getattr(event, "data", None)
        if not isinstance(data, HumanApprovalRequest):
            continue
        source = getattr(event, "source_executor_id", None)
        if not isinstance(source, str):
            source = "approval-gate"
        digest = _hash_for(executor_state, data.correlation_id)
        pending = PendingRequest(
            scope="",
            workflow_name=checkpoint.workflow_name,
            run_id=data.correlation_id,
            correlation_id=data.correlation_id,
            request_id=request_id,
            action=data.action,
            checkpoint_id=checkpoint.checkpoint_id,
            created_at=checkpoint.timestamp,
            status="pending",
        )
        requests.append((pending, digest))
        safe_requests.append(
            {
                "request_id": request_id,
                "source_executor_id": source,
                "correlation_id": data.correlation_id,
                "action": data.action,
            }
        )
    payload = {
        "platform_version": PLATFORM_STATE_VERSION,
        "framework_version": checkpoint.version,
        "workflow_name": checkpoint.workflow_name,
        "graph_signature_hash": checkpoint.graph_signature_hash,
        "checkpoint_id": checkpoint.checkpoint_id,
        "previous_checkpoint_id": checkpoint.previous_checkpoint_id,
        "timestamp": checkpoint.timestamp,
        "iteration_count": checkpoint.iteration_count,
        "executor_state": executor_state,
        "requests": safe_requests,
    }
    return payload, requests


def _restore_checkpoint(encoded: str) -> WorkflowCheckpoint:
    try:
        payload = _object(encoded)
        if payload.get("platform_version") not in SUPPORTED_PLATFORM_STATE_VERSIONS:
            raise CheckpointCompatibilityError()
        workflow_name = _text(payload, "workflow_name")
        graph_hash = _text(payload, "graph_signature_hash")
        checkpoint_id = _text(payload, "checkpoint_id")
        timestamp = _text(payload, "timestamp")
        iteration = payload.get("iteration_count")
        if not isinstance(iteration, int):
            raise CheckpointCompatibilityError()
        executor_state = payload.get("executor_state")
        if not isinstance(executor_state, dict):
            raise CheckpointCompatibilityError()
        events: dict[str, WorkflowEvent[Any]] = {}
        raw_requests = payload.get("requests")
        if not isinstance(raw_requests, list):
            raise CheckpointCompatibilityError()
        for item in cast(Sequence[object], raw_requests):
            request = _as_map(item)
            if request is None:
                raise CheckpointCompatibilityError()
            request_id = _text(request, "request_id")
            events[request_id] = WorkflowEvent.request_info(
                request_id,
                _text(request, "source_executor_id"),
                HumanApprovalRequest(
                    correlation_id=_text(request, "correlation_id"),
                    action=_text(request, "action"),
                ),
                HumanApprovalResponse,
            )
        previous = payload.get("previous_checkpoint_id")
        if previous is not None and not isinstance(previous, str):
            raise CheckpointCompatibilityError()
        return WorkflowCheckpoint(
            workflow_name=workflow_name,
            graph_signature_hash=graph_hash,
            checkpoint_id=checkpoint_id,
            previous_checkpoint_id=previous,
            timestamp=timestamp,
            messages={},
            state={"_executor_state": executor_state},
            pending_request_info_events=events,
            iteration_count=iteration,
            metadata={},
            version=_FRAMEWORK_CHECKPOINT_VERSION,
        )
    except CheckpointCompatibilityError:
        raise
    except Exception:
        raise CheckpointCompatibilityError() from None


def _hash_for(executor_state: Mapping[str, Any], correlation_id: str) -> str:
    for snapshot in executor_state.values():
        snapshot_map = _as_map(snapshot)
        if snapshot_map is None:
            continue
        pending_map = _as_map(snapshot_map.get("pending"))
        if pending_map is None:
            continue
        body_map = _as_map(pending_map.get(correlation_id))
        if body_map is None:
            continue
        digest = body_map.get("argument_hash")
        if isinstance(digest, str):
            return digest
    raise CheckpointCompatibilityError()


def _object(encoded: str) -> dict[str, Any]:
    parsed = _as_map(json.loads(encoded))
    if parsed is None:
        raise CheckpointCompatibilityError()
    return dict(parsed)


def _as_map(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, Any] = {}
    for key, item in cast(Mapping[Any, Any], value).items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CheckpointCompatibilityError()
    return value


def _pending_from_row(row: sqlite3.Row) -> PendingRequest:
    status = str(row["status"])
    if status not in {"pending", "rejected", "completed", "failed"}:
        raise CheckpointCompatibilityError()
    return PendingRequest(
        scope=str(row["scope"]),
        workflow_name=str(row["workflow_name"]),
        run_id=str(row["run_id"]),
        correlation_id=str(row["correlation_id"]),
        request_id=str(row["request_id"]),
        action=str(row["action"]),
        checkpoint_id=str(row["checkpoint_id"]),
        created_at=str(row["created_at"]),
        status=status,  # type: ignore[arg-type]
    )


def _effect_status(value: str) -> EffectStatus:
    if value not in {"started", "completed", "failed", "rejected"}:
        raise CheckpointCompatibilityError()
    return value  # type: ignore[return-value]


def _stage(compiled: CompiledWorkflow, correlation_id: str, arguments: Mapping[str, Any]) -> None:
    for executor in compiled.workflow.executors.values():
        if isinstance(executor, ApprovalGate):
            executor.stage_arguments(correlation_id, arguments)
            return
    raise WorkflowRunError("Approval state for this run is missing.")


def _finished(correlation_id: str, status: str) -> WorkflowRun:
    return WorkflowRun(
        correlation_id=correlation_id,
        output=WorkflowEnvelope(
            correlation_id=correlation_id,
            source="approval-gate",
            body={"status": status},
        ),
    )


def _public_status(status: EffectStatus) -> str:
    if status == "rejected":
        return "rejected"
    if status == "completed":
        return "completed"
    return "already_attempted"


def _copy(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in arguments.items()}


def _now() -> str:
    return datetime.now(UTC).isoformat()
