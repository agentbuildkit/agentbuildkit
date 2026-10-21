"""GitHub binding records on the version 2 registry.

GitHub remains the source of truth for issues, branches, commits, pull requests,
checks, reviews, and releases. These records keep identifiers, SHAs, and
content hashes. They do not keep issue text, pull request bodies, or credentials.
"""

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from agent_platform.state.models import (
    _GITHUB_SIGNAL_NAMES,
    SIGNAL_GITHUB_CHECK_COMPLETED,
    CheckpointCompatibilityError,
    GithubBindingRecord,
    GithubCheckRecord,
    GithubNoteRecord,
    GithubPullRequestRecord,
    GithubRefRecord,
    GithubReleaseRecord,
    GithubSignalStatus,
    RegistryRejected,
    RepositoryRecord,
    RunRecord,
    RunStatus,
    SignalName,
    SignalRecord,
    WorkItemRecord,
    assert_implementation_branch,
    github_owner_repo,
    require_argument_hash,
    require_branch,
    require_issue_number,
    require_sha,
    require_signal_name,
    require_signal_outcome,
    require_token,
)

_TERMINAL_RUNS = frozenset({"completed", "failed"})
PullState = Literal["open", "closed", "merged"]
NoteKind = Literal["comment", "review"]


class GithubDirectory(Protocol):
    """Registry operations the GitHub binding rules reuse."""

    def get_repository(self, scope: str, name: str) -> RepositoryRecord: ...

    def get_work_item(self, scope: str, work_item_id: str) -> WorkItemRecord: ...

    def get_run(self, scope: str, run_id: str) -> RunRecord: ...

    def transition_run(self, scope: str, run_id: str, status: RunStatus) -> RunRecord: ...


def signal_from_row(row: sqlite3.Row) -> SignalRecord:
    """Read one signal. Unknown names and outcomes fail closed."""
    keys = set(row.keys())
    subject_kind = _optional_column(row, keys, "subject_kind")
    subject_id = _optional_column(row, keys, "subject_id")
    try:
        name = require_signal_name(str(row["name"]))
        return SignalRecord(
            scope=str(row["scope"]),
            signal_id=str(row["signal_id"]),
            name=name,
            target_run_id=str(row["target_run_id"]),
            source_run_id=str(row["source_run_id"]),
            correlation_id=str(row["correlation_id"]),
            outcome_status=require_signal_outcome(
                name,
                str(row["outcome_status"]),
                subject_kind=subject_kind,
                subject_id=subject_id,
            ),
            created_at=str(row["created_at"]),
            consumed_at=None if row["consumed_at"] is None else str(row["consumed_at"]),
            subject_kind=subject_kind,
            subject_id=subject_id,
        )
    except (ValidationError, RegistryRejected):
        raise CheckpointCompatibilityError() from None


class GithubStorage(Protocol):
    """Identifier storage for one registry backend."""

    def list_bindings(self, scope: str) -> tuple[GithubBindingRecord, ...]: ...

    def get_binding(self, scope: str, binding_id: str) -> GithubBindingRecord | None: ...

    def insert_binding(self, record: GithubBindingRecord) -> None: ...

    def update_binding(self, record: GithubBindingRecord) -> None: ...

    def list_protections(self, scope: str, repository: str) -> tuple[str, ...]: ...

    def save_protection(self, scope: str, repository: str, branch: str) -> None: ...

    def get_ref(self, scope: str, binding_id: str, ref: str) -> GithubRefRecord | None: ...

    def list_refs(self, scope: str, binding_id: str) -> tuple[GithubRefRecord, ...]: ...

    def insert_ref(self, record: GithubRefRecord) -> None: ...

    def update_ref(self, record: GithubRefRecord) -> None: ...

    def list_pulls(self, scope: str) -> tuple[GithubPullRequestRecord, ...]: ...

    def get_pull(self, scope: str, binding_id: str) -> GithubPullRequestRecord | None: ...

    def insert_pull(self, record: GithubPullRequestRecord) -> None: ...

    def update_pull(self, record: GithubPullRequestRecord) -> None: ...

    def get_check(self, scope: str, binding_id: str, check_id: str) -> GithubCheckRecord | None: ...

    def list_checks(self, scope: str, binding_id: str) -> tuple[GithubCheckRecord, ...]: ...

    def insert_check(self, record: GithubCheckRecord) -> None: ...

    def update_check(self, record: GithubCheckRecord) -> None: ...

    def list_releases(self, scope: str, binding_id: str) -> tuple[GithubReleaseRecord, ...]: ...

    def get_release(
        self, scope: str, binding_id: str, release_id: str
    ) -> GithubReleaseRecord | None: ...

    def insert_release(self, record: GithubReleaseRecord) -> None: ...

    def get_note(self, scope: str, binding_id: str, note_id: str) -> GithubNoteRecord | None: ...

    def insert_note(self, record: GithubNoteRecord) -> None: ...

    def list_signals(
        self, scope: str, *, target_run_id: str | None
    ) -> tuple[SignalRecord, ...]: ...

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


