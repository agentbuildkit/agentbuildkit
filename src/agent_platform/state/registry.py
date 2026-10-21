"""Memory and SQLite storage for the version 2 directory, ledger, and runs.

Rules live in ``models``. Each backend enforces the same transitions. SQLite is
the durable backend. The memory backend is the single-process stand-in.
"""

import json
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import ValidationError

from agent_platform.observability import argument_hash
from agent_platform.observability.audit import AuditEvent
from agent_platform.state.models import (
    CheckpointCompatibilityError,
    LedgerRecord,
    OrganisationRecord,
    RegistryRejected,
    RepositoryRecord,
    RoleRecord,
    RunRecord,
    RunStatus,
    TeamRecord,
    WorkflowCatalogEntry,
    WorkItemRecord,
    WorkItemStatus,
    assert_mutating_start,
    assert_parent_chain,
    assert_readable_work_item,
    assert_run_parent,
    assert_run_transition,
    assert_work_item_transition,
    require_argument_hash,
    require_branch,
    require_label,
    require_repository_remote,
    require_signature,
    require_token,
    require_tools,
)

V2_TABLES = (
    "ledger",
    "runs",
    "organisations",
    "roles",
    "repositories",
    "workflow_catalog",
    "teams",
    "work_items",
)
V2_INDEXES = ("runs_one_mutating", "runs_one_checkpoint", "teams_name")

V2_DDL = """
CREATE TABLE IF NOT EXISTS ledger (
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
CREATE TABLE IF NOT EXISTS runs (
    scope TEXT NOT NULL,
    run_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_run_id TEXT,
    checkpoint_id TEXT,
    work_item_id TEXT,
    mutating INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_mutating ON runs(scope, work_item_id)
    WHERE mutating = 1 AND status IN ('running', 'paused');
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_checkpoint ON runs(scope, checkpoint_id)
    WHERE checkpoint_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS organisations (
    scope TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    PRIMARY KEY (scope, name)
);
CREATE TABLE IF NOT EXISTS repositories (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    remote TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    PRIMARY KEY (scope, name)
);
CREATE TABLE IF NOT EXISTS workflow_catalog (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    graph_signature_hash TEXT,
    PRIMARY KEY (scope, name)
);
CREATE TABLE IF NOT EXISTS teams (
    scope TEXT NOT NULL,
    team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    roles_json TEXT NOT NULL,
    workflows_json TEXT NOT NULL,
    delegations_json TEXT NOT NULL,
    PRIMARY KEY (scope, team_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS teams_name ON teams(scope, name);
CREATE TABLE IF NOT EXISTS work_items (
    scope TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_work_item_id TEXT,
    active_run_id TEXT,
    acceptance_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, work_item_id)
);
"""


