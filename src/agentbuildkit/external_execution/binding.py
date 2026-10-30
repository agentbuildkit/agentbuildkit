"""Durable external-execution bindings.

The record is the control plane: provider, ids, scope, status, and hashes.
Provider transcripts are not stored.
"""

import json
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import ValidationError

from agentbuildkit.external_execution.models import (
    ExecutionPolicyRecord,
    ExternalExecutionRecord,
    ExternalExecutionStatus,
    require_policy_lines,
)
from agentbuildkit.state.models import (
    SIGNAL_CHILD_FINISHED,
    SIGNAL_IMPLEMENTATION_COMPLETED,
    SIGNAL_IMPLEMENTATION_FAILED,
    SIGNAL_IMPLEMENTATION_STARTED,
    CheckpointCompatibilityError,
    RegistryRejected,
    RepositoryRecord,
    RunRecord,
    RunStatus,
    SignalName,
    SignalRecord,
    require_argument_hash,
    require_signal_name,
    require_signal_outcome,
    require_token,
)

_TERMINAL_RUNS = frozenset({"completed", "failed"})
_LIVE_STATUSES = frozenset({"starting", "running", "lost_contact"})
_IMPLEMENTATION_SIGNALS = frozenset(
    {
        SIGNAL_IMPLEMENTATION_STARTED,
        SIGNAL_IMPLEMENTATION_COMPLETED,
        SIGNAL_IMPLEMENTATION_FAILED,
    }
)
_TRANSITIONS: dict[ExternalExecutionStatus, frozenset[ExternalExecutionStatus]] = {
    "starting": frozenset({"running", "startup_failed", "lost_contact"}),
    "startup_failed": frozenset({"starting"}),
    "running": frozenset(
        {"running", "completed", "failed", "cancelled", "expired", "lost_contact"}
    ),
    "lost_contact": frozenset(
        {"running", "completed", "failed", "cancelled", "expired", "lost_contact"}
    ),
    "completed": frozenset({"running"}),
    "failed": frozenset({"running"}),
    "cancelled": frozenset({"running"}),
    "expired": frozenset({"running"}),
}


class ExecutionDirectory(Protocol):
    """Registry reads the binding rules reuse."""

    def get_repository(self, scope: str, name: str) -> RepositoryRecord: ...

    def get_run(self, scope: str, run_id: str) -> RunRecord: ...

    def transition_run(self, scope: str, run_id: str, status: RunStatus) -> RunRecord: ...


class ExecutionStorage(Protocol):
    """Identifier storage for one registry backend."""

    def get(self, scope: str, execution_id: str) -> ExternalExecutionRecord | None: ...

    def find_attempt(
        self, scope: str, grant_id: str, attempt_id: str
    ) -> ExternalExecutionRecord | None: ...

    def find_external_id(
        self, scope: str, provider: str, external_id: str
    ) -> ExternalExecutionRecord | None: ...

    def list_for_work_item(
        self, scope: str, work_item_id: str
    ) -> tuple[ExternalExecutionRecord, ...]: ...

    def insert(self, record: ExternalExecutionRecord) -> None: ...

    def update(self, record: ExternalExecutionRecord) -> None: ...

    def get_policy(self, scope: str, repository: str) -> ExecutionPolicyRecord | None: ...

    def insert_policy(self, record: ExecutionPolicyRecord) -> None: ...

    def find_signal(
        self,
        scope: str,
        *,
        name: SignalName,
        target_run_id: str,
        subject_kind: str,
        subject_id: str,
        outcome_status: str,
    ) -> SignalRecord | None: ...

    def get_signal(self, scope: str, signal_id: str) -> SignalRecord | None: ...

    def insert_signal(self, record: SignalRecord) -> None: ...

    def consume_signal(self, scope: str, signal_id: str, consumed_at: str) -> None: ...