class GithubBindings:
    """Directory-backed GitHub identifiers. Both registry backends use this."""

    def __init__(self, storage: GithubStorage, directory: GithubDirectory) -> None:
        self._storage = storage
        self._directory = directory

    def protect_branch(self, scope: str, repository: str, branch: str) -> str:
        record = self._directory.get_repository(scope, repository)
        github_owner_repo(record.remote)
        name = require_branch(branch)
        if name != record.default_branch:
            self._storage.save_protection(scope, record.name, name)
        return name

    def protected_branches(self, scope: str, repository: str) -> tuple[str, ...]:
        record = self._directory.get_repository(scope, repository)
        names = {record.default_branch, *self._storage.list_protections(scope, record.name)}
        return tuple(sorted(names))

    def bind_repository(
        self,
        scope: str,
        *,
        repository: str,
        work_item_id: str,
        run_id: str | None,
        issue_number: int | None,
        binding_id: str | None,
    ) -> GithubBindingRecord:
        repo = self._directory.get_repository(scope, repository)
        github_owner_repo(repo.remote)
        item = self._directory.get_work_item(scope, work_item_id)
        if item.status == "cancelled":
            raise RegistryRejected("Work item transition is not allowed.")
        run = None if run_id is None else self._directory.get_run(scope, run_id)
        if run is not None and run.work_item_id not in {None, item.work_item_id}:
            raise RegistryRejected("GitHub binding is not allowed.")
        number = None if issue_number is None else require_issue_number(issue_number)
        planned = _plan_binding(
            scope=scope,
            bindings=self._storage.list_bindings(scope),
            repository=repo,
            work_item=item,
            run=run,
            issue_number=number,
            binding_id=binding_id,
        )
        return self._write_binding(planned)

    def bindings_for_work_item(
        self, scope: str, *, repository: str, work_item_id: str
    ) -> tuple[GithubBindingRecord, ...]:
        self._directory.get_repository(scope, repository)
        self._directory.get_work_item(scope, work_item_id)
        rows = [
            row
            for row in self._storage.list_bindings(scope)
            if row.repository == repository and row.work_item_id == work_item_id
        ]
        return tuple(sorted(rows, key=lambda row: row.binding_id))

    def get_binding(self, scope: str, binding_id: str) -> GithubBindingRecord:
        found = self._storage.get_binding(scope, binding_id)
        if found is None:
            raise RegistryRejected("GitHub binding is unknown.")
        return found

    def note_issue(self, scope: str, binding_id: str, *, content_hash: str) -> GithubBindingRecord:
        current = self.get_binding(scope, binding_id)
        if current.issue_number is None:
            raise RegistryRejected("GitHub issue is not allowed.")
        digest = require_argument_hash(content_hash)
        if digest == "lifecycle":
            raise RegistryRejected("GitHub issue is not allowed.")
        if current.issue_content_hash == digest:
            return current
        return self._write_binding(current.model_copy(update={"issue_content_hash": digest}))

    def find_ref(self, scope: str, binding_id: str, ref: str) -> GithubRefRecord | None:
        binding = self.get_binding(scope, binding_id)
        repository = self._directory.get_repository(scope, binding.repository)
        name = assert_implementation_branch(
            ref, set(self.protected_branches(scope, repository.name))
        )
        return self._storage.get_ref(scope, binding.binding_id, name)

    def list_refs(self, scope: str, binding_id: str) -> tuple[GithubRefRecord, ...]:
        self.get_binding(scope, binding_id)
        return self._storage.list_refs(scope, binding_id)

    def record_ref(self, scope: str, binding_id: str, *, ref: str, sha: str) -> GithubRefRecord:
        binding = self.get_binding(scope, binding_id)
        repository = self._directory.get_repository(scope, binding.repository)
        name = assert_implementation_branch(
            ref, set(self.protected_branches(scope, repository.name))
        )
        digest = require_sha(sha)
        current = self._storage.get_ref(scope, binding.binding_id, name)
        planned = GithubRefRecord(
            scope=scope,
            binding_id=binding.binding_id,
            ref=name,
            sha=digest,
            created_at=current.created_at if current is not None else _now(),
        )
        if current == planned:
            return planned
        if current is None:
            self._storage.insert_ref(planned)
        else:
            self._storage.update_ref(planned)
        return planned

    def find_pull_request(self, scope: str, binding_id: str) -> GithubPullRequestRecord | None:
        self.get_binding(scope, binding_id)
        return self._storage.get_pull(scope, binding_id)

    def record_pull_request(
        self,
        scope: str,
        binding_id: str,
        *,
        number: int,
        head_ref: str,
        head_sha: str,
        base_ref: str,
        state: PullState,
        merge_sha: str | None,
    ) -> GithubPullRequestRecord:
        binding = self.get_binding(scope, binding_id)
        repository = self._directory.get_repository(scope, binding.repository)
        protected = set(self.protected_branches(scope, repository.name))
        head = assert_implementation_branch(head_ref, protected)
        base = require_branch(base_ref)
        if base != repository.default_branch and base not in protected:
            raise RegistryRejected("GitHub pull request is not allowed.")
        parsed_number = require_issue_number(number)
        digest = require_sha(head_sha)
        merged = None if merge_sha is None else require_sha(merge_sha)
        self._reject_duplicate_number(scope, binding, parsed_number)
        current = self._storage.get_pull(scope, binding.binding_id)
        if current is not None and (
            current.number != parsed_number or current.head_ref != head or current.base_ref != base
        ):
            raise RegistryRejected("GitHub pull request is not allowed.")
        created_at = _now()
        updated_at = created_at
        merge_value = merged
        if current is not None:
            created_at = current.created_at
            merge_value = current.merge_sha if merged is None else merged
            updated_at = current.updated_at
            if (
                current.head_sha != digest
                or current.state != state
                or merge_value != current.merge_sha
            ):
                updated_at = _now()
        planned = GithubPullRequestRecord(
            scope=scope,
            binding_id=binding.binding_id,
            number=parsed_number,
            head_ref=head,
            head_sha=digest,
            base_ref=base,
            state=state,
            merge_sha=merge_value,
            created_at=created_at,
            updated_at=updated_at,
        )
        if current is not None and current == planned:
            return current
        if current is None:
            self._storage.insert_pull(planned)
        else:
            self._storage.update_pull(planned)
        return planned

    def list_checks(self, scope: str, binding_id: str) -> tuple[GithubCheckRecord, ...]:
        self.get_binding(scope, binding_id)
        return self._storage.list_checks(scope, binding_id)

    def record_check(
        self,
        scope: str,
        binding_id: str,
        *,
        check_id: str,
        conclusion: str,
        head_sha: str,
    ) -> GithubCheckRecord:
        binding = self.get_binding(scope, binding_id)
        identifier = require_token(check_id, "GitHub check is not allowed.")
        outcome = _check_conclusion(conclusion)
        digest = require_sha(head_sha)
        current = self._storage.get_check(scope, binding.binding_id, identifier)
        planned = GithubCheckRecord(
            scope=scope,
            binding_id=binding.binding_id,
            check_id=identifier,
            conclusion=outcome,
            head_sha=digest,
            recorded_at=current.recorded_at
            if current is not None and current.conclusion == outcome and current.head_sha == digest
            else _now(),
        )
        if current == planned:
            return planned
        if current is None:
            self._storage.insert_check(planned)
        else:
            self._storage.update_check(planned)
        return planned

    def list_releases(self, scope: str, binding_id: str) -> tuple[GithubReleaseRecord, ...]:
        self.get_binding(scope, binding_id)
        return self._storage.list_releases(scope, binding_id)

    def record_release(
        self,
        scope: str,
        binding_id: str,
        *,
        release_id: str,
        tag: str,
        target_sha: str,
    ) -> GithubReleaseRecord:
        binding = self.get_binding(scope, binding_id)
        identifier = require_token(release_id, "GitHub release is not allowed.")
        parsed_tag = require_token(tag, "GitHub release is not allowed.")
        digest = require_sha(target_sha)
        for existing in self._storage.list_releases(scope, binding.binding_id):
            if existing.tag == parsed_tag and existing.release_id != identifier:
                raise RegistryRejected("GitHub release is not allowed.")
        current = self._storage.get_release(scope, binding.binding_id, identifier)
        if current is not None:
            if current.tag != parsed_tag or current.target_sha != digest:
                raise RegistryRejected("GitHub release is not allowed.")
            return current
        planned = GithubReleaseRecord(
            scope=scope,
            binding_id=binding.binding_id,
            release_id=identifier,
            tag=parsed_tag,
            target_sha=digest,
            created_at=_now(),
        )
        self._storage.insert_release(planned)
        return planned

    def record_note(
        self,
        scope: str,
        binding_id: str,
        *,
        note_id: str,
        kind: NoteKind,
    ) -> GithubNoteRecord:
        binding = self.get_binding(scope, binding_id)
        if kind not in {"comment", "review"}:
            raise RegistryRejected("GitHub review is not allowed.")
        identifier = require_token(note_id, "GitHub review is not allowed.")
        current = self._storage.get_note(scope, binding.binding_id, identifier)
        if current is not None:
            if current.kind != kind:
                raise RegistryRejected("GitHub review is not allowed.")
            return current
        planned = GithubNoteRecord(
            scope=scope,
            binding_id=binding.binding_id,
            note_id=identifier,
            kind=kind,
            created_at=_now(),
        )
        self._storage.insert_note(planned)
        return planned

    def publish_signal(
        self,
        scope: str,
        *,
        name: str,
        target_run_id: str,
        correlation_id: str,
        outcome_status: str,
        subject_kind: str,
        subject_id: str,
    ) -> SignalRecord:
        signal_name = require_signal_name(name)
        if signal_name == "child_finished":
            raise RegistryRejected("GitHub signal is not allowed.")
        run = self._directory.get_run(scope, target_run_id)
        outcome = require_signal_outcome(
            signal_name,
            outcome_status,
            subject_kind=subject_kind,
            subject_id=subject_id,
        )
        correlation = require_token(correlation_id, "Directory name is not allowed.")
        existing = self._storage.find_signal(
            scope,
            name=signal_name,
            target_run_id=run.run_id,
            subject_kind=subject_kind,
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
            subject_kind=subject_kind,
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
            or signal.name not in _GITHUB_SIGNAL_NAMES
        ):
            raise RegistryRejected("GitHub signal is unknown.")
        if signal.consumed_at is None:
            self._storage.consume_signal(scope, signal.signal_id, _now())
            if run.status == "paused":
                self._directory.transition_run(scope, run.run_id, "running")
            signal = signal.model_copy(update={"consumed_at": _now()})
        return self._storage.get_signal(scope, signal.signal_id) or signal

    def pause_for_signal(self, scope: str, run_id: str) -> RunRecord:
        run = self._directory.get_run(scope, run_id)
        if run.status == "paused":
            return run
        if run.status in _TERMINAL_RUNS:
            raise RegistryRejected("Run transition is not allowed.")
        if run.checkpoint_id is None:
            raise RegistryRejected("A paused run needs a checkpoint.")
        return self._directory.transition_run(scope, run.run_id, "paused")

    def list_signals(
        self, scope: str, *, target_run_id: str | None = None
    ) -> tuple[SignalRecord, ...]:
        if target_run_id is not None:
            self._directory.get_run(scope, target_run_id)
        return self._storage.list_signals(scope, target_run_id=target_run_id)

    def _write_binding(self, planned: GithubBindingRecord) -> GithubBindingRecord:
        current = self._storage.get_binding(planned.scope, planned.binding_id)
        if current == planned:
            return planned
        if current is None:
            self._storage.insert_binding(planned)
        else:
            self._storage.update_binding(planned)
        return planned

    def _reject_duplicate_number(
        self, scope: str, binding: GithubBindingRecord, number: int
    ) -> None:
        bindings = {row.binding_id: row for row in self._storage.list_bindings(scope)}
        for pull in self._storage.list_pulls(scope):
            owner = bindings.get(pull.binding_id)
            if owner is None or owner.repository != binding.repository:
                continue
            if pull.number == number and pull.binding_id != binding.binding_id:
                raise RegistryRejected("GitHub pull request is not allowed.")


