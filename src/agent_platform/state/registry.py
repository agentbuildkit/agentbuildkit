"""Memory and SQLite storage for the version 2 directory, ledger, and runs.

Rules live in ``models``. Each backend enforces the same transitions. SQLite is
the durable backend. The memory backend is the single-process stand-in.
"""

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from agent_platform.observability import argument_hash
from agent_platform.observability.audit import AuditEvent
from agent_platform.state.github import (
    GithubBindings,
    GithubRegistryMethods,
    MemoryGithubStorage,
    SqliteGithubStorage,
    signal_from_row,
)
from agent_platform.state.models import (
    SIGNAL_CHILD_FINISHED,
    CheckpointCompatibilityError,
    ChildOutcome,
    ChildOutcomeStatus,
    ChildWaitRecord,
    DelegationRecord,
    GateRecord,
    GithubBindingRecord,
    GithubCheckRecord,
    GithubNoteRecord,
    GithubPullRequestRecord,
    GithubRefRecord,
    GithubReleaseRecord,
    GrantRecord,
    GrantStatus,
    GrantUseRecord,
    LedgerRecord,
    OrganisationRecord,
    PermissionClass,
    RegistryRejected,
    RepositoryRecord,
    RoleRecord,
    RunRecord,
    RunStatus,
    SignalRecord,
    TeamRecord,
    ToolPermissionRecord,
    WorkflowCatalogEntry,
    WorkItemRecord,
    WorkItemStatus,
    assert_gate_tools,
    assert_mutating_start,
    assert_parent_chain,
    assert_readable_work_item,
    assert_run_parent,
    assert_run_transition,
    assert_work_item_transition,
    branch_in_prefix,
    holder_authorizes_child,
    outcome_to_run_status,
    require_argument_hash,
    require_branch,
    require_child_outcome,
    require_label,
    require_permission_class,
    require_repository_remote,
    require_signature,
    require_timestamp,
    require_token,
    require_tools,
    timestamp_reached,
)

V2_CORE_TABLES = (
    "ledger",
    "runs",
    "organisations",
    "roles",
    "repositories",
    "workflow_catalog",
    "teams",
    "work_items",
)
V2_EXTENSION_TABLES = (
    "workflow_delegations",
    "child_outcomes",
    "signals",
    "child_waits",
    "tool_permissions",
    "gates",
    "grants",
    "grant_uses",
    "repository_protections",
    "github_bindings",
    "github_refs",
    "github_pull_requests",
    "github_checks",
    "github_releases",
    "github_notes",
)
V2_TABLES = V2_CORE_TABLES + V2_EXTENSION_TABLES
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
    WHERE mutating = 1 AND parent_run_id IS NULL AND status IN ('running', 'paused');
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
    permission_class TEXT NOT NULL DEFAULT 'implementation',
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
CREATE TABLE IF NOT EXISTS workflow_delegations (
    scope TEXT NOT NULL,
    parent_workflow TEXT NOT NULL,
    child_workflow TEXT NOT NULL,
    PRIMARY KEY (scope, parent_workflow, child_workflow)
);
CREATE TABLE IF NOT EXISTS child_outcomes (
    scope TEXT NOT NULL,
    child_run_id TEXT NOT NULL,
    parent_run_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, child_run_id)
);
CREATE TABLE IF NOT EXISTS signals (
    scope TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    name TEXT NOT NULL,
    target_run_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    outcome_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    subject_kind TEXT,
    subject_id TEXT,
    PRIMARY KEY (scope, signal_id)
);
CREATE TABLE IF NOT EXISTS child_waits (
    scope TEXT NOT NULL,
    parent_run_id TEXT NOT NULL,
    child_run_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, parent_run_id, child_run_id)
);
CREATE TABLE IF NOT EXISTS tool_permissions (
    scope TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    permission_class TEXT NOT NULL,
    PRIMARY KEY (scope, tool_name)
);
CREATE TABLE IF NOT EXISTS gates (
    scope TEXT NOT NULL,
    gate_id TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    run_id TEXT,
    correlation_id TEXT NOT NULL,
    argument_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    PRIMARY KEY (scope, gate_id)
);
CREATE TABLE IF NOT EXISTS grants (
    scope TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    gate_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    role TEXT NOT NULL,
    permission_class TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    repository TEXT,
    branch_prefix TEXT,
    authorised_status TEXT NOT NULL,
    expires_at TEXT,
    argument_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, grant_id),
    UNIQUE (scope, gate_id)
);
CREATE TABLE IF NOT EXISTS grant_uses (
    scope TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    argument_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, grant_id, tool_name, argument_hash)
);
CREATE TABLE IF NOT EXISTS repository_protections (
    scope TEXT NOT NULL,
    repository TEXT NOT NULL,
    branch TEXT NOT NULL,
    PRIMARY KEY (scope, repository, branch)
);
CREATE TABLE IF NOT EXISTS github_bindings (
    scope TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    run_id TEXT,
    issue_number INTEGER,
    issue_content_hash TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, binding_id)
);
CREATE TABLE IF NOT EXISTS github_refs (
    scope TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    ref TEXT NOT NULL,
    sha TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, binding_id, ref)
);
CREATE TABLE IF NOT EXISTS github_pull_requests (
    scope TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_ref TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    merge_sha TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, binding_id)
);
CREATE TABLE IF NOT EXISTS github_checks (
    scope TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (scope, binding_id, check_id)
);
CREATE TABLE IF NOT EXISTS github_releases (
    scope TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    target_sha TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, binding_id, release_id)
);
CREATE TABLE IF NOT EXISTS github_notes (
    scope TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    note_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, binding_id, note_id)
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

    def register_role(
        self,
        scope: str,
        name: str,
        tools: Sequence[str],
        *,
        permission_class: PermissionClass = "implementation",
    ) -> RoleRecord: ...

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

    def allow_delegation(
        self, scope: str, parent_workflow: str, child_workflow: str
    ) -> DelegationRecord: ...

    def start_child_run(
        self,
        scope: str,
        parent_run_id: str,
        workflow_name: str,
        correlation_id: str,
        *,
        work_item_id: str | None = None,
        mutating: bool = False,
        run_id: str | None = None,
    ) -> RunRecord: ...

    def pause_for_child(self, scope: str, parent_run_id: str, child_run_id: str) -> RunRecord: ...

    def finish_child_run(
        self, scope: str, child_run_id: str, outcome: ChildOutcomeStatus
    ) -> ChildOutcome: ...

    def get_child_outcome(self, scope: str, child_run_id: str) -> ChildOutcome: ...

    def take_child_outcome(
        self, scope: str, parent_run_id: str, child_run_id: str
    ) -> ChildOutcome: ...

    def register_tool_permission(
        self, scope: str, tool_name: str, permission_class: PermissionClass
    ) -> ToolPermissionRecord: ...

    def open_gate(
        self,
        scope: str,
        name: str,
        *,
        role: str,
        work_item_id: str,
        stage: str,
        tools: Sequence[str],
        correlation_id: str,
        argument_hash: str,
        run_id: str | None = None,
        gate_id: str | None = None,
    ) -> GateRecord: ...

    def decide_gate(
        self,
        scope: str,
        gate_id: str,
        *,
        approved: bool,
        repository: str | None = None,
        branch_prefix: str | None = None,
        expires_at: str | None = None,
        grant_id: str | None = None,
    ) -> tuple[GateRecord, GrantRecord | None]: ...

    def get_gate(self, scope: str, gate_id: str) -> GateRecord: ...

    def get_grant(self, scope: str, grant_id: str) -> GrantRecord: ...

    def authorize_grant(
        self,
        scope: str,
        grant_id: str,
        *,
        tool_name: str,
        argument_hash: str,
        repository: str | None,
        branch: str | None,
    ) -> Literal["authorized", "replayed"]: ...

    def finish_grant_use(
        self,
        scope: str,
        grant_id: str,
        tool_name: str,
        argument_hash: str,
        *,
        status: Literal["completed", "failed"],
    ) -> None: ...

    def protect_branch(self, scope: str, repository: str, branch: str) -> str: ...

    def protected_branches(self, scope: str, repository: str) -> tuple[str, ...]: ...

    def bind_github_repository(
        self,
        scope: str,
        *,
        repository: str,
        work_item_id: str,
        run_id: str | None = None,
        issue_number: int | None = None,
        binding_id: str | None = None,
    ) -> GithubBindingRecord: ...

    def get_github_binding(self, scope: str, binding_id: str) -> GithubBindingRecord: ...

    def note_github_issue(
        self, scope: str, binding_id: str, *, content_hash: str
    ) -> GithubBindingRecord: ...

    def find_github_ref(self, scope: str, binding_id: str, ref: str) -> GithubRefRecord | None: ...

    def list_github_refs(self, scope: str, binding_id: str) -> tuple[GithubRefRecord, ...]: ...

    def record_github_ref(
        self, scope: str, binding_id: str, *, ref: str, sha: str
    ) -> GithubRefRecord: ...

    def find_github_pull_request(
        self, scope: str, binding_id: str
    ) -> GithubPullRequestRecord | None: ...

    def record_github_pull_request(
        self,
        scope: str,
        binding_id: str,
        *,
        number: int,
        head_ref: str,
        head_sha: str,
        base_ref: str,
        state: Literal["open", "closed", "merged"],
        merge_sha: str | None = None,
    ) -> GithubPullRequestRecord: ...

    def list_github_checks(self, scope: str, binding_id: str) -> tuple[GithubCheckRecord, ...]: ...

    def record_github_check(
        self,
        scope: str,
        binding_id: str,
        *,
        check_id: str,
        conclusion: str,
        head_sha: str,
    ) -> GithubCheckRecord: ...

    def list_github_releases(
        self, scope: str, binding_id: str
    ) -> tuple[GithubReleaseRecord, ...]: ...

    def record_github_release(
        self,
        scope: str,
        binding_id: str,
        *,
        release_id: str,
        tag: str,
        target_sha: str,
    ) -> GithubReleaseRecord: ...

    def record_github_note(
        self,
        scope: str,
        binding_id: str,
        *,
        note_id: str,
        kind: Literal["comment", "review"],
    ) -> GithubNoteRecord: ...

    def publish_github_signal(
        self,
        scope: str,
        *,
        name: str,
        target_run_id: str,
        correlation_id: str,
        outcome_status: str,
        subject_kind: str,
        subject_id: str,
    ) -> SignalRecord: ...

    def take_github_signal(self, scope: str, run_id: str, signal_id: str) -> SignalRecord: ...

    def pause_for_signal(self, scope: str, run_id: str) -> RunRecord: ...

    def list_signals(
        self, scope: str, *, target_run_id: str | None = None
    ) -> tuple[SignalRecord, ...]: ...


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
        self.delegations: dict[tuple[str, str, str], DelegationRecord] = {}
        self.outcomes: dict[tuple[str, str], ChildOutcome] = {}
        self.signals: dict[tuple[str, str], SignalRecord] = {}
        self.waits: dict[tuple[str, str, str], ChildWaitRecord] = {}
        self.tool_permissions: dict[tuple[str, str], ToolPermissionRecord] = {}
        self.gates: dict[tuple[str, str], GateRecord] = {}
        self.grants: dict[tuple[str, str], GrantRecord] = {}
        self.grant_uses: dict[tuple[str, str, str, str], GrantUseRecord] = {}