class ExecutionBindings:
    """Directory-backed external execution records. Both backends use this."""

    def __init__(self, storage: ExecutionStorage, directory: ExecutionDirectory) -> None:
        self._storage = storage
        self._directory = directory

    def register_policy(
        self,
        scope: str,
        repository: str,
        *,
        constraints: tuple[str, ...] | list[str],
        validation_commands: tuple[str, ...] | list[str],
    ) -> ExecutionPolicyRecord:
        repo = self._directory.get_repository(scope, repository)
        record = ExecutionPolicyRecord(
            scope=scope,
            repository=repo.name,
            constraints=require_policy_lines(constraints, "External execution is not allowed."),
            validation_commands=require_policy_lines(
                validation_commands, "External execution is not allowed."
            ),
        )
        current = self._storage.get_policy(scope, repo.name)
        if current is not None:
            if current != record:
                raise RegistryRejected("External execution is not allowed.")
            return current
        self._storage.insert_policy(record)
        return record

    def get_policy(self, scope: str, repository: str) -> ExecutionPolicyRecord | None:
        repo = self._directory.get_repository(scope, repository)
        return self._storage.get_policy(scope, repo.name)

    def save(self, record: ExternalExecutionRecord) -> ExternalExecutionRecord:
        current = self._storage.get(record.scope, record.execution_id)
        if current is None:
            if record.status != "starting":
                raise RegistryRejected("External execution is not allowed.")
            self._reject_duplicate(record)
            self._storage.insert(record)
            return record
        if (
            current.provider != record.provider
            or current.external_id != record.external_id
            or current.work_item_id != record.work_item_id
            or current.repository != record.repository
            or current.branch != record.branch
            or current.scope != record.scope
        ):
            raise RegistryRejected("External execution is not allowed.")
        allowed = _TRANSITIONS[current.status]
        if record.status not in allowed:
            raise RegistryRejected("External execution is not allowed.")
        other = self._storage.find_attempt(record.scope, record.grant_id, record.attempt_id)
        if other is not None and other.execution_id != record.execution_id:
            raise RegistryRejected("External execution already exists.")
        self._storage.update(record)
        return record

    def get(self, scope: str, execution_id: str) -> ExternalExecutionRecord:
        found = self._storage.get(scope, execution_id)
        if found is None:
            raise RegistryRejected("External execution is unknown.")
        return found

    def get_by_external_id(
        self, scope: str, provider: str, external_id: str
    ) -> ExternalExecutionRecord:
        found = self._storage.find_external_id(scope, provider, external_id)
        if found is None:
            raise RegistryRejected("External execution is unknown.")
        return found

    def find_attempt(
        self, scope: str, grant_id: str, attempt_id: str
    ) -> ExternalExecutionRecord | None:
        return self._storage.find_attempt(scope, grant_id, attempt_id)

    def list_for_work_item(
        self, scope: str, work_item_id: str
    ) -> tuple[ExternalExecutionRecord, ...]:
        return self._storage.list_for_work_item(scope, work_item_id)

    def publish_signal(
        self,
        scope: str,
        *,
        name: str,
        target_run_id: str,
        correlation_id: str,
        outcome_status: str,
        subject_id: str,
    ) -> SignalRecord:
        signal_name = require_signal_name(name)
        if signal_name not in _IMPLEMENTATION_SIGNALS:
            raise RegistryRejected("External execution signal is not allowed.")
        run = self._directory.get_run(scope, target_run_id)
        outcome = require_signal_outcome(
            signal_name,
            outcome_status,
            subject_kind="external_execution",
            subject_id=subject_id,
        )
        correlation = require_token(correlation_id, "Directory name is not allowed.")
        existing = self._storage.find_signal(
            scope,
            name=signal_name,
            target_run_id=run.run_id,
            subject_kind="external_execution",
            subject_id=subject_id,
            outcome_status=outcome,
        )
        if existing is not None:
            return existing
        record = SignalRecord(
            scope=scope,
            signal_id=_identifier(None),
            name=signal_name,
            target_run_id=run.run_id,
            source_run_id=run.run_id,
            correlation_id=correlation,
            outcome_status=outcome,
            created_at=_now(),
            subject_kind="external_execution",
            subject_id=subject_id,
        )
        self._storage.insert_signal(record)
        return record

    def take_signal(self, scope: str, run_id: str, signal_id: str) -> SignalRecord:
        run = self._directory.get_run(scope, run_id)
        signal = self._storage.get_signal(scope, signal_id)
        if (
            signal is None
            or signal.target_run_id != run.run_id
            or signal.name not in _IMPLEMENTATION_SIGNALS
        ):
            raise RegistryRejected("External execution signal is unknown.")
        if signal.consumed_at is None:
            consumed_at = _now()
            self._storage.consume_signal(scope, signal.signal_id, consumed_at)
            if run.status == "paused":
                self._directory.transition_run(scope, run.run_id, "running")
            elif run.status in _TERMINAL_RUNS:
                raise RegistryRejected("Run transition is not allowed.")
            signal = signal.model_copy(update={"consumed_at": consumed_at})
        return self._storage.get_signal(scope, signal.signal_id) or signal

    def _reject_duplicate(self, record: ExternalExecutionRecord) -> None:
        if self._storage.find_attempt(record.scope, record.grant_id, record.attempt_id) is not None:
            raise RegistryRejected("External execution already exists.")
        if (
            self._storage.find_external_id(record.scope, record.provider, record.external_id)
            is not None
        ):
            raise RegistryRejected("External execution already exists.")
        for existing in self._storage.list_for_work_item(record.scope, record.work_item_id):
            if existing.status in _LIVE_STATUSES:
                raise RegistryRejected("External execution already exists.")


