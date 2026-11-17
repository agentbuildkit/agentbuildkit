"""Durable drafts, revisions, and activation history.

This store is separate from workflow, recovery, and connection databases.
Opening a file with an unknown schema copies a backup and leaves that file
unchanged. It does not delete or rebuild an existing database.
"""

import json
import shutil
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

from agentbuildkit.revisions.models import (
    Activation,
    Draft,
    Finding,
    Readiness,
    Revision,
    RunPin,
    TeamConfig,
)
from agentbuildkit.revisions.validate import content_hash

_SCHEMA = "1"


class RevisionStore(Protocol):
    """The persistence a revision service needs. Callers stay on one thread."""

    def transaction(self) -> AbstractContextManager[None]: ...

    def close(self) -> None: ...

    def revisions(self, team_id: str) -> tuple[Revision, ...]: ...

    def revision(self, revision_id: str) -> Revision | None: ...

    def add_revision(self, revision: Revision) -> None: ...

    def active_id(self, team_id: str) -> str | None: ...

    def set_active(self, team_id: str, revision_id: str) -> None: ...

    def draft_for_team(self, team_id: str) -> Draft | None: ...

    def draft(self, draft_id: str) -> Draft | None: ...

    def save_draft(self, draft: Draft) -> None: ...

    def delete_draft(self, draft_id: str) -> None: ...

    def readiness(self, draft_id: str) -> Readiness | None: ...

    def save_readiness(self, item: Readiness) -> None: ...

    def clear_readiness(self, draft_id: str) -> None: ...

    def add_activation(self, item: Activation) -> None: ...

    def activations(self, team_id: str) -> tuple[Activation, ...]: ...

    def pin(self, item: RunPin) -> None: ...

    def pin_for_request(self, team_id: str, request_id: str) -> RunPin | None: ...

    def pin_for_correlation(self, team_id: str, correlation_id: str) -> RunPin | None: ...


class MemoryRevisionStore:
    """Process-local revisions for tests and a dashboard without a state directory."""

    def __init__(self) -> None:
        self._revisions: dict[str, Revision] = {}
        self._active: dict[str, str] = {}
        self._drafts: dict[str, Draft] = {}
        self._readiness: dict[str, Readiness] = {}
        self._activations: list[Activation] = []
        self._pins: list[RunPin] = []

    @contextmanager
    def transaction(self) -> Generator[None]:
        """Undo this store's writes when the block raises."""
        snapshot = (
            dict(self._revisions),
            dict(self._active),
            dict(self._drafts),
            dict(self._readiness),
            list(self._activations),
            list(self._pins),
        )
        try:
            yield
        except Exception:
            (
                self._revisions,
                self._active,
                self._drafts,
                self._readiness,
                self._activations,
                self._pins,
            ) = snapshot
            raise

    def close(self) -> None:
        """Memory storage has no connection."""

    def revisions(self, team_id: str) -> tuple[Revision, ...]:
        rows = [item for item in self._revisions.values() if item.team_id == team_id]
        return tuple(sorted(rows, key=lambda item: item.number))

    def revision(self, revision_id: str) -> Revision | None:
        return self._revisions.get(revision_id)

    def add_revision(self, revision: Revision) -> None:
        self._revisions[revision.revision_id] = revision

    def active_id(self, team_id: str) -> str | None:
        return self._active.get(team_id)

    def set_active(self, team_id: str, revision_id: str) -> None:
        self._active[team_id] = revision_id

    def draft_for_team(self, team_id: str) -> Draft | None:
        for item in self._drafts.values():
            if item.team_id == team_id:
                return item
        return None

    def draft(self, draft_id: str) -> Draft | None:
        return self._drafts.get(draft_id)

    def save_draft(self, draft: Draft) -> None:
        self._drafts[draft.draft_id] = draft

    def delete_draft(self, draft_id: str) -> None:
        self._drafts.pop(draft_id, None)
        self._readiness.pop(draft_id, None)

    def readiness(self, draft_id: str) -> Readiness | None:
        return self._readiness.get(draft_id)

    def save_readiness(self, item: Readiness) -> None:
        self._readiness[item.draft_id] = item

    def clear_readiness(self, draft_id: str) -> None:
        self._readiness.pop(draft_id, None)

    def add_activation(self, item: Activation) -> None:
        self._activations.append(item)

    def activations(self, team_id: str) -> tuple[Activation, ...]:
        return tuple(item for item in self._activations if item.team_id == team_id)

    def pin(self, item: RunPin) -> None:
        self._pins = [
            current
            for current in self._pins
            if current.team_id != item.team_id or current.correlation_id != item.correlation_id
        ]
        self._pins.append(item)

    def pin_for_request(self, team_id: str, request_id: str) -> RunPin | None:
        if not request_id:
            return None
        for item in reversed(self._pins):
            if item.team_id == team_id and item.request_id == request_id:
                return item
        return None

    def pin_for_correlation(self, team_id: str, correlation_id: str) -> RunPin | None:
        for item in reversed(self._pins):
            if item.team_id == team_id and item.correlation_id == correlation_id:
                return item
        return None