class MemoryRegistry(GithubRegistryMethods):
    """In-process registry. A new memory store is already version 2."""

    def __init__(self) -> None:
        self._tables = _MemoryTables()
        self._github = GithubBindings(MemoryGithubStorage(self._tables.signals), self)

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

    def register_role(
        self,
        scope: str,
        name: str,
        tools: Sequence[str],
        *,
        permission_class: PermissionClass = "implementation",
    ) -> RoleRecord:
        _require_organisation(scope in self._tables.organisations)
        token = require_token(name, "Directory name is not allowed.")
        if (scope, token) in self._tables.roles:
            raise RegistryRejected("Role is already registered.")
        record = RoleRecord(
            scope=scope,
            name=token,
            tools=require_tools(tools),
            permission_class=require_permission_class(permission_class),
        )
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
        self._invalidate_grants(scope, work_item_id, updated.status)
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
        return self._insert_run(
            scope,
            workflow_name,
            correlation_id,
            parent_run_id=parent_run_id,
            work_item_id=work_item_id,
            mutating=mutating,
            run_id=run_id,
            authorized_child=False,
        )

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

    def allow_delegation(
        self, scope: str, parent_workflow: str, child_workflow: str
    ) -> DelegationRecord:
        parent = self.get_workflow(scope, parent_workflow)
        child = self.get_workflow(scope, child_workflow)
        if parent.name == child.name:
            raise RegistryRejected("Workflow delegation is not allowed.")
        key = (scope, parent.name, child.name)
        existing = self._tables.delegations.get(key)
        if existing is not None:
            return existing
        record = DelegationRecord(
            scope=scope, parent_workflow=parent.name, child_workflow=child.name
        )
        self._tables.delegations[key] = record
        return record

    def start_child_run(
        self,
        scope: str,
        parent_run_id: str,
        workflow_name: str,
        correlation_id: str,
        *,
        work_item_id: str | None = None,
        mutating: bool = False,
        run_id: str | None = None,
    ) -> RunRecord:
        parent = self.get_run(scope, parent_run_id)
        if parent.status not in {"running", "paused"}:
            raise RegistryRejected("Parent run is not active.")
        child_workflow = self.get_workflow(scope, workflow_name)
        if (scope, parent.workflow_name, child_workflow.name) not in self._tables.delegations:
            raise RegistryRejected("Workflow delegation is not allowed.")
        return self._insert_run(
            scope,
            child_workflow.name,
            correlation_id,
            parent_run_id=parent.run_id,
            work_item_id=work_item_id,
            mutating=mutating,
            run_id=run_id,
            authorized_child=True,
        )

    def pause_for_child(self, scope: str, parent_run_id: str, child_run_id: str) -> RunRecord:
        parent = self.get_run(scope, parent_run_id)
        child = self.get_run(scope, child_run_id)
        self._require_open_child(parent, child)
        if parent.checkpoint_id is None:
            raise RegistryRejected("A paused run needs a checkpoint.")
        key = (scope, parent.run_id, child.run_id)
        if key not in self._tables.waits:
            self._tables.waits[key] = ChildWaitRecord(
                scope=scope,
                parent_run_id=parent.run_id,
                child_run_id=child.run_id,
                checkpoint_id=parent.checkpoint_id,
                created_at=_now(),
            )
        if parent.status == "paused":
            return parent
        return self.transition_run(scope, parent.run_id, "paused")

    def finish_child_run(
        self, scope: str, child_run_id: str, outcome: ChildOutcomeStatus
    ) -> ChildOutcome:
        status = require_child_outcome(outcome)
        existing = self._tables.outcomes.get((scope, child_run_id))
        if existing is not None:
            if existing.status != status:
                raise RegistryRejected("Child outcome is already recorded.")
            return existing
        child = self.get_run(scope, child_run_id)
        if child.parent_run_id is None:
            raise RegistryRejected("Run is not a child.")
        if child.status in {"completed", "failed"}:
            raise RegistryRejected("Child outcome is already recorded.")
        self.transition_run(scope, child.run_id, outcome_to_run_status(status))
        finished = self.get_run(scope, child.run_id)
        record = ChildOutcome(
            scope=scope,
            child_run_id=finished.run_id,
            parent_run_id=child.parent_run_id,
            workflow_name=finished.workflow_name,
            correlation_id=finished.correlation_id,
            status=status,
            created_at=_now(),
        )
        self._tables.outcomes[(scope, finished.run_id)] = record
        signal_id = _identifier(None)
        self._tables.signals[(scope, signal_id)] = SignalRecord(
            scope=scope,
            signal_id=signal_id,
            name=SIGNAL_CHILD_FINISHED,
            target_run_id=child.parent_run_id,
            source_run_id=finished.run_id,
            correlation_id=finished.correlation_id,
            outcome_status=status,
            created_at=record.created_at,
        )
        self._record_child_outcome(record)
        return record

    def get_child_outcome(self, scope: str, child_run_id: str) -> ChildOutcome:
        found = self._tables.outcomes.get((scope, child_run_id))
        if found is None:
            raise RegistryRejected("Child outcome is not available.")
        return found

    def take_child_outcome(self, scope: str, parent_run_id: str, child_run_id: str) -> ChildOutcome:
        outcome = self.get_child_outcome(scope, child_run_id)
        if outcome.parent_run_id != parent_run_id:
            raise RegistryRejected("Child outcome is not available.")
        signal = self._signal_for(scope, parent_run_id, child_run_id)
        parent = self.get_run(scope, parent_run_id)
        if signal.consumed_at is None:
            self._tables.signals[(scope, signal.signal_id)] = signal.model_copy(
                update={"consumed_at": _now()}
            )
            if parent.status == "paused":
                self.transition_run(scope, parent.run_id, "running")
        return outcome

    def register_tool_permission(
        self, scope: str, tool_name: str, permission_class: PermissionClass
    ) -> ToolPermissionRecord:
        _require_organisation(scope in self._tables.organisations)
        token = require_token(tool_name, "Directory name is not allowed.")
        classification = require_permission_class(permission_class)
        key = (scope, token)
        existing = self._tables.tool_permissions.get(key)
        if existing is not None:
            if existing.permission_class != classification:
                raise RegistryRejected("Permission class is not allowed.")
            return existing
        record = ToolPermissionRecord(scope=scope, tool_name=token, permission_class=classification)
        self._tables.tool_permissions[key] = record
        return record

    def open_gate(
        self,
        scope: str,
        name: str,
        *,
        role: str,
        work_item_id: str,
        stage: str,
        tools: Sequence[str],
        correlation_id: str,
        argument_hash: str,
        run_id: str | None = None,
        gate_id: str | None = None,
    ) -> GateRecord:
        _require_organisation(scope in self._tables.organisations)
        role_record = self.get_role(scope, role)
        item = self.get_work_item(scope, work_item_id)
        if item.status == "cancelled":
            raise RegistryRejected("Work item transition is not allowed.")
        if run_id is not None:
            self.get_run(scope, run_id)
        requested = assert_gate_tools(role_record, tools, self._permission_map(scope))
        identifier = _identifier(gate_id)
        if (scope, identifier) in self._tables.gates:
            raise RegistryRejected("Directory name is not allowed.")
        record = GateRecord(
            scope=scope,
            gate_id=identifier,
            name=require_token(name, "Directory name is not allowed."),
            role=role_record.name,
            work_item_id=item.work_item_id,
            stage=require_token(stage, "Directory name is not allowed."),
            tools=requested,
            decision="pending",
            run_id=run_id,
            correlation_id=require_token(correlation_id, "Directory name is not allowed."),
            argument_hash=require_argument_hash(argument_hash),
            created_at=_now(),
        )
        self._tables.gates[(scope, identifier)] = record
        return record

    def decide_gate(
        self,
        scope: str,
        gate_id: str,
        *,
        approved: bool,
        repository: str | None = None,
        branch_prefix: str | None = None,
        expires_at: str | None = None,
        grant_id: str | None = None,
    ) -> tuple[GateRecord, GrantRecord | None]:
        gate = self.get_gate(scope, gate_id)
        if gate.decision != "pending":
            return self._repeat_gate_decision(scope, gate, approved=approved)
        if not approved:
            rejected = gate.model_copy(update={"decision": "rejected", "decided_at": _now()})
            self._tables.gates[(scope, gate.gate_id)] = rejected
            self._record_gate_decision(rejected, approved=False)
            return rejected, None
        grant = self._issue_grant(
            scope,
            gate,
            repository=repository,
            branch_prefix=branch_prefix,
            expires_at=expires_at,
            grant_id=grant_id,
        )
        approved_gate = gate.model_copy(
            update={"decision": "approved", "decided_at": grant.created_at}
        )
        self._tables.gates[(scope, gate.gate_id)] = approved_gate
        self._tables.grants[(scope, grant.grant_id)] = grant
        self._record_gate_decision(approved_gate, approved=True)
        return approved_gate, grant

    def get_gate(self, scope: str, gate_id: str) -> GateRecord:
        found = self._tables.gates.get((scope, gate_id))
        if found is None:
            raise RegistryRejected("Gate is unknown.")
        return found

    def get_grant(self, scope: str, grant_id: str) -> GrantRecord:
        found = self._tables.grants.get((scope, grant_id))
        if found is None:
            raise RegistryRejected("Grant is unknown.")
        return found

    def authorize_grant(
        self,
        scope: str,
        grant_id: str,
        *,
        tool_name: str,
        argument_hash: str,
        repository: str | None,
        branch: str | None,
    ) -> Literal["authorized", "replayed"]:
        grant = self._current_grant(
            scope,
            grant_id,
            tool_name=tool_name,
            argument_hash=argument_hash,
            repository=repository,
            branch=branch,
        )
        key = (scope, grant.grant_id, tool_name, argument_hash)
        existing = self._tables.grant_uses.get(key)
        if existing is not None:
            self._record_grant_use(grant, tool_name, argument_hash, outcome="rejected")
            return "replayed"
        self._tables.grant_uses[key] = GrantUseRecord(
            scope=scope,
            grant_id=grant.grant_id,
            tool_name=tool_name,
            argument_hash=argument_hash,
            status="started",
            created_at=_now(),
        )
        self._record_grant_use(grant, tool_name, argument_hash, outcome="pending_approval")
        return "authorized"

    def finish_grant_use(
        self,
        scope: str,
        grant_id: str,
        tool_name: str,
        argument_hash: str,
        *,
        status: Literal["completed", "failed"],
    ) -> None:
        key = (scope, grant_id, tool_name, argument_hash)
        current = self._tables.grant_uses.get(key)
        if current is None or current.status != "started":
            raise RegistryRejected("Grant is not active.")
        self._tables.grant_uses[key] = current.model_copy(update={"status": status})
        grant = self.get_grant(scope, grant_id)
        outcome: Literal["completed", "failed"] = status
        self._record_grant_use(grant, tool_name, argument_hash, outcome=outcome)

    def _insert_run(
        self,
        scope: str,
        workflow_name: str,
        correlation_id: str,
        *,
        parent_run_id: str | None,
        work_item_id: str | None,
        mutating: bool,
        run_id: str | None,
        authorized_child: bool,
    ) -> RunRecord:
        workflow = self.get_workflow(scope, workflow_name)
        identifier = _identifier(run_id)
        if (scope, identifier) in self._tables.runs:
            raise RegistryRejected("Directory name is not allowed.")
        runs = {key[1]: row for key, row in self._tables.runs.items() if key[0] == scope}
        assert_run_parent(runs, run_id=identifier, parent_run_id=parent_run_id)
        item = self._claim_work_item(
            scope,
            work_item_id,
            run_id=identifier,
            mutating=mutating,
            parent_run_id=parent_run_id if authorized_child else None,
            runs=runs,
        )
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
        if not authorized_child:
            self._guard_mutating(scope, record)
        self._tables.runs[(scope, identifier)] = record
        if item is not None:
            self._tables.work_items[(scope, item.work_item_id)] = item
        return record

    def _claim_work_item(
        self,
        scope: str,
        work_item_id: str | None,
        *,
        run_id: str,
        mutating: bool,
        parent_run_id: str | None = None,
        runs: Mapping[str, RunRecord] | None = None,
    ) -> WorkItemRecord | None:
        if work_item_id is None:
            return None
        item = self.get_work_item(scope, work_item_id)
        if not mutating:
            assert_readable_work_item(item.status)
            return None
        if (
            item.active_run_id is not None
            and parent_run_id is not None
            and runs is not None
            and holder_authorizes_child(
                runs, parent_run_id=parent_run_id, holder_id=item.active_run_id
            )
        ):
            self._guard_child_mutating(scope, item.work_item_id, item.active_run_id)
            return None
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
        released = item.model_copy(
            update={"active_run_id": None, "status": status, "updated_at": _now()}
        )
        self._tables.work_items[(scope, item.work_item_id)] = released
        if released.status != item.status:
            self._invalidate_grants(scope, item.work_item_id, released.status)

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

    def _guard_child_mutating(self, scope: str, work_item_id: str, holder_id: str) -> None:
        runs = {key[1]: row for key, row in self._tables.runs.items() if key[0] == scope}
        for existing in runs.values():
            if existing.work_item_id != work_item_id or not existing.mutating:
                continue
            if existing.status not in {"running", "paused"}:
                continue
            if existing.run_id == holder_id:
                continue
            parent_id = existing.parent_run_id
            if parent_id is not None and holder_authorizes_child(
                runs, parent_run_id=parent_id, holder_id=holder_id
            ):
                continue
            raise RegistryRejected("A mutating run is already active for this work item.")

    def _require_open_child(self, parent: RunRecord, child: RunRecord) -> None:
        if child.parent_run_id != parent.run_id:
            raise RegistryRejected("Workflow delegation is not allowed.")
        if child.status in {"completed", "failed"}:
            raise RegistryRejected("Child run is already finished.")
        if parent.status not in {"running", "paused"}:
            raise RegistryRejected("Parent run is not active.")

    def _signal_for(self, scope: str, parent_run_id: str, child_run_id: str) -> SignalRecord:
        matches = [
            row
            for key, row in self._tables.signals.items()
            if key[0] == scope
            and row.target_run_id == parent_run_id
            and row.source_run_id == child_run_id
            and row.name == SIGNAL_CHILD_FINISHED
        ]
        if len(matches) != 1:
            raise RegistryRejected("Child outcome is not available.")
        return matches[0]

    def _permission_map(self, scope: str) -> dict[str, PermissionClass]:
        return {
            key[1]: row.permission_class
            for key, row in self._tables.tool_permissions.items()
            if key[0] == scope
        }

    def _repeat_gate_decision(
        self, scope: str, gate: GateRecord, *, approved: bool
    ) -> tuple[GateRecord, GrantRecord | None]:
        if gate.decision == "approved" and approved:
            for key, grant in self._tables.grants.items():
                if key[0] == scope and grant.gate_id == gate.gate_id:
                    return gate, grant
            raise RegistryRejected("Grant is unknown.")
        if gate.decision == "rejected" and not approved:
            return gate, None
        raise RegistryRejected("Gate decision is already recorded.")

    def _issue_grant(
        self,
        scope: str,
        gate: GateRecord,
        *,
        repository: str | None,
        branch_prefix: str | None,
        expires_at: str | None,
        grant_id: str | None,
    ) -> GrantRecord:
        item = self.get_work_item(scope, gate.work_item_id)
        if item.status == "cancelled":
            raise RegistryRejected("Work item transition is not allowed.")
        role = self.get_role(scope, gate.role)
        assert_gate_tools(role, gate.tools, self._permission_map(scope))
        repository_name = None
        if repository is not None:
            repository_name = self.get_repository(scope, repository).name
        prefix = None if branch_prefix is None else require_branch(branch_prefix)
        expiry = None
        if expires_at is not None:
            expiry = require_timestamp(expires_at)
            if timestamp_reached(expiry, _now()):
                raise RegistryRejected("Grant expiry is not allowed.")
        identifier = _identifier(grant_id)
        if (scope, identifier) in self._tables.grants:
            raise RegistryRejected("Directory name is not allowed.")
        duplicate = False
        for key, row in self._tables.grants.items():
            if key[0] == scope and row.gate_id == gate.gate_id:
                duplicate = True
                break
        if duplicate:
            raise RegistryRejected("Gate decision is already recorded.")
        return GrantRecord(
            scope=scope,
            grant_id=identifier,
            gate_id=gate.gate_id,
            work_item_id=item.work_item_id,
            role=role.name,
            permission_class=role.permission_class,
            tools=gate.tools,
            repository=repository_name,
            branch_prefix=prefix,
            authorised_status=item.status,
            expires_at=expiry,
            argument_hash=gate.argument_hash,
            status="active",
            created_at=_now(),
        )

    def _current_grant(
        self,
        scope: str,
        grant_id: str,
        *,
        tool_name: str,
        argument_hash: str,
        repository: str | None,
        branch: str | None,
    ) -> GrantRecord:
        grant = self.get_grant(scope, grant_id)
        role = self.get_role(scope, grant.role)
        item = self.get_work_item(scope, grant.work_item_id)
        permission = self._tables.tool_permissions.get((scope, tool_name))
        if grant.status != "active":
            raise RegistryRejected("Grant is not active.")
        if (
            argument_hash != grant.argument_hash
            or tool_name not in grant.tools
            or tool_name not in role.tools
            or role.permission_class != grant.permission_class
            or permission is None
            or permission.permission_class != grant.permission_class
        ):
            raise RegistryRejected("Grant does not allow this tool.")
        if item.status != grant.authorised_status:
            self._mark_grant(grant, "invalidated")
            raise RegistryRejected("Grant is not active.")
        if grant.expires_at is not None and timestamp_reached(grant.expires_at, _now()):
            self._mark_grant(grant, "expired")
            raise RegistryRejected("Grant is not active.")
        if grant.repository is None:
            if repository is not None:
                raise RegistryRejected("Grant does not allow this repository.")
        elif repository != grant.repository:
            raise RegistryRejected("Grant does not allow this repository.")
        if grant.branch_prefix is None:
            if branch is not None:
                raise RegistryRejected("Grant does not allow this branch.")
        elif branch is None or not branch_in_prefix(branch, grant.branch_prefix):
            raise RegistryRejected("Grant does not allow this branch.")
        return grant

    def _mark_grant(self, grant: GrantRecord, status: GrantStatus) -> None:
        self._tables.grants[(grant.scope, grant.grant_id)] = grant.model_copy(
            update={"status": status}
        )

    def _invalidate_grants(self, scope: str, work_item_id: str, status: WorkItemStatus) -> None:
        for key, grant in list(self._tables.grants.items()):
            if key[0] != scope or grant.work_item_id != work_item_id:
                continue
            if grant.status == "active" and grant.authorised_status != status:
                self._tables.grants[key] = grant.model_copy(update={"status": "invalidated"})

    def _record_child_outcome(self, outcome: ChildOutcome) -> None:
        ledger_outcome: Literal["completed", "rejected", "failed"] = "failed"
        if outcome.status == "completed":
            ledger_outcome = "completed"
        elif outcome.status in {"rejected", "escalated"}:
            ledger_outcome = "rejected"
        self.append_ledger(
            outcome.scope,
            AuditEvent(
                correlation_id=outcome.correlation_id,
                agent_name=None,
                tool_name=outcome.workflow_name,
                approval="not_required",
                outcome=ledger_outcome,
                argument_hash=argument_hash(
                    {"child_run_id": outcome.child_run_id, "status": outcome.status}
                ),
                kind="child_outcome",
            ),
            run_id=outcome.child_run_id,
        )

    def _record_gate_decision(self, gate: GateRecord, *, approved: bool) -> None:
        self.append_ledger(
            gate.scope,
            AuditEvent(
                correlation_id=gate.correlation_id,
                agent_name=gate.role,
                tool_name=gate.name,
                approval="approved" if approved else "rejected",
                outcome="completed" if approved else "rejected",
                argument_hash=gate.argument_hash,
                kind="approval_accepted" if approved else "approval_rejected",
            ),
            run_id=gate.run_id,
        )

    def _record_grant_use(
        self,
        grant: GrantRecord,
        tool_name: str,
        digest: str,
        *,
        outcome: Literal["completed", "pending_approval", "rejected", "failed"],
    ) -> None:
        approval: Literal["approved", "rejected"] = (
            "rejected" if outcome == "rejected" else "approved"
        )
        self.append_ledger(
            grant.scope,
            AuditEvent(
                correlation_id=grant.grant_id,
                agent_name=grant.role,
                tool_name=tool_name,
                approval=approval,
                outcome=outcome,
                argument_hash=digest,
                kind="grant_used",
            ),
            run_id=None,
        )


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