class ExecutionRegistryMethods:
    """Registry methods shared by the memory and SQLite backends."""

    _executions: ExecutionBindings

    def register_execution_policy(
        self,
        scope: str,
        repository: str,
        *,
        constraints: tuple[str, ...] | list[str],
        validation_commands: tuple[str, ...] | list[str],
    ) -> ExecutionPolicyRecord:
        return self._executions.register_policy(
            scope,
            repository,
            constraints=constraints,
            validation_commands=validation_commands,
        )

    def get_execution_policy(self, scope: str, repository: str) -> ExecutionPolicyRecord | None:
        return self._executions.get_policy(scope, repository)

    def save_external_execution(self, record: ExternalExecutionRecord) -> ExternalExecutionRecord:
        return self._executions.save(record)

    def get_external_execution(self, scope: str, execution_id: str) -> ExternalExecutionRecord:
        return self._executions.get(scope, execution_id)

    def get_external_execution_by_external_id(
        self, scope: str, provider: str, external_id: str
    ) -> ExternalExecutionRecord:
        return self._executions.get_by_external_id(scope, provider, external_id)

    def find_external_execution_attempt(
        self, scope: str, grant_id: str, attempt_id: str
    ) -> ExternalExecutionRecord | None:
        return self._executions.find_attempt(scope, grant_id, attempt_id)

    def list_external_executions(
        self, scope: str, work_item_id: str
    ) -> tuple[ExternalExecutionRecord, ...]:
        return self._executions.list_for_work_item(scope, work_item_id)

    def publish_execution_signal(
        self,
        scope: str,
        *,
        name: str,
        target_run_id: str,
        correlation_id: str,
        outcome_status: str,
        subject_id: str,
    ) -> SignalRecord:
        return self._executions.publish_signal(
            scope,
            name=name,
            target_run_id=target_run_id,
            correlation_id=correlation_id,
            outcome_status=outcome_status,
            subject_id=subject_id,
        )

    def take_execution_signal(self, scope: str, run_id: str, signal_id: str) -> SignalRecord:
        return self._executions.take_signal(scope, run_id, signal_id)

    def release_unstarted_grant_use(
        self,
        scope: str,
        grant_id: str,
        tool_name: str,
        argument_hash: str,
        *,
        attempt_id: str,
    ) -> None:
        execution = self._executions.find_attempt(scope, grant_id, attempt_id)
        digest = require_argument_hash(argument_hash)
        if execution is None or execution.status != "startup_failed":
            raise RegistryRejected("External execution is not allowed.")
        if execution.argument_hash != digest:
            raise RegistryRejected("Grant does not allow this tool.")
        self._delete_failed_grant_use(scope, grant_id, tool_name, digest)

    def _delete_failed_grant_use(
        self, scope: str, grant_id: str, tool_name: str, argument_hash: str
    ) -> None:
        raise RegistryRejected("Grant is not active.")


