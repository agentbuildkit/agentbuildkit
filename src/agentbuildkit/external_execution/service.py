"""Governed delegation to an external engineering execution provider.

The platform derives the repository and branch, checks the implementation grant,
starts the provider, stores the binding, and returns. It does not keep a process
open while the provider works. Completion arrives later as a signal.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from agentbuildkit.external_execution.models import (
    IMPLEMENTATION_COMPLETION,
    IMPLEMENTATION_PROHIBITIONS,
    ExternalExecutionRecord,
    ExternalExecutionStatus,
    ImplementationAssignment,
    ProviderObservation,
    ProviderStart,
    external_execution_ids,
)
from agentbuildkit.external_execution.provider import (
    ExecutionConflict,
    ExecutionContactError,
    ExecutionMalformed,
    ExecutionNotFound,
    ExecutionRejected,
    ExternalExecutionProvider,
)
from agentbuildkit.guardrails import (
    ApprovalDecision,
    RepeatedGrantUse,
    ToolEffect,
    execute_tool,
    prepare_tool,
)
from agentbuildkit.observability import AuditLog, argument_hash
from agentbuildkit.state.models import (
    SIGNAL_IMPLEMENTATION_COMPLETED,
    SIGNAL_IMPLEMENTATION_FAILED,
    SIGNAL_IMPLEMENTATION_STARTED,
    GrantRecord,
    PermissionClass,
    RegistryRejected,
    assert_implementation_branch,
    branch_in_prefix,
    github_owner_repo,
    require_token,
)
from agentbuildkit.workflows import WorkflowStateStore

TOOL_DELEGATE_IMPLEMENTATION = "delegate_implementation"
TOOL_FOLLOW_UP_IMPLEMENTATION = "follow_up_implementation"
EXECUTION_TOOL_PERMISSIONS: dict[str, PermissionClass] = {
    TOOL_DELEGATE_IMPLEMENTATION: "implementation",
    TOOL_FOLLOW_UP_IMPLEMENTATION: "implementation",
}
_TERMINAL = frozenset({"completed", "failed", "cancelled", "expired"})


@dataclass(frozen=True)
class _Plan:
    tool_name: str
    grant_id: str
    attempt_id: str
    correlation_id: str
    work_item_id: str
    repository: str
    branch: str
    binding_id: str
    run_id: str
    organisation: str
    argument_hash: str
    arguments: dict[str, str]


class ExecutionService:
    """One provider bound to one organisation store."""

    def __init__(
        self,
        *,
        store: WorkflowStateStore,
        provider: ExternalExecutionProvider,
        audit_log: AuditLog,
    ) -> None:
        self._store = store
        self._provider = provider
        self._audit_log = audit_log

    async def delegate_implementation(
        self,
        *,
        work_item_id: str,
        grant_id: str,
        attempt_id: str,
        correlation_id: str,
    ) -> ExternalExecutionRecord | RepeatedGrantUse:
        """Start one external implementation on the grant's authorised branch.

        Repository and branch are read from the work item, the GitHub binding,
        and the grant. A repeated call does not start a second execution.
        """
        plan = self._plan(
            tool_name=TOOL_DELEGATE_IMPLEMENTATION,
            work_item_id=work_item_id,
            grant_id=grant_id,
            attempt_id=attempt_id,
            correlation_id=correlation_id,
        )
        existing = self._store.find_external_execution_attempt(plan.grant_id, plan.attempt_id)
        keep = None
        if existing is not None:
            self._same_provider(existing)
            if existing.status == "startup_failed":
                self._store.release_unstarted_grant_use(
                    plan.grant_id,
                    plan.tool_name,
                    plan.argument_hash,
                    attempt_id=plan.attempt_id,
                )
                keep = existing.execution_id
            elif existing.status == "starting":
                return await self._reconcile(existing, plan)
            else:
                return _replay(plan)
        self._reject_other_execution(plan.work_item_id, keep=keep)
        holder: list[ExternalExecutionRecord] = []

        async def perform(
            repository: str,
            branch: str,
            binding_id: str,
            work_item_id: str,
            attempt_id: str,
        ) -> str:
            _same_arguments(
                plan,
                repository=repository,
                branch=branch,
                binding_id=binding_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
            updated = await self._start_or_adopt(self._reserve(plan), plan, retry=True)
            holder.append(updated)
            return updated.execution_id

        result = await self._governed(plan, perform)
        if isinstance(result, RepeatedGrantUse):
            return result
        return holder[0]

    async def follow_up_implementation(
        self,
        *,
        execution_id: str,
        grant_id: str,
        attempt_id: str,
        correlation_id: str,
    ) -> ExternalExecutionRecord | RepeatedGrantUse:
        """Continue an existing execution. This does not create another agent."""
        current = self._known(execution_id)
        if current.status not in _TERMINAL or current.provider_run_id is None:
            raise RegistryRejected("External execution is not allowed.")
        if attempt_id == current.attempt_id:
            raise RegistryRejected("External execution is not allowed.")
        plan = self._plan(
            tool_name=TOOL_FOLLOW_UP_IMPLEMENTATION,
            work_item_id=current.work_item_id,
            grant_id=grant_id,
            attempt_id=attempt_id,
            correlation_id=correlation_id,
            execution_id=current.execution_id,
        )
        if plan.repository != current.repository or plan.branch != current.branch:
            raise RegistryRejected("Grant does not allow this branch.")
        holder: list[ExternalExecutionRecord] = []

        async def perform(
            repository: str,
            branch: str,
            execution_id: str,
            work_item_id: str,
            attempt_id: str,
        ) -> str:
            if execution_id != current.execution_id or work_item_id != current.work_item_id:
                raise RegistryRejected("External execution is not allowed.")
            if repository != current.repository or branch != current.branch:
                raise RegistryRejected("Grant does not allow this branch.")
            if attempt_id != plan.attempt_id:
                raise RegistryRejected("External execution is not allowed.")
            assignment = self._assignment(plan)
            try:
                started = await self._provider.follow_up(
                    assignment,
                    external_id=current.external_id,
                    correlation_id=plan.correlation_id,
                )
            except ExecutionConflict as exc:
                raise RegistryRejected("External execution is not allowed.") from exc
            if started.external_id != current.external_id:
                raise ExecutionMalformed("External execution response is not allowed.")
            updated = self._mark(
                current,
                "running",
                provider_run_id=started.provider_run_id,
                grant_id=plan.grant_id,
                attempt_id=plan.attempt_id,
                argument_hash=plan.argument_hash,
                assignment_hash=argument_hash(assignment.model_dump()),
                completed=False,
            )
            self._emit(updated, plan.correlation_id)
            holder.append(updated)
            return updated.execution_id

        result = await self._governed(plan, perform)
        if isinstance(result, RepeatedGrantUse):
            return result
        return holder[0]

    async def cancel_implementation(
        self, execution_id: str, *, correlation_id: str
    ) -> ExternalExecutionRecord:
        """Cancel the provider run. Cancellation does not start another execution."""
        record = self._known(execution_id)
        correlation = require_token(correlation_id, "Directory name is not allowed.")
        if record.provider_run_id is None or record.status not in {"running", "lost_contact"}:
            raise RegistryRejected("External execution is not allowed.")
        try:
            await self._provider.cancel(
                external_id=record.external_id,
                provider_run_id=record.provider_run_id,
                correlation_id=correlation,
            )
        except ExecutionConflict:
            pass
        except (ExecutionContactError, ExecutionNotFound, ExecutionMalformed):
            self._mark(record, "lost_contact")
            raise
        return await self.observe_execution(record.execution_id, correlation_id=correlation)

    async def observe_execution(
        self, execution_id: str, *, correlation_id: str
    ) -> ExternalExecutionRecord:
        """Read provider status and publish at most one terminal signal."""
        record = self._known(execution_id)
        correlation = require_token(correlation_id, "Directory name is not allowed.")
        if record.status in _TERMINAL or record.status == "startup_failed":
            return record
        try:
            observation = await self._provider.inspect(
                external_id=record.external_id,
                provider_run_id=record.provider_run_id,
                correlation_id=correlation,
            )
        except ExecutionNotFound:
            if record.provider_run_id is None:
                return self._mark(record, "startup_failed", completed=True)
            self._mark(record, "lost_contact")
            raise
        except (ExecutionContactError, ExecutionMalformed):
            self._mark(record, "lost_contact")
            raise
        return self._apply(record, observation, correlation)

    def get_by_external_id(self, external_id: str) -> ExternalExecutionRecord:
        """Return the binding for a provider execution id. Unknown ids fail closed."""
        return self._store.get_external_execution_by_external_id(self._provider.name, external_id)

    async def _reconcile(
        self, record: ExternalExecutionRecord, plan: _Plan
    ) -> ExternalExecutionRecord:
        if (
            record.work_item_id != plan.work_item_id
            or record.repository != plan.repository
            or record.branch != plan.branch
            or record.argument_hash != plan.argument_hash
        ):
            raise RegistryRejected("External execution is not allowed.")
        try:
            updated = await self._start_or_adopt(record, plan, retry=True)
        except Exception:
            self._finish_use(plan, "failed")
            raise
        self._finish_use(plan, "completed")
        return updated

    async def _start_or_adopt(
        self, record: ExternalExecutionRecord, plan: _Plan, *, retry: bool
    ) -> ExternalExecutionRecord:
        assignment = self._assignment(plan)
        try:
            started = await self._provider.start(
                assignment,
                external_id=record.external_id,
                correlation_id=plan.correlation_id,
            )
        except ExecutionConflict:
            return await self._adopt(record, plan.correlation_id)
        except (ExecutionContactError, ExecutionMalformed):
            return await self._recover(record, plan, retry=retry)
        except ExecutionRejected:
            self._mark(record, "startup_failed", completed=True)
            raise
        else:
            try:
                return self._accept_start(record, started, assignment, plan.correlation_id)
            except ExecutionMalformed:
                return await self._recover(record, plan, retry=False)

    async def _recover(
        self, record: ExternalExecutionRecord, plan: _Plan, *, retry: bool
    ) -> ExternalExecutionRecord:
        try:
            observation = await self._provider.inspect(
                external_id=record.external_id,
                provider_run_id=record.provider_run_id,
                correlation_id=plan.correlation_id,
            )
        except ExecutionNotFound:
            if not retry:
                self._mark(record, "startup_failed", completed=True)
                raise ExecutionRejected("External execution did not start.") from None
            return await self._start_or_adopt(record, plan, retry=False)
        except (ExecutionContactError, ExecutionMalformed):
            self._mark(record, "lost_contact")
            raise
        return self._apply(record, observation, plan.correlation_id)

    async def _adopt(
        self, record: ExternalExecutionRecord, correlation_id: str
    ) -> ExternalExecutionRecord:
        try:
            observation = await self._provider.inspect(
                external_id=record.external_id,
                provider_run_id=record.provider_run_id,
                correlation_id=correlation_id,
            )
        except (ExecutionNotFound, ExecutionContactError, ExecutionMalformed):
            self._mark(record, "lost_contact")
            raise ExecutionContactError("External execution contact was lost.") from None
        return self._apply(record, observation, correlation_id)

    def _accept_start(
        self,
        record: ExternalExecutionRecord,
        started: ProviderStart,
        assignment: ImplementationAssignment,
        correlation_id: str,
    ) -> ExternalExecutionRecord:
        if started.external_id != record.external_id:
            raise ExecutionMalformed("External execution response is not allowed.")
        updated = self._mark(
            record,
            "running",
            provider_run_id=started.provider_run_id,
            assignment_hash=argument_hash(assignment.model_dump()),
            completed=False,
        )
        self._emit(updated, correlation_id)
        return updated

    def _apply(
        self,
        record: ExternalExecutionRecord,
        observation: ProviderObservation,
        correlation_id: str,
    ) -> ExternalExecutionRecord:
        if observation.external_id != record.external_id:
            self._mark(record, "lost_contact")
            raise ExecutionMalformed("External execution response is not allowed.")
        if (
            record.provider_run_id is not None
            and observation.provider_run_id != record.provider_run_id
        ):
            self._mark(record, "lost_contact")
            raise ExecutionMalformed("External execution response is not allowed.")
        digest = _observation_hash(observation)
        outside = _outside_authorised_branch(record, observation, self._repository_url(record))
        status = _status_for(observation, outside=outside)
        if status is None:
            self._mark(record, "lost_contact", observation_hash=digest)
            raise ExecutionMalformed("External execution response is not allowed.")
        updated = self._mark(
            record,
            status,
            provider_run_id=observation.provider_run_id,
            observation_hash=digest,
            completed=status in _TERMINAL,
        )
        self._emit(updated, correlation_id)
        return updated

    def _reserve(self, plan: _Plan) -> ExternalExecutionRecord:
        execution_id, external_id = external_execution_ids(
            self._store.scope, plan.grant_id, plan.attempt_id
        )
        assignment = self._assignment(plan)
        now = _now()
        existing = self._store.find_external_execution_attempt(plan.grant_id, plan.attempt_id)
        if existing is None:
            record = ExternalExecutionRecord(
                scope=self._store.scope,
                execution_id=execution_id,
                provider=self._provider.name,
                external_id=external_id,
                provider_run_id=None,
                organisation=plan.organisation,
                work_item_id=plan.work_item_id,
                run_id=plan.run_id,
                repository=plan.repository,
                branch=plan.branch,
                grant_id=plan.grant_id,
                attempt_id=plan.attempt_id,
                status="starting",
                argument_hash=plan.argument_hash,
                assignment_hash=argument_hash(assignment.model_dump()),
                created_at=now,
                updated_at=now,
            )
            return self._store.save_external_execution(record)
        if existing.status != "startup_failed" or existing.external_id != external_id:
            raise RegistryRejected("External execution already exists.")
        return self._mark(
            existing,
            "starting",
            provider_run_id=None,
            argument_hash=plan.argument_hash,
            assignment_hash=argument_hash(assignment.model_dump()),
            observation_hash=None,
            completed=False,
        )

    def _plan(
        self,
        *,
        tool_name: str,
        work_item_id: str,
        grant_id: str,
        attempt_id: str,
        correlation_id: str,
        execution_id: str | None = None,
    ) -> _Plan:
        correlation = require_token(correlation_id, "Directory name is not allowed.")
        attempt = require_token(attempt_id, "Directory name is not allowed.")
        item = self._store.get_work_item(work_item_id)
        grant = self._store.get_grant(grant_id)
        if (
            grant.permission_class != "implementation"
            or tool_name not in grant.tools
            or grant.work_item_id != item.work_item_id
        ):
            raise RegistryRejected("Grant does not allow this tool.")
        if grant.repository is None or grant.branch_prefix is None:
            raise RegistryRejected("Grant does not allow this repository.")
        bindings = self._store.github_bindings_for_work_item(grant.repository, item.work_item_id)
        if len(bindings) != 1 or bindings[0].run_id is None:
            raise RegistryRejected("Grant does not allow this repository.")
        binding = bindings[0]
        run_id = binding.run_id
        if run_id is None:
            raise RegistryRejected("Grant does not allow this repository.")
        self._store.get_run(run_id)
        branch = _authorised_branch(self._store, binding.binding_id, grant)
        if execution_id is None:
            arguments = delegation_arguments(
                work_item_id=item.work_item_id,
                attempt_id=attempt,
                repository=grant.repository,
                branch=branch,
                binding_id=binding.binding_id,
            )
        else:
            arguments = follow_up_arguments(
                work_item_id=item.work_item_id,
                attempt_id=attempt,
                repository=grant.repository,
                branch=branch,
                execution_id=execution_id,
            )
        return _Plan(
            tool_name=tool_name,
            grant_id=grant.grant_id,
            attempt_id=attempt,
            correlation_id=correlation,
            work_item_id=item.work_item_id,
            repository=grant.repository,
            branch=branch,
            binding_id=binding.binding_id,
            run_id=run_id,
            organisation=self._store.get_organisation().name,
            argument_hash=argument_hash(arguments),
            arguments=arguments,
        )

    def _assignment(self, plan: _Plan) -> ImplementationAssignment:
        item = self._store.get_work_item(plan.work_item_id)
        binding = self._store.get_github_binding(plan.binding_id)
        policy = self._store.get_execution_policy(plan.repository)
        if binding.issue_number is not None:
            source = f"github-issue:{binding.issue_number}"
        else:
            source = f"acceptance-hash:{item.acceptance_hash}"
        constraints = () if policy is None else policy.constraints
        commands = () if policy is None else policy.validation_commands
        assignment = ImplementationAssignment(
            work_item_id=item.work_item_id,
            objective=item.title,
            acceptance_source=source,
            repository_url=self._repository_url_name(plan.repository),
            authorised_branch=plan.branch,
            constraints=constraints,
            validation_commands=commands,
            prohibitions=IMPLEMENTATION_PROHIBITIONS,
            completion_output=IMPLEMENTATION_COMPLETION,
        )
        return assignment

    def _repository_url(self, record: ExternalExecutionRecord) -> str:
        return self._repository_url_name(record.repository)

    def _repository_url_name(self, repository: str) -> str:
        owner, repo = github_owner_repo(self._store.get_repository(repository).remote)
        return f"https://github.com/{owner}/{repo}"

    def _mark(
        self,
        record: ExternalExecutionRecord,
        status: ExternalExecutionStatus,
        *,
        provider_run_id: str | None = None,
        grant_id: str | None = None,
        attempt_id: str | None = None,
        argument_hash: str | None = None,
        assignment_hash: str | None = None,
        observation_hash: str | None = None,
        completed: bool = False,
    ) -> ExternalExecutionRecord:
        now = _now()
        run_id = record.provider_run_id if provider_run_id is None else provider_run_id
        if status == "starting":
            run_id = None
        updated = record.model_copy(
            update={
                "status": status,
                "provider_run_id": run_id,
                "grant_id": record.grant_id if grant_id is None else grant_id,
                "attempt_id": record.attempt_id if attempt_id is None else attempt_id,
                "argument_hash": record.argument_hash if argument_hash is None else argument_hash,
                "assignment_hash": (
                    record.assignment_hash if assignment_hash is None else assignment_hash
                ),
                "observation_hash": (
                    record.observation_hash if observation_hash is None else observation_hash
                ),
                "updated_at": now,
                "completed_at": now if completed else None,
            }
        )
        return self._store.save_external_execution(updated)

    def _emit(self, record: ExternalExecutionRecord, correlation_id: str) -> None:
        signal = _signal_for(record.status)
        if signal is None or record.provider_run_id is None:
            return
        name, outcome = signal
        self._store.publish_execution_signal(
            name=name,
            target_run_id=record.run_id,
            correlation_id=correlation_id,
            outcome_status=outcome,
            subject_id=record.provider_run_id,
        )

    def _same_provider(self, record: ExternalExecutionRecord) -> None:
        if record.provider != self._provider.name:
            raise RegistryRejected("External execution is unknown.")

    def _known(self, execution_id: str) -> ExternalExecutionRecord:
        record = self._store.get_external_execution(execution_id)
        self._same_provider(record)
        return record

    def _reject_other_execution(self, work_item_id: str, keep: str | None) -> None:
        for record in self._store.list_external_executions(work_item_id):
            if keep is not None and record.execution_id == keep:
                continue
            raise RegistryRejected("External execution already exists.")

    def _finish_use(self, plan: _Plan, status: Literal["completed", "failed"]) -> None:
        try:
            self._store.finish_grant_use(
                plan.grant_id,
                plan.tool_name,
                plan.argument_hash,
                status=status,
            )
        except RegistryRejected:
            return

    async def _governed(
        self,
        plan: _Plan,
        perform: Callable[..., Awaitable[str]],
    ) -> object:
        tool = prepare_tool(
            perform,
            effect=ToolEffect.SIDE_EFFECT,
            name=plan.tool_name,
            description=plan.tool_name,
        )
        return await execute_tool(
            tool,
            plan.arguments,
            audit_log=self._audit_log,
            correlation_id=plan.correlation_id,
            agent_name=self._provider.name,
            decision=ApprovalDecision(plan.tool_name, approved=True),
            grant_id=plan.grant_id,
            grants=self._store,
        )


def register_execution_tool_permissions(store: WorkflowStateStore) -> None:
    """Register delegation tools in the implementation permission class."""
    for tool_name, permission in EXECUTION_TOOL_PERMISSIONS.items():
        store.register_tool_permission(tool_name, permission)


def delegation_arguments(
    *,
    work_item_id: str,
    attempt_id: str,
    repository: str,
    branch: str,
    binding_id: str,
) -> dict[str, str]:
    """Arguments hashed into an implementation grant. The model does not supply them."""
    return {
        "attempt_id": attempt_id,
        "binding_id": binding_id,
        "branch": branch,
        "repository": repository,
        "work_item_id": work_item_id,
    }


def follow_up_arguments(
    *,
    work_item_id: str,
    attempt_id: str,
    repository: str,
    branch: str,
    execution_id: str,
) -> dict[str, str]:
    """Arguments hashed into a follow-up grant for an existing execution."""
    return {
        "attempt_id": attempt_id,
        "branch": branch,
        "execution_id": execution_id,
        "repository": repository,
        "work_item_id": work_item_id,
    }


def _authorised_branch(store: WorkflowStateStore, binding_id: str, grant: GrantRecord) -> str:
    if grant.branch_prefix is None or grant.repository is None:
        raise RegistryRejected("Grant does not allow this branch.")
    protected = set(store.protected_branches(grant.repository))
    matches: list[str] = []
    for ref in store.list_github_refs(binding_id):
        try:
            name = assert_implementation_branch(ref.ref, protected)
        except RegistryRejected:
            continue
        if branch_in_prefix(name, grant.branch_prefix):
            matches.append(name)
    if len(matches) != 1:
        raise RegistryRejected("Grant does not allow this branch.")
    return matches[0]


def _same_arguments(
    plan: _Plan,
    *,
    repository: str,
    branch: str,
    binding_id: str,
    work_item_id: str,
    attempt_id: str,
) -> None:
    if (
        repository != plan.repository
        or branch != plan.branch
        or binding_id != plan.binding_id
        or work_item_id != plan.work_item_id
        or attempt_id != plan.attempt_id
    ):
        raise RegistryRejected("Grant does not allow this repository.")


def _outside_authorised_branch(
    record: ExternalExecutionRecord, observation: ProviderObservation, repository_url: str
) -> bool:
    if observation.pull_request_opened:
        return True
    if not observation.branches:
        return observation.phase == "completed"
    if len(observation.branches) != 1:
        return True
    branch = observation.branches[0]
    return branch.branch != record.branch or not _same_repository(
        branch.repository_url, repository_url
    )


def _status_for(
    observation: ProviderObservation, *, outside: bool
) -> ExternalExecutionStatus | None:
    if outside or observation.phase == "failed":
        return "failed"
    if observation.phase == "cancelled":
        return "cancelled"
    if observation.phase == "expired":
        return "expired"
    if observation.phase in {"creating", "running"}:
        return "running"
    if observation.phase == "completed":
        return "completed"
    return None


def _signal_for(status: ExternalExecutionStatus) -> tuple[str, str] | None:
    if status == "running":
        return SIGNAL_IMPLEMENTATION_STARTED, "started"
    if status == "completed":
        return SIGNAL_IMPLEMENTATION_COMPLETED, "completed"
    if status == "failed":
        return SIGNAL_IMPLEMENTATION_FAILED, "failed"
    if status == "cancelled":
        return SIGNAL_IMPLEMENTATION_FAILED, "cancelled"
    if status == "expired":
        return SIGNAL_IMPLEMENTATION_FAILED, "expired"
    return None


def _observation_hash(observation: ProviderObservation) -> str:
    payload: dict[str, object] = {
        "branches": [
            {"branch": item.branch, "repository_url": item.repository_url}
            for item in observation.branches
        ],
        "external_id": observation.external_id,
        "phase": observation.phase,
        "provider_run_id": observation.provider_run_id,
        "pull_request_opened": observation.pull_request_opened,
    }
    return argument_hash(payload)


def _same_repository(left: str, right: str) -> bool:
    return _canonical_repository(left) == _canonical_repository(right)


def _canonical_repository(url: str) -> str:
    text = url.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.strip("/").lower()


def _replay(plan: _Plan) -> RepeatedGrantUse:
    return RepeatedGrantUse(
        grant_id=plan.grant_id,
        tool_name=plan.tool_name,
        argument_hash=plan.argument_hash,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
