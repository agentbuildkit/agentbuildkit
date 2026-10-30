"""Governed GitHub operations for allowlisted repositories.

GitHub is the source of truth for issues, branches, commits, pull requests,
checks, reviews, and releases. The platform stores identifiers and content
hashes. Credentials stay in the GitHub App header provider. The model cannot
supply a URL, a token, a repository remote, or a grant.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
import jwt
from agent_framework import SecretString
from pydantic import BaseModel, ConfigDict, Field

from agentbuildkit.guardrails import (
    ApprovalDecision,
    RepeatedGrantUse,
    ToolEffect,
    execute_tool,
    prepare_tool,
)
from agentbuildkit.integrations.http import HttpClient, HttpPayloadError
from agentbuildkit.observability import AuditLog, argument_hash
from agentbuildkit.state.models import (
    SIGNAL_GITHUB_BRANCH_CREATED,
    SIGNAL_GITHUB_CHECK_COMPLETED,
    SIGNAL_GITHUB_PULL_REQUEST_MERGED,
    SIGNAL_GITHUB_PULL_REQUEST_OPENED,
    SIGNAL_GITHUB_RELEASE_PUBLISHED,
    SIGNAL_GITHUB_REVIEW_SUBMITTED,
    GithubBindingRecord,
    GithubPullRequestRecord,
    GithubRefRecord,
    GithubReleaseRecord,
    PermissionClass,
    RegistryRejected,
    RepositoryRecord,
    assert_implementation_branch,
    github_owner_repo,
    require_github_text,
    require_sha,
    require_token,
)
from agentbuildkit.workflows.persistence import WorkflowStateStore

GITHUB_API_BASE_URL = "https://api.github.com"
TOOL_READ_ISSUE = "github.read_issue"
TOOL_READ_PULL_REQUEST = "github.read_pull_request"
TOOL_READ_CHECK = "github.read_check"
TOOL_CREATE_BRANCH = "github.create_branch"
TOOL_CREATE_PULL_REQUEST = "github.create_pull_request"
TOOL_CREATE_COMMENT = "github.create_comment"
TOOL_CREATE_REVIEW = "github.create_review"
TOOL_MERGE_PULL_REQUEST = "github.merge_pull_request"
TOOL_CREATE_RELEASE = "github.create_release"
GITHUB_READ_TOOLS = (TOOL_READ_ISSUE, TOOL_READ_PULL_REQUEST, TOOL_READ_CHECK)
GITHUB_TOOL_PERMISSIONS: dict[str, PermissionClass] = {
    TOOL_CREATE_BRANCH: "implementation",
    TOOL_CREATE_PULL_REQUEST: "implementation",
    TOOL_CREATE_COMMENT: "implementation",
    TOOL_CREATE_REVIEW: "implementation",
    TOOL_MERGE_PULL_REQUEST: "merge",
    TOOL_CREATE_RELEASE: "release",
}
_REVIEW_EVENTS = {
    "comment": "COMMENT",
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
}
_TERMINAL_CHECKS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "timed_out",
        "action_required",
        "skipped",
        "stale",
    }
)
ReviewEvent = Literal["comment", "approve", "request_changes"]


class GithubError(Exception):
    """A GitHub operation failed closed. The message contains no credential."""


class GithubCredentialError(GithubError):
    """GitHub App credentials were rejected. The message contains no secret."""

    def __init__(self) -> None:
        super().__init__("GitHub App credential was rejected.")


class GithubIssueView(BaseModel):
    """Issue identifiers returned to a workflow. Title and body are not included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    number: int
    state: Literal["open", "closed"]
    content_hash: str


class GithubPullRequestView(BaseModel):
    """Pull request identifiers. Title and body are not included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    number: int
    state: Literal["open", "closed", "merged"]
    head_ref: str
    head_sha: str
    base_ref: str
    merge_sha: str | None = None


class GithubCheckView(BaseModel):
    """One check identifier and conclusion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    conclusion: str
    head_sha: str