class SqliteRevisionStore:
    """SQLite revisions. The connection stays on the thread that opened it."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._in_transaction = False
        self._prepare()

    @contextmanager
    def transaction(self) -> Generator[None]:
        """Commit the block once. A failure rolls the file back to the previous commit."""
        if self._in_transaction:
            yield
            return
        self._in_transaction = True
        try:
            yield
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        finally:
            self._in_transaction = False

    def _commit(self) -> None:
        if not self._in_transaction:
            self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def revisions(self, team_id: str) -> tuple[Revision, ...]:
        rows = self._connection.execute(
            "SELECT * FROM revisions WHERE team_id = ? ORDER BY number",
            (team_id,),
        ).fetchall()
        return tuple(_revision(row) for row in rows)

    def revision(self, revision_id: str) -> Revision | None:
        row = self._connection.execute(
            "SELECT * FROM revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        return _revision(row)

    def add_revision(self, revision: Revision) -> None:
        self._connection.execute(
            """
            INSERT INTO revisions (
                revision_id, team_id, template_id, number, content_hash, config_json,
                suite_version, evaluation_passed, evaluation_note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision.revision_id,
                revision.team_id,
                revision.template_id,
                revision.number,
                revision.content_hash,
                revision.config.model_dump_json(),
                revision.suite_version,
                int(revision.evaluation_passed),
                revision.evaluation_note,
                revision.created_at,
            ),
        )
        self._commit()

    def active_id(self, team_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT revision_id FROM active_revision WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["revision_id"])

    def set_active(self, team_id: str, revision_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO active_revision (team_id, revision_id) VALUES (?, ?)
            ON CONFLICT(team_id) DO UPDATE SET revision_id = excluded.revision_id
            """,
            (team_id, revision_id),
        )
        self._commit()

    def draft_for_team(self, team_id: str) -> Draft | None:
        row = self._connection.execute(
            "SELECT * FROM drafts WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        if row is None:
            return None
        return _draft(row)

    def draft(self, draft_id: str) -> Draft | None:
        row = self._connection.execute(
            "SELECT * FROM drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            return None
        return _draft(row)

    def save_draft(self, draft: Draft) -> None:
        self._connection.execute(
            """
            INSERT INTO drafts (
                draft_id, team_id, template_id, version, config_json, content_hash, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(draft_id) DO UPDATE SET
                version = excluded.version,
                config_json = excluded.config_json,
                content_hash = excluded.content_hash,
                updated_at = excluded.updated_at
            """,
            (
                draft.draft_id,
                draft.team_id,
                draft.template_id,
                draft.version,
                draft.config.model_dump_json(),
                draft.content_hash,
                draft.updated_at,
            ),
        )
        self._commit()

    def delete_draft(self, draft_id: str) -> None:
        self._connection.execute("DELETE FROM drafts WHERE draft_id = ?", (draft_id,))
        self._connection.execute("DELETE FROM readiness WHERE draft_id = ?", (draft_id,))
        self._commit()

    def readiness(self, draft_id: str) -> Readiness | None:
        row = self._connection.execute(
            "SELECT * FROM readiness WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            return None
        return _readiness(row)

    def save_readiness(self, item: Readiness) -> None:
        evaluation = None if item.evaluation_ok is None else int(item.evaluation_ok)
        self._connection.execute(
            """
            INSERT INTO readiness (
                draft_id, version, content_hash, validation_ok, findings_json,
                evaluation_ok, suite_version, evaluated_at, passed, failed, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(draft_id) DO UPDATE SET
                version = excluded.version,
                content_hash = excluded.content_hash,
                validation_ok = excluded.validation_ok,
                findings_json = excluded.findings_json,
                evaluation_ok = excluded.evaluation_ok,
                suite_version = excluded.suite_version,
                evaluated_at = excluded.evaluated_at,
                passed = excluded.passed,
                failed = excluded.failed,
                note = excluded.note
            """,
            (
                item.draft_id,
                item.version,
                item.content_hash,
                int(item.validation_ok),
                json.dumps([finding.model_dump(mode="json") for finding in item.findings]),
                evaluation,
                item.suite_version,
                item.evaluated_at,
                item.passed,
                item.failed,
                item.note,
            ),
        )
        self._commit()

    def clear_readiness(self, draft_id: str) -> None:
        self._connection.execute("DELETE FROM readiness WHERE draft_id = ?", (draft_id,))
        self._commit()

    def add_activation(self, item: Activation) -> None:
        self._connection.execute(
            """
            INSERT INTO activations (
                activation_id, team_id, revision_id, previous_revision_id,
                content_hash, kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.activation_id,
                item.team_id,
                item.revision_id,
                item.previous_revision_id,
                item.content_hash,
                item.kind,
                item.created_at,
            ),
        )
        self._commit()

    def activations(self, team_id: str) -> tuple[Activation, ...]:
        rows = self._connection.execute(
            "SELECT * FROM activations WHERE team_id = ? ORDER BY created_at, activation_id",
            (team_id,),
        ).fetchall()
        return tuple(_activation(row) for row in rows)

    def pin(self, item: RunPin) -> None:
        self._connection.execute(
            """
            INSERT INTO run_pins (team_id, correlation_id, request_id, revision_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id, correlation_id) DO UPDATE SET
                request_id = excluded.request_id,
                revision_id = excluded.revision_id
            """,
            (item.team_id, item.correlation_id, item.request_id, item.revision_id),
        )
        self._commit()

    def pin_for_request(self, team_id: str, request_id: str) -> RunPin | None:
        if not request_id:
            return None
        row = self._connection.execute(
            """
            SELECT * FROM run_pins WHERE team_id = ? AND request_id = ?
            ORDER BY correlation_id DESC LIMIT 1
            """,
            (team_id, request_id),
        ).fetchone()
        if row is None:
            return None
        return _pin(row)

    def pin_for_correlation(self, team_id: str, correlation_id: str) -> RunPin | None:
        row = self._connection.execute(
            "SELECT * FROM run_pins WHERE team_id = ? AND correlation_id = ?",
            (team_id, correlation_id),
        ).fetchone()
        if row is None:
            return None
        return _pin(row)

    def _prepare(self) -> None:
        tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = ""
        if "meta" in tables:
            row = self._connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            version = "" if row is None else str(row["value"])
        user_tables = tables - {"meta"}
        if user_tables and version != _SCHEMA:
            self._connection.close()
            _backup(self._path)
            raise RuntimeError(
                "The revision database is not version 1. "
                "A backup was written and the file was not changed."
            )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                revision_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                template_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                config_json TEXT NOT NULL,
                suite_version TEXT NOT NULL,
                evaluation_passed INTEGER NOT NULL,
                evaluation_note TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (team_id, number)
            );
            CREATE TABLE IF NOT EXISTS active_revision (
                team_id TEXT PRIMARY KEY,
                revision_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL UNIQUE,
                template_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS readiness (
                draft_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                validation_ok INTEGER NOT NULL,
                findings_json TEXT NOT NULL,
                evaluation_ok INTEGER,
                suite_version TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                passed INTEGER NOT NULL,
                failed INTEGER NOT NULL,
                note TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activations (
                activation_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                previous_revision_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_pins (
                team_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                PRIMARY KEY (team_id, correlation_id)
            );
            """
        )
        self._connection.execute(
            """
            INSERT INTO meta (key, value) VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_SCHEMA,),
        )
        self._commit()


def _backup(path: Path) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))


