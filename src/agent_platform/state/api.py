"""Public directory, ledger, run, and work-item operations on a workflow store."""

from collections.abc import Sequence
from typing import Literal

from agent_platform.observability.audit import AuditEvent
from agent_platform.state.github import NoteKind, PullState
from agent_platform.state.models import (
    ChildOutcome,
    ChildOutcomeStatus,
    DelegationRecord,
    GateRecord,
    GithubBindingRecord,
    GithubCheckRecord,
    GithubNoteRecord,
    GithubPullRequestRecord,
    GithubRefRecord,
    GithubReleaseRecord,
    GrantRecord,
    LedgerRecord,
    OrganisationRecord,
    PermissionClass,
    RepositoryRecord,
    RoleRecord,
    RunRecord,
    RunStatus,
    SignalRecord,
    StateMigrationRequired,
    TeamRecord,
    ToolPermissionRecord,
    WorkflowCatalogEntry,
    WorkItemRecord,
    WorkItemStatus,
)
from agent_platform.state.registry import PlatformRegistry


class PlatformStateMixin:
    """Version 2 operations. A version 1 store fails closed until it is migrated."""

    scope: str
    _registry: PlatformRegistry | None

    def _require_registry(self) -> PlatformRegistry:
        if self._registry is None:
            raise StateMigrationRequired()
        return self._registry

    def append_ledger(
        self,
        event: AuditEvent,
        *,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> LedgerRecord:
        """Persist one redacted audit event."""
        return self._require_registry().append_ledger(
            self.scope,
            event,
            run_id=run_id,
            event_id=event_id,
        )

    def list_ledger(
        self,
        *,
        correlation_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[LedgerRecord, ...]:
        """Return durable audit events for this scope, oldest first."""
        return self._require_registry().list_ledger(
            self.scope,
            correlation_id=correlation_id,
            run_id=run_id,
        )

    def register_organisation(self, name: str, policy_version: str) -> OrganisationRecord:
        """Register the one organisation for this scope."""
        return self._require_registry().register_organisation(self.scope, name, policy_version)

    def get_organisation(self) -> OrganisationRecord:
        """Return the organisation registered for this scope."""
        return self._require_registry().get_organisation(self.scope)

    def register_role(
        self,
        name: str,
        tools: Sequence[str] = (),
        *,
        permission_class: PermissionClass = "implementation",
    ) -> RoleRecord:
        """Register a role and the tool names it may later be granted."""
        return self._require_registry().register_role(
            self.scope, name, tools, permission_class=permission_class
        )

    def get_role(self, name: str) -> RoleRecord:
        """Return one role. An unknown name fails closed."""
        return self._require_registry().get_role(self.scope, name)

    def list_roles(self) -> tuple[RoleRecord, ...]:
        """Return the roles in this scope."""
        return self._require_registry().list_roles(self.scope)

    def register_repository(self, name: str, remote: str, default_branch: str) -> RepositoryRecord:
        """Allowlist a repository remote. This does not call the remote."""
        return self._require_registry().register_repository(
            self.scope, name, remote, default_branch
        )

    def get_repository(self, name: str) -> RepositoryRecord:
        """Return one allowlisted repository."""
        return self._require_registry().get_repository(self.scope, name)

    def list_repositories(self) -> tuple[RepositoryRecord, ...]:
        """Return the repository allowlist."""
        return self._require_registry().list_repositories(self.scope)

    def register_workflow(
        self, name: str, *, graph_signature_hash: str | None = None
    ) -> WorkflowCatalogEntry:
        """Add a workflow name to the catalog."""
        return self._require_registry().register_workflow(
            self.scope, name, graph_signature_hash=graph_signature_hash
        )

    def set_workflow_signature(self, name: str, graph_signature_hash: str) -> WorkflowCatalogEntry:
        """Record a graph signature once. A different later value fails closed."""
        return self._require_registry().set_workflow_signature(
            self.scope, name, graph_signature_hash
        )

    def get_workflow(self, name: str) -> WorkflowCatalogEntry:
        """Return one catalog entry. An unknown workflow fails closed."""
        return self._require_registry().get_workflow(self.scope, name)

    def list_workflows(self) -> tuple[WorkflowCatalogEntry, ...]:
        """Return the workflow catalog."""
        return self._require_registry().list_workflows(self.scope)

    def register_team(
        self,
        name: str,
        *,
        roles: Sequence[str],
        workflows: Sequence[str] = (),
        delegations: Sequence[str] = (),
        team_id: str | None = None,
    ) -> TeamRecord:
        """Register a team. Unknown roles or workflows fail closed."""
        return self._require_registry().register_team(
            self.scope,
            name,
            roles=roles,
            workflows=workflows,
            delegations=delegations,
            team_id=team_id,
        )

    def get_team(self, team_id: str) -> TeamRecord:
        """Return one team."""
        return self._require_registry().get_team(self.scope, team_id)

    def list_teams(self) -> tuple[TeamRecord, ...]:
        """Return the teams in this scope."""
        return self._require_registry().list_teams(self.scope)

    def open_work_item(
        self,
        *,
        item_type: str,
        title: str,
        acceptance: str,
        parent_work_item_id: str | None = None,
        work_item_id: str | None = None,
    ) -> WorkItemRecord:
        """Open a draft work item. Acceptance text is hashed and not stored."""
        return self._require_registry().open_work_item(
            self.scope,
            item_type=item_type,
            title=title,
            acceptance=acceptance,
            parent_work_item_id=parent_work_item_id,
            work_item_id=work_item_id,
        )

    def get_work_item(self, work_item_id: str) -> WorkItemRecord:
        """Return one work item."""
        return self._require_registry().get_work_item(self.scope, work_item_id)

    def list_work_items(
        self, *, status: WorkItemStatus | None = None
    ) -> tuple[WorkItemRecord, ...]:
        """Return work items in this scope."""
        return self._require_registry().list_work_items(self.scope, status=status)

    def transition_work_item(self, work_item_id: str, status: WorkItemStatus) -> WorkItemRecord:
        """Apply one allowed work-item transition. Active runs block the change."""
        return self._require_registry().transition_work_item(self.scope, work_item_id, status)

    def start_run(
        self,
        workflow_name: str,
        correlation_id: str,
        *,
        parent_run_id: str | None = None,
        work_item_id: str | None = None,
        mutating: bool = False,
        run_id: str | None = None,
    ) -> RunRecord:
        """Start a catalogued workflow. An unknown name fails closed."""
        return self._require_registry().start_run(
            self.scope,
            workflow_name,
            correlation_id,
            parent_run_id=parent_run_id,
            work_item_id=work_item_id,
            mutating=mutating,
            run_id=run_id,
        )

    def get_run(self, run_id: str) -> RunRecord:
        """Return one run."""
        return self._require_registry().get_run(self.scope, run_id)

    def list_runs(
        self,
        *,
        workflow_name: str | None = None,
        status: RunStatus | None = None,
        work_item_id: str | None = None,
    ) -> tuple[RunRecord, ...]:
        """Return runs in this scope."""
        return self._require_registry().list_runs(
            self.scope,
            workflow_name=workflow_name,
            status=status,
            work_item_id=work_item_id,
        )

    def transition_run(self, run_id: str, status: RunStatus) -> RunRecord:
        """Move a run along its status machine. Pausing requires a bound checkpoint."""
        return self._require_registry().transition_run(self.scope, run_id, status)

    def allow_delegation(self, parent_workflow: str, child_workflow: str) -> DelegationRecord:
        """Allow one catalogued workflow to start another. Unknown names fail closed."""
        return self._require_registry().allow_delegation(
            self.scope, parent_workflow, child_workflow
        )

    def start_child_run(
        self,
        parent_run_id: str,
        workflow_name: str,
        correlation_id: str,
        *,
        work_item_id: str | None = None,
        mutating: bool = False,
        run_id: str | None = None,
    ) -> RunRecord:
        """Start a child run when the parent workflow is explicitly allowed to delegate."""
        return self._require_registry().start_child_run(
            self.scope,
            parent_run_id,
            workflow_name,
            correlation_id,
            work_item_id=work_item_id,
            mutating=mutating,
            run_id=run_id,
        )

    def pause_for_child(self, parent_run_id: str, child_run_id: str) -> RunRecord:
        """Pause a parent that already has a checkpoint until the child finishes."""
        return self._require_registry().pause_for_child(self.scope, parent_run_id, child_run_id)

    def finish_child_run(self, child_run_id: str, outcome: ChildOutcomeStatus) -> ChildOutcome:
        """Record a typed child outcome and signal the parent."""
        return self._require_registry().finish_child_run(self.scope, child_run_id, outcome)

    def get_child_outcome(self, child_run_id: str) -> ChildOutcome:
        """Return the typed outcome for a finished child run."""
        return self._require_registry().get_child_outcome(self.scope, child_run_id)

    def take_child_outcome(self, parent_run_id: str, child_run_id: str) -> ChildOutcome:
        """Consume the child signal and resume a paused parent."""
        return self._require_registry().take_child_outcome(self.scope, parent_run_id, child_run_id)

    def register_tool_permission(
        self, tool_name: str, permission_class: PermissionClass
    ) -> ToolPermissionRecord:
        """Classify a tool as implementation, merge, or release."""
        return self._require_registry().register_tool_permission(
            self.scope, tool_name, permission_class
        )

    def open_gate(
        self,
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
        """Open a named gate. Requested tools must match the role's permission class."""
        return self._require_registry().open_gate(
            self.scope,
            name,
            role=role,
            work_item_id=work_item_id,
            stage=stage,
            tools=tools,
            correlation_id=correlation_id,
            argument_hash=argument_hash,
            run_id=run_id,
            gate_id=gate_id,
        )

    def decide_gate(
        self,
        gate_id: str,
        *,
        approved: bool,
        repository: str | None = None,
        branch_prefix: str | None = None,
        expires_at: str | None = None,
        grant_id: str | None = None,
    ) -> tuple[GateRecord, GrantRecord | None]:
        """Record a gate decision. Approval copies a grant from the gate and does not widen it."""
        return self._require_registry().decide_gate(
            self.scope,
            gate_id,
            approved=approved,
            repository=repository,
            branch_prefix=branch_prefix,
            expires_at=expires_at,
            grant_id=grant_id,
        )

    def get_gate(self, gate_id: str) -> GateRecord:
        """Return one gate."""
        return self._require_registry().get_gate(self.scope, gate_id)

    def get_grant(self, grant_id: str) -> GrantRecord:
        """Return one grant."""
        return self._require_registry().get_grant(self.scope, grant_id)

    def authorize_grant(
        self,
        grant_id: str,
        *,
        tool_name: str,
        argument_hash: str,
        repository: str | None,
        branch: str | None,
    ) -> Literal["authorized", "replayed"]:
        """Reserve one grant use, or report that the same use already happened."""
        return self._require_registry().authorize_grant(
            self.scope,
            grant_id,
            tool_name=tool_name,
            argument_hash=argument_hash,
            repository=repository,
            branch=branch,
        )

    def finish_grant_use(
        self,
        grant_id: str,
        tool_name: str,
        argument_hash: str,
        *,
        status: Literal["completed", "failed"],
    ) -> None:
        """Record the outcome of a reserved grant use."""
        self._require_registry().finish_grant_use(
            self.scope,
            grant_id,
            tool_name,
            argument_hash,
            status=status,
        )

    def protect_branch(self, repository: str, branch: str) -> str:
        """Record a protected branch. The default branch is always protected."""
        return self._require_registry().protect_branch(self.scope, repository, branch)

    def protected_branches(self, repository: str) -> tuple[str, ...]:
        """Return the default branch and any extra protected branches."""
        return self._require_registry().protected_branches(self.scope, repository)

    def bind_github_repository(
        self,
        *,
        repository: str,
        work_item_id: str,
        run_id: str | None = None,
        issue_number: int | None = None,
        binding_id: str | None = None,
    ) -> GithubBindingRecord:
        """Bind a work item to an allowlisted GitHub repository. This does not call GitHub."""
        return self._require_registry().bind_github_repository(
            self.scope,
            repository=repository,
            work_item_id=work_item_id,
            run_id=run_id,
            issue_number=issue_number,
            binding_id=binding_id,
        )

    def get_github_binding(self, binding_id: str) -> GithubBindingRecord:
        """Return one GitHub binding."""
        return self._require_registry().get_github_binding(self.scope, binding_id)

    def note_github_issue(self, binding_id: str, *, content_hash: str) -> GithubBindingRecord:
        """Store a hash of the GitHub issue content. The text is not stored."""
        return self._require_registry().note_github_issue(
            self.scope, binding_id, content_hash=content_hash
        )

    def find_github_ref(self, binding_id: str, ref: str) -> GithubRefRecord | None:
        """Return a recorded branch ref, or none when it has not been recorded."""
        return self._require_registry().find_github_ref(self.scope, binding_id, ref)

    def list_github_refs(self, binding_id: str) -> tuple[GithubRefRecord, ...]:
        """Return branch refs recorded for one binding."""
        return self._require_registry().list_github_refs(self.scope, binding_id)

    def record_github_ref(self, binding_id: str, *, ref: str, sha: str) -> GithubRefRecord:
        """Record a branch ref and commit SHA. Protected branches are rejected."""
        return self._require_registry().record_github_ref(self.scope, binding_id, ref=ref, sha=sha)

    def find_github_pull_request(self, binding_id: str) -> GithubPullRequestRecord | None:
        """Return the pull request recorded for one binding."""
        return self._require_registry().find_github_pull_request(self.scope, binding_id)

    def record_github_pull_request(
        self,
        binding_id: str,
        *,
        number: int,
        head_ref: str,
        head_sha: str,
        base_ref: str,
        state: PullState,
        merge_sha: str | None = None,
    ) -> GithubPullRequestRecord:
        """Record pull request identifiers. Title and body are not stored."""
        return self._require_registry().record_github_pull_request(
            self.scope,
            binding_id,
            number=number,
            head_ref=head_ref,
            head_sha=head_sha,
            base_ref=base_ref,
            state=state,
            merge_sha=merge_sha,
        )

    def list_github_checks(self, binding_id: str) -> tuple[GithubCheckRecord, ...]:
        """Return check identifiers recorded for one binding."""
        return self._require_registry().list_github_checks(self.scope, binding_id)

    def record_github_check(
        self,
        binding_id: str,
        *,
        check_id: str,
        conclusion: str,
        head_sha: str,
    ) -> GithubCheckRecord:
        """Record a GitHub check identifier and conclusion."""
        return self._require_registry().record_github_check(
            self.scope,
            binding_id,
            check_id=check_id,
            conclusion=conclusion,
            head_sha=head_sha,
        )

    def list_github_releases(self, binding_id: str) -> tuple[GithubReleaseRecord, ...]:
        """Return releases recorded for one binding."""
        return self._require_registry().list_github_releases(self.scope, binding_id)

    def record_github_release(
        self,
        binding_id: str,
        *,
        release_id: str,
        tag: str,
        target_sha: str,
    ) -> GithubReleaseRecord:
        """Record a GitHub release identifier."""
        return self._require_registry().record_github_release(
            self.scope,
            binding_id,
            release_id=release_id,
            tag=tag,
            target_sha=target_sha,
        )

    def record_github_note(
        self, binding_id: str, *, note_id: str, kind: NoteKind
    ) -> GithubNoteRecord:
        """Record a comment or review identifier. The text is not stored."""
        return self._require_registry().record_github_note(
            self.scope, binding_id, note_id=note_id, kind=kind
        )

    def publish_github_signal(
        self,
        *,
        name: str,
        target_run_id: str,
        correlation_id: str,
        outcome_status: str,
        subject_kind: str,
        subject_id: str,
    ) -> SignalRecord:
        """Record a GitHub outcome a workflow can consume."""
        return self._require_registry().publish_github_signal(
            self.scope,
            name=name,
            target_run_id=target_run_id,
            correlation_id=correlation_id,
            outcome_status=outcome_status,
            subject_kind=subject_kind,
            subject_id=subject_id,
        )

    def take_github_signal(self, run_id: str, signal_id: str) -> SignalRecord:
        """Consume one GitHub signal and resume a paused run."""
        return self._require_registry().take_github_signal(self.scope, run_id, signal_id)

    def pause_for_signal(self, run_id: str) -> RunRecord:
        """Pause a run that already has a checkpoint until a signal arrives."""
        return self._require_registry().pause_for_signal(self.scope, run_id)

    def list_signals(self, *, target_run_id: str | None = None) -> tuple[SignalRecord, ...]:
        """Return signals in this scope, oldest first."""
        return self._require_registry().list_signals(self.scope, target_run_id=target_run_id)