class GithubChecksView(BaseModel):
    """Check results for one recorded commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    head_sha: str
    checks: tuple[GithubCheckView, ...]


@dataclass(frozen=True)
class GithubAppCredentials:
    """GitHub App identity. The private key never leaves this object except to sign."""

    app_id: str
    installation_id: str
    private_key: SecretString


class GithubAppAuthorizer:
    """Exchange a GitHub App JWT for an installation token.

    The token is held as a ``SecretString`` and returned only as an authorization
    header. It is not written to the audit log, the ledger, or a checkpoint.
    """

    def __init__(
        self,
        credentials: GithubAppCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._authorization: SecretString | None = None
        self._refresh_after: datetime | None = None

    def __call__(self) -> Mapping[str, str | SecretString]:
        return {"Authorization": self._header()}

    def _header(self) -> SecretString:
        now = datetime.now(UTC)
        cached = self._authorization
        if cached is not None and self._refresh_after is not None and now < self._refresh_after:
            return cached
        minted = _mint_installation_token(self._credentials, transport=self._transport, now=now)
        self._authorization = minted[0]
        self._refresh_after = minted[1]
        return minted[0]


def github_tool_permission(tool_name: str) -> PermissionClass:
    """Return the permission class of a known GitHub write. Unknown names fail closed."""
    found = GITHUB_TOOL_PERMISSIONS.get(tool_name)
    if found is None:
        raise RegistryRejected("Grant does not allow this tool.")
    return found


def register_github_tool_permissions(store: WorkflowStateStore) -> None:
    """Register GitHub write tools in their fixed permission classes."""
    for tool_name, permission in GITHUB_TOOL_PERMISSIONS.items():
        store.register_tool_permission(tool_name, permission)


def branch_arguments(*, repository: str, branch: str, binding_id: str) -> dict[str, str]:
    """Arguments hashed into an implementation or merge grant."""
    return {"binding_id": binding_id, "branch": branch, "repository": repository}


def comment_arguments(
    *, repository: str, branch: str, binding_id: str, body: str
) -> dict[str, str]:
    """Arguments hashed into a governed comment grant. The text is not stored."""
    arguments = branch_arguments(repository=repository, branch=branch, binding_id=binding_id)
    arguments["body"] = body
    return arguments


def review_arguments(
    *,
    repository: str,
    branch: str,
    binding_id: str,
    body: str,
    event: str,
) -> dict[str, str]:
    """Arguments hashed into a governed review grant."""
    arguments = comment_arguments(
        repository=repository, branch=branch, binding_id=binding_id, body=body
    )
    arguments["event"] = event
    return arguments


def release_arguments(
    *, repository: str, binding_id: str, tag: str, target_sha: str
) -> dict[str, str]:
    """Arguments hashed into a release grant. A release grant has no branch."""
    return {
        "binding_id": binding_id,
        "repository": repository,
        "tag": tag,
        "target_sha": target_sha,
    }


def github_http_client(
    *,
    audit_log: AuditLog,
    header_provider: Callable[[], Mapping[str, str | SecretString]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> HttpClient:
    """Build the only HTTP client GitHub operations use."""

    def headers() -> dict[str, str | SecretString]:
        merged: dict[str, str | SecretString] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agentbuildkit",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        merged.update(dict(header_provider()))
        return merged

    return HttpClient(
        name="github",
        base_url=GITHUB_API_BASE_URL,
        audit_log=audit_log,
        header_provider=headers,
        transport=transport,
    )


class GithubService:
    """GitHub reads and governed writes for one organisation store."""

    def __init__(
        self,
        *,
        store: WorkflowStateStore,
        audit_log: AuditLog,
        http: HttpClient,
    ) -> None:
        _assert_github_base_url(http.base_url)
        self._store = store
        self._audit_log = audit_log
        self._http = http

    async def read_issue(
        self, *, binding_id: str, role: str, correlation_id: str
    ) -> GithubIssueView:
        """Read one bound issue. The role must list the read tool. No grant is required."""
        correlation = _correlation(correlation_id)
        self._require_reader(role, TOOL_READ_ISSUE)
        binding, _repository, coordinates = self._target(binding_id)
        if binding.issue_number is None:
            raise RegistryRejected("GitHub issue is not allowed.")
        path = _repo_path(coordinates, f"/issues/{binding.issue_number}")
        payload = await self._http.read(
            path,
            response_type=_IssuePayload,
            operation=TOOL_READ_ISSUE,
            correlation_id=correlation,
        )
        if payload.number != binding.issue_number:
            raise HttpPayloadError(method="GET", path=path)
        content_hash = argument_hash({"body": payload.body or "", "title": payload.title})
        self._store.note_github_issue(binding.binding_id, content_hash=content_hash)
        return GithubIssueView(
            repository=binding.repository,
            number=payload.number,
            state=payload.state,
            content_hash=content_hash,
        )

    async def read_pull_request(
        self, *, binding_id: str, role: str, correlation_id: str
    ) -> GithubPullRequestView:
        """Read the pull request recorded for a binding and refresh its identifiers."""
        correlation = _correlation(correlation_id)
        self._require_reader(role, TOOL_READ_PULL_REQUEST)
        binding, _repository, coordinates = self._target(binding_id)
        current = self._store.find_github_pull_request(binding.binding_id)
        if current is None:
            raise RegistryRejected("GitHub pull request is unknown.")
        path = _repo_path(coordinates, f"/pulls/{current.number}")
        payload = await self._http.read(
            path,
            response_type=_PullPayload,
            operation=TOOL_READ_PULL_REQUEST,
            correlation_id=correlation,
        )
        recorded = self._accept_pull(
            binding,
            current,
            payload,
            path,
            expected_head=current.head_ref,
            expected_sha=None,
            expected_base=current.base_ref,
        )
        if recorded.state == "merged":
            self._signal(
                binding,
                name=SIGNAL_GITHUB_PULL_REQUEST_MERGED,
                outcome="merged",
                subject_kind="pull_request",
                subject_id=str(recorded.number),
                correlation_id=correlation,
            )
        return _pull_view(binding.repository, recorded)

    async def read_checks(
        self, *, binding_id: str, role: str, ref: str, correlation_id: str
    ) -> GithubChecksView:
        """Read check runs for a recorded branch tip."""
        correlation = _correlation(correlation_id)
        self._require_reader(role, TOOL_READ_CHECK)
        binding, _repository, coordinates = self._target(binding_id)
        recorded_ref = self._store.find_github_ref(binding.binding_id, ref)
        if recorded_ref is None:
            raise RegistryRejected("GitHub ref is unknown.")
        path = _repo_path(coordinates, f"/commits/{recorded_ref.sha}/check-runs")
        payload = await self._http.read(
            path,
            response_type=_ChecksPayload,
            operation=TOOL_READ_CHECK,
            correlation_id=correlation,
        )
        views: list[GithubCheckView] = []
        for run in payload.check_runs:
            conclusion = _conclusion(run, path=path, expected_sha=recorded_ref.sha)
            check = self._store.record_github_check(
                binding.binding_id,
                check_id=str(run.id),
                conclusion=conclusion,
                head_sha=recorded_ref.sha,
            )
            if conclusion in _TERMINAL_CHECKS:
                self._signal(
                    binding,
                    name=SIGNAL_GITHUB_CHECK_COMPLETED,
                    outcome=conclusion,
                    subject_kind="check",
                    subject_id=check.check_id,
                    correlation_id=correlation,
                )
            views.append(
                GithubCheckView(
                    check_id=check.check_id,
                    conclusion=check.conclusion,
                    head_sha=check.head_sha,
                )
            )
        return GithubChecksView(
            repository=binding.repository,
            head_sha=recorded_ref.sha,
            checks=tuple(views),
        )

    async def create_branch(
        self,
        *,
        binding_id: str,
        branch: str,
        grant_id: str,
        correlation_id: str,
    ) -> GithubRefRecord | RepeatedGrantUse:
        """Create an implementation branch from the repository default branch."""
        correlation = _correlation(correlation_id)
        binding, repository, coordinates = self._target(binding_id)
        name = self._implementation_branch(repository, branch)
        repository_record = repository
        arguments = branch_arguments(
            repository=binding.repository, branch=name, binding_id=binding.binding_id
        )
        holder: list[GithubRefRecord] = []

        async def perform(repository: str, branch: str, binding_id: str) -> str:
            self._same_branch(
                binding, repository_name=repository, branch=branch, binding_id=binding_id
            )
            if self._store.find_github_ref(binding.binding_id, name) is not None:
                raise RegistryRejected("GitHub ref is not allowed.")
            base_path = _repo_path(
                coordinates, f"/git/ref/heads/{repository_record.default_branch}"
            )
            base = await self._http.send(
                "GET",
                base_path,
                response_type=_RefPayload,
                correlation_id=correlation,
            )
            base_sha = _commit_sha(base.git_object.sha, method="GET", path=base_path)
            created_path = _repo_path(coordinates, "/git/refs")
            created = await self._http.send(
                "POST",
                created_path,
                response_type=_RefPayload,
                json_body=_CreateRefBody(ref=f"refs/heads/{name}", sha=base_sha),
                correlation_id=correlation,
            )
            if created.ref != f"refs/heads/{name}":
                raise HttpPayloadError(method="POST", path=created_path)
            tip = _commit_sha(created.git_object.sha, method="POST", path=created_path)
            record = self._store.record_github_ref(binding.binding_id, ref=name, sha=tip)
            self._signal(
                binding,
                name=SIGNAL_GITHUB_BRANCH_CREATED,
                outcome="created",
                subject_kind="branch",
                subject_id=record.sha,
                correlation_id=correlation,
            )
            holder.append(record)
            return record.sha

        result = await self._governed(
            TOOL_CREATE_BRANCH,
            arguments,
            grant_id=grant_id,
            correlation_id=correlation,
            perform=perform,
        )
        if isinstance(result, RepeatedGrantUse):
            return result
        return holder[0]

    async def create_pull_request(
        self,
        *,
        binding_id: str,
        branch: str,
        grant_id: str,
        correlation_id: str,
    ) -> GithubPullRequestRecord | RepeatedGrantUse:
        """Open a pull request from a recorded implementation branch into the default branch."""
        correlation = _correlation(correlation_id)
        binding, repository, coordinates = self._target(binding_id)
        name = self._implementation_branch(repository, branch)
        ref = self._store.find_github_ref(binding.binding_id, name)
        if ref is None:
            raise RegistryRejected("GitHub ref is unknown.")
        repository_record = repository
        arguments = branch_arguments(
            repository=binding.repository, branch=name, binding_id=binding.binding_id
        )
        holder: list[GithubPullRequestRecord] = []

        async def perform(repository: str, branch: str, binding_id: str) -> str:
            self._same_branch(
                binding, repository_name=repository, branch=branch, binding_id=binding_id
            )
            if self._store.find_github_pull_request(binding.binding_id) is not None:
                raise RegistryRejected("GitHub pull request is not allowed.")
            item = self._store.get_work_item(binding.work_item_id)
            path = _repo_path(coordinates, "/pulls")
            payload = await self._http.send(
                "POST",
                path,
                response_type=_PullPayload,
                json_body=_CreatePullBody(
                    title=item.title,
                    head=name,
                    base=repository_record.default_branch,
                    body=f"Work item {binding.work_item_id}.",
                ),
                correlation_id=correlation,
            )
            recorded = self._accept_pull(
                binding,
                None,
                payload,
                path,
                expected_head=name,
                expected_sha=ref.sha,
                expected_base=repository_record.default_branch,
            )
            self._signal(
                binding,
                name=SIGNAL_GITHUB_PULL_REQUEST_OPENED,
                outcome="opened",
                subject_kind="pull_request",
                subject_id=str(recorded.number),
                correlation_id=correlation,
            )
            holder.append(recorded)
            return str(recorded.number)

        result = await self._governed(
            TOOL_CREATE_PULL_REQUEST,
            arguments,
            grant_id=grant_id,
            correlation_id=correlation,
            perform=perform,
        )
        if isinstance(result, RepeatedGrantUse):
            return result
        return holder[0]

    async def create_comment(
        self,
        *,
        binding_id: str,
        branch: str,
        body: str,
        grant_id: str,
        correlation_id: str,
    ) -> str | RepeatedGrantUse:
        """Post a governed pull request comment. The text is not stored."""
        correlation = _correlation(correlation_id)
        text = require_github_text(body)
        binding, repository, coordinates = self._target(binding_id)
        name = self._implementation_branch(repository, branch)
        pull = self._require_open_pull(binding, name)
        arguments = comment_arguments(
            repository=binding.repository,
            branch=name,
            binding_id=binding.binding_id,
            body=text,
        )
        holder: list[str] = []

        async def perform(repository: str, branch: str, binding_id: str, body: str) -> str:
            self._same_branch(
                binding, repository_name=repository, branch=branch, binding_id=binding_id
            )
            if body != text:
                raise RegistryRejected("GitHub text is not allowed.")
            path = _repo_path(coordinates, f"/issues/{pull.number}/comments")
            payload = await self._http.send(
                "POST",
                path,
                response_type=_IdPayload,
                json_body=_TextBody(body=text),
                correlation_id=correlation,
            )
            note = self._store.record_github_note(
                binding.binding_id, note_id=str(payload.id), kind="comment"
            )
            self._signal(
                binding,
                name=SIGNAL_GITHUB_REVIEW_SUBMITTED,
                outcome="submitted",
                subject_kind="comment",
                subject_id=note.note_id,
                correlation_id=correlation,
            )
            holder.append(note.note_id)
            return note.note_id

        result = await self._governed(
            TOOL_CREATE_COMMENT,
            arguments,
            grant_id=grant_id,
            correlation_id=correlation,
            perform=perform,
        )
        if isinstance(result, RepeatedGrantUse):
            return result
        return holder[0]

    async def create_review(
        self,
        *,
        binding_id: str,
        branch: str,
        body: str,
        event: ReviewEvent,
        grant_id: str,
        correlation_id: str,
    ) -> str | RepeatedGrantUse:
        """Post a governed pull request review. The text is not stored."""
        correlation = _correlation(correlation_id)
        text = require_github_text(body)
        github_event = _REVIEW_EVENTS.get(event)
        if github_event is None:
            raise RegistryRejected("GitHub review is not allowed.")
        binding, repository, coordinates = self._target(binding_id)
        name = self._implementation_branch(repository, branch)
        pull = self._require_open_pull(binding, name)
        arguments = review_arguments(
            repository=binding.repository,
            branch=name,
            binding_id=binding.binding_id,
            body=text,
            event=event,
        )
        holder: list[str] = []

        async def perform(
            repository: str, branch: str, binding_id: str, body: str, event: str
        ) -> str:
            self._same_branch(
                binding, repository_name=repository, branch=branch, binding_id=binding_id
            )
            if body != text or event != review_event:
                raise RegistryRejected("GitHub review is not allowed.")
            path = _repo_path(coordinates, f"/pulls/{pull.number}/reviews")
            payload = await self._http.send(
                "POST",
                path,
                response_type=_IdPayload,
                json_body=_ReviewBody(body=text, event=github_event),
                correlation_id=correlation,
            )
            note = self._store.record_github_note(
                binding.binding_id, note_id=str(payload.id), kind="review"
            )
            self._signal(
                binding,
                name=SIGNAL_GITHUB_REVIEW_SUBMITTED,
                outcome="submitted",
                subject_kind="review",
                subject_id=note.note_id,
                correlation_id=correlation,
            )
            holder.append(note.note_id)
            return note.note_id

        review_event = event
        result = await self._governed(
            TOOL_CREATE_REVIEW,
            arguments,
            grant_id=grant_id,
            correlation_id=correlation,
            perform=perform,
        )
        if isinstance(result, RepeatedGrantUse):
            return result
        return holder[0]

    async def merge_pull_request(
        self,
        *,
        binding_id: str,
        branch: str,
        grant_id: str,
        correlation_id: str,
    ) -> GithubPullRequestRecord | RepeatedGrantUse:
        """Merge a recorded pull request. This requires a merge grant."""
        correlation = _correlation(correlation_id)
        binding, repository, coordinates = self._target(binding_id)
        name = self._implementation_branch(repository, branch)
        arguments = branch_arguments(
            repository=binding.repository, branch=name, binding_id=binding.binding_id
        )
        holder: list[GithubPullRequestRecord] = []

        async def perform(repository: str, branch: str, binding_id: str) -> str:
            self._same_branch(
                binding, repository_name=repository, branch=branch, binding_id=binding_id
            )
            pull = self._require_open_pull(binding, name)
            path = _repo_path(coordinates, f"/pulls/{pull.number}/merge")
            payload = await self._http.send(
                "PUT",
                path,
                response_type=_MergePayload,
                json_body=_MergeBody(),
                correlation_id=correlation,
            )
            if not payload.merged:
                raise HttpPayloadError(method="PUT", path=path)
            merge_sha = _commit_sha(payload.sha, method="PUT", path=path)
            recorded = self._store.record_github_pull_request(
                binding.binding_id,
                number=pull.number,
                head_ref=pull.head_ref,
                head_sha=pull.head_sha,
                base_ref=pull.base_ref,
                state="merged",
                merge_sha=merge_sha,
            )
            self._signal(
                binding,
                name=SIGNAL_GITHUB_PULL_REQUEST_MERGED,
                outcome="merged",
                subject_kind="pull_request",
                subject_id=str(recorded.number),
                correlation_id=correlation,
            )
            holder.append(recorded)
            return merge_sha

        result = await self._governed(
            TOOL_MERGE_PULL_REQUEST,
            arguments,
            grant_id=grant_id,
            correlation_id=correlation,
            perform=perform,
        )
        if isinstance(result, RepeatedGrantUse):
            return result
        return holder[0]

    async def create_release(
        self,
        *,
        binding_id: str,
        tag: str,
        target_sha: str,
        grant_id: str,
        correlation_id: str,
    ) -> GithubReleaseRecord | RepeatedGrantUse:
        """Create a release for a commit the platform has already recorded."""
        correlation = _correlation(correlation_id)
        binding, _repository, coordinates = self._target(binding_id)
        parsed_tag = require_token(tag, "GitHub release is not allowed.")
        digest = self._known_sha(binding.binding_id, target_sha)
        arguments = release_arguments(
            repository=binding.repository,
            binding_id=binding.binding_id,
            tag=parsed_tag,
            target_sha=digest,
        )
        holder: list[GithubReleaseRecord] = []

        async def perform(repository: str, binding_id: str, tag: str, target_sha: str) -> str:
            if (
                repository != binding.repository
                or binding_id != binding.binding_id
                or tag != parsed_tag
                or target_sha != digest
            ):
                raise RegistryRejected("Grant does not allow this tool.")
            for existing in self._store.list_github_releases(binding.binding_id):
                if existing.tag == parsed_tag:
                    raise RegistryRejected("GitHub release is not allowed.")
            path = _repo_path(coordinates, "/releases")
            payload = await self._http.send(
                "POST",
                path,
                response_type=_ReleasePayload,
                json_body=_ReleaseBody(tag_name=parsed_tag, target_commitish=digest),
                correlation_id=correlation,
            )
            if payload.tag_name != parsed_tag or payload.id < 1:
                raise HttpPayloadError(method="POST", path=path)
            record = self._store.record_github_release(
                binding.binding_id,
                release_id=str(payload.id),
                tag=parsed_tag,
                target_sha=digest,
            )
            self._signal(
                binding,
                name=SIGNAL_GITHUB_RELEASE_PUBLISHED,
                outcome="published",
                subject_kind="release",
                subject_id=record.release_id,
                correlation_id=correlation,
            )
            holder.append(record)
            return record.release_id

        result = await self._governed(
            TOOL_CREATE_RELEASE,
            arguments,
            grant_id=grant_id,
            correlation_id=correlation,
            perform=perform,
        )
        if isinstance(result, RepeatedGrantUse):
            return result
        return holder[0]

    def _require_reader(self, role: str, tool_name: str) -> None:
        if tool_name not in GITHUB_READ_TOOLS:
            raise RegistryRejected("Role is not allowed to read this GitHub resource.")
        record = self._store.get_role(role)
        if tool_name not in record.tools:
            raise RegistryRejected("Role is not allowed to read this GitHub resource.")

    def _target(
        self, binding_id: str
    ) -> tuple[GithubBindingRecord, RepositoryRecord, tuple[str, str]]:
        binding = self._store.get_github_binding(binding_id)
        repository = self._store.get_repository(binding.repository)
        return binding, repository, github_owner_repo(repository.remote)

    def _implementation_branch(self, repository: RepositoryRecord, branch: str) -> str:
        protected = set(self._store.protected_branches(repository.name))
        return assert_implementation_branch(branch, protected)

    def _same_branch(
        self,
        binding: GithubBindingRecord,
        *,
        repository_name: str,
        branch: str,
        binding_id: str,
    ) -> None:
        if repository_name != binding.repository or binding_id != binding.binding_id:
            raise RegistryRejected("Grant does not allow this repository.")
        if branch != self._implementation_branch(
            self._store.get_repository(binding.repository), branch
        ):
            raise RegistryRejected("Branch is protected.")

    def _require_open_pull(
        self, binding: GithubBindingRecord, branch: str
    ) -> GithubPullRequestRecord:
        pull = self._store.find_github_pull_request(binding.binding_id)
        if pull is None or pull.head_ref != branch or pull.state != "open":
            raise RegistryRejected("GitHub pull request is not allowed.")
        return pull

    def _known_sha(self, binding_id: str, sha: str) -> str:
        digest = require_sha(sha)
        known = {row.sha for row in self._store.list_github_refs(binding_id)}
        pull = self._store.find_github_pull_request(binding_id)
        if pull is not None:
            known.add(pull.head_sha)
            if pull.merge_sha is not None:
                known.add(pull.merge_sha)
        if digest not in known:
            raise RegistryRejected("GitHub commit is not allowed.")
        return digest

    def _accept_pull(
        self,
        binding: GithubBindingRecord,
        current: GithubPullRequestRecord | None,
        payload: "_PullPayload",
        path: str,
        *,
        expected_head: str,
        expected_sha: str | None,
        expected_base: str,
    ) -> GithubPullRequestRecord:
        if (
            payload.number < 1
            or payload.head.ref != expected_head
            or payload.base.ref != expected_base
        ):
            raise HttpPayloadError(method="GET", path=path)
        if current is not None and payload.number != current.number:
            raise HttpPayloadError(method="GET", path=path)
        head_sha = _commit_sha(payload.head.sha, method="GET", path=path)
        if expected_sha is not None and head_sha != expected_sha:
            raise HttpPayloadError(method="GET", path=path)
        state: Literal["open", "closed", "merged"] = "merged" if payload.merged else payload.state
        try:
            return self._store.record_github_pull_request(
                binding.binding_id,
                number=payload.number,
                head_ref=payload.head.ref,
                head_sha=head_sha,
                base_ref=payload.base.ref,
                state=state,
            )
        except RegistryRejected:
            raise HttpPayloadError(method="GET", path=path) from None

    def _signal(
        self,
        binding: GithubBindingRecord,
        *,
        name: str,
        outcome: str,
        subject_kind: str,
        subject_id: str,
        correlation_id: str,
    ) -> None:
        if binding.run_id is None:
            return
        self._store.publish_github_signal(
            name=name,
            target_run_id=binding.run_id,
            correlation_id=correlation_id,
            outcome_status=outcome,
            subject_kind=subject_kind,
            subject_id=subject_id,
        )

    async def _governed(
        self,
        tool_name: str,
        arguments: Mapping[str, str],
        *,
        grant_id: str,
        correlation_id: str,
        perform: Callable[..., object],
    ) -> object:
        if github_tool_permission(tool_name) != GITHUB_TOOL_PERMISSIONS[tool_name]:
            raise RegistryRejected("Grant does not allow this tool.")
        tool = prepare_tool(
            perform,
            effect=ToolEffect.SIDE_EFFECT,
            name=tool_name,
            description=tool_name,
        )
        return await execute_tool(
            tool,
            arguments,
            audit_log=self._audit_log,
            correlation_id=correlation_id,
            agent_name="github",
            decision=ApprovalDecision(tool_name, approved=True),
            grant_id=grant_id,
            grants=self._store,
        )


def _assert_github_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise GithubError("GitHub client is not configured.")


def _correlation(value: str) -> str:
    return require_token(value, "Directory name is not allowed.")


def _repo_path(coordinates: tuple[str, str], suffix: str) -> str:
    owner, repo = coordinates
    return f"/repos/{owner}/{repo}{suffix}"


def _commit_sha(value: str, *, method: str, path: str) -> str:
    try:
        return require_sha(value)
    except RegistryRejected:
        raise HttpPayloadError(method=method, path=path) from None


def _conclusion(run: "_CheckRunPayload", *, path: str, expected_sha: str) -> str:
    observed = _commit_sha(run.head_sha, method="GET", path=path)
    if observed != expected_sha or run.id < 1:
        raise HttpPayloadError(method="GET", path=path)
    if run.status != "completed" or run.conclusion is None:
        return "pending"
    if run.conclusion not in _TERMINAL_CHECKS:
        raise HttpPayloadError(method="GET", path=path)
    return run.conclusion


def _pull_view(repository: str, record: GithubPullRequestRecord) -> GithubPullRequestView:
    return GithubPullRequestView(
        repository=repository,
        number=record.number,
        state=record.state,
        head_ref=record.head_ref,
        head_sha=record.head_sha,
        base_ref=record.base_ref,
        merge_sha=record.merge_sha,
    )


def _mint_installation_token(
    credentials: GithubAppCredentials,
    *,
    transport: httpx.BaseTransport | None,
    now: datetime,
) -> tuple[SecretString, datetime]:
    app_id = _numeric(credentials.app_id)
    installation_id = _numeric(credentials.installation_id)
    issued = int(now.timestamp())
    try:
        assertion = jwt.encode(
            {"iat": issued - 60, "exp": issued + 8 * 60, "iss": app_id},
            credentials.private_key.get_secret_value(),
            algorithm="RS256",
        )
    except Exception:
        raise GithubCredentialError() from None
    if isinstance(assertion, bytes):
        assertion = assertion.decode("ascii")
    try:
        with httpx.Client(
            base_url=GITHUB_API_BASE_URL,
            transport=transport,
            trust_env=False,
            timeout=10.0,
            follow_redirects=False,
        ) as client:
            response = client.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {assertion}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "agentbuildkit",
                },
            )
    except httpx.HTTPError:
        raise GithubCredentialError() from None
    if response.status_code >= 400:
        raise GithubCredentialError()
    try:
        payload = response.json()
    except json.JSONDecodeError:
        raise GithubCredentialError() from None
    if not isinstance(payload, dict):
        raise GithubCredentialError()
    fields = cast(dict[object, object], payload)
    token = fields.get("token")
    if not isinstance(token, str) or not token or any(character.isspace() for character in token):
        raise GithubCredentialError()
    header = SecretString(f"Bearer {token}")
    expires = now + timedelta(minutes=8)
    raw_expiry = fields.get("expires_at")
    if isinstance(raw_expiry, str):
        try:
            parsed = datetime.fromisoformat(raw_expiry)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            expires = parsed.astimezone(UTC) - timedelta(seconds=60)
    return header, expires


def _numeric(value: str) -> str:
    if not value.isdigit() or len(value) > 20:
        raise GithubCredentialError()
    return value


class _GitObject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str


class _RefPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ref: str
    git_object: _GitObject = Field(alias="object")


class _CreateRefBody(BaseModel):
    ref: str
    sha: str


class _IssuePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    state: Literal["open", "closed"]
    title: str
    body: str | None = None


class _PullEndpoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    sha: str


class _PullPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    state: Literal["open", "closed"]
    merged: bool = False
    head: _PullEndpoint
    base: _PullEndpoint


class _CreatePullBody(BaseModel):
    title: str
    head: str
    base: str
    body: str


class _TextBody(BaseModel):
    body: str


class _ReviewBody(BaseModel):
    body: str
    event: str


class _IdPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int


class _MergeBody(BaseModel):
    merge_method: Literal["merge"] = "merge"


class _MergePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str
    merged: bool


class _ReleaseBody(BaseModel):
    tag_name: str
    target_commitish: str


class _ReleasePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    tag_name: str


class _CheckRunPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    status: str
    conclusion: str | None = None
    head_sha: str


class _ChecksPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    check_runs: list[_CheckRunPayload]