class GithubRegistryMethods:
    """Registry methods shared by the memory and SQLite backends."""

    _github: GithubBindings

    def protect_branch(self, scope: str, repository: str, branch: str) -> str:
        return self._github.protect_branch(scope, repository, branch)

    def protected_branches(self, scope: str, repository: str) -> tuple[str, ...]:
        return self._github.protected_branches(scope, repository)

    def bind_github_repository(
        self,
        scope: str,
        *,
        repository: str,
        work_item_id: str,
        run_id: str | None = None,
        issue_number: int | None = None,
        binding_id: str | None = None,
    ) -> GithubBindingRecord:
        return self._github.bind_repository(
            scope,
            repository=repository,
            work_item_id=work_item_id,
            run_id=run_id,
            issue_number=issue_number,
            binding_id=binding_id,
        )

    def github_bindings_for_work_item(
        self, scope: str, repository: str, work_item_id: str
    ) -> tuple[GithubBindingRecord, ...]:
        return self._github.bindings_for_work_item(
            scope, repository=repository, work_item_id=work_item_id
        )

    def get_github_binding(self, scope: str, binding_id: str) -> GithubBindingRecord:
        return self._github.get_binding(scope, binding_id)

    def note_github_issue(
        self, scope: str, binding_id: str, *, content_hash: str
    ) -> GithubBindingRecord:
        return self._github.note_issue(scope, binding_id, content_hash=content_hash)

    def find_github_ref(self, scope: str, binding_id: str, ref: str) -> GithubRefRecord | None:
        return self._github.find_ref(scope, binding_id, ref)

    def list_github_refs(self, scope: str, binding_id: str) -> tuple[GithubRefRecord, ...]:
        return self._github.list_refs(scope, binding_id)

    def record_github_ref(
        self, scope: str, binding_id: str, *, ref: str, sha: str
    ) -> GithubRefRecord:
        return self._github.record_ref(scope, binding_id, ref=ref, sha=sha)

    def find_github_pull_request(
        self, scope: str, binding_id: str
    ) -> GithubPullRequestRecord | None:
        return self._github.find_pull_request(scope, binding_id)

    def record_github_pull_request(
        self,
        scope: str,
        binding_id: str,
        *,
        number: int,
        head_ref: str,
        head_sha: str,
        base_ref: str,
        state: PullState,
        merge_sha: str | None = None,
    ) -> GithubPullRequestRecord:
        return self._github.record_pull_request(
            scope,
            binding_id,
            number=number,
            head_ref=head_ref,
            head_sha=head_sha,
            base_ref=base_ref,
            state=state,
            merge_sha=merge_sha,
        )

    def list_github_checks(self, scope: str, binding_id: str) -> tuple[GithubCheckRecord, ...]:
        return self._github.list_checks(scope, binding_id)

    def record_github_check(
        self,
        scope: str,
        binding_id: str,
        *,
        check_id: str,
        conclusion: str,
        head_sha: str,
    ) -> GithubCheckRecord:
        return self._github.record_check(
            scope,
            binding_id,
            check_id=check_id,
            conclusion=conclusion,
            head_sha=head_sha,
        )

    def list_github_releases(self, scope: str, binding_id: str) -> tuple[GithubReleaseRecord, ...]:
        return self._github.list_releases(scope, binding_id)

    def record_github_release(
        self,
        scope: str,
        binding_id: str,
        *,
        release_id: str,
        tag: str,
        target_sha: str,
    ) -> GithubReleaseRecord:
        return self._github.record_release(
            scope,
            binding_id,
            release_id=release_id,
            tag=tag,
            target_sha=target_sha,
        )

    def record_github_note(
        self,
        scope: str,
        binding_id: str,
        *,
        note_id: str,
        kind: NoteKind,
    ) -> GithubNoteRecord:
        return self._github.record_note(scope, binding_id, note_id=note_id, kind=kind)

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
    ) -> SignalRecord:
        return self._github.publish_signal(
            scope,
            name=name,
            target_run_id=target_run_id,
            correlation_id=correlation_id,
            outcome_status=outcome_status,
            subject_kind=subject_kind,
            subject_id=subject_id,
        )

    def take_github_signal(self, scope: str, run_id: str, signal_id: str) -> SignalRecord:
        return self._github.take_signal(scope, run_id, signal_id)

    def pause_for_signal(self, scope: str, run_id: str) -> RunRecord:
        return self._github.pause_for_signal(scope, run_id)

    def list_signals(
        self, scope: str, *, target_run_id: str | None = None
    ) -> tuple[SignalRecord, ...]:
        return self._github.list_signals(scope, target_run_id=target_run_id)


