"""Typed records for platform state version 2.

These models store identifiers, statuses, and hashes. They do not store prompts,
tool arguments, credentials, or repository file contents.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

PLATFORM_STATE_VERSION = "2"
SUPPORTED_PLATFORM_STATE_VERSIONS = ("1", "2")

RunStatus = Literal["running", "paused", "completed", "failed"]
WorkItemStatus = Literal["draft", "ready", "active", "blocked", "done", "cancelled"]

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    "running": frozenset({"paused", "completed", "failed"}),
    "paused": frozenset({"running", "completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}
_WORK_TRANSITIONS: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    "draft": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"cancelled"}),
    "active": frozenset({"blocked", "done", "cancelled"}),
    "blocked": frozenset({"ready", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}
_MUTATING_START: frozenset[WorkItemStatus] = frozenset({"ready", "blocked", "active"})
_PARENT_LIMIT = 16


class CheckpointCompatibilityError(Exception):
    """Stored state cannot be resumed by this platform version."""

    def __init__(self) -> None:
        super().__init__("Stored workflow state is incompatible with this platform version.")


class StateMigrationRequired(Exception):
    """A version 1 store was asked for a version 2 primitive."""

    def __init__(self) -> None:
        super().__init__(
            "Platform state version 1 must be migrated before directory, ledger, or run operations."
        )


class RegistryRejected(Exception):
    """A directory, run, or work-item rule failed closed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LedgerRecord(BaseModel):
    """One durable audit event. Argument values are not included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    event_id: str
    recorded_at: str
    correlation_id: str
    run_id: str | None = None
    agent_name: str | None = None
    tool_name: str
    approval: Literal["not_required", "pending", "approved", "rejected"]
    outcome: Literal["completed", "pending_approval", "rejected", "failed"]
    argument_hash: str
    kind: Literal[
        "tool",
        "checkpoint_created",
        "workflow_paused",
        "workflow_resumed",
        "approval_accepted",
        "approval_rejected",
        "side_effect_attempted",
        "side_effect_completed",
        "side_effect_failed",
    ] = "tool"


class RunRecord(BaseModel):
    """One execution of one catalogued workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    run_id: str
    workflow_name: str
    correlation_id: str
    status: RunStatus
    parent_run_id: str | None = None
    checkpoint_id: str | None = None
    work_item_id: str | None = None
    mutating: bool
    created_at: str
    updated_at: str


class OrganisationRecord(BaseModel):
    """The directory root for one store scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    name: str
    policy_version: str
    created_at: str
    updated_at: str


class RoleRecord(BaseModel):
    """A role and the tool names it may be granted. Tools are not executed here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    name: str
    tools: tuple[str, ...] = ()


class RepositoryRecord(BaseModel):
    """An allowlisted repository. The record does not connect to the remote."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    name: str
    remote: str
    default_branch: str


class WorkflowCatalogEntry(BaseModel):
    """A workflow name the organisation is allowed to start."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    name: str
    graph_signature_hash: str | None = None


class TeamRecord(BaseModel):
    """A team, the roles it holds, and the workflows it may start."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    team_id: str
    name: str
    roles: tuple[str, ...]
    workflows: tuple[str, ...] = ()
    delegations: tuple[str, ...] = ()


class WorkItemRecord(BaseModel):
    """Backlog state for one piece of work. Acceptance text is stored as a hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    work_item_id: str
    item_type: str
    title: str
    status: WorkItemStatus
    parent_work_item_id: str | None = None
    active_run_id: str | None = None
    acceptance_hash: str
    created_at: str
    updated_at: str


def require_token(value: str, message: str) -> str:
    """Accept an identifier. Reject blank, padded, or oversized text."""
    if _TOKEN.fullmatch(value) is None:
        raise RegistryRejected(message)
    return value


def require_label(value: str, message: str) -> str:
    """Accept a short human label. Reject control characters and blank text."""
    if (
        not value.strip()
        or value != value.strip()
        or len(value) > 200
        or any(character < " " for character in value)
    ):
        raise RegistryRejected(message)
    return value


def require_argument_hash(value: str) -> str:
    """Accept a sha256 hex digest or the existing lifecycle sentinel."""
    if value == "lifecycle" or _HASH.fullmatch(value) is not None:
        return value
    raise RegistryRejected("Ledger argument hash is not allowed.")