def _config(payload: str) -> TeamConfig:
    return TeamConfig.model_validate_json(payload)


def _revision(row: Mapping[str, object]) -> Revision:
    return Revision(
        revision_id=str(row["revision_id"]),
        team_id=str(row["team_id"]),
        template_id=str(row["template_id"]),
        number=int(cast(int, row["number"])),
        content_hash=str(row["content_hash"]),
        config=_config(str(row["config_json"])),
        suite_version=str(row["suite_version"]),
        evaluation_passed=bool(row["evaluation_passed"]),
        evaluation_note=str(row["evaluation_note"]),
        created_at=str(row["created_at"]),
    )


def _draft(row: Mapping[str, object]) -> Draft:
    config = _config(str(row["config_json"]))
    return Draft(
        draft_id=str(row["draft_id"]),
        team_id=str(row["team_id"]),
        template_id=str(row["template_id"]),
        version=int(cast(int, row["version"])),
        config=config,
        content_hash=str(row["content_hash"]) or content_hash(config),
        updated_at=str(row["updated_at"]),
    )


def _readiness(row: Mapping[str, object]) -> Readiness:
    raw = json.loads(str(row["findings_json"]))
    loaded = cast(list[object], raw) if isinstance(raw, list) else []
    findings = tuple(Finding.model_validate(item) for item in loaded)
    evaluation = row["evaluation_ok"]
    return Readiness(
        draft_id=str(row["draft_id"]),
        version=int(cast(int, row["version"])),
        content_hash=str(row["content_hash"]),
        validation_ok=bool(row["validation_ok"]),
        findings=findings,
        evaluation_ok=None if evaluation is None else bool(evaluation),
        suite_version=str(row["suite_version"]),
        evaluated_at=str(row["evaluated_at"]),
        passed=int(cast(int, row["passed"])),
        failed=int(cast(int, row["failed"])),
        note=str(row["note"]),
    )


def _activation(row: Mapping[str, object]) -> Activation:
    kind = str(row["kind"])
    if kind not in {"baseline", "activate", "rollback"}:
        kind = "activate"
    return Activation(
        activation_id=str(row["activation_id"]),
        team_id=str(row["team_id"]),
        revision_id=str(row["revision_id"]),
        previous_revision_id=str(row["previous_revision_id"]),
        content_hash=str(row["content_hash"]),
        kind=cast(Literal["baseline", "activate", "rollback"], kind),
        created_at=str(row["created_at"]),
    )


def _pin(row: Mapping[str, object]) -> RunPin:
    return RunPin(
        team_id=str(row["team_id"]),
        correlation_id=str(row["correlation_id"]),
        request_id=str(row["request_id"]),
        revision_id=str(row["revision_id"]),
    )
