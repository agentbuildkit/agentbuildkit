"""Local recovery for dashboard approvals.

The workflow store keeps hashes and approval status. This file keeps the
customer reference, action arguments, and shown outcomes so a new process can
resume the same decision. It is not an audit ledger and it is not exported.
"""

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from customer_service.models import FinalOutcome, OutcomeStatus

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
    ) -> None: ...

    def pause_for(self, request_id: str) -> tuple[str, dict[str, str]] | None: ...

    def save_outcome(self, outcome: FinalOutcome) -> None: ...

    def outcomes(self) -> tuple[FinalOutcome, ...]: ...

    def close(self) -> None: ...


class MemoryActionRecovery:
    """Process-local recovery used when no state directory is configured."""

    def __init__(self) -> None:
        self._pauses: dict[str, tuple[str, dict[str, str]]] = {}
        self._outcomes: list[FinalOutcome] = []

    def save_pause(
        self,
        request_id: str,
        reference: str,
        arguments: Mapping[str, str],
    ) -> None:
        self._pauses[request_id] = (reference, dict(arguments))

    def pause_for(self, request_id: str) -> tuple[str, dict[str, str]] | None:
        found = self._pauses.get(request_id)
        if found is None:
            return None
        return found[0], dict(found[1])

    def save_outcome(self, outcome: FinalOutcome) -> None:
        self._outcomes.append(outcome)

    def outcomes(self) -> tuple[FinalOutcome, ...]:
        return tuple(self._outcomes)

    def close(self) -> None:
        """Memory recovery has no database connection."""


class SqliteActionRecovery:
    """Recovery beside the workflow database. Argument values stay out of the ledger."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pauses (
                request_id TEXT PRIMARY KEY,
                reference TEXT NOT NULL,
                arguments_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outcomes (
                outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT NOT NULL,
                status TEXT NOT NULL,
                specialist TEXT NOT NULL,
                response TEXT NOT NULL,
                correlation_id TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    def save_pause(
        self,
        request_id: str,
        reference: str,
        arguments: Mapping[str, str],
    ) -> None:
        payload = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))
        self._connection.execute(
            """
            INSERT INTO pauses (request_id, reference, arguments_json)
            VALUES (?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                reference = excluded.reference,
                arguments_json = excluded.arguments_json
            """,
            (request_id, reference, payload),
        )
        self._connection.commit()

    def pause_for(self, request_id: str) -> tuple[str, dict[str, str]] | None:
        row = self._connection.execute(
            "SELECT reference, arguments_json FROM pauses WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["reference"]), _arguments(str(row["arguments_json"]))

    def save_outcome(self, outcome: FinalOutcome) -> None:
        self._connection.execute(
            """
            INSERT INTO outcomes (reference, status, specialist, response, correlation_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                outcome.reference,
                outcome.status,
                outcome.specialist,
                outcome.response,
                outcome.correlation_id,
            ),
        )
        self._connection.commit()

    def outcomes(self) -> tuple[FinalOutcome, ...]:
        rows = self._connection.execute(
            """
            SELECT reference, status, specialist, response, correlation_id
            FROM outcomes ORDER BY outcome_id
            """
        ).fetchall()
        return tuple(_outcome(row) for row in rows)

    def close(self) -> None:
        """Close the database from the thread that opened it."""
        self._connection.close()


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
