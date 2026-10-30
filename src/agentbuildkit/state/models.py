"""Typed records for platform state version 2.

These models store identifiers, statuses, and hashes. They do not store prompts,
tool arguments, credentials, or repository file contents.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

PLATFORM_STATE_VERSION = "2"
SUPPORTED_PLATFORM_STATE_VERSIONS = ("1", "2")

RunStatus = Literal["running", "paused", "completed", "failed"]
WorkItemStatus = Literal["draft", "ready", "active", "blocked", "done", "cancelled"]
PermissionClass = Literal["implementation", "merge", "release"]
ChildOutcomeStatus = Literal["completed", "failed", "rejected", "escalated"]
GateDecisionState = Literal["pending", "approved", "rejected"]
GrantStatus = Literal["active", "expired", "invalidated"]
GrantUseStatus = Literal["started", "completed", "failed"]
SIGNAL_CHILD_FINISHED = "child_finished"
SIGNAL_GITHUB_BRANCH_CREATED = "github_branch_created"
SIGNAL_GITHUB_PULL_REQUEST_OPENED = "github_pull_request_opened"
SIGNAL_GITHUB_CHECK_COMPLETED = "github_check_completed"
SIGNAL_GITHUB_REVIEW_SUBMITTED = "github_review_submitted"
SIGNAL_GITHUB_PULL_REQUEST_MERGED = "github_pull_request_merged"
SIGNAL_GITHUB_RELEASE_PUBLISHED = "github_release_published"
SIGNAL_IMPLEMENTATION_STARTED = "implementation_started"
SIGNAL_IMPLEMENTATION_COMPLETED = "implementation_completed"
SIGNAL_IMPLEMENTATION_FAILED = "implementation_failed"
SignalName = Literal[
    "child_finished",
    "github_branch_created",
    "github_pull_request_opened",
    "github_check_completed",
    "github_review_submitted",
    "github_pull_request_merged",
    "github_release_published",
    "implementation_started",
    "implementation_completed",
    "implementation_failed",
]
GithubSignalStatus = Literal[
    "created",
    "opened",
    "success",
    "failure",
    "neutral",
    "cancelled",
    "timed_out",
    "action_required",
    "skipped",
    "stale",
    "submitted",
    "merged",
    "published",
    "pending",
]
ImplementationSignalStatus = Literal["started", "completed", "failed", "cancelled", "expired"]
SignalOutcome = ChildOutcomeStatus | GithubSignalStatus | ImplementationSignalStatus
_IMPLEMENTATION_SIGNAL_NAMES = frozenset(
    {
        SIGNAL_IMPLEMENTATION_STARTED,
        SIGNAL_IMPLEMENTATION_COMPLETED,
        SIGNAL_IMPLEMENTATION_FAILED,
    }
)
_GITHUB_SIGNAL_NAMES = frozenset(
    {
        SIGNAL_GITHUB_BRANCH_CREATED,
        SIGNAL_GITHUB_PULL_REQUEST_OPENED,
        SIGNAL_GITHUB_CHECK_COMPLETED,
        SIGNAL_GITHUB_REVIEW_SUBMITTED,
        SIGNAL_GITHUB_PULL_REQUEST_MERGED,
        SIGNAL_GITHUB_RELEASE_PUBLISHED,
    }
)
_GITHUB_SIGNAL_STATUSES = frozenset(
    {
        "created",
        "opened",
        "success",
        "failure",
        "neutral",
        "cancelled",
        "timed_out",
        "action_required",
        "skipped",
        "stale",
        "submitted",
        "merged",
        "published",
        "pending",
    }
)
_GITHUB_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

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
    """One durable audit event. Argument values are not included.

    ``provider`` and ``model`` are optional. Rows written before those columns
    existed stay readable with both values empty.
    """

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
    provider: str | None = None
    model: str | None = None
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
        "grant_used",
        "child_outcome",
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
    permission_class: PermissionClass = "implementation"


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


class DelegationRecord(BaseModel):
    """An explicit permission for one workflow to start another."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    parent_workflow: str
    child_workflow: str