class PlatformRegistry(Protocol):
    """Operations shared by the memory and SQLite registries."""

    def append_ledger(
        self,
        scope: str,
        event: AuditEvent,
        *,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> LedgerRecord: ...

    def list_ledger(
        self,
        scope: str,
        *,
        correlation_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[LedgerRecord, ...]: ...

    def register_organisation(
        self, scope: str, name: str, policy_version: str
    ) -> OrganisationRecord: ...

    def get_organisation(self, scope: str) -> OrganisationRecord: ...

    def register_role(self, scope: str, name: str, tools: Sequence[str]) -> RoleRecord: ...

    def get_role(self, scope: str, name: str) -> RoleRecord: ...

    def list_roles(self, scope: str) -> tuple[RoleRecord, ...]: ...

    def register_repository(
        self, scope: str, name: str, remote: str, default_branch: str
    ) -> RepositoryRecord: ...

    def get_repository(self, scope: str, name: str) -> RepositoryRecord: ...

    def list_repositories(self, scope: str) -> tuple[RepositoryRecord, ...]: ...

    def register_workflow(
        self, scope: str, name: str, *, graph_signature_hash: str | None = None
    ) -> WorkflowCatalogEntry: ...

    def set_workflow_signature(
        self, scope: str, name: str, graph_signature_hash: str
    ) -> WorkflowCatalogEntry: ...

    def get_workflow(self, scope: str, name: str) -> WorkflowCatalogEntry: ...

    def list_workflows(self, scope: str) -> tuple[WorkflowCatalogEntry, ...]: ...

    def register_team(
        self,
        scope: str,
        name: str,
        *,
        roles: Sequence[str],
        workflows: Sequence[str] = (),
        delegations: Sequence[str] = (),
        team_id: str | None = None,
    ) -> TeamRecord: ...

    def get_team(self, scope: str, team_id: str) -> TeamRecord: ...

    def list_teams(self, scope: str) -> tuple[TeamRecord, ...]: ...

    def open_work_item(
        self,
        scope: str,
        *,
        item_type: str,
        title: str,
        acceptance: str,
        parent_work_item_id: str | None = None,
        work_item_id: str | None = None,
    ) -> WorkItemRecord: ...

    def get_work_item(self, scope: str, work_item_id: str) -> WorkItemRecord: ...

    def list_work_items(
        self, scope: str, *, status: WorkItemStatus | None = None
    ) -> tuple[WorkItemRecord, ...]: ...

    def transition_work_item(
        self, scope: str, work_item_id: str, status: WorkItemStatus
    ) -> WorkItemRecord: ...

    def start_run(
        self,
        scope: str,
        workflow_name: str,
        correlation_id: str,
        *,
        parent_run_id: str | None = None,
        work_item_id: str | None = None,
        mutating: bool = False,
        run_id: str | None = None,
    ) -> RunRecord: ...

    def get_run(self, scope: str, run_id: str) -> RunRecord: ...

    def list_runs(
        self,
        scope: str,
        *,
        workflow_name: str | None = None,
        status: RunStatus | None = None,
        work_item_id: str | None = None,
    ) -> tuple[RunRecord, ...]: ...

    def transition_run(self, scope: str, run_id: str, status: RunStatus) -> RunRecord: ...

    def bind_checkpoint(
        self,
        scope: str,
        run_id: str,
        *,
        checkpoint_id: str,
        workflow_name: str,
        graph_signature_hash: str,
    ) -> RunRecord: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: str | None) -> str:
    chosen = value if value is not None else uuid.uuid4().hex
    return require_token(chosen, "Directory name is not allowed.")


def _note(value: str | None, message: str) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 200 or any(character < " " for character in value):
        raise RegistryRejected(message)
    return value


def _names(values: Sequence[str], *, message: str) -> tuple[str, ...]:
    collected: list[str] = []
    for value in values:
        require_token(value, message)
        if value in collected:
            raise RegistryRejected(message)
        collected.append(value)
    return tuple(collected)


def _dump(values: Sequence[str]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _load_names(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, str):
        raise CheckpointCompatibilityError()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        raise CheckpointCompatibilityError() from None
    if not isinstance(parsed, list):
        raise CheckpointCompatibilityError()
    names: list[str] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, str):
            raise CheckpointCompatibilityError()
        names.append(item)
    return tuple(names)


def _acceptance_hash(acceptance: str) -> str:
    if not acceptance.strip() or len(acceptance) > 100_000:
        raise RegistryRejected("Work item transition is not allowed.")
    return argument_hash({"acceptance": acceptance})


def _require_organisation(found: bool) -> None:
    if not found:
        raise RegistryRejected("Organisation is not registered.")


class _MemoryTables:
    def __init__(self) -> None:
        self.ledger: dict[tuple[str, str], LedgerRecord] = {}
        self.runs: dict[tuple[str, str], RunRecord] = {}
        self.organisations: dict[str, OrganisationRecord] = {}
        self.roles: dict[tuple[str, str], RoleRecord] = {}
        self.repositories: dict[tuple[str, str], RepositoryRecord] = {}
        self.workflows: dict[tuple[str, str], WorkflowCatalogEntry] = {}
        self.teams: dict[tuple[str, str], TeamRecord] = {}
        self.work_items: dict[tuple[str, str], WorkItemRecord] = {}


class MemoryRegistry:
    """In-process registry. A new memory store is already version 2."""

    def __init__(self) -> None:
        self._tables = _MemoryTables()

    def append_ledger(
        self,
        scope: str,
        event: AuditEvent,
        *,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> LedgerRecord:
        record = _ledger_record(scope, event, run_id=run_id, event_id=event_id, now=_now())
        if run_id is not None and (scope, run_id) not in self._tables.runs:
            raise RegistryRejected("Run is unknown.")
        if (scope, record.event_id) in self._tables.ledger:
            raise RegistryRejected("Directory name is not allowed.")
        self._tables.ledger[(scope, record.event_id)] = record
        return record

    def list_ledger(
        self,
        scope: str,
        *,
        correlation_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[LedgerRecord, ...]:
        rows = [row for key, row in self._tables.ledger.items() if key[0] == scope]
        if correlation_id is not None:
            rows = [row for row in rows if row.correlation_id == correlation_id]
        if run_id is not None:
            rows = [row for row in rows if row.run_id == run_id]
        rows.sort(key=lambda row: (row.recorded_at, row.event_id))
        return tuple(rows)

    def register_organisation(
        self, scope: str, name: str, policy_version: str
    ) -> OrganisationRecord:
        if scope in self._tables.organisations:
            raise RegistryRejected("Organisation is already registered.")
        record = OrganisationRecord(
            scope=scope,
            name=require_label(name, "Directory name is not allowed."),
            policy_version=require_token(policy_version, "Directory name is not allowed."),
            created_at=_now(),
            updated_at=_now(),
        )
        stamped = record.model_copy(update={"updated_at": record.created_at})
        self._tables.organisations[scope] = stamped
        return stamped

    def get_organisation(self, scope: str) -> OrganisationRecord:
        found = self._tables.organisations.get(scope)
        if found is None:
            raise RegistryRejected("Organisation is not registered.")
        return found

    def register_role(self, scope: str, name: str, tools: Sequence[str]) -> RoleRecord:
        _require_organisation(scope in self._tables.organisations)
        token = require_token(name, "Directory name is not allowed.")
        if (scope, token) in self._tables.roles:
            raise RegistryRejected("Role is already registered.")
        record = RoleRecord(scope=scope, name=token, tools=require_tools(tools))
        self._tables.roles[(scope, token)] = record
        return record

    def get_role(self, scope: str, name: str) -> RoleRecord:
        found = self._tables.roles.get((scope, name))
        if found is None:
            raise RegistryRejected("Role is unknown.")
        return found

    def list_roles(self, scope: str) -> tuple[RoleRecord, ...]:
        rows = [row for key, row in self._tables.roles.items() if key[0] == scope]
        return tuple(sorted(rows, key=lambda row: row.name))

    def register_repository(
        self, scope: str, name: str, remote: str, default_branch: str
    ) -> RepositoryRecord:
        _require_organisation(scope in self._tables.organisations)
        token = require_token(name, "Directory name is not allowed.")
        if (scope, token) in self._tables.repositories:
            raise RegistryRejected("Repository is already registered.")
        record = RepositoryRecord(
            scope=scope,
            name=token,
            remote=require_repository_remote(remote),
            default_branch=require_branch(default_branch),
        )
        self._tables.repositories[(scope, token)] = record
        return record

    def get_repository(self, scope: str, name: str) -> RepositoryRecord:
        found = self._tables.repositories.get((scope, name))
        if found is None:
            raise RegistryRejected("Repository is unknown.")
        return found

    def list_repositories(self, scope: str) -> tuple[RepositoryRecord, ...]:
        rows = [row for key, row in self._tables.repositories.items() if key[0] == scope]
        return tuple(sorted(rows, key=lambda row: row.name))

    def register_workflow(
        self, scope: str, name: str, *, graph_signature_hash: str | None = None
    ) -> WorkflowCatalogEntry:
        _require_organisation(scope in self._tables.organisations)
        token = require_token(name, "Directory name is not allowed.")
        if (scope, token) in self._tables.workflows:
            raise RegistryRejected("Workflow is already registered.")
        signature = (
            None if graph_signature_hash is None else require_signature(graph_signature_hash)
        )
        record = WorkflowCatalogEntry(scope=scope, name=token, graph_signature_hash=signature)
        self._tables.workflows[(scope, token)] = record
        return record

    def set_workflow_signature(
        self, scope: str, name: str, graph_signature_hash: str
    ) -> WorkflowCatalogEntry:
        current = self.get_workflow(scope, name)
        signature = require_signature(graph_signature_hash)
        if current.graph_signature_hash is not None and current.graph_signature_hash != signature:
            raise RegistryRejected("Workflow graph signature does not match the checkpoint.")
        updated = current.model_copy(update={"graph_signature_hash": signature})
        self._tables.workflows[(scope, name)] = updated
        return updated

    def get_workflow(self, scope: str, name: str) -> WorkflowCatalogEntry:
        found = self._tables.workflows.get((scope, name))
        if found is None:
            raise RegistryRejected("Workflow is not in the catalog.")
        return found

    def list_workflows(self, scope: str) -> tuple[WorkflowCatalogEntry, ...]:
        rows = [row for key, row in self._tables.workflows.items() if key[0] == scope]
        return tuple(sorted(rows, key=lambda row: row.name))

    def register_team(
        self,
        scope: str,
        name: str,
        *,
        roles: Sequence[str],
        workflows: Sequence[str] = (),
        delegations: Sequence[str] = (),
        team_id: str | None = None,
    ) -> TeamRecord:
        _require_organisation(scope in self._tables.organisations)
        label = require_token(name, "Directory name is not allowed.")
        if any(row.name == label for key, row in self._tables.teams.items() if key[0] == scope):
            raise RegistryRejected("Team is already registered.")
        role_names = _names(roles, message="Role is unknown.")
        if not role_names:
            raise RegistryRejected("Role is unknown.")
        workflow_names = _names(workflows, message="Workflow is not in the catalog.")
        delegation_names = _names(delegations, message="Role is unknown.")
        for role_name in (*role_names, *delegation_names):
            if (scope, role_name) not in self._tables.roles:
                raise RegistryRejected("Role is unknown.")
        for workflow_name in workflow_names:
            if (scope, workflow_name) not in self._tables.workflows:
                raise RegistryRejected("Workflow is not in the catalog.")
        identifier = _identifier(team_id)
        if (scope, identifier) in self._tables.teams:
            raise RegistryRejected("Team is already registered.")
        record = TeamRecord(
            scope=scope,
            team_id=identifier,
            name=label,
            roles=role_names,
            workflows=workflow_names,
            delegations=delegation_names,
        )
        self._tables.teams[(scope, identifier)] = record
        return record

    def get_team(self, scope: str, team_id: str) -> TeamRecord:
        found = self._tables.teams.get((scope, team_id))
        if found is None:
            raise RegistryRejected("Team is unknown.")
        return found

    def list_teams(self, scope: str) -> tuple[TeamRecord, ...]:
        rows = [row for key, row in self._tables.teams.items() if key[0] == scope]
        return tuple(sorted(rows, key=lambda row: row.name))

    def open_work_item(
        self,
        scope: str,
        *,
        item_type: str,
        title: str,
        acceptance: str,
        parent_work_item_id: str | None = None,
        work_item_id: str | None = None,
    ) -> WorkItemRecord:
        _require_organisation(scope in self._tables.organisations)
        identifier = _identifier(work_item_id)
        if (scope, identifier) in self._tables.work_items:
            raise RegistryRejected("Directory name is not allowed.")
        items = {key[1]: row for key, row in self._tables.work_items.items() if key[0] == scope}
        assert_parent_chain(items, work_item_id=identifier, parent_work_item_id=parent_work_item_id)
        stamp = _now()
        record = WorkItemRecord(
            scope=scope,
            work_item_id=identifier,
            item_type=require_token(item_type, "Directory name is not allowed."),
            title=require_label(title, "Directory name is not allowed."),
            status="draft",
            parent_work_item_id=parent_work_item_id,
            active_run_id=None,
            acceptance_hash=_acceptance_hash(acceptance),
            created_at=stamp,
            updated_at=stamp,
        )
        self._tables.work_items[(scope, identifier)] = record
        return record

    def get_work_item(self, scope: str, work_item_id: str) -> WorkItemRecord:
        found = self._tables.work_items.get((scope, work_item_id))
        if found is None:
            raise RegistryRejected("Work item is unknown.")
        return found

    def list_work_items(
        self, scope: str, *, status: WorkItemStatus | None = None
    ) -> tuple[WorkItemRecord, ...]:
        rows = [row for key, row in self._tables.work_items.items() if key[0] == scope]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        rows.sort(key=lambda row: (row.created_at, row.work_item_id))
        return tuple(rows)

    def transition_work_item(
        self, scope: str, work_item_id: str, status: WorkItemStatus
    ) -> WorkItemRecord:
        current = self.get_work_item(scope, work_item_id)
        assert_work_item_transition(current.status, status, active_run_id=current.active_run_id)
        updated = current.model_copy(update={"status": status, "updated_at": _now()})
        self._tables.work_items[(scope, work_item_id)] = updated
        return updated

    def start_run(
        self,
        scope: str,
        workflow_name: str,
        correlation_id: str,
        *,
        parent_run_id: str | None = None,
        work_item_id: str | None = None,
        mutating: bool = False,
        run_id: str | None = None,
    ) -> RunRecord:
        workflow = self.get_workflow(scope, workflow_name)
        identifier = _identifier(run_id)
        if (scope, identifier) in self._tables.runs:
            raise RegistryRejected("Directory name is not allowed.")
        runs = {key[1]: row for key, row in self._tables.runs.items() if key[0] == scope}
        assert_run_parent(runs, run_id=identifier, parent_run_id=parent_run_id)
        item = self._claim_work_item(scope, work_item_id, run_id=identifier, mutating=mutating)
        stamp = _now()
        record = RunRecord(
            scope=scope,
            run_id=identifier,
            workflow_name=workflow.name,
            correlation_id=require_token(correlation_id, "Directory name is not allowed."),
            status="running",
            parent_run_id=parent_run_id,
            checkpoint_id=None,
            work_item_id=work_item_id,
            mutating=mutating,
            created_at=stamp,
            updated_at=stamp,
        )
        self._guard_mutating(scope, record)
        self._tables.runs[(scope, identifier)] = record
        if item is not None:
            self._tables.work_items[(scope, item.work_item_id)] = item
        return record

    def get_run(self, scope: str, run_id: str) -> RunRecord:
        found = self._tables.runs.get((scope, run_id))
        if found is None:
            raise RegistryRejected("Run is unknown.")
        return found

    def list_runs(
        self,
        scope: str,
        *,
        workflow_name: str | None = None,
        status: RunStatus | None = None,
        work_item_id: str | None = None,
    ) -> tuple[RunRecord, ...]:
        rows = [row for key, row in self._tables.runs.items() if key[0] == scope]
        if workflow_name is not None:
            rows = [row for row in rows if row.workflow_name == workflow_name]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        if work_item_id is not None:
            rows = [row for row in rows if row.work_item_id == work_item_id]
        rows.sort(key=lambda row: (row.created_at, row.run_id))
        return tuple(rows)

    def transition_run(self, scope: str, run_id: str, status: RunStatus) -> RunRecord:
        current = self.get_run(scope, run_id)
        assert_run_transition(current.status, status)
        if status == "paused" and current.checkpoint_id is None:
            raise RegistryRejected("A paused run needs a checkpoint.")
        updated = current.model_copy(update={"status": status, "updated_at": _now()})
        self._tables.runs[(scope, run_id)] = updated
        self._release_work_item(scope, updated)
        return updated

    def bind_checkpoint(
        self,
        scope: str,
        run_id: str,
        *,
        checkpoint_id: str,
        workflow_name: str,
        graph_signature_hash: str,
    ) -> RunRecord:
        current = self.get_run(scope, run_id)
        updated = _bind(
            current,
            self.get_workflow(scope, current.workflow_name),
            checkpoint_id,
            workflow_name,
            graph_signature_hash,
        )
        self._guard_checkpoint(scope, updated)
        self._tables.runs[(scope, run_id)] = updated
        return updated

    def _claim_work_item(
        self,
        scope: str,
        work_item_id: str | None,
        *,
        run_id: str,
        mutating: bool,
    ) -> WorkItemRecord | None:
        if work_item_id is None:
            if mutating:
                return None
            return None
        item = self.get_work_item(scope, work_item_id)
        if mutating:
            assert_mutating_start(item.status, active_run_id=item.active_run_id)
            self._guard_mutating(
                scope,
                RunRecord(
                    scope=scope,
                    run_id=run_id,
                    workflow_name="pending",
                    correlation_id="pending",
                    status="running",
                    work_item_id=work_item_id,
                    mutating=True,
                    created_at=_now(),
                    updated_at=_now(),
                ),
            )
            return item.model_copy(
                update={"status": "active", "active_run_id": run_id, "updated_at": _now()}
            )
        assert_readable_work_item(item.status)
        return None

    def _release_work_item(self, scope: str, run: RunRecord) -> None:
        if not run.mutating or run.work_item_id is None:
            return
        if run.status not in {"completed", "failed"}:
            return
        item = self._tables.work_items.get((scope, run.work_item_id))
        if item is None or item.active_run_id != run.run_id:
            return
        status: WorkItemStatus = item.status
        if run.status == "failed" and item.status == "active":
            status = "blocked"
        self._tables.work_items[(scope, item.work_item_id)] = item.model_copy(
            update={"active_run_id": None, "status": status, "updated_at": _now()}
        )

    def _guard_mutating(self, scope: str, run: RunRecord) -> None:
        if not run.mutating or run.work_item_id is None or run.status not in {"running", "paused"}:
            return
        for key, existing in self._tables.runs.items():
            if key[0] != scope or existing.run_id == run.run_id:
                continue
            if (
                existing.mutating
                and existing.work_item_id == run.work_item_id
                and existing.status in {"running", "paused"}
            ):
                raise RegistryRejected("A mutating run is already active for this work item.")

    def _guard_checkpoint(self, scope: str, run: RunRecord) -> None:
        if run.checkpoint_id is None:
            return
        for key, existing in self._tables.runs.items():
            if key[0] != scope or existing.run_id == run.run_id:
                continue
            if existing.checkpoint_id == run.checkpoint_id:
                raise RegistryRejected("Checkpoint is already bound to a run.")


def _ledger_record(
    scope: str,
    event: AuditEvent,
    *,
    run_id: str | None,
    event_id: str | None,
    now: str,
) -> LedgerRecord:
    try:
        return LedgerRecord(
            scope=scope,
            event_id=_identifier(event_id),
            recorded_at=now,
            correlation_id=require_token(event.correlation_id, "Directory name is not allowed."),
            run_id=None if run_id is None else require_token(run_id, "Run is unknown."),
            agent_name=_note(event.agent_name, "Directory name is not allowed."),
            tool_name=require_token(event.tool_name, "Directory name is not allowed."),
            approval=event.approval,
            outcome=event.outcome,
            argument_hash=require_argument_hash(event.argument_hash),
            kind=event.kind,
        )
    except ValidationError:
        raise CheckpointCompatibilityError() from None


def _bind(
    current: RunRecord,
    workflow: WorkflowCatalogEntry,
    checkpoint_id: str,
    workflow_name: str,
    graph_signature_hash: str,
) -> RunRecord:
    if current.status in {"completed", "failed"}:
        raise RegistryRejected("Run transition is not allowed.")
    token = require_token(checkpoint_id, "Checkpoint workflow does not match the run.")
    if workflow_name != current.workflow_name:
        raise RegistryRejected("Checkpoint workflow does not match the run.")
    if (
        workflow.graph_signature_hash is not None
        and workflow.graph_signature_hash != graph_signature_hash
    ):
        raise RegistryRejected("Workflow graph signature does not match the checkpoint.")
    return current.model_copy(update={"checkpoint_id": token, "updated_at": _now()})


class SqliteRegistry:
    """Registry tables on the workflow store connection."""

    def __init__(self, connection: sqlite3.Connection, commit: Callable[[], None]) -> None:
        self._connection = connection
        self._commit = commit

    def append_ledger(
        self,
        scope: str,
        event: AuditEvent,
        *,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> LedgerRecord:
        if run_id is not None and self._maybe_run(scope, run_id) is None:
            raise RegistryRejected("Run is unknown.")
        record = _ledger_record(scope, event, run_id=run_id, event_id=event_id, now=_now())
        try:
            self._connection.execute(
                """
                INSERT INTO ledger (
                    scope, event_id, recorded_at, correlation_id, run_id, agent_name,
                    tool_name, approval, outcome, argument_hash, kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.scope,
                    record.event_id,
                    record.recorded_at,
                    record.correlation_id,
                    record.run_id,
                    record.agent_name,
                    record.tool_name,
                    record.approval,
                    record.outcome,
                    record.argument_hash,
                    record.kind,
                ),
            )
        except sqlite3.IntegrityError:
            self._connection.rollback()
            raise RegistryRejected("Directory name is not allowed.") from None
        self._commit()
        return record

    def list_ledger(
        self,
        scope: str,
        *,
        correlation_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[LedgerRecord, ...]:
        sql = "SELECT * FROM ledger WHERE scope = ?"
        params: list[object] = [scope]
        if correlation_id is not None:
            sql += " AND correlation_id = ?"
            params.append(correlation_id)
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY recorded_at, event_id"
        return tuple(_ledger_from_row(row) for row in self._fetch(sql, tuple(params)))

    def register_organisation(
        self, scope: str, name: str, policy_version: str
    ) -> OrganisationRecord:
        if self._organisation(scope) is not None:
            raise RegistryRejected("Organisation is already registered.")
        stamp = _now()
        record = OrganisationRecord(
            scope=scope,
            name=require_label(name, "Directory name is not allowed."),
            policy_version=require_token(policy_version, "Directory name is not allowed."),
            created_at=stamp,
            updated_at=stamp,
        )
        self._connection.execute(
            """
            INSERT INTO organisations (scope, name, policy_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.scope,
                record.name,
                record.policy_version,
                record.created_at,
                record.updated_at,
            ),
        )
        self._commit()
        return record

    def get_organisation(self, scope: str) -> OrganisationRecord:
        found = self._organisation(scope)
        if found is None:
            raise RegistryRejected("Organisation is not registered.")
        return found

    def register_role(self, scope: str, name: str, tools: Sequence[str]) -> RoleRecord:
        self.get_organisation(scope)
        token = require_token(name, "Directory name is not allowed.")
        if self._role(scope, token) is not None:
            raise RegistryRejected("Role is already registered.")
        record = RoleRecord(scope=scope, name=token, tools=require_tools(tools))
        self._connection.execute(
            "INSERT INTO roles (scope, name, tools_json) VALUES (?, ?, ?)",
            (scope, token, _dump(record.tools)),
        )
        self._commit()
        return record

    def get_role(self, scope: str, name: str) -> RoleRecord:
        found = self._role(scope, name)
        if found is None:
            raise RegistryRejected("Role is unknown.")
        return found

    def list_roles(self, scope: str) -> tuple[RoleRecord, ...]:
        rows = self._fetch("SELECT * FROM roles WHERE scope = ? ORDER BY name", (scope,))
        return tuple(_role_from_row(row) for row in rows)

    def register_repository(
        self, scope: str, name: str, remote: str, default_branch: str
    ) -> RepositoryRecord:
        self.get_organisation(scope)
        token = require_token(name, "Directory name is not allowed.")
        if self._repository(scope, token) is not None:
            raise RegistryRejected("Repository is already registered.")
        record = RepositoryRecord(
            scope=scope,
            name=token,
            remote=require_repository_remote(remote),
            default_branch=require_branch(default_branch),
        )
        self._connection.execute(
            """
            INSERT INTO repositories (scope, name, remote, default_branch)
            VALUES (?, ?, ?, ?)
            """,
            (scope, token, record.remote, record.default_branch),
        )
        self._commit()
        return record

    def get_repository(self, scope: str, name: str) -> RepositoryRecord:
        found = self._repository(scope, name)
        if found is None:
            raise RegistryRejected("Repository is unknown.")
        return found

    def list_repositories(self, scope: str) -> tuple[RepositoryRecord, ...]:
        rows = self._fetch("SELECT * FROM repositories WHERE scope = ? ORDER BY name", (scope,))
        return tuple(_repository_from_row(row) for row in rows)

    def register_workflow(
        self, scope: str, name: str, *, graph_signature_hash: str | None = None
    ) -> WorkflowCatalogEntry:
        self.get_organisation(scope)
        token = require_token(name, "Directory name is not allowed.")
        if self._workflow(scope, token) is not None:
            raise RegistryRejected("Workflow is already registered.")
        signature = (
            None if graph_signature_hash is None else require_signature(graph_signature_hash)
        )
        record = WorkflowCatalogEntry(scope=scope, name=token, graph_signature_hash=signature)
        self._connection.execute(
            """
            INSERT INTO workflow_catalog (scope, name, graph_signature_hash)
            VALUES (?, ?, ?)
            """,
            (scope, token, signature),
        )
        self._commit()
        return record

    def set_workflow_signature(
        self, scope: str, name: str, graph_signature_hash: str
    ) -> WorkflowCatalogEntry:
        current = self.get_workflow(scope, name)
        signature = require_signature(graph_signature_hash)
        if current.graph_signature_hash is not None and current.graph_signature_hash != signature:
            raise RegistryRejected("Workflow graph signature does not match the checkpoint.")
        self._connection.execute(
            """
            UPDATE workflow_catalog SET graph_signature_hash = ?
            WHERE scope = ? AND name = ?
            """,
            (signature, scope, name),
        )
        self._commit()
        return current.model_copy(update={"graph_signature_hash": signature})

    def get_workflow(self, scope: str, name: str) -> WorkflowCatalogEntry:
        found = self._workflow(scope, name)
        if found is None:
            raise RegistryRejected("Workflow is not in the catalog.")
        return found

    def list_workflows(self, scope: str) -> tuple[WorkflowCatalogEntry, ...]:
        rows = self._fetch(
            "SELECT * FROM workflow_catalog WHERE scope = ? ORDER BY name",
            (scope,),
        )
        return tuple(_workflow_from_row(row) for row in rows)

    def register_team(
        self,
        scope: str,
        name: str,
        *,
        roles: Sequence[str],
        workflows: Sequence[str] = (),
        delegations: Sequence[str] = (),
        team_id: str | None = None,
    ) -> TeamRecord:
        self.get_organisation(scope)
        label = require_token(name, "Directory name is not allowed.")
        existing = self._fetch(
            "SELECT team_id FROM teams WHERE scope = ? AND name = ?",
            (scope, label),
        )
        if existing:
            raise RegistryRejected("Team is already registered.")
        role_names = _names(roles, message="Role is unknown.")
        if not role_names:
            raise RegistryRejected("Role is unknown.")
        workflow_names = _names(workflows, message="Workflow is not in the catalog.")
        delegation_names = _names(delegations, message="Role is unknown.")
        for role_name in (*role_names, *delegation_names):
            if self._role(scope, role_name) is None:
                raise RegistryRejected("Role is unknown.")
        for workflow_name in workflow_names:
            if self._workflow(scope, workflow_name) is None:
                raise RegistryRejected("Workflow is not in the catalog.")
        identifier = _identifier(team_id)
        if self._team(scope, identifier) is not None:
            raise RegistryRejected("Team is already registered.")
        record = TeamRecord(
            scope=scope,
            team_id=identifier,
            name=label,
            roles=role_names,
            workflows=workflow_names,
            delegations=delegation_names,
        )
        try:
            self._connection.execute(
                """
                INSERT INTO teams (
                    scope, team_id, name, roles_json, workflows_json, delegations_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    identifier,
                    label,
                    _dump(role_names),
                    _dump(workflow_names),
                    _dump(delegation_names),
                ),
            )
        except sqlite3.IntegrityError:
            self._connection.rollback()
            raise RegistryRejected("Team is already registered.") from None
        self._commit()
        return record

    def get_team(self, scope: str, team_id: str) -> TeamRecord:
        found = self._team(scope, team_id)
        if found is None:
            raise RegistryRejected("Team is unknown.")
        return found

    def list_teams(self, scope: str) -> tuple[TeamRecord, ...]:
        rows = self._fetch("SELECT * FROM teams WHERE scope = ? ORDER BY name", (scope,))
        return tuple(_team_from_row(row) for row in rows)

    def open_work_item(
        self,
        scope: str,
        *,
        item_type: str,
        title: str,
        acceptance: str,
        parent_work_item_id: str | None = None,
        work_item_id: str | None = None,
    ) -> WorkItemRecord:
        self.get_organisation(scope)
        identifier = _identifier(work_item_id)
        if self._work_item(scope, identifier) is not None:
            raise RegistryRejected("Directory name is not allowed.")
        items = {row.work_item_id: row for row in self.list_work_items(scope)}
        assert_parent_chain(items, work_item_id=identifier, parent_work_item_id=parent_work_item_id)
        stamp = _now()
        record = WorkItemRecord(
            scope=scope,
            work_item_id=identifier,
            item_type=require_token(item_type, "Directory name is not allowed."),
            title=require_label(title, "Directory name is not allowed."),
            status="draft",
            parent_work_item_id=parent_work_item_id,
            active_run_id=None,
            acceptance_hash=_acceptance_hash(acceptance),
            created_at=stamp,
            updated_at=stamp,
        )
        self._connection.execute(
            """
            INSERT INTO work_items (
                scope, work_item_id, item_type, title, status, parent_work_item_id,
                active_run_id, acceptance_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _work_item_values(record),
        )
        self._commit()
        return record

    def get_work_item(self, scope: str, work_item_id: str) -> WorkItemRecord:
        found = self._work_item(scope, work_item_id)
        if found is None:
            raise RegistryRejected("Work item is unknown.")
        return found

    def list_work_items(
        self, scope: str, *, status: WorkItemStatus | None = None
    ) -> tuple[WorkItemRecord, ...]:
        if status is None:
            rows = self._fetch(
                """
                SELECT * FROM work_items WHERE scope = ?
                ORDER BY created_at, work_item_id
                """,
                (scope,),
            )
        else:
            rows = self._fetch(
                """
                SELECT * FROM work_items WHERE scope = ? AND status = ?
                ORDER BY created_at, work_item_id
                """,
                (scope, status),
            )
        return tuple(_work_item_from_row(row) for row in rows)

    def transition_work_item(
        self, scope: str, work_item_id: str, status: WorkItemStatus
    ) -> WorkItemRecord:
        current = self.get_work_item(scope, work_item_id)
        assert_work_item_transition(current.status, status, active_run_id=current.active_run_id)
        updated = current.model_copy(update={"status": status, "updated_at": _now()})
        self._save_work_item(updated)
        return updated

    def start_run(
        self,
        scope: str,
        workflow_name: str,
        correlation_id: str,
        *,
        parent_run_id: str | None = None,
        work_item_id: str | None = None,
        mutating: bool = False,
        run_id: str | None = None,
    ) -> RunRecord:
        workflow = self.get_workflow(scope, workflow_name)
        identifier = _identifier(run_id)
        if self._maybe_run(scope, identifier) is not None:
            raise RegistryRejected("Directory name is not allowed.")
        runs = {row.run_id: row for row in self.list_runs(scope)}
        assert_run_parent(runs, run_id=identifier, parent_run_id=parent_run_id)
        item = self._prepare_work_item(scope, work_item_id, run_id=identifier, mutating=mutating)
        stamp = _now()
        record = RunRecord(
            scope=scope,
            run_id=identifier,
            workflow_name=workflow.name,
            correlation_id=require_token(correlation_id, "Directory name is not allowed."),
            status="running",
            parent_run_id=parent_run_id,
            checkpoint_id=None,
            work_item_id=work_item_id,
            mutating=mutating,
            created_at=stamp,
            updated_at=stamp,
        )
        try:
            self._insert_run(record)
            if item is not None:
                self._save_work_item(item, commit=False)
            self._commit()
        except sqlite3.IntegrityError:
            self._connection.rollback()
            raise RegistryRejected("A mutating run is already active for this work item.") from None
        return record

    def get_run(self, scope: str, run_id: str) -> RunRecord:
        found = self._maybe_run(scope, run_id)
        if found is None:
            raise RegistryRejected("Run is unknown.")
        return found

    def list_runs(
        self,
        scope: str,
        *,
        workflow_name: str | None = None,
        status: RunStatus | None = None,
        work_item_id: str | None = None,
    ) -> tuple[RunRecord, ...]:
        sql = "SELECT * FROM runs WHERE scope = ?"
        params: list[object] = [scope]
        if workflow_name is not None:
            sql += " AND workflow_name = ?"
            params.append(workflow_name)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if work_item_id is not None:
            sql += " AND work_item_id = ?"
            params.append(work_item_id)
        sql += " ORDER BY created_at, run_id"
        return tuple(_run_from_row(row) for row in self._fetch(sql, tuple(params)))

    def transition_run(self, scope: str, run_id: str, status: RunStatus) -> RunRecord:
        current = self.get_run(scope, run_id)
        assert_run_transition(current.status, status)
        if status == "paused" and current.checkpoint_id is None:
            raise RegistryRejected("A paused run needs a checkpoint.")
        updated = current.model_copy(update={"status": status, "updated_at": _now()})
        self._save_run(updated, commit=False)
        self._release_work_item(scope, updated, commit=False)
        self._commit()
        return updated

    def bind_checkpoint(
        self,
        scope: str,
        run_id: str,
        *,
        checkpoint_id: str,
        workflow_name: str,
        graph_signature_hash: str,
    ) -> RunRecord:
        current = self.get_run(scope, run_id)
        updated = _bind(
            current,
            self.get_workflow(scope, current.workflow_name),
            checkpoint_id,
            workflow_name,
            graph_signature_hash,
        )
        try:
            self._save_run(updated)
        except sqlite3.IntegrityError:
            self._connection.rollback()
            raise RegistryRejected("Checkpoint is already bound to a run.") from None
        return updated

    def _prepare_work_item(
        self,
        scope: str,
        work_item_id: str | None,
        *,
        run_id: str,
        mutating: bool,
    ) -> WorkItemRecord | None:
        if work_item_id is None:
            return None
        item = self.get_work_item(scope, work_item_id)
        if not mutating:
            assert_readable_work_item(item.status)
            return None
        assert_mutating_start(item.status, active_run_id=item.active_run_id)
        return item.model_copy(
            update={"status": "active", "active_run_id": run_id, "updated_at": _now()}
        )

    def _release_work_item(self, scope: str, run: RunRecord, *, commit: bool) -> None:
        if not run.mutating or run.work_item_id is None:
            return
        if run.status not in {"completed", "failed"}:
            return
        item = self._work_item(scope, run.work_item_id)
        if item is None or item.active_run_id != run.run_id:
            return
        status: WorkItemStatus = (
            "blocked" if run.status == "failed" and item.status == "active" else item.status
        )
        self._save_work_item(
            item.model_copy(update={"active_run_id": None, "status": status, "updated_at": _now()}),
            commit=commit,
        )

    def _insert_run(self, record: RunRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO runs (
                scope, run_id, workflow_name, correlation_id, status, parent_run_id,
                checkpoint_id, work_item_id, mutating, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _run_values(record),
        )

    def _save_run(self, record: RunRecord, *, commit: bool = True) -> None:
        self._connection.execute(
            """
            UPDATE runs SET
                status = ?, parent_run_id = ?, checkpoint_id = ?, work_item_id = ?,
                mutating = ?, updated_at = ?
            WHERE scope = ? AND run_id = ?
            """,
            (
                record.status,
                record.parent_run_id,
                record.checkpoint_id,
                record.work_item_id,
                1 if record.mutating else 0,
                record.updated_at,
                record.scope,
                record.run_id,
            ),
        )
        if commit:
            self._commit()

    def _save_work_item(self, record: WorkItemRecord, *, commit: bool = True) -> None:
        self._connection.execute(
            """
            UPDATE work_items SET
                status = ?, parent_work_item_id = ?, active_run_id = ?, updated_at = ?
            WHERE scope = ? AND work_item_id = ?
            """,
            (
                record.status,
                record.parent_work_item_id,
                record.active_run_id,
                record.updated_at,
                record.scope,
                record.work_item_id,
            ),
        )
        if commit:
            self._commit()

    def _organisation(self, scope: str) -> OrganisationRecord | None:
        row = self._one("SELECT * FROM organisations WHERE scope = ?", (scope,))
        if row is None:
            return None
        return OrganisationRecord(
            scope=str(row["scope"]),
            name=str(row["name"]),
            policy_version=str(row["policy_version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _role(self, scope: str, name: str) -> RoleRecord | None:
        row = self._one("SELECT * FROM roles WHERE scope = ? AND name = ?", (scope, name))
        if row is None:
            return None
        return _role_from_row(row)

    def _repository(self, scope: str, name: str) -> RepositoryRecord | None:
        row = self._one(
            "SELECT * FROM repositories WHERE scope = ? AND name = ?",
            (scope, name),
        )
        if row is None:
            return None
        return _repository_from_row(row)

    def _workflow(self, scope: str, name: str) -> WorkflowCatalogEntry | None:
        row = self._one(
            "SELECT * FROM workflow_catalog WHERE scope = ? AND name = ?",
            (scope, name),
        )
        if row is None:
            return None
        return _workflow_from_row(row)

    def _team(self, scope: str, team_id: str) -> TeamRecord | None:
        row = self._one("SELECT * FROM teams WHERE scope = ? AND team_id = ?", (scope, team_id))
        if row is None:
            return None
        return _team_from_row(row)

    def _work_item(self, scope: str, work_item_id: str) -> WorkItemRecord | None:
        row = self._one(
            "SELECT * FROM work_items WHERE scope = ? AND work_item_id = ?",
            (scope, work_item_id),
        )
        if row is None:
            return None
        return _work_item_from_row(row)

    def _maybe_run(self, scope: str, run_id: str) -> RunRecord | None:
        row = self._one("SELECT * FROM runs WHERE scope = ? AND run_id = ?", (scope, run_id))
        if row is None:
            return None
        return _run_from_row(row)

    def _one(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        row = self._connection.execute(sql, params).fetchone()
        if row is None:
            return None
        return cast(sqlite3.Row, row)

    def _fetch(self, sql: str, params: tuple[object, ...]) -> list[sqlite3.Row]:
        return [cast(sqlite3.Row, row) for row in self._connection.execute(sql, params).fetchall()]


def _ledger_from_row(row: sqlite3.Row) -> LedgerRecord:
    try:
        return LedgerRecord(
            scope=str(row["scope"]),
            event_id=str(row["event_id"]),
            recorded_at=str(row["recorded_at"]),
            correlation_id=str(row["correlation_id"]),
            run_id=None if row["run_id"] is None else str(row["run_id"]),
            agent_name=None if row["agent_name"] is None else str(row["agent_name"]),
            tool_name=str(row["tool_name"]),
            approval=str(row["approval"]),  # type: ignore[arg-type]
            outcome=str(row["outcome"]),  # type: ignore[arg-type]
            argument_hash=str(row["argument_hash"]),
            kind=str(row["kind"]),  # type: ignore[arg-type]
        )
    except ValidationError:
        raise CheckpointCompatibilityError() from None


def _role_from_row(row: sqlite3.Row) -> RoleRecord:
    return RoleRecord(
        scope=str(row["scope"]),
        name=str(row["name"]),
        tools=_load_names(row["tools_json"]),
    )


def _repository_from_row(row: sqlite3.Row) -> RepositoryRecord:
    return RepositoryRecord(
        scope=str(row["scope"]),
        name=str(row["name"]),
        remote=str(row["remote"]),
        default_branch=str(row["default_branch"]),
    )


def _workflow_from_row(row: sqlite3.Row) -> WorkflowCatalogEntry:
    signature = row["graph_signature_hash"]
    return WorkflowCatalogEntry(
        scope=str(row["scope"]),
        name=str(row["name"]),
        graph_signature_hash=None if signature is None else str(signature),
    )


def _team_from_row(row: sqlite3.Row) -> TeamRecord:
    return TeamRecord(
        scope=str(row["scope"]),
        team_id=str(row["team_id"]),
        name=str(row["name"]),
        roles=_load_names(row["roles_json"]),
        workflows=_load_names(row["workflows_json"]),
        delegations=_load_names(row["delegations_json"]),
    )


def _work_item_from_row(row: sqlite3.Row) -> WorkItemRecord:
    try:
        return WorkItemRecord(
            scope=str(row["scope"]),
            work_item_id=str(row["work_item_id"]),
            item_type=str(row["item_type"]),
            title=str(row["title"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            parent_work_item_id=(
                None if row["parent_work_item_id"] is None else str(row["parent_work_item_id"])
            ),
            active_run_id=None if row["active_run_id"] is None else str(row["active_run_id"]),
            acceptance_hash=str(row["acceptance_hash"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
    except ValidationError:
        raise CheckpointCompatibilityError() from None


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    try:
        return RunRecord(
            scope=str(row["scope"]),
            run_id=str(row["run_id"]),
            workflow_name=str(row["workflow_name"]),
            correlation_id=str(row["correlation_id"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            parent_run_id=None if row["parent_run_id"] is None else str(row["parent_run_id"]),
            checkpoint_id=None if row["checkpoint_id"] is None else str(row["checkpoint_id"]),
            work_item_id=None if row["work_item_id"] is None else str(row["work_item_id"]),
            mutating=bool(row["mutating"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
    except ValidationError:
        raise CheckpointCompatibilityError() from None


def _run_values(record: RunRecord) -> tuple[object, ...]:
    return (
        record.scope,
        record.run_id,
        record.workflow_name,
        record.correlation_id,
        record.status,
        record.parent_run_id,
        record.checkpoint_id,
        record.work_item_id,
        1 if record.mutating else 0,
        record.created_at,
        record.updated_at,
    )


def _work_item_values(record: WorkItemRecord) -> tuple[object, ...]:
    return (
        record.scope,
        record.work_item_id,
        record.item_type,
        record.title,
        record.status,
        record.parent_work_item_id,
        record.active_run_id,
        record.acceptance_hash,
        record.created_at,
        record.updated_at,
    )


def schema_objects(connection: sqlite3.Connection) -> set[str]:
    """Return table and index names present in a workflow database."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def v2_schema_ready(connection: sqlite3.Connection) -> bool:
    """A version 2 file has every registry table and index."""
    present = schema_objects(connection)
    return set(V2_TABLES).issubset(present) and set(V2_INDEXES).issubset(present)