class MemoryGithubStorage:
    """In-process GitHub identifiers. Signals are the registry signal map."""

    def __init__(self, signals: dict[tuple[str, str], SignalRecord]) -> None:
        self.bindings: dict[tuple[str, str], GithubBindingRecord] = {}
        self.protections: dict[tuple[str, str, str], str] = {}
        self.refs: dict[tuple[str, str, str], GithubRefRecord] = {}
        self.pulls: dict[tuple[str, str], GithubPullRequestRecord] = {}
        self.checks: dict[tuple[str, str, str], GithubCheckRecord] = {}
        self.releases: dict[tuple[str, str, str], GithubReleaseRecord] = {}
        self.notes: dict[tuple[str, str, str], GithubNoteRecord] = {}
        self.signals = signals

    def list_bindings(self, scope: str) -> tuple[GithubBindingRecord, ...]:
        rows = [row for key, row in self.bindings.items() if key[0] == scope]
        return tuple(sorted(rows, key=lambda row: row.binding_id))

    def get_binding(self, scope: str, binding_id: str) -> GithubBindingRecord | None:
        return self.bindings.get((scope, binding_id))

    def insert_binding(self, record: GithubBindingRecord) -> None:
        self.bindings[(record.scope, record.binding_id)] = record

    def update_binding(self, record: GithubBindingRecord) -> None:
        self.bindings[(record.scope, record.binding_id)] = record

    def list_protections(self, scope: str, repository: str) -> tuple[str, ...]:
        rows = [
            branch
            for (item_scope, item_repository, branch) in self.protections
            if item_scope == scope and item_repository == repository
        ]
        return tuple(sorted(rows))

    def save_protection(self, scope: str, repository: str, branch: str) -> None:
        self.protections[(scope, repository, branch)] = branch

    def get_ref(self, scope: str, binding_id: str, ref: str) -> GithubRefRecord | None:
        return self.refs.get((scope, binding_id, ref))

    def list_refs(self, scope: str, binding_id: str) -> tuple[GithubRefRecord, ...]:
        rows = [
            row
            for (item_scope, item_binding, _), row in self.refs.items()
            if item_scope == scope and item_binding == binding_id
        ]
        return tuple(sorted(rows, key=lambda row: row.ref))

    def insert_ref(self, record: GithubRefRecord) -> None:
        self.refs[(record.scope, record.binding_id, record.ref)] = record

    def update_ref(self, record: GithubRefRecord) -> None:
        self.refs[(record.scope, record.binding_id, record.ref)] = record

    def list_pulls(self, scope: str) -> tuple[GithubPullRequestRecord, ...]:
        rows = [row for key, row in self.pulls.items() if key[0] == scope]
        return tuple(sorted(rows, key=lambda row: row.number))

    def get_pull(self, scope: str, binding_id: str) -> GithubPullRequestRecord | None:
        return self.pulls.get((scope, binding_id))

    def insert_pull(self, record: GithubPullRequestRecord) -> None:
        self.pulls[(record.scope, record.binding_id)] = record

    def update_pull(self, record: GithubPullRequestRecord) -> None:
        self.pulls[(record.scope, record.binding_id)] = record

    def get_check(self, scope: str, binding_id: str, check_id: str) -> GithubCheckRecord | None:
        return self.checks.get((scope, binding_id, check_id))

    def list_checks(self, scope: str, binding_id: str) -> tuple[GithubCheckRecord, ...]:
        rows = [
            row
            for (item_scope, item_binding, _), row in self.checks.items()
            if item_scope == scope and item_binding == binding_id
        ]
        return tuple(sorted(rows, key=lambda row: row.check_id))

    def insert_check(self, record: GithubCheckRecord) -> None:
        self.checks[(record.scope, record.binding_id, record.check_id)] = record

    def update_check(self, record: GithubCheckRecord) -> None:
        self.checks[(record.scope, record.binding_id, record.check_id)] = record

    def list_releases(self, scope: str, binding_id: str) -> tuple[GithubReleaseRecord, ...]:
        rows = [
            row
            for (item_scope, item_binding, _), row in self.releases.items()
            if item_scope == scope and item_binding == binding_id
        ]
        return tuple(sorted(rows, key=lambda row: row.release_id))

    def get_release(
        self, scope: str, binding_id: str, release_id: str
    ) -> GithubReleaseRecord | None:
        return self.releases.get((scope, binding_id, release_id))

    def insert_release(self, record: GithubReleaseRecord) -> None:
        self.releases[(record.scope, record.binding_id, record.release_id)] = record

    def get_note(self, scope: str, binding_id: str, note_id: str) -> GithubNoteRecord | None:
        return self.notes.get((scope, binding_id, note_id))

    def insert_note(self, record: GithubNoteRecord) -> None:
        self.notes[(record.scope, record.binding_id, record.note_id)] = record

    def list_signals(self, scope: str, *, target_run_id: str | None) -> tuple[SignalRecord, ...]:
        rows = [row for key, row in self.signals.items() if key[0] == scope]
        if target_run_id is not None:
            rows = [row for row in rows if row.target_run_id == target_run_id]
        return tuple(sorted(rows, key=lambda row: (row.created_at, row.signal_id)))

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
        for row in self.list_signals(scope, target_run_id=target_run_id):
            if (
                row.name == name
                and row.subject_kind == subject_kind
                and row.subject_id == subject_id
                and row.outcome_status == outcome_status
            ):
                return row
        return None

    def get_signal(self, scope: str, signal_id: str) -> SignalRecord | None:
        return self.signals.get((scope, signal_id))

    def insert_signal(self, record: SignalRecord) -> None:
        self.signals[(record.scope, record.signal_id)] = record

    def consume_signal(self, scope: str, signal_id: str, consumed_at: str) -> None:
        current = self.signals.get((scope, signal_id))
        if current is None:
            raise RegistryRejected("GitHub signal is unknown.")
        self.signals[(scope, signal_id)] = current.model_copy(update={"consumed_at": consumed_at})


