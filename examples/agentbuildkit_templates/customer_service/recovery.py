"""Local recovery for dashboard approvals.

The workflow store keeps hashes and approval status. This file keeps the
customer reference, action arguments, and shown outcomes so a new process can
resume the same decision. It is not an audit ledger and it is not exported.

Pauses and outcomes are stored by team id. Rows written before team ids existed
are kept and marked as customer-service. Opening an older file copies it to a
backup first. The database is not deleted.
"""

import json
import shutil
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from agentbuildkit_templates.customer_service.connections import LEGACY_TEAM_ID
from agentbuildkit_templates.customer_service.models import FinalOutcome, OutcomeStatus

_STATUSES: frozenset[str] = frozenset(
    {"completed", "rejected", "pending_approval", "escalated", "route_rejected"}
)


class ActionRecovery(Protocol):
    """Durable pause arguments and outcomes for one dashboard state directory."""

    def save_pause(
        self,
        request_id: str,
        reference: str,
        arguments: Mapping[str, str],
        *,
        team_id: str,
    ) -> None: ...

    def pause_for(self, team_id: str, request_id: str) -> tuple[str, dict[str, str]] | None: ...

    def save_outcome(self, outcome: FinalOutcome, *, team_id: str) -> None: ...

    def outcomes(self, team_id: str) -> tuple[FinalOutcome, ...]: ...

    def close(self) -> None: ...


class MemoryActionRecovery:
    """Process-local recovery used when no state directory is configured."""

    def __init__(self) -> None:
        self._pauses: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
        self._outcomes: list[tuple[str, FinalOutcome]] = []

    def save_pause(
        self,
        request_id: str,
        reference: str,
        arguments: Mapping[str, str],
        *,
        team_id: str,
    ) -> None:
        self._pauses[(team_id, request_id)] = (reference, dict(arguments))

    def pause_for(self, team_id: str, request_id: str) -> tuple[str, dict[str, str]] | None:
        found = self._pauses.get((team_id, request_id))
        if found is None:
            return None
        return found[0], dict(found[1])

    def save_outcome(self, outcome: FinalOutcome, *, team_id: str) -> None:
        self._outcomes.append((team_id, outcome))

    def outcomes(self, team_id: str) -> tuple[FinalOutcome, ...]:
        return tuple(item for owner, item in self._outcomes if owner == team_id)

    def close(self) -> None:
        """Memory recovery has no database connection."""


class SqliteActionRecovery:
    """Recovery beside the workflow database. Argument values stay out of the ledger."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._prepare()
        self._connection.commit()

    def _prepare(self) -> None:
        if _recovery_table(self._connection, "pauses") and not _recovery_column(
            self._connection, "pauses", "team_id"
        ):
            self._migrate()
            return
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pauses (
                team_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                reference TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                PRIMARY KEY (team_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS outcomes (
                outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL,
                reference TEXT NOT NULL,
                status TEXT NOT NULL,
                specialist TEXT NOT NULL,
                response TEXT NOT NULL,
                correlation_id TEXT NOT NULL
            );
            """
        )

    def _migrate(self) -> None:
        """Back up the file, then keep every existing row under customer-service."""
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.commit()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(self._path, self._path.with_name(f"{self._path.name}.bak-{stamp}"))
        self._connection.execute("BEGIN")
        self._connection.execute(
            """
            CREATE TABLE pauses_next (
                team_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                reference TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                PRIMARY KEY (team_id, request_id)
            )
            """
        )
        self._connection.execute(
            """
            INSERT INTO pauses_next (team_id, request_id, reference, arguments_json)
            SELECT ?, request_id, reference, arguments_json FROM pauses
            """,
            (LEGACY_TEAM_ID,),
        )
        self._connection.execute("DROP TABLE pauses")
        self._connection.execute("ALTER TABLE pauses_next RENAME TO pauses")
        self._connection.execute(
            """
            CREATE TABLE outcomes_next (
                outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL,
                reference TEXT NOT NULL,
                status TEXT NOT NULL,
                specialist TEXT NOT NULL,
                response TEXT NOT NULL,
                correlation_id TEXT NOT NULL
            )
            """
        )
        if _recovery_table(self._connection, "outcomes"):
            self._connection.execute(
                """
                INSERT INTO outcomes_next (
                    team_id, reference, status, specialist, response, correlation_id
                )
                SELECT ?, reference, status, specialist, response, correlation_id
                FROM outcomes ORDER BY outcome_id
                """,
                (LEGACY_TEAM_ID,),
            )
            self._connection.execute("DROP TABLE outcomes")
        self._connection.execute("ALTER TABLE outcomes_next RENAME TO outcomes")
        self._connection.commit()

    def save_pause(
        self,
        request_id: str,
        reference: str,
        arguments: Mapping[str, str],
        *,
        team_id: str,
    ) -> None:
        payload = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))
        self._connection.execute(
            """
            INSERT INTO pauses (team_id, request_id, reference, arguments_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id, request_id) DO UPDATE SET
                reference = excluded.reference,
                arguments_json = excluded.arguments_json
            """,
            (team_id, request_id, reference, payload),
        )
        self._connection.commit()

    def pause_for(self, team_id: str, request_id: str) -> tuple[str, dict[str, str]] | None:
        row = self._connection.execute(
            """
            SELECT reference, arguments_json FROM pauses
            WHERE team_id = ? AND request_id = ?
            """,
            (team_id, request_id),
        ).fetchone()
        if row is None:
            return None
        return str(row["reference"]), _arguments(str(row["arguments_json"]))

    def save_outcome(self, outcome: FinalOutcome, *, team_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO outcomes (
                team_id, reference, status, specialist, response, correlation_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                team_id,
                outcome.reference,
                outcome.status,
                outcome.specialist,
                outcome.response,
                outcome.correlation_id,
            ),
        )
        self._connection.commit()

    def outcomes(self, team_id: str) -> tuple[FinalOutcome, ...]:
        rows = self._connection.execute(
            """
            SELECT reference, status, specialist, response, correlation_id
            FROM outcomes WHERE team_id = ? ORDER BY outcome_id
            """,
            (team_id,),
        ).fetchall()
        return tuple(_outcome(row) for row in rows)

    def close(self) -> None:
        """Close the database from the thread that opened it."""
        self._connection.close()


def _recovery_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _recovery_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    found = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    return column in found


def _arguments(payload: str) -> dict[str, str]:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("Recovered action arguments are unreadable.")
    arguments: dict[str, str] = {}
    for key, value in cast(dict[object, object], parsed).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Recovered action arguments are unreadable.")
        arguments[key] = value
    return arguments


def _outcome(row: sqlite3.Row) -> FinalOutcome:
    status = str(row["status"])
    if status not in _STATUSES:
        raise ValueError("Recovered outcome is unreadable.")
    return FinalOutcome(
        reference=str(row["reference"]),
        status=_status(status),
        specialist=str(row["specialist"]),
        response=str(row["response"]),
        correlation_id=str(row["correlation_id"]),
    )


def _status(value: str) -> OutcomeStatus:
    if value == "completed":
        return "completed"
    if value == "rejected":
        return "rejected"
    if value == "pending_approval":
        return "pending_approval"
    if value == "escalated":
        return "escalated"
    return "route_rejected"