class ChildOutcome(BaseModel):
    """Typed terminal result of a child run. No prompt or argument text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    child_run_id: str
    parent_run_id: str
    workflow_name: str
    correlation_id: str
    status: ChildOutcomeStatus
    created_at: str


class SignalRecord(BaseModel):
    """A durable wake-up. Child runs, GitHub, and external execution share this record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    signal_id: str
    name: SignalName
    target_run_id: str
    source_run_id: str
    correlation_id: str
    outcome_status: SignalOutcome
    created_at: str
    consumed_at: str | None = None
    subject_kind: str | None = None
    subject_id: str | None = None


class ChildWaitRecord(BaseModel):
    """A parent checkpoint held until one child run finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    parent_run_id: str
    child_run_id: str
    checkpoint_id: str
    created_at: str


class GithubBindingRecord(BaseModel):
    """A work item bound to an allowlisted GitHub repository. Content is not stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    binding_id: str
    repository: str
    work_item_id: str
    run_id: str | None = None
    issue_number: int | None = None
    issue_content_hash: str | None = None
    created_at: str


class GithubRefRecord(BaseModel):
    """A branch ref and the commit SHA GitHub reported for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    binding_id: str
    ref: str
    sha: str
    created_at: str


class GithubPullRequestRecord(BaseModel):
    """Identifiers for one pull request. Title and body are not stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    binding_id: str
    number: int
    head_ref: str
    head_sha: str
    base_ref: str
    state: Literal["open", "closed", "merged"]
    merge_sha: str | None = None
    created_at: str
    updated_at: str


class GithubCheckRecord(BaseModel):
    """One GitHub check identifier and its conclusion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    binding_id: str
    check_id: str
    conclusion: GithubSignalStatus
    head_sha: str
    recorded_at: str


class GithubReleaseRecord(BaseModel):
    """Identifiers for one GitHub release."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    binding_id: str
    release_id: str
    tag: str
    target_sha: str
    created_at: str


class GithubNoteRecord(BaseModel):
    """A pull-request comment or review identifier. The text is not stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    binding_id: str
    note_id: str
    kind: Literal["comment", "review"]
    created_at: str


class ToolPermissionRecord(BaseModel):
    """The single permission class of one tool name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    tool_name: str
    permission_class: PermissionClass


class GateRecord(BaseModel):
    """A named approval stage. The model cannot change its tools or role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    gate_id: str
    name: str
    role: str
    work_item_id: str
    stage: str
    tools: tuple[str, ...]
    decision: GateDecisionState
    run_id: str | None = None
    correlation_id: str
    argument_hash: str
    created_at: str
    decided_at: str | None = None


class GrantRecord(BaseModel):
    """Authority copied from an approved gate. It cannot be widened later."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    grant_id: str
    gate_id: str
    work_item_id: str
    role: str
    permission_class: PermissionClass
    tools: tuple[str, ...]
    repository: str | None = None
    branch_prefix: str | None = None
    authorised_status: WorkItemStatus
    expires_at: str | None = None
    argument_hash: str
    status: GrantStatus
    created_at: str


class GrantUseRecord(BaseModel):
    """One reserved use of a grant. The same arguments are not executed twice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    grant_id: str
    tool_name: str
    argument_hash: str
    status: GrantUseStatus
    created_at: str


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


def require_permission_class(value: str) -> PermissionClass:
    """Accept one of the three permission classes."""
    if value not in {"implementation", "merge", "release"}:
        raise RegistryRejected("Permission class is not allowed.")
    if value == "implementation":
        return "implementation"
    if value == "merge":
        return "merge"
    return "release"


def require_child_outcome(value: str) -> ChildOutcomeStatus:
    """Accept a typed child terminal status."""
    if value == "completed":
        return "completed"
    if value == "failed":
        return "failed"
    if value == "rejected":
        return "rejected"
    if value == "escalated":
        return "escalated"
    raise RegistryRejected("Child outcome is not allowed.")


def outcome_to_run_status(status: ChildOutcomeStatus) -> RunStatus:
    """Map a typed child outcome onto the run status machine."""
    if status == "completed":
        return "completed"
    return "failed"


def require_timestamp(value: str) -> str:
    """Accept a timezone-aware timestamp and return a UTC ISO value."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RegistryRejected("Grant expiry is not allowed.") from None
    if parsed.tzinfo is None:
        raise RegistryRejected("Grant expiry is not allowed.")
    return parsed.astimezone(UTC).isoformat()