class SqliteRegistry(GithubRegistryMethods):
    """Registry tables on the workflow store connection."""

    def __init__(self, connection: sqlite3.Connection, commit: Callable[[], None]) -> None:
        self._connection = connection
        self._commit = commit
        self._github = GithubBindings(SqliteGithubStorage(connection, commit), self)

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

    def register_role(
        self,
        scope: str,
        name: str,
        tools: Sequence[str],
        *,
        permission_class: PermissionClass = "implementation",
    ) -> RoleRecord:
        self.get_organisation(scope)
        token = require_token(name, "Directory name is not allowed.")
        if self._role(scope, token) is not None:
            raise RegistryRejected("Role is already registered.")
        record = RoleRecord(
            scope=scope,
            name=token,
            tools=require_tools(tools),
            permission_class=require_permission_class(permission_class),
        )
        self._connection.execute(
            """
            INSERT INTO roles (scope, name, tools_json, permission_class)
            VALUES (?, ?, ?, ?)
            """,
            (scope, token, _dump(record.tools), record.permission_class),
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
        self._save_work_item(updated, commit=False)
        self._invalidate_grants(scope, work_item_id, updated.status, commit=False)
        self._commit()
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

    def allow_delegation(
        self, scope: str, parent_workflow: str, child_workflow: str
    ) -> DelegationRecord:
        parent = self.get_workflow(scope, parent_workflow)
        child = self.get_workflow(scope, child_workflow)
        if parent.name == child.name:
            raise RegistryRejected("Workflow delegation is not allowed.")
        self._connection.execute(
            """
            INSERT INTO workflow_delegations (scope, parent_workflow, child_workflow)
            VALUES (?, ?, ?)
            ON CONFLICT (scope, parent_workflow, child_workflow) DO NOTHING
            """,
            (scope, parent.name, child.name),
        )
        self._commit()
        return DelegationRecord(scope=scope, parent_workflow=parent.name, child_workflow=child.name)

    def start_child_run(
        self,
        scope: str,
        parent_run_id: str,
        workflow_name: str,
        correlation_id: str,
        *,
        work_item_id: str | None = None,
        mutating: bool = False,
        run_id: str | None = None,
    ) -> RunRecord:
        parent = self.get_run(scope, parent_run_id)
        if parent.status not in {"running", "paused"}:
            raise RegistryRejected("Parent run is not active.")
        child_workflow = self.get_workflow(scope, workflow_name)
        edge = self._one(
            """
            SELECT parent_workflow FROM workflow_delegations
            WHERE scope = ? AND parent_workflow = ? AND child_workflow = ?
            """,
            (scope, parent.workflow_name, child_workflow.name),
        )
        if edge is None:
            raise RegistryRejected("Workflow delegation is not allowed.")
        identifier = _identifier(run_id)
        if self._maybe_run(scope, identifier) is not None:
            raise RegistryRejected("Directory name is not allowed.")
        runs = {row.run_id: row for row in self.list_runs(scope)}
        assert_run_parent(runs, run_id=identifier, parent_run_id=parent.run_id)
        item = self._prepare_work_item(
            scope,
            work_item_id,
            run_id=identifier,
            mutating=mutating,
            parent_run_id=parent.run_id,
        )
        stamp = _now()
        record = RunRecord(
            scope=scope,
            run_id=identifier,
            workflow_name=child_workflow.name,
            correlation_id=require_token(correlation_id, "Directory name is not allowed."),
            status="running",
            parent_run_id=parent.run_id,
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

    def pause_for_child(self, scope: str, parent_run_id: str, child_run_id: str) -> RunRecord:
        parent = self.get_run(scope, parent_run_id)
        child = self.get_run(scope, child_run_id)
        _require_open_child(parent, child)
        if parent.checkpoint_id is None:
            raise RegistryRejected("A paused run needs a checkpoint.")
        self._connection.execute(
            """
            INSERT INTO child_waits (
                scope, parent_run_id, child_run_id, checkpoint_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (scope, parent_run_id, child_run_id) DO NOTHING
            """,
            (scope, parent.run_id, child.run_id, parent.checkpoint_id, _now()),
        )
        self._commit()
        if parent.status == "paused":
            return parent
        return self.transition_run(scope, parent.run_id, "paused")

    def finish_child_run(
        self, scope: str, child_run_id: str, outcome: ChildOutcomeStatus
    ) -> ChildOutcome:
        status = require_child_outcome(outcome)
        existing = self._outcome(scope, child_run_id)
        if existing is not None:
            if existing.status != status:
                raise RegistryRejected("Child outcome is already recorded.")
            return existing
        child = self.get_run(scope, child_run_id)
        if child.parent_run_id is None:
            raise RegistryRejected("Run is not a child.")
        if child.status in {"completed", "failed"}:
            raise RegistryRejected("Child outcome is already recorded.")
        self.transition_run(scope, child.run_id, outcome_to_run_status(status))
        finished = self.get_run(scope, child.run_id)
        parent_run_id = child.parent_run_id
        record = ChildOutcome(
            scope=scope,
            child_run_id=finished.run_id,
            parent_run_id=parent_run_id,
            workflow_name=finished.workflow_name,
            correlation_id=finished.correlation_id,
            status=status,
            created_at=_now(),
        )
        signal_id = _identifier(None)
        self._connection.execute(
            """
            INSERT INTO child_outcomes (
                scope, child_run_id, parent_run_id, workflow_name, correlation_id,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.scope,
                record.child_run_id,
                record.parent_run_id,
                record.workflow_name,
                record.correlation_id,
                record.status,
                record.created_at,
            ),
        )
        self._connection.execute(
            """
            INSERT INTO signals (
                scope, signal_id, name, target_run_id, source_run_id, correlation_id,
                outcome_status, created_at, consumed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                scope,
                signal_id,
                SIGNAL_CHILD_FINISHED,
                parent_run_id,
                finished.run_id,
                finished.correlation_id,
                status,
                record.created_at,
            ),
        )
        self._commit()
        _record_child_outcome(self, record)
        return record

    def get_child_outcome(self, scope: str, child_run_id: str) -> ChildOutcome:
        found = self._outcome(scope, child_run_id)
        if found is None:
            raise RegistryRejected("Child outcome is not available.")
        return found

    def take_child_outcome(self, scope: str, parent_run_id: str, child_run_id: str) -> ChildOutcome:
        outcome = self.get_child_outcome(scope, child_run_id)
        if outcome.parent_run_id != parent_run_id:
            raise RegistryRejected("Child outcome is not available.")
        signal = self._signal_row(scope, parent_run_id, child_run_id)
        parent = self.get_run(scope, parent_run_id)
        if signal.consumed_at is None:
            self._connection.execute(
                "UPDATE signals SET consumed_at = ? WHERE scope = ? AND signal_id = ?",
                (_now(), scope, signal.signal_id),
            )
            self._commit()
            if parent.status == "paused":
                self.transition_run(scope, parent.run_id, "running")
        return outcome

    def register_tool_permission(
        self, scope: str, tool_name: str, permission_class: PermissionClass
    ) -> ToolPermissionRecord:
        self.get_organisation(scope)
        token = require_token(tool_name, "Directory name is not allowed.")
        classification = require_permission_class(permission_class)
        existing = self._permission(scope, token)
        if existing is not None:
            if existing.permission_class != classification:
                raise RegistryRejected("Permission class is not allowed.")
            return existing
        record = ToolPermissionRecord(scope=scope, tool_name=token, permission_class=classification)
        self._connection.execute(
            """
            INSERT INTO tool_permissions (scope, tool_name, permission_class)
            VALUES (?, ?, ?)
            """,
            (scope, token, classification),
        )
        self._commit()
        return record

    def open_gate(
        self,
        scope: str,
        name: str,
        *,
        role: str,
        work_item_id: str,
        stage: str,
        tools: Sequence[str],
        correlation_id: str,
        argument_hash: str,
        run_id: str | None = None,
        gate_id: str | None = None,
    ) -> GateRecord:
        self.get_organisation(scope)
        role_record = self.get_role(scope, role)
        item = self.get_work_item(scope, work_item_id)
        if item.status == "cancelled":
            raise RegistryRejected("Work item transition is not allowed.")
        if run_id is not None:
            self.get_run(scope, run_id)
        requested = assert_gate_tools(role_record, tools, self._permission_map(scope))
        identifier = _identifier(gate_id)
        if self._gate_row(scope, identifier) is not None:
            raise RegistryRejected("Directory name is not allowed.")
        record = GateRecord(
            scope=scope,
            gate_id=identifier,
            name=require_token(name, "Directory name is not allowed."),
            role=role_record.name,
            work_item_id=item.work_item_id,
            stage=require_token(stage, "Directory name is not allowed."),
            tools=requested,
            decision="pending",
            run_id=run_id,
            correlation_id=require_token(correlation_id, "Directory name is not allowed."),
            argument_hash=require_argument_hash(argument_hash),
            created_at=_now(),
        )
        self._insert_gate(record)
        return record

    def decide_gate(
        self,
        scope: str,
        gate_id: str,
        *,
        approved: bool,
        repository: str | None = None,
        branch_prefix: str | None = None,
        expires_at: str | None = None,
        grant_id: str | None = None,
    ) -> tuple[GateRecord, GrantRecord | None]:
        gate = self.get_gate(scope, gate_id)
        if gate.decision != "pending":
            return self._repeat_gate_decision(scope, gate, approved=approved)
        if not approved:
            rejected = gate.model_copy(update={"decision": "rejected", "decided_at": _now()})
            self._save_gate(rejected)
            _record_gate_decision(self, rejected, approved=False)
            return rejected, None
        grant = self._issue_grant(
            scope,
            gate,
            repository=repository,
            branch_prefix=branch_prefix,
            expires_at=expires_at,
            grant_id=grant_id,
        )
        approved_gate = gate.model_copy(
            update={"decision": "approved", "decided_at": grant.created_at}
        )
        self._save_gate(approved_gate, commit=False)
        self._insert_grant(grant, commit=False)
        self._commit()
        _record_gate_decision(self, approved_gate, approved=True)
        return approved_gate, grant

    def get_gate(self, scope: str, gate_id: str) -> GateRecord:
        found = self._gate_row(scope, gate_id)
        if found is None:
            raise RegistryRejected("Gate is unknown.")
        return found

    def get_grant(self, scope: str, grant_id: str) -> GrantRecord:
        found = self._grant_row(scope, grant_id)
        if found is None:
            raise RegistryRejected("Grant is unknown.")
        return found

    def authorize_grant(
        self,
        scope: str,
        grant_id: str,
        *,
        tool_name: str,
        argument_hash: str,
        repository: str | None,
        branch: str | None,
    ) -> Literal["authorized", "replayed"]:
        grant = self._current_grant(
            scope,
            grant_id,
            tool_name=tool_name,
            argument_hash=argument_hash,
            repository=repository,
            branch=branch,
        )
        existing = self._grant_use(scope, grant.grant_id, tool_name, argument_hash)
        if existing is not None:
            _record_grant_use(self, grant, tool_name, argument_hash, outcome="rejected")
            return "replayed"
        self._connection.execute(
            """
            INSERT INTO grant_uses (
                scope, grant_id, tool_name, argument_hash, status, created_at
            ) VALUES (?, ?, ?, ?, 'started', ?)
            """,
            (scope, grant.grant_id, tool_name, argument_hash, _now()),
        )
        self._commit()
        _record_grant_use(self, grant, tool_name, argument_hash, outcome="pending_approval")
        return "authorized"

    def finish_grant_use(
        self,
        scope: str,
        grant_id: str,
        tool_name: str,
        argument_hash: str,
        *,
        status: Literal["completed", "failed"],
    ) -> None:
        current = self._grant_use(scope, grant_id, tool_name, argument_hash)
        if current is None or current.status != "started":
            raise RegistryRejected("Grant is not active.")
        self._connection.execute(
            """
            UPDATE grant_uses SET status = ?
            WHERE scope = ? AND grant_id = ? AND tool_name = ? AND argument_hash = ?
            """,
            (status, scope, grant_id, tool_name, argument_hash),
        )
        self._commit()
        _record_grant_use(
            self, self.get_grant(scope, grant_id), tool_name, argument_hash, outcome=status
        )

    def _prepare_work_item(
        self,
        scope: str,
        work_item_id: str | None,
        *,
        run_id: str,
        mutating: bool,
        parent_run_id: str | None = None,
    ) -> WorkItemRecord | None:
        if work_item_id is None:
            return None
        item = self.get_work_item(scope, work_item_id)
        if not mutating:
            assert_readable_work_item(item.status)
            return None
        if item.active_run_id is not None and parent_run_id is not None:
            runs = {row.run_id: row for row in self.list_runs(scope)}
            if holder_authorizes_child(
                runs, parent_run_id=parent_run_id, holder_id=item.active_run_id
            ):
                self._reject_foreign_mutating(runs, item.work_item_id, item.active_run_id)
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
        released = item.model_copy(
            update={"active_run_id": None, "status": status, "updated_at": _now()}
        )
        self._save_work_item(released, commit=commit)
        if released.status != item.status:
            self._invalidate_grants(scope, item.work_item_id, released.status, commit=commit)

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

    def _reject_foreign_mutating(
        self, runs: Mapping[str, RunRecord], work_item_id: str, holder_id: str
    ) -> None:
        for existing in runs.values():
            if existing.work_item_id != work_item_id or not existing.mutating:
                continue
            if existing.status not in {"running", "paused"}:
                continue
            if existing.run_id == holder_id:
                continue
            parent_id = existing.parent_run_id
            if parent_id is not None and holder_authorizes_child(
                runs, parent_run_id=parent_id, holder_id=holder_id
            ):
                continue
            raise RegistryRejected("A mutating run is already active for this work item.")

    def _invalidate_grants(
        self, scope: str, work_item_id: str, status: WorkItemStatus, *, commit: bool
    ) -> None:
        self._connection.execute(
            """
            UPDATE grants SET status = 'invalidated'
            WHERE scope = ? AND work_item_id = ? AND status = 'active'
                AND authorised_status != ?
            """,
            (scope, work_item_id, status),
        )
        if commit:
            self._commit()

    def _outcome(self, scope: str, child_run_id: str) -> ChildOutcome | None:
        row = self._one(
            "SELECT * FROM child_outcomes WHERE scope = ? AND child_run_id = ?",
            (scope, child_run_id),
        )
        if row is None:
            return None
        return _outcome_from_row(row)

    def _signal_row(self, scope: str, parent_run_id: str, child_run_id: str) -> SignalRecord:
        rows = self._fetch(
            """
            SELECT * FROM signals
            WHERE scope = ? AND target_run_id = ? AND source_run_id = ? AND name = ?
            """,
            (scope, parent_run_id, child_run_id, SIGNAL_CHILD_FINISHED),
        )
        if len(rows) != 1:
            raise RegistryRejected("Child outcome is not available.")
        return _signal_from_row(rows[0])

    def _permission(self, scope: str, tool_name: str) -> ToolPermissionRecord | None:
        row = self._one(
            "SELECT * FROM tool_permissions WHERE scope = ? AND tool_name = ?",
            (scope, tool_name),
        )
        if row is None:
            return None
        return ToolPermissionRecord(
            scope=scope,
            tool_name=tool_name,
            permission_class=require_permission_class(str(row["permission_class"])),
        )

    def _permission_map(self, scope: str) -> dict[str, PermissionClass]:
        rows = self._fetch(
            "SELECT tool_name, permission_class FROM tool_permissions WHERE scope = ?",
            (scope,),
        )
        return {
            str(row["tool_name"]): require_permission_class(str(row["permission_class"]))
            for row in rows
        }

    def _gate_row(self, scope: str, gate_id: str) -> GateRecord | None:
        row = self._one("SELECT * FROM gates WHERE scope = ? AND gate_id = ?", (scope, gate_id))
        if row is None:
            return None
        return _gate_from_row(row)

    def _grant_row(self, scope: str, grant_id: str) -> GrantRecord | None:
        row = self._one("SELECT * FROM grants WHERE scope = ? AND grant_id = ?", (scope, grant_id))
        if row is None:
            return None
        return _grant_from_row(row)

    def _grant_for_gate(self, scope: str, gate_id: str) -> GrantRecord | None:
        row = self._one("SELECT * FROM grants WHERE scope = ? AND gate_id = ?", (scope, gate_id))
        if row is None:
            return None
        return _grant_from_row(row)

    def _grant_use(
        self, scope: str, grant_id: str, tool_name: str, argument_hash: str
    ) -> GrantUseRecord | None:
        row = self._one(
            """
            SELECT * FROM grant_uses
            WHERE scope = ? AND grant_id = ? AND tool_name = ? AND argument_hash = ?
            """,
            (scope, grant_id, tool_name, argument_hash),
        )
        if row is None:
            return None
        return _grant_use_from_row(row)

    def _insert_gate(self, record: GateRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO gates (
                scope, gate_id, name, role, work_item_id, stage, tools_json, decision,
                run_id, correlation_id, argument_hash, created_at, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _gate_values(record),
        )
        self._commit()

    def _save_gate(self, record: GateRecord, *, commit: bool = True) -> None:
        self._connection.execute(
            """
            UPDATE gates SET decision = ?, decided_at = ?
            WHERE scope = ? AND gate_id = ?
            """,
            (record.decision, record.decided_at, record.scope, record.gate_id),
        )
        if commit:
            self._commit()

    def _insert_grant(self, record: GrantRecord, *, commit: bool = True) -> None:
        self._connection.execute(
            """
            INSERT INTO grants (
                scope, grant_id, gate_id, work_item_id, role, permission_class, tools_json,
                repository, branch_prefix, authorised_status, expires_at, argument_hash,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _grant_values(record),
        )
        if commit:
            self._commit()

    def _repeat_gate_decision(
        self, scope: str, gate: GateRecord, *, approved: bool
    ) -> tuple[GateRecord, GrantRecord | None]:
        if gate.decision == "approved" and approved:
            grant = self._grant_for_gate(scope, gate.gate_id)
            if grant is None:
                raise RegistryRejected("Grant is unknown.")
            return gate, grant
        if gate.decision == "rejected" and not approved:
            return gate, None
        raise RegistryRejected("Gate decision is already recorded.")

    def _issue_grant(
        self,
        scope: str,
        gate: GateRecord,
        *,
        repository: str | None,
        branch_prefix: str | None,
        expires_at: str | None,
        grant_id: str | None,
    ) -> GrantRecord:
        item = self.get_work_item(scope, gate.work_item_id)
        if item.status == "cancelled":
            raise RegistryRejected("Work item transition is not allowed.")
        role = self.get_role(scope, gate.role)
        assert_gate_tools(role, gate.tools, self._permission_map(scope))
        repository_name = None
        if repository is not None:
            repository_name = self.get_repository(scope, repository).name
        prefix = None if branch_prefix is None else require_branch(branch_prefix)
        expiry = None
        if expires_at is not None:
            expiry = require_timestamp(expires_at)
            if timestamp_reached(expiry, _now()):
                raise RegistryRejected("Grant expiry is not allowed.")
        identifier = _identifier(grant_id)
        existing_grant = self._grant_row(scope, identifier)
        if existing_grant is None:
            existing_grant = self._grant_for_gate(scope, gate.gate_id)
        if existing_grant is not None:
            raise RegistryRejected("Gate decision is already recorded.")
        return GrantRecord(
            scope=scope,
            grant_id=identifier,
            gate_id=gate.gate_id,
            work_item_id=item.work_item_id,
            role=role.name,
            permission_class=role.permission_class,
            tools=gate.tools,
            repository=repository_name,
            branch_prefix=prefix,
            authorised_status=item.status,
            expires_at=expiry,
            argument_hash=gate.argument_hash,
            status="active",
            created_at=_now(),
        )

    def _current_grant(
        self,
        scope: str,
        grant_id: str,
        *,
        tool_name: str,
        argument_hash: str,
        repository: str | None,
        branch: str | None,
    ) -> GrantRecord:
        grant = self.get_grant(scope, grant_id)
        role = self.get_role(scope, grant.role)
        item = self.get_work_item(scope, grant.work_item_id)
        permission = self._permission(scope, tool_name)
        if grant.status != "active":
            raise RegistryRejected("Grant is not active.")
        if (
            argument_hash != grant.argument_hash
            or tool_name not in grant.tools
            or tool_name not in role.tools
            or role.permission_class != grant.permission_class
            or permission is None
            or permission.permission_class != grant.permission_class
        ):
            raise RegistryRejected("Grant does not allow this tool.")
        if item.status != grant.authorised_status:
            self._mark_grant(grant, "invalidated")
            raise RegistryRejected("Grant is not active.")
        if grant.expires_at is not None and timestamp_reached(grant.expires_at, _now()):
            self._mark_grant(grant, "expired")
            raise RegistryRejected("Grant is not active.")
        if grant.repository is None:
            if repository is not None:
                raise RegistryRejected("Grant does not allow this repository.")
        elif repository != grant.repository:
            raise RegistryRejected("Grant does not allow this repository.")
        if grant.branch_prefix is None:
            if branch is not None:
                raise RegistryRejected("Grant does not allow this branch.")
        elif branch is None or not branch_in_prefix(branch, grant.branch_prefix):
            raise RegistryRejected("Grant does not allow this branch.")
        return grant

    def _mark_grant(self, grant: GrantRecord, status: GrantStatus) -> None:
        self._connection.execute(
            "UPDATE grants SET status = ? WHERE scope = ? AND grant_id = ?",
            (status, grant.scope, grant.grant_id),
        )
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
        permission_class=require_permission_class(str(row["permission_class"])),
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


def _require_open_child(parent: RunRecord, child: RunRecord) -> None:
    if child.parent_run_id != parent.run_id:
        raise RegistryRejected("Workflow delegation is not allowed.")
    if child.status in {"completed", "failed"}:
        raise RegistryRejected("Child run is already finished.")
    if parent.status not in {"running", "paused"}:
        raise RegistryRejected("Parent run is not active.")


def _record_child_outcome(registry: PlatformRegistry, outcome: ChildOutcome) -> None:
    ledger_outcome: Literal["completed", "rejected", "failed"] = "failed"
    if outcome.status == "completed":
        ledger_outcome = "completed"
    elif outcome.status in {"rejected", "escalated"}:
        ledger_outcome = "rejected"
    registry.append_ledger(
        outcome.scope,
        AuditEvent(
            correlation_id=outcome.correlation_id,
            agent_name=None,
            tool_name=outcome.workflow_name,
            approval="not_required",
            outcome=ledger_outcome,
            argument_hash=argument_hash(
                {"child_run_id": outcome.child_run_id, "status": outcome.status}
            ),
            kind="child_outcome",
        ),
        run_id=outcome.child_run_id,
    )


def _record_gate_decision(registry: PlatformRegistry, gate: GateRecord, *, approved: bool) -> None:
    registry.append_ledger(
        gate.scope,
        AuditEvent(
            correlation_id=gate.correlation_id,
            agent_name=gate.role,
            tool_name=gate.name,
            approval="approved" if approved else "rejected",
            outcome="completed" if approved else "rejected",
            argument_hash=gate.argument_hash,
            kind="approval_accepted" if approved else "approval_rejected",
        ),
        run_id=gate.run_id,
    )


def _record_grant_use(
    registry: PlatformRegistry,
    grant: GrantRecord,
    tool_name: str,
    digest: str,
    *,
    outcome: Literal["completed", "pending_approval", "rejected", "failed"],
) -> None:
    approval: Literal["approved", "rejected"] = "rejected" if outcome == "rejected" else "approved"
    registry.append_ledger(
        grant.scope,
        AuditEvent(
            correlation_id=grant.grant_id,
            agent_name=grant.role,
            tool_name=tool_name,
            approval=approval,
            outcome=outcome,
            argument_hash=digest,
            kind="grant_used",
        ),
        run_id=None,
    )


def _outcome_from_row(row: sqlite3.Row) -> ChildOutcome:
    try:
        return ChildOutcome(
            scope=str(row["scope"]),
            child_run_id=str(row["child_run_id"]),
            parent_run_id=str(row["parent_run_id"]),
            workflow_name=str(row["workflow_name"]),
            correlation_id=str(row["correlation_id"]),
            status=require_child_outcome(str(row["status"])),
            created_at=str(row["created_at"]),
        )
    except ValidationError:
        raise CheckpointCompatibilityError() from None


def _signal_from_row(row: sqlite3.Row) -> SignalRecord:
    return signal_from_row(row)


def _gate_from_row(row: sqlite3.Row) -> GateRecord:
    try:
        return GateRecord(
            scope=str(row["scope"]),
            gate_id=str(row["gate_id"]),
            name=str(row["name"]),
            role=str(row["role"]),
            work_item_id=str(row["work_item_id"]),
            stage=str(row["stage"]),
            tools=_load_names(row["tools_json"]),
            decision=str(row["decision"]),  # type: ignore[arg-type]
            run_id=None if row["run_id"] is None else str(row["run_id"]),
            correlation_id=str(row["correlation_id"]),
            argument_hash=str(row["argument_hash"]),
            created_at=str(row["created_at"]),
            decided_at=None if row["decided_at"] is None else str(row["decided_at"]),
        )
    except ValidationError:
        raise CheckpointCompatibilityError() from None


def _grant_from_row(row: sqlite3.Row) -> GrantRecord:
    try:
        return GrantRecord(
            scope=str(row["scope"]),
            grant_id=str(row["grant_id"]),
            gate_id=str(row["gate_id"]),
            work_item_id=str(row["work_item_id"]),
            role=str(row["role"]),
            permission_class=require_permission_class(str(row["permission_class"])),
            tools=_load_names(row["tools_json"]),
            repository=None if row["repository"] is None else str(row["repository"]),
            branch_prefix=None if row["branch_prefix"] is None else str(row["branch_prefix"]),
            authorised_status=str(row["authorised_status"]),  # type: ignore[arg-type]
            expires_at=None if row["expires_at"] is None else str(row["expires_at"]),
            argument_hash=str(row["argument_hash"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            created_at=str(row["created_at"]),
        )
    except ValidationError:
        raise CheckpointCompatibilityError() from None


def _grant_use_from_row(row: sqlite3.Row) -> GrantUseRecord:
    try:
        return GrantUseRecord(
            scope=str(row["scope"]),
            grant_id=str(row["grant_id"]),
            tool_name=str(row["tool_name"]),
            argument_hash=str(row["argument_hash"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            created_at=str(row["created_at"]),
        )
    except ValidationError:
        raise CheckpointCompatibilityError() from None


def _gate_values(record: GateRecord) -> tuple[object, ...]:
    return (
        record.scope,
        record.gate_id,
        record.name,
        record.role,
        record.work_item_id,
        record.stage,
        _dump(record.tools),
        record.decision,
        record.run_id,
        record.correlation_id,
        record.argument_hash,
        record.created_at,
        record.decided_at,
    )


def _grant_values(record: GrantRecord) -> tuple[object, ...]:
    return (
        record.scope,
        record.grant_id,
        record.gate_id,
        record.work_item_id,
        record.role,
        record.permission_class,
        _dump(record.tools),
        record.repository,
        record.branch_prefix,
        record.authorised_status,
        record.expires_at,
        record.argument_hash,
        record.status,
        record.created_at,
    )


def schema_objects(connection: sqlite3.Connection) -> set[str]:
    """Return table and index names present in a workflow database."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def v2_schema_ready(connection: sqlite3.Connection) -> bool:
    """A version 2 file has the core registry tables and indexes.

    Delegation, gate, and grant tables are added by ``ensure_v2_extensions``.
    A missing core table still fails closed.
    """
    present = schema_objects(connection)
    return set(V2_CORE_TABLES).issubset(present) and set(V2_INDEXES).issubset(present)


def ensure_v2_extensions(connection: sqlite3.Connection) -> None:
    """Add phase 3, 4, and 5 tables to an existing version 2 file.

    Checkpoint payloads are not rewritten. Core tables that are already missing
    stay missing so a damaged file is not rebuilt.
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflow_delegations (
            scope TEXT NOT NULL,
            parent_workflow TEXT NOT NULL,
            child_workflow TEXT NOT NULL,
            PRIMARY KEY (scope, parent_workflow, child_workflow)
        );
        CREATE TABLE IF NOT EXISTS child_outcomes (
            scope TEXT NOT NULL,
            child_run_id TEXT NOT NULL,
            parent_run_id TEXT NOT NULL,
            workflow_name TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scope, child_run_id)
        );
        CREATE TABLE IF NOT EXISTS signals (
            scope TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            name TEXT NOT NULL,
            target_run_id TEXT NOT NULL,
            source_run_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            outcome_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT,
            subject_kind TEXT,
            subject_id TEXT,
            PRIMARY KEY (scope, signal_id)
        );
        CREATE TABLE IF NOT EXISTS child_waits (
            scope TEXT NOT NULL,
            parent_run_id TEXT NOT NULL,
            child_run_id TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scope, parent_run_id, child_run_id)
        );
        CREATE TABLE IF NOT EXISTS tool_permissions (
            scope TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            permission_class TEXT NOT NULL,
            PRIMARY KEY (scope, tool_name)
        );
        CREATE TABLE IF NOT EXISTS gates (
            scope TEXT NOT NULL,
            gate_id TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            tools_json TEXT NOT NULL,
            decision TEXT NOT NULL,
            run_id TEXT,
            correlation_id TEXT NOT NULL,
            argument_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            PRIMARY KEY (scope, gate_id)
        );
        CREATE TABLE IF NOT EXISTS grants (
            scope TEXT NOT NULL,
            grant_id TEXT NOT NULL,
            gate_id TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            role TEXT NOT NULL,
            permission_class TEXT NOT NULL,
            tools_json TEXT NOT NULL,
            repository TEXT,
            branch_prefix TEXT,
            authorised_status TEXT NOT NULL,
            expires_at TEXT,
            argument_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scope, grant_id),
            UNIQUE (scope, gate_id)
        );
        CREATE TABLE IF NOT EXISTS grant_uses (
            scope TEXT NOT NULL,
            grant_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            argument_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scope, grant_id, tool_name, argument_hash)
        );
        CREATE TABLE IF NOT EXISTS repository_protections (
            scope TEXT NOT NULL,
            repository TEXT NOT NULL,
            branch TEXT NOT NULL,
            PRIMARY KEY (scope, repository, branch)
        );
        CREATE TABLE IF NOT EXISTS github_bindings (
            scope TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            repository TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            run_id TEXT,
            issue_number INTEGER,
            issue_content_hash TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scope, binding_id)
        );
        CREATE TABLE IF NOT EXISTS github_refs (
            scope TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            ref TEXT NOT NULL,
            sha TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scope, binding_id, ref)
        );
        CREATE TABLE IF NOT EXISTS github_pull_requests (
            scope TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            number INTEGER NOT NULL,
            head_ref TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            base_ref TEXT NOT NULL,
            state TEXT NOT NULL,
            merge_sha TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, binding_id)
        );
        CREATE TABLE IF NOT EXISTS github_checks (
            scope TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            check_id TEXT NOT NULL,
            conclusion TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (scope, binding_id, check_id)
        );
        CREATE TABLE IF NOT EXISTS github_releases (
            scope TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            release_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            target_sha TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scope, binding_id, release_id)
        );
        CREATE TABLE IF NOT EXISTS github_notes (
            scope TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            note_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scope, binding_id, note_id)
        );
        """
    )
    signal_columns = {
        str(column[1]) for column in connection.execute("PRAGMA table_info(signals)").fetchall()
    }
    if "subject_kind" not in signal_columns:
        connection.execute("ALTER TABLE signals ADD COLUMN subject_kind TEXT")
    if "subject_id" not in signal_columns:
        connection.execute("ALTER TABLE signals ADD COLUMN subject_id TEXT")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(roles)").fetchall()}
    if "permission_class" not in columns:
        connection.execute(
            """
            ALTER TABLE roles
            ADD COLUMN permission_class TEXT NOT NULL DEFAULT 'implementation'
            """
        )
    index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'runs_one_mutating'"
    ).fetchone()
    definition = "" if index is None or index[0] is None else str(index[0])
    if "parent_run_id IS NULL" not in definition:
        connection.execute("DROP INDEX IF EXISTS runs_one_mutating")
        connection.execute(
            """
            CREATE UNIQUE INDEX runs_one_mutating ON runs(scope, work_item_id)
            WHERE mutating = 1 AND parent_run_id IS NULL AND status IN ('running', 'paused')
            """
        )
