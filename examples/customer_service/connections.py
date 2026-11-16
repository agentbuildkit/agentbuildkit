"""Local Google Sheets connection references.

The file stores account ids, status, and which agent may use which read.
It does not store API keys, OAuth tokens, or spreadsheet cell values.
"""

import hashlib
import hmac
import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

_ACTIVE = "ACTIVE"
_DISCONNECTED = "DISCONNECTED"


@dataclass(frozen=True)
class StoredConnection:
    """One connected account reference for this local dashboard."""

    connection_id: str
    toolkit: str
    connected_account_id: str
    user_id: str
    status: str
    label: str


@dataclass(frozen=True)
class StoredAssignment:
    """One agent's saved spreadsheet window and data mode."""

    agent_name: str
    connection_id: str
    tools: tuple[str, ...]
    spreadsheet_id: str
    cell_range: str
    mode: str


class ConnectionStore(Protocol):
    """Connection references for one local operator."""

    def operator_id(self) -> str: ...

    def remember_link(self, *, state_hash: str, session_hash: str, toolkit: str) -> None: ...

    def take_link(self, *, state_hash: str, session_hash: str) -> bool: ...

    def save_connection(self, connection: StoredConnection) -> None: ...

    def connections(self) -> tuple[StoredConnection, ...]: ...

    def connection(self, connection_id: str) -> StoredConnection | None: ...

    def mark(self, connection_id: str, status: str) -> None: ...

    def disconnect(self, connection_id: str) -> None: ...

    def assign(
        self,
        agent_name: str,
        connection_id: str,
        tools: Sequence[str],
        spreadsheet_id: str,
        cell_range: str,
        mode: str,
    ) -> None: ...

    def unassign(self, agent_name: str) -> None: ...

    def assignment(self, agent_name: str) -> StoredAssignment | None: ...

    def assignments(self) -> tuple[StoredAssignment, ...]: ...

    def data_mode(self, agent_name: str) -> str: ...

    def set_data_mode(self, agent_name: str, mode: str) -> None: ...

    def close(self) -> None: ...


class MemoryConnectionStore:
    """Process-local connections used when no state directory is configured."""

    def __init__(self) -> None:
        self._operator = uuid.uuid4().hex
        self._links: dict[str, tuple[str, str]] = {}
        self._connections: dict[str, StoredConnection] = {}
        self._assignments: dict[str, StoredAssignment] = {}
        self._modes: dict[str, str] = {}

    def operator_id(self) -> str:
        return self._operator

    def remember_link(self, *, state_hash: str, session_hash: str, toolkit: str) -> None:
        self._links[state_hash] = (session_hash, toolkit)

    def take_link(self, *, state_hash: str, session_hash: str) -> bool:
        found = self._links.get(state_hash)
        if found is None or not _same(found[0], session_hash):
            return False
        del self._links[state_hash]
        return True

    def save_connection(self, connection: StoredConnection) -> None:
        self._connections[connection.connection_id] = connection

    def connections(self) -> tuple[StoredConnection, ...]:
        return tuple(self._connections.values())

    def connection(self, connection_id: str) -> StoredConnection | None:
        return self._connections.get(connection_id)

    def mark(self, connection_id: str, status: str) -> None:
        current = self._connections.get(connection_id)
        if current is None:
            return
        self._connections[connection_id] = StoredConnection(
            connection_id=current.connection_id,
            toolkit=current.toolkit,
            connected_account_id=current.connected_account_id,
            user_id=current.user_id,
            status=status,
            label=current.label,
        )

    def disconnect(self, connection_id: str) -> None:
        self.mark(connection_id, _DISCONNECTED)
        self._assignments = {
            name: item
            for name, item in self._assignments.items()
            if item.connection_id != connection_id
        }

    def assign(
        self,
        agent_name: str,
        connection_id: str,
        tools: Sequence[str],
        spreadsheet_id: str,
        cell_range: str,
        mode: str,
    ) -> None:
        self._assignments[agent_name] = StoredAssignment(
            agent_name=agent_name,
            connection_id=connection_id,
            tools=tuple(tools),
            spreadsheet_id=spreadsheet_id,
            cell_range=cell_range,
            mode=mode,
        )

    def unassign(self, agent_name: str) -> None:
        self._assignments.pop(agent_name, None)

    def assignment(self, agent_name: str) -> StoredAssignment | None:
        return self._assignments.get(agent_name)

    def assignments(self) -> tuple[StoredAssignment, ...]:
        return tuple(self._assignments.values())

    def data_mode(self, agent_name: str) -> str:
        return self._modes.get(agent_name, "demo")

    def set_data_mode(self, agent_name: str, mode: str) -> None:
        self._modes[agent_name] = mode

    def close(self) -> None:
        """Memory storage has no database connection."""