class MemoryExecutionStorage:
    """In-process external execution records. Signals use the registry signal map."""

    def __init__(self, signals: dict[tuple[str, str], SignalRecord]) -> None:
        self.executions: dict[tuple[str, str], ExternalExecutionRecord] = {}
        self.policies: dict[tuple[str, str], ExecutionPolicyRecord] = {}
        self.signals = signals

    def get(self, scope: str, execution_id: str) -> ExternalExecutionRecord | None:
        return self.executions.get((scope, execution_id))

    def find_attempt(
        self, scope: str, grant_id: str, attempt_id: str
    ) -> ExternalExecutionRecord | None:
        for key, record in self.executions.items():
            if key[0] == scope and record.grant_id == grant_id and record.attempt_id == attempt_id:
                return record
        return None

    def find_external_id(
        self, scope: str, provider: str, external_id: str
    ) -> ExternalExecutionRecord | None:
        for key, record in self.executions.items():
            if (
                key[0] == scope
                and record.provider == provider
                and record.external_id == external_id
            ):
                return record
        return None

    def list_for_work_item(
        self, scope: str, work_item_id: str
    ) -> tuple[ExternalExecutionRecord, ...]:
        rows = [
            record
            for key, record in self.executions.items()
            if key[0] == scope and record.work_item_id == work_item_id
        ]
        return tuple(sorted(rows, key=lambda record: record.created_at))

    def insert(self, record: ExternalExecutionRecord) -> None:
        self.executions[(record.scope, record.execution_id)] = record

    def update(self, record: ExternalExecutionRecord) -> None:
        self.executions[(record.scope, record.execution_id)] = record

    def get_policy(self, scope: str, repository: str) -> ExecutionPolicyRecord | None:
        return self.policies.get((scope, repository))

    def insert_policy(self, record: ExecutionPolicyRecord) -> None:
        self.policies[(record.scope, record.repository)] = record

    def find_signal(
        self,
        scope: str,
        *,
        name: SignalName,
        target_run_id: str,
        subject_kind: str,
        subject_id: str,
        outcome_status: str,
    ) -> SignalRecord | None:
        for record in self.signals.values():
            if (
                record.scope == scope
                and record.name == name
                and record.target_run_id == target_run_id
                and record.subject_kind == subject_kind
                and record.subject_id == subject_id
                and record.outcome_status == outcome_status
            ):
                return record
        return None

    def get_signal(self, scope: str, signal_id: str) -> SignalRecord | None:
        return self.signals.get((scope, signal_id))

    def insert_signal(self, record: SignalRecord) -> None:
        if record.name == SIGNAL_CHILD_FINISHED:
            raise RegistryRejected("External execution signal is not allowed.")
        self.signals[(record.scope, record.signal_id)] = record

    def consume_signal(self, scope: str, signal_id: str, consumed_at: str) -> None:
        current = self.signals.get((scope, signal_id))
        if current is None:
            raise RegistryRejected("External execution signal is unknown.")
        self.signals[(scope, signal_id)] = current.model_copy(update={"consumed_at": consumed_at})