class SqliteGithubStorage:
    """GitHub identifier tables on the workflow SQLite connection."""

    def __init__(self, connection: sqlite3.Connection, commit: Callable[[], None]) -> None:
        self._connection = connection
        self._commit = commit

    def list_bindings(self, scope: str) -> tuple[GithubBindingRecord, ...]:
        rows = self._fetch(
            "SELECT * FROM github_bindings WHERE scope = ? ORDER BY binding_id", (scope,)
        )
        return tuple(_binding_from_row(row) for row in rows)

    def get_binding(self, scope: str, binding_id: str) -> GithubBindingRecord | None:
        row = self._one(
            "SELECT * FROM github_bindings WHERE scope = ? AND binding_id = ?",
            (scope, binding_id),
        )
        if row is None:
            return None
        return _binding_from_row(row)

    def insert_binding(self, record: GithubBindingRecord) -> None:
        self._execute(
            """
            INSERT INTO github_bindings (
                scope, binding_id, repository, work_item_id, run_id, issue_number,
                issue_content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.scope,
                record.binding_id,
                record.repository,
                record.work_item_id,
                record.run_id,
                record.issue_number,
                record.issue_content_hash,
                record.created_at,
            ),
        )

    def update_binding(self, record: GithubBindingRecord) -> None:
        self._execute(
            """
            UPDATE github_bindings
            SET run_id = ?, issue_number = ?, issue_content_hash = ?
            WHERE scope = ? AND binding_id = ?
            """,
            (
                record.run_id,
                record.issue_number,
                record.issue_content_hash,
                record.scope,
                record.binding_id,
            ),
        )

    def list_protections(self, scope: str, repository: str) -> tuple[str, ...]:
        rows = self._fetch(
            """
            SELECT branch FROM repository_protections
            WHERE scope = ? AND repository = ?
            ORDER BY branch
            """,
            (scope, repository),
        )
        return tuple(str(row["branch"]) for row in rows)

    def save_protection(self, scope: str, repository: str, branch: str) -> None:
        self._connection.execute(
            """
            INSERT INTO repository_protections (scope, repository, branch)
            VALUES (?, ?, ?)
            ON CONFLICT (scope, repository, branch) DO NOTHING
            """,
            (scope, repository, branch),
        )
        self._commit()

    def get_ref(self, scope: str, binding_id: str, ref: str) -> GithubRefRecord | None:
        row = self._one(
            "SELECT * FROM github_refs WHERE scope = ? AND binding_id = ? AND ref = ?",
            (scope, binding_id, ref),
        )
        if row is None:
            return None
        return _ref_from_row(row)

    def list_refs(self, scope: str, binding_id: str) -> tuple[GithubRefRecord, ...]:
        rows = self._fetch(
            """
            SELECT * FROM github_refs
            WHERE scope = ? AND binding_id = ?
            ORDER BY ref
            """,
            (scope, binding_id),
        )
        return tuple(_ref_from_row(row) for row in rows)

    def insert_ref(self, record: GithubRefRecord) -> None:
        self._execute(
            """
            INSERT INTO github_refs (scope, binding_id, ref, sha, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (record.scope, record.binding_id, record.ref, record.sha, record.created_at),
        )

    def update_ref(self, record: GithubRefRecord) -> None:
        self._execute(
            """
            UPDATE github_refs SET sha = ?
            WHERE scope = ? AND binding_id = ? AND ref = ?
            """,
            (record.sha, record.scope, record.binding_id, record.ref),
        )

    def list_pulls(self, scope: str) -> tuple[GithubPullRequestRecord, ...]:
        rows = self._fetch(
            "SELECT * FROM github_pull_requests WHERE scope = ? ORDER BY number",
            (scope,),
        )
        return tuple(_pull_from_row(row) for row in rows)

    def get_pull(self, scope: str, binding_id: str) -> GithubPullRequestRecord | None:
        row = self._one(
            "SELECT * FROM github_pull_requests WHERE scope = ? AND binding_id = ?",
            (scope, binding_id),
        )
        if row is None:
            return None
        return _pull_from_row(row)

    def insert_pull(self, record: GithubPullRequestRecord) -> None:
        self._execute(
            """
            INSERT INTO github_pull_requests (
                scope, binding_id, number, head_ref, head_sha, base_ref, state,
                merge_sha, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _pull_values(record),
        )

    def update_pull(self, record: GithubPullRequestRecord) -> None:
        self._execute(
            """
            UPDATE github_pull_requests
            SET head_sha = ?, state = ?, merge_sha = ?, updated_at = ?
            WHERE scope = ? AND binding_id = ?
            """,
            (
                record.head_sha,
                record.state,
                record.merge_sha,
                record.updated_at,
                record.scope,
                record.binding_id,
            ),
        )

    def get_check(self, scope: str, binding_id: str, check_id: str) -> GithubCheckRecord | None:
        row = self._one(
            """
            SELECT * FROM github_checks
            WHERE scope = ? AND binding_id = ? AND check_id = ?
            """,
            (scope, binding_id, check_id),
        )
        if row is None:
            return None
        return _check_from_row(row)

    def list_checks(self, scope: str, binding_id: str) -> tuple[GithubCheckRecord, ...]:
        rows = self._fetch(
            """
            SELECT * FROM github_checks
            WHERE scope = ? AND binding_id = ?
            ORDER BY check_id
            """,
            (scope, binding_id),
        )
        return tuple(_check_from_row(row) for row in rows)

    def insert_check(self, record: GithubCheckRecord) -> None:
        self._execute(
            """
            INSERT INTO github_checks (
                scope, binding_id, check_id, conclusion, head_sha, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.scope,
                record.binding_id,
                record.check_id,
                record.conclusion,
                record.head_sha,
                record.recorded_at,
            ),
        )

    def update_check(self, record: GithubCheckRecord) -> None:
        self._execute(
            """
            UPDATE github_checks
            SET conclusion = ?, head_sha = ?, recorded_at = ?
            WHERE scope = ? AND binding_id = ? AND check_id = ?
            """,
            (
                record.conclusion,
                record.head_sha,
                record.recorded_at,
                record.scope,
                record.binding_id,
                record.check_id,
            ),
        )

    def list_releases(self, scope: str, binding_id: str) -> tuple[GithubReleaseRecord, ...]:
        rows = self._fetch(
            """
            SELECT * FROM github_releases
            WHERE scope = ? AND binding_id = ?
            ORDER BY release_id
            """,
            (scope, binding_id),
        )
        return tuple(_release_from_row(row) for row in rows)

    def get_release(
        self, scope: str, binding_id: str, release_id: str
    ) -> GithubReleaseRecord | None:
        row = self._one(
            """
            SELECT * FROM github_releases
            WHERE scope = ? AND binding_id = ? AND release_id = ?
            """,
            (scope, binding_id, release_id),
        )
        if row is None:
            return None
        return _release_from_row(row)

    def insert_release(self, record: GithubReleaseRecord) -> None:
        self._execute(
            """
            INSERT INTO github_releases (
                scope, binding_id, release_id, tag, target_sha, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.scope,
                record.binding_id,
                record.release_id,
                record.tag,
                record.target_sha,
                record.created_at,
            ),
        )

    def get_note(self, scope: str, binding_id: str, note_id: str) -> GithubNoteRecord | None:
        row = self._one(
            """
            SELECT * FROM github_notes
            WHERE scope = ? AND binding_id = ? AND note_id = ?
            """,
            (scope, binding_id, note_id),
        )
        if row is None:
            return None
        return _note_from_row(row)

    def insert_note(self, record: GithubNoteRecord) -> None:
        self._execute(
            """
            INSERT INTO github_notes (scope, binding_id, note_id, kind, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (record.scope, record.binding_id, record.note_id, record.kind, record.created_at),
        )

    def list_signals(self, scope: str, *, target_run_id: str | None) -> tuple[SignalRecord, ...]:
        sql = "SELECT * FROM signals WHERE scope = ?"
        params: list[object] = [scope]
        if target_run_id is not None:
            sql += " AND target_run_id = ?"
            params.append(target_run_id)
        sql += " ORDER BY created_at, signal_id"
        return tuple(signal_from_row(row) for row in self._fetch(sql, tuple(params)))

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
        row = self._one(
            """
            SELECT * FROM signals
            WHERE scope = ? AND name = ? AND target_run_id = ? AND subject_kind = ?
                AND subject_id = ? AND outcome_status = ?
            """,
            (scope, name, target_run_id, subject_kind, subject_id, outcome_status),
        )
        if row is None:
            return None
        return signal_from_row(row)

    def get_signal(self, scope: str, signal_id: str) -> SignalRecord | None:
        row = self._one(
            "SELECT * FROM signals WHERE scope = ? AND signal_id = ?",
            (scope, signal_id),
        )
        if row is None:
            return None
        return signal_from_row(row)

    def insert_signal(self, record: SignalRecord) -> None:
        self._execute(
            """
            INSERT INTO signals (
                scope, signal_id, name, target_run_id, source_run_id, correlation_id,
                outcome_status, created_at, consumed_at, subject_kind, subject_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
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
        try:
            self._connection.execute(sql, params)
        except sqlite3.IntegrityError:
            self._connection.rollback()
            raise RegistryRejected("GitHub binding is not allowed.") from None
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


def _optional_column(row: sqlite3.Row, keys: set[str], name: str) -> str | None:
    if name not in keys or row[name] is None:
        return None
    return str(row[name])


def _plan_binding(
    *,
    scope: str,
    bindings: Sequence[GithubBindingRecord],
    repository: RepositoryRecord,
    work_item: WorkItemRecord,
    run: RunRecord | None,
    issue_number: int | None,
    binding_id: str | None,
) -> GithubBindingRecord:
    run_id = None if run is None else run.run_id
    identifier = binding_id
    if identifier is None:
        for existing in bindings:
            if (
                existing.repository == repository.name
                and existing.work_item_id == work_item.work_item_id
            ):
                identifier = existing.binding_id
                break
    resolved = _identifier(identifier)
    for existing in bindings:
        same_id = existing.binding_id == resolved
        same_pair = (
            existing.repository == repository.name
            and existing.work_item_id == work_item.work_item_id
        )
        if not same_id and not same_pair:
            continue
        if not same_id or not same_pair or existing.issue_number != issue_number:
            raise RegistryRejected("GitHub binding is not allowed.")
        if run_id is not None and existing.run_id not in {None, run_id}:
            raise RegistryRejected("GitHub binding is not allowed.")
        if run_id is not None and existing.run_id is None:
            return existing.model_copy(update={"run_id": run_id})
        return existing
    return GithubBindingRecord(
        scope=scope,
        binding_id=resolved,
        repository=repository.name,
        work_item_id=work_item.work_item_id,
        run_id=run_id,
        issue_number=issue_number,
        issue_content_hash=None,
        created_at=_now(),
    )


def _check_conclusion(value: str) -> GithubSignalStatus:
    parsed = require_signal_outcome(
        SIGNAL_GITHUB_CHECK_COMPLETED,
        value,
        subject_kind="check",
        subject_id="1",
    )
    if parsed in {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "timed_out",
        "action_required",
        "skipped",
        "stale",
        "pending",
    }:
        return cast(GithubSignalStatus, parsed)
    raise RegistryRejected("GitHub check is not allowed.")


def _binding_from_row(row: sqlite3.Row) -> GithubBindingRecord:
    try:
        number = row["issue_number"]
        return GithubBindingRecord(
            scope=str(row["scope"]),
            binding_id=str(row["binding_id"]),
            repository=str(row["repository"]),
            work_item_id=str(row["work_item_id"]),
            run_id=None if row["run_id"] is None else str(row["run_id"]),
            issue_number=None if number is None else require_issue_number(int(number)),
            issue_content_hash=None
            if row["issue_content_hash"] is None
            else str(row["issue_content_hash"]),
            created_at=str(row["created_at"]),
        )
    except (ValidationError, RegistryRejected, TypeError, ValueError):
        raise CheckpointCompatibilityError() from None


def _ref_from_row(row: sqlite3.Row) -> GithubRefRecord:
    try:
        return GithubRefRecord(
            scope=str(row["scope"]),
            binding_id=str(row["binding_id"]),
            ref=require_branch(str(row["ref"])),
            sha=require_sha(str(row["sha"])),
            created_at=str(row["created_at"]),
        )
    except (ValidationError, RegistryRejected):
        raise CheckpointCompatibilityError() from None


def _pull_from_row(row: sqlite3.Row) -> GithubPullRequestRecord:
    try:
        state = str(row["state"])
        if state not in {"open", "closed", "merged"}:
            raise RegistryRejected("GitHub pull request is not allowed.")
        merge_sha = None if row["merge_sha"] is None else require_sha(str(row["merge_sha"]))
        return GithubPullRequestRecord(
            scope=str(row["scope"]),
            binding_id=str(row["binding_id"]),
            number=require_issue_number(int(row["number"])),
            head_ref=require_branch(str(row["head_ref"])),
            head_sha=require_sha(str(row["head_sha"])),
            base_ref=require_branch(str(row["base_ref"])),
            state=cast(PullState, state),
            merge_sha=merge_sha,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
    except (ValidationError, RegistryRejected, TypeError, ValueError):
        raise CheckpointCompatibilityError() from None


def _pull_values(record: GithubPullRequestRecord) -> tuple[object, ...]:
    return (
        record.scope,
        record.binding_id,
        record.number,
        record.head_ref,
        record.head_sha,
        record.base_ref,
        record.state,
        record.merge_sha,
        record.created_at,
        record.updated_at,
    )


def _check_from_row(row: sqlite3.Row) -> GithubCheckRecord:
    try:
        return GithubCheckRecord(
            scope=str(row["scope"]),
            binding_id=str(row["binding_id"]),
            check_id=require_token(str(row["check_id"]), "GitHub check is not allowed."),
            conclusion=_check_conclusion(str(row["conclusion"])),
            head_sha=require_sha(str(row["head_sha"])),
            recorded_at=str(row["recorded_at"]),
        )
    except (ValidationError, RegistryRejected):
        raise CheckpointCompatibilityError() from None


def _release_from_row(row: sqlite3.Row) -> GithubReleaseRecord:
    try:
        return GithubReleaseRecord(
            scope=str(row["scope"]),
            binding_id=str(row["binding_id"]),
            release_id=require_token(str(row["release_id"]), "GitHub release is not allowed."),
            tag=require_token(str(row["tag"]), "GitHub release is not allowed."),
            target_sha=require_sha(str(row["target_sha"])),
            created_at=str(row["created_at"]),
        )
    except (ValidationError, RegistryRejected):
        raise CheckpointCompatibilityError() from None


def _note_from_row(row: sqlite3.Row) -> GithubNoteRecord:
    try:
        kind = str(row["kind"])
        if kind not in {"comment", "review"}:
            raise RegistryRejected("GitHub review is not allowed.")
        return GithubNoteRecord(
            scope=str(row["scope"]),
            binding_id=str(row["binding_id"]),
            note_id=require_token(str(row["note_id"]), "GitHub review is not allowed."),
            kind=cast(NoteKind, kind),
            created_at=str(row["created_at"]),
        )
    except (ValidationError, RegistryRejected):
        raise CheckpointCompatibilityError() from None