class SqliteConnectionStore:
    """Connections beside the workflow database, on the dashboard owner thread."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS links (
                state_hash TEXT PRIMARY KEY,
                session_hash TEXT NOT NULL,
                toolkit TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connections (
                connection_id TEXT PRIMARY KEY,
                toolkit TEXT NOT NULL,
                connected_account_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                label TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assignments (
                agent_name TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL,
                tools_json TEXT NOT NULL,
                spreadsheet_id TEXT NOT NULL DEFAULT '',
                cell_range TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'demo'
            );
            CREATE TABLE IF NOT EXISTS agent_modes (
                agent_name TEXT PRIMARY KEY,
                mode TEXT NOT NULL
            );
            """
        )
        self._ensure_assignment_columns()
        self._connection.commit()

    def operator_id(self) -> str:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = 'operator_id'"
        ).fetchone()
        if row is not None:
            return str(row["value"])
        operator = uuid.uuid4().hex
        self._connection.execute(
            "INSERT INTO meta (key, value) VALUES ('operator_id', ?)",
            (operator,),
        )
        self._connection.commit()
        return operator

    def remember_link(self, *, state_hash: str, session_hash: str, toolkit: str) -> None:
        self._connection.execute(
            """
            INSERT INTO links (state_hash, session_hash, toolkit, status)
            VALUES (?, ?, ?, 'pending')
            """,
            (state_hash, session_hash, toolkit),
        )
        self._connection.commit()

    def take_link(self, *, state_hash: str, session_hash: str) -> bool:
        row = self._connection.execute(
            """
            SELECT session_hash FROM links
            WHERE state_hash = ? AND status = 'pending'
            """,
            (state_hash,),
        ).fetchone()
        if row is None or not _same(str(row["session_hash"]), session_hash):
            return False
        self._connection.execute(
            "UPDATE links SET status = 'completed' WHERE state_hash = ?",
            (state_hash,),
        )
        self._connection.commit()
        return True

    def save_connection(self, connection: StoredConnection) -> None:
        self._connection.execute(
            """
            INSERT INTO connections (
                connection_id, toolkit, connected_account_id, user_id, status, label
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                connection.connection_id,
                connection.toolkit,
                connection.connected_account_id,
                connection.user_id,
                connection.status,
                connection.label,
            ),
        )
        self._connection.commit()

    def connections(self) -> tuple[StoredConnection, ...]:
        rows = self._connection.execute(
            """
            SELECT connection_id, toolkit, connected_account_id, user_id, status, label
            FROM connections ORDER BY connection_id
            """
        ).fetchall()
        return tuple(_connection_row(row) for row in rows)

    def connection(self, connection_id: str) -> StoredConnection | None:
        row = self._connection.execute(
            """
            SELECT connection_id, toolkit, connected_account_id, user_id, status, label
            FROM connections WHERE connection_id = ?
            """,
            (connection_id,),
        ).fetchone()
        if row is None:
            return None
        return _connection_row(row)

    def mark(self, connection_id: str, status: str) -> None:
        self._connection.execute(
            "UPDATE connections SET status = ? WHERE connection_id = ?",
            (status, connection_id),
        )
        self._connection.commit()

    def disconnect(self, connection_id: str) -> None:
        self.mark(connection_id, _DISCONNECTED)
        self._connection.execute(
            "DELETE FROM assignments WHERE connection_id = ?",
            (connection_id,),
        )
        self._connection.commit()

    def assign(
        self,
        agent_name: str,
        connection_id: str,
        tools: Sequence[str],
        spreadsheet_id: str,
        cell_range: str,
        mode: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO assignments (
                agent_name, connection_id, tools_json, spreadsheet_id, cell_range, mode
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_name) DO UPDATE SET
                connection_id = excluded.connection_id,
                tools_json = excluded.tools_json,
                spreadsheet_id = excluded.spreadsheet_id,
                cell_range = excluded.cell_range,
                mode = excluded.mode
            """,
            (agent_name, connection_id, json.dumps(list(tools)), spreadsheet_id, cell_range, mode),
        )
        self._connection.commit()

    def unassign(self, agent_name: str) -> None:
        self._connection.execute("DELETE FROM assignments WHERE agent_name = ?", (agent_name,))
        self._connection.commit()

    def assignment(self, agent_name: str) -> StoredAssignment | None:
        row = self._connection.execute(
            """
            SELECT agent_name, connection_id, tools_json, spreadsheet_id, cell_range, mode
            FROM assignments WHERE agent_name = ?
            """,
            (agent_name,),
        ).fetchone()
        if row is None:
            return None
        return _assignment_row(row)

    def assignments(self) -> tuple[StoredAssignment, ...]:
        rows = self._connection.execute(
            """
            SELECT agent_name, connection_id, tools_json, spreadsheet_id, cell_range, mode
            FROM assignments ORDER BY agent_name
            """
        ).fetchall()
        return tuple(_assignment_row(row) for row in rows)

    def data_mode(self, agent_name: str) -> str:
        row = self._connection.execute(
            "SELECT mode FROM agent_modes WHERE agent_name = ?",
            (agent_name,),
        ).fetchone()
        if row is None:
            return "demo"
        return str(row["mode"])

    def set_data_mode(self, agent_name: str, mode: str) -> None:
        self._connection.execute(
            """
            INSERT INTO agent_modes (agent_name, mode)
            VALUES (?, ?)
            ON CONFLICT(agent_name) DO UPDATE SET mode = excluded.mode
            """,
            (agent_name, mode),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def _ensure_assignment_columns(self) -> None:
        found = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(assignments)")}
        additions = (
            (
                "spreadsheet_id",
                "ALTER TABLE assignments ADD COLUMN spreadsheet_id TEXT NOT NULL DEFAULT ''",
            ),
            (
                "cell_range",
                "ALTER TABLE assignments ADD COLUMN cell_range TEXT NOT NULL DEFAULT ''",
            ),
            (
                "mode",
                "ALTER TABLE assignments ADD COLUMN mode TEXT NOT NULL DEFAULT 'demo'",
            ),
        )
        for name, statement in additions:
            if name not in found:
                self._connection.execute(statement)


def session_hash(token: str) -> str:
    """Hash a browser session token before it is stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _connection_row(row: Mapping[str, object]) -> StoredConnection:
    return StoredConnection(
        connection_id=str(row["connection_id"]),
        toolkit=str(row["toolkit"]),
        connected_account_id=str(row["connected_account_id"]),
        user_id=str(row["user_id"]),
        status=str(row["status"]),
        label=str(row["label"]),
    )


def _assignment_row(row: Mapping[str, object]) -> StoredAssignment:
    parsed = json.loads(str(row["tools_json"]))
    tools: tuple[str, ...] = ()
    if isinstance(parsed, list):
        values = cast(list[object], parsed)
        tools = tuple(item for item in values if isinstance(item, str))
    columns = set(row.keys())
    return StoredAssignment(
        agent_name=str(row["agent_name"]),
        connection_id=str(row["connection_id"]),
        tools=tools,
        spreadsheet_id=str(row["spreadsheet_id"]) if "spreadsheet_id" in columns else "",
        cell_range=str(row["cell_range"]) if "cell_range" in columns else "",
        mode=str(row["mode"]) if "mode" in columns else "demo",
    )