class SqliteExecutionStorage:
    """External execution tables on the workflow SQLite connection."""

    def __init__(self, connection: sqlite3.Connection, commit: Callable[[], None]) -> None:
        self._connection = connection
        self._commit = commit

    def get(self, scope: str, execution_id: str) -> ExternalExecutionRecord | None:
        row = self._one(
            "SELECT * FROM external_executions WHERE scope = ? AND execution_id = ?",
            (scope, execution_id),
        )
        return None if row is None else _execution_from_row(row)

    def find_attempt(
        self, scope: str, grant_id: str, attempt_id: str
    ) -> ExternalExecutionRecord | None:
        row = self._one(
            """
            SELECT * FROM external_executions
            WHERE scope = ? AND grant_id = ? AND attempt_id = ?
            """,
            (scope, grant_id, attempt_id),
        )
        return None if row is None else _execution_from_row(row)

    def find_external_id(
        self, scope: str, provider: str, external_id: str
    ) -> ExternalExecutionRecord | None:
        row = self._one(
            """
            SELECT * FROM external_executions
            WHERE scope = ? AND provider = ? AND external_id = ?
            """,
            (scope, provider, external_id),
        )
        return None if row is None else _execution_from_row(row)

    def list_for_work_item(
        self, scope: str, work_item_id: str
    ) -> tuple[ExternalExecutionRecord, ...]:
        rows = self._fetch(
            """
            SELECT * FROM external_executions
            WHERE scope = ? AND work_item_id = ?
            ORDER BY created_at
            """,
            (scope, work_item_id),
        )
        return tuple(_execution_from_row(row) for row in rows)

    def insert(self, record: ExternalExecutionRecord) -> None:
        self._execute(
            """
            INSERT INTO external_executions (
                scope, execution_id, provider, external_id, provider_run_id, organisation,
                work_item_id, run_id, repository, branch, grant_id, attempt_id, status,
                argument_hash, assignment_hash, observation_hash, created_at, updated_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _execution_values(record),
        )

    def update(self, record: ExternalExecutionRecord) -> None:
        self._execute(
            """
            UPDATE external_executions
            SET provider = ?, external_id = ?, provider_run_id = ?, organisation = ?,
                work_item_id = ?, run_id = ?, repository = ?, branch = ?, grant_id = ?,
                attempt_id = ?, status = ?, argument_hash = ?, assignment_hash = ?,
                observation_hash = ?, updated_at = ?, completed_at = ?
            WHERE scope = ? AND execution_id = ?
            """,
            (
                record.provider,
                record.external_id,
                record.provider_run_id,
                record.organisation,
                record.work_item_id,
                record.run_id,
                record.repository,
                record.branch,
                record.grant_id,
                record.attempt_id,
                record.status,
                record.argument_hash,
                record.assignment_hash,
                record.observation_hash,
                record.updated_at,
                record.completed_at,
                record.scope,
                record.execution_id,
            ),
        )

    def get_policy(self, scope: str, repository: str) -> ExecutionPolicyRecord | None:
        row = self._one(
            "SELECT * FROM execution_policies WHERE scope = ? AND repository = ?",
            (scope, repository),
        )
        return None if row is None else _policy_from_row(row)

    def insert_policy(self, record: ExecutionPolicyRecord) -> None:
        self._execute(
            """
            INSERT INTO execution_policies (
                scope, repository, constraints_json, validation_commands_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                record.scope,
                record.repository,
                json.dumps(list(record.constraints)),
                json.dumps(list(record.validation_commands)),
            ),
        )

    def find_signal(
        self,
        scope: str,
        *,
        name: SignalName,
        target_run_id: str,
        subject_kind: str,
        subject_id: str,
        outcome_status: str,
    ) -> SignalRecord | None:
        from agentbuildkit.state.github import signal_from_row

        row = self._one(
            """
            SELECT * FROM signals
            WHERE scope = ? AND name = ? AND target_run_id = ? AND subject_kind = ?
                AND subject_id = ? AND outcome_status = ?
            """,
            (scope, name, target_run_id, subject_kind, subject_id, outcome_status),
        )
        return None if row is None else signal_from_row(row)

    def get_signal(self, scope: str, signal_id: str) -> SignalRecord | None:
        from agentbuildkit.state.github import signal_from_row

        row = self._one(
            "SELECT * FROM signals WHERE scope = ? AND signal_id = ?",
            (scope, signal_id),
        )
        return None if row is None else signal_from_row(row)

    def insert_signal(self, record: SignalRecord) -> None:
        self._execute(
            """
            INSERT INTO signals (
                scope, signal_id, name, target_run_id, source_run_id, correlation_id,
                outcome_status, created_at, consumed_at, subject_kind, subject_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.scope,
                record.signal_id,
                record.name,
                record.target_run_id,
                record.source_run_id,
                record.correlation_id,
                record.outcome_status,
                record.created_at,
                record.consumed_at,
                record.subject_kind,
                record.subject_id,
            ),
        )

    def consume_signal(self, scope: str, signal_id: str, consumed_at: str) -> None:
        self._execute(
            "UPDATE signals SET consumed_at = ? WHERE scope = ? AND signal_id = ?",
            (consumed_at, scope, signal_id),
        )

    def _execute(self, sql: str, params: tuple[object, ...]) -> None:
        self._connection.execute(sql, params)
        self._commit()

    def _one(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        row = self._connection.execute(sql, params).fetchone()
        if row is None:
            return None
        return cast(sqlite3.Row, row)

    def _fetch(self, sql: str, params: tuple[object, ...]) -> list[sqlite3.Row]:
        return [cast(sqlite3.Row, row) for row in self._connection.execute(sql, params).fetchall()]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: str | None) -> str:
    chosen = value if value is not None else uuid.uuid4().hex
    return require_token(chosen, "Directory name is not allowed.")


def _execution_values(record: ExternalExecutionRecord) -> tuple[object, ...]:
    return (
        record.scope,
        record.execution_id,
        record.provider,
        record.external_id,
        record.provider_run_id,
        record.organisation,
        record.work_item_id,
        record.run_id,
        record.repository,
        record.branch,
        record.grant_id,
        record.attempt_id,
        record.status,
        record.argument_hash,
        record.assignment_hash,
        record.observation_hash,
        record.created_at,
        record.updated_at,
        record.completed_at,
    )


def _execution_from_row(row: sqlite3.Row) -> ExternalExecutionRecord:
    try:
        return ExternalExecutionRecord(
            scope=str(row["scope"]),
            execution_id=str(row["execution_id"]),
            provider=str(row["provider"]),
            external_id=str(row["external_id"]),
            provider_run_id=None if row["provider_run_id"] is None else str(row["provider_run_id"]),
            organisation=str(row["organisation"]),
            work_item_id=str(row["work_item_id"]),
            run_id=str(row["run_id"]),
            repository=str(row["repository"]),
            branch=str(row["branch"]),
            grant_id=str(row["grant_id"]),
            attempt_id=str(row["attempt_id"]),
            status=_status(str(row["status"])),
            argument_hash=require_argument_hash(str(row["argument_hash"])),
            assignment_hash=require_argument_hash(str(row["assignment_hash"])),
            observation_hash=(
                None
                if row["observation_hash"] is None
                else require_argument_hash(str(row["observation_hash"]))
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
        )
    except (ValidationError, RegistryRejected):
        raise CheckpointCompatibilityError() from None


def _policy_from_row(row: sqlite3.Row) -> ExecutionPolicyRecord:
    try:
        parsed_constraints = json.loads(str(row["constraints_json"]))
        parsed_commands = json.loads(str(row["validation_commands_json"]))
        if not isinstance(parsed_constraints, list) or not isinstance(parsed_commands, list):
            raise RegistryRejected("External execution is not allowed.")
        return ExecutionPolicyRecord(
            scope=str(row["scope"]),
            repository=str(row["repository"]),
            constraints=require_policy_lines(
                cast(Sequence[object], parsed_constraints),
                "External execution is not allowed.",
            ),
            validation_commands=require_policy_lines(
                cast(Sequence[object], parsed_commands),
                "External execution is not allowed.",
            ),
        )
    except (json.JSONDecodeError, TypeError, ValidationError, RegistryRejected):
        raise CheckpointCompatibilityError() from None


def _status(value: str) -> ExternalExecutionStatus:
    if value == "starting":
        return "starting"
    if value == "startup_failed":
        return "startup_failed"
    if value == "running":
        return "running"
    if value == "lost_contact":
        return "lost_contact"
    if value == "completed":
        return "completed"
    if value == "failed":
        return "failed"
    if value == "cancelled":
        return "cancelled"
    if value == "expired":
        return "expired"
    raise RegistryRejected("External execution is not allowed.")