def timestamp_reached(expires_at: str, now: str) -> bool:
    """True when ``now`` is at or after ``expires_at``."""
    return datetime.fromisoformat(now) >= datetime.fromisoformat(expires_at)


def branch_in_prefix(branch: str, prefix: str) -> bool:
    """A branch matches a prefix exactly or as a path underneath it."""
    candidate = require_branch(branch)
    root = prefix.rstrip("/")
    if candidate in {prefix, root}:
        return True
    return candidate.startswith(f"{root}/")


def holder_authorizes_child(
    records: Mapping[str, RunRecord],
    *,
    parent_run_id: str,
    holder_id: str,
) -> bool:
    """True when the parent chain is the run that holds the work item."""
    current: str | None = parent_run_id
    seen: set[str] = set()
    while current is not None and current not in seen:
        if current == holder_id:
            return True
        seen.add(current)
        parent = records.get(current)
        if parent is None:
            return False
        current = parent.parent_run_id
    return False


def assert_gate_tools(
    role: RoleRecord,
    tools: Sequence[str],
    permissions: Mapping[str, PermissionClass],
) -> tuple[str, ...]:
    """Tools on a gate must belong to that role and that permission class."""
    requested = require_tools(tools)
    if not requested:
        raise RegistryRejected("Gate tools are not allowed.")
    for tool_name in requested:
        if tool_name not in role.tools:
            raise RegistryRejected("Gate tools are not allowed.")
        registered = permissions.get(tool_name)
        if registered is None or registered != role.permission_class:
            raise RegistryRejected("Gate tools are not allowed.")
    return requested


def github_owner_repo(remote: str) -> tuple[str, str]:
    """Return the owner and repository parsed from an allowlisted GitHub remote."""
    cleaned = require_repository_remote(remote)
    if cleaned.startswith("git@"):
        host, _, raw_path = cleaned[len("git@") :].partition(":")
        if host != "github.com":
            raise RegistryRejected("GitHub repository is not allowlisted.")
        path = raw_path
    else:
        parsed = urlsplit(cleaned)
        if parsed.hostname != "github.com":
            raise RegistryRejected("GitHub repository is not allowlisted.")
        path = parsed.path
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        raise RegistryRejected("GitHub repository is not allowlisted.")
    owner, repo = parts
    if (
        _GITHUB_NAME.fullmatch(owner) is None
        or _GITHUB_NAME.fullmatch(repo) is None
        or owner in {".", ".."}
        or repo in {".", ".."}
    ):
        raise RegistryRejected("GitHub repository is not allowlisted.")
    return owner, repo


def require_sha(value: str) -> str:
    """Accept a full Git commit SHA."""
    lowered = value.lower()
    if _SHA.fullmatch(lowered) is None:
        raise RegistryRejected("GitHub commit is not allowed.")
    return lowered