def require_tools(tools: Sequence[str]) -> tuple[str, ...]:
    """Return tool names in order. Duplicates and blank names fail closed."""
    collected: list[str] = []
    for tool_name in tools:
        require_token(tool_name, "Directory name is not allowed.")
        if tool_name in collected:
            raise RegistryRejected("Directory name is not allowed.")
        collected.append(tool_name)
    return tuple(collected)


def require_repository_remote(remote: str) -> str:
    """Allow an https URL or an scp-style git remote without embedded credentials."""
    if not remote or any(character.isspace() for character in remote):
        raise RegistryRejected("Repository remote is not allowlisted.")
    if "://" in remote:
        parsed = urlsplit(remote)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or not parsed.path
            or parsed.path == "/"
        ):
            raise RegistryRejected("Repository remote is not allowlisted.")
        return remote
    if remote.startswith("git@"):
        host, separator, path = remote[len("git@") :].partition(":")
        if separator and host and "/" in path and ".." not in path.split("/"):
            return remote
    raise RegistryRejected("Repository remote is not allowlisted.")


def require_branch(branch: str) -> str:
    """Accept a branch name without parent-directory segments."""
    if _BRANCH.fullmatch(branch) is None or ".." in branch.split("/"):
        raise RegistryRejected("Directory name is not allowed.")
    return branch


def require_signature(signature: str) -> str:
    """Accept a graph signature hash recorded from a built workflow."""
    return require_token(signature, "Workflow graph signature does not match the checkpoint.")


def assert_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Reject a status change that is not in the run machine."""
    if target not in _RUN_TRANSITIONS[current]:
        raise RegistryRejected("Run transition is not allowed.")


def assert_work_item_transition(
    current: WorkItemStatus,
    target: WorkItemStatus,
    *,
    active_run_id: str | None,
) -> None:
    """Reject a work-item change while a mutating run holds it, or off the machine."""
    if active_run_id is not None or target not in _WORK_TRANSITIONS[current]:
        raise RegistryRejected("Work item transition is not allowed.")


def assert_mutating_start(status: WorkItemStatus, *, active_run_id: str | None) -> None:
    """A mutating run starts only from a free ready, blocked, or idle active item."""
    if active_run_id is not None:
        raise RegistryRejected("A mutating run is already active for this work item.")
    if status not in _MUTATING_START:
        raise RegistryRejected("Work item transition is not allowed.")


def assert_readable_work_item(status: WorkItemStatus) -> None:
    """A read-only run may observe any item that has not been cancelled."""
    if status == "cancelled":
        raise RegistryRejected("Work item transition is not allowed.")


def assert_parent_chain(
    records: Mapping[str, WorkItemRecord],
    *,
    work_item_id: str,
    parent_work_item_id: str | None,
) -> None:
    """Reject a missing parent, a self parent, or a cycle."""
    if parent_work_item_id is None:
        return
    if parent_work_item_id == work_item_id:
        raise RegistryRejected("Work item cannot be its own parent.")
    seen = {work_item_id}
    current = parent_work_item_id
    for _ in range(_PARENT_LIMIT):
        if current in seen:
            raise RegistryRejected("Parent work item chain is not allowed.")
        parent = records.get(current)
        if parent is None:
            raise RegistryRejected("Parent work item is unknown.")
        seen.add(current)
        if parent.parent_work_item_id is None:
            return
        current = parent.parent_work_item_id
    raise RegistryRejected("Parent work item chain is not allowed.")


def assert_run_parent(
    records: Mapping[str, RunRecord],
    *,
    run_id: str,
    parent_run_id: str | None,
) -> None:
    """Reject a missing parent run or a cycle. The parent is an identity, not a scheduler."""
    if parent_run_id is None:
        return
    if parent_run_id == run_id:
        raise RegistryRejected("Run parent chain is not allowed.")
    seen = {run_id}
    current = parent_run_id
    for _ in range(_PARENT_LIMIT):
        if current in seen:
            raise RegistryRejected("Run parent chain is not allowed.")
        parent = records.get(current)
        if parent is None:
            raise RegistryRejected("Parent run is unknown.")
        seen.add(current)
        if parent.parent_run_id is None:
            return
        current = parent.parent_run_id
    raise RegistryRejected("Run parent chain is not allowed.")