def require_issue_number(value: object) -> int:
    """Accept a positive GitHub issue or pull request number."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 100_000_000:
        raise RegistryRejected("GitHub issue is not allowed.")
    return value


def assert_implementation_branch(branch: str, protected: set[str]) -> str:
    """Reject a protected, default, or ref-qualified branch."""
    candidate = require_branch(branch)
    if candidate.startswith("refs/") or candidate in protected or candidate == "HEAD":
        raise RegistryRejected("Branch is protected.")
    return candidate


def require_github_text(value: str) -> str:
    """Accept comment text for a governed write. The caller must not store it."""
    if not value.strip() or len(value) > 20_000 or "\x00" in value:
        raise RegistryRejected("GitHub text is not allowed.")
    return value


def require_signal_name(value: str) -> SignalName:
    """Accept a platform-defined signal name."""
    if value == SIGNAL_CHILD_FINISHED:
        return SIGNAL_CHILD_FINISHED
    if value == SIGNAL_GITHUB_BRANCH_CREATED:
        return SIGNAL_GITHUB_BRANCH_CREATED
    if value == SIGNAL_GITHUB_PULL_REQUEST_OPENED:
        return SIGNAL_GITHUB_PULL_REQUEST_OPENED
    if value == SIGNAL_GITHUB_CHECK_COMPLETED:
        return SIGNAL_GITHUB_CHECK_COMPLETED
    if value == SIGNAL_GITHUB_REVIEW_SUBMITTED:
        return SIGNAL_GITHUB_REVIEW_SUBMITTED
    if value == SIGNAL_GITHUB_PULL_REQUEST_MERGED:
        return SIGNAL_GITHUB_PULL_REQUEST_MERGED
    if value == SIGNAL_GITHUB_RELEASE_PUBLISHED:
        return SIGNAL_GITHUB_RELEASE_PUBLISHED
    if value == SIGNAL_IMPLEMENTATION_STARTED:
        return SIGNAL_IMPLEMENTATION_STARTED
    if value == SIGNAL_IMPLEMENTATION_COMPLETED:
        return SIGNAL_IMPLEMENTATION_COMPLETED
    if value == SIGNAL_IMPLEMENTATION_FAILED:
        return SIGNAL_IMPLEMENTATION_FAILED
    raise RegistryRejected("GitHub signal is not allowed.")


def _implementation_outcome(
    outcome: str,
    *,
    subject_kind: str | None,
    subject_id: str | None,
) -> ImplementationSignalStatus:
    """Accept an external-execution outcome. Provider status strings are not outcomes."""
    if subject_kind != "external_execution" or subject_id is None:
        raise RegistryRejected("External execution signal is not allowed.")
    require_token(subject_id, "External execution signal is not allowed.")
    if outcome == "started":
        return "started"
    if outcome == "completed":
        return "completed"
    if outcome == "failed":
        return "failed"
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "expired":
        return "expired"
    raise RegistryRejected("External execution signal is not allowed.")


def is_github_signal(name: SignalName) -> bool:
    """Return whether the signal belongs to the GitHub binding."""
    return name in _GITHUB_SIGNAL_NAMES


def require_signal_outcome(
    name: SignalName,
    outcome: str,
    *,
    subject_kind: str | None,
    subject_id: str | None,
) -> SignalOutcome:
    """Accept the outcome that belongs to this signal name."""
    if name == SIGNAL_CHILD_FINISHED:
        if subject_kind is not None or subject_id is not None:
            raise RegistryRejected("GitHub signal is not allowed.")
        return require_child_outcome(outcome)
    if name in _IMPLEMENTATION_SIGNAL_NAMES:
        return _implementation_outcome(outcome, subject_kind=subject_kind, subject_id=subject_id)
    if name not in _GITHUB_SIGNAL_NAMES:
        raise RegistryRejected("GitHub signal is not allowed.")
    if subject_kind is None or subject_id is None:
        raise RegistryRejected("GitHub signal is not allowed.")
    require_token(subject_kind, "GitHub signal is not allowed.")
    require_token(subject_id, "GitHub signal is not allowed.")
    if outcome not in _GITHUB_SIGNAL_STATUSES:
        raise RegistryRejected("GitHub signal is not allowed.")
    if outcome == "created":
        return "created"
    if outcome == "opened":
        return "opened"
    if outcome == "success":
        return "success"
    if outcome == "failure":
        return "failure"
    if outcome == "neutral":
        return "neutral"
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "timed_out":
        return "timed_out"
    if outcome == "action_required":
        return "action_required"
    if outcome == "skipped":
        return "skipped"
    if outcome == "stale":
        return "stale"
    if outcome == "submitted":
        return "submitted"
    if outcome == "merged":
        return "merged"
    if outcome == "published":
        return "published"
    return "pending"
