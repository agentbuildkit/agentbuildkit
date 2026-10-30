"""External engineering execution provider contract.

Cursor is one implementation of this contract. The platform does not treat a
provider name as authority to choose a repository, a branch, or a credential.
"""

from typing import Literal, Protocol

from agentbuildkit.external_execution.models import (
    ImplementationAssignment,
    ProviderBranch,
    ProviderObservation,
    ProviderStart,
)


class ExecutionError(Exception):
    """An external execution call failed. The message does not include a payload."""


class ExecutionContactError(ExecutionError):
    """The provider could not be reached. An execution may already exist."""


class ExecutionRejected(ExecutionError):
    """The provider refused the call before an execution existed."""


class ExecutionConflict(ExecutionError):
    """The provider already has this execution."""


class ExecutionNotFound(ExecutionError):
    """The provider has no execution with this id."""


class ExecutionMalformed(ExecutionError):
    """The provider response did not match the contract."""


class ExternalExecutionProvider(Protocol):
    """Operations an engineering execution provider must support.

    ``start`` must use the supplied external id so a retry cannot create a
    second execution. ``follow_up`` continues that same execution.
    """

    name: str

    async def start(
        self,
        assignment: ImplementationAssignment,
        *,
        external_id: str,
        correlation_id: str,
    ) -> ProviderStart: ...

    async def inspect(
        self,
        *,
        external_id: str,
        provider_run_id: str | None,
        correlation_id: str,
    ) -> ProviderObservation: ...

    async def follow_up(
        self,
        assignment: ImplementationAssignment,
        *,
        external_id: str,
        correlation_id: str,
    ) -> ProviderStart: ...

    async def cancel(
        self,
        *,
        external_id: str,
        provider_run_id: str,
        correlation_id: str,
    ) -> None: ...


class FakeExecutionProvider:
    """In-memory provider for offline tests. It does not call a network."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.starts: list[ImplementationAssignment] = []
        self.follow_ups: list[ImplementationAssignment] = []
        self.cancels: list[tuple[str, str]] = []
        self.start_errors: list[Exception] = []
        self.inspect_errors: list[Exception] = []
        self.inspect_results: list[ProviderObservation] = []
        self._runs: dict[str, ProviderObservation] = {}
        self._counter = 0

    async def start(
        self,
        assignment: ImplementationAssignment,
        *,
        external_id: str,
        correlation_id: str,
    ) -> ProviderStart:
        del correlation_id
        if self.start_errors:
            raise self.start_errors.pop(0)
        if external_id in self._runs:
            raise ExecutionConflict("External execution already exists.")
        self.starts.append(assignment)
        return self._remember(external_id, assignment, phase="creating")

    async def inspect(
        self,
        *,
        external_id: str,
        provider_run_id: str | None,
        correlation_id: str,
    ) -> ProviderObservation:
        del correlation_id
        if self.inspect_errors:
            raise self.inspect_errors.pop(0)
        if self.inspect_results:
            observed = self.inspect_results.pop(0)
            self._runs[observed.external_id] = observed
            return observed
        found = self._runs.get(external_id)
        if found is None or (
            provider_run_id is not None and found.provider_run_id != provider_run_id
        ):
            raise ExecutionNotFound("External execution is unknown.")
        return found

    async def follow_up(
        self,
        assignment: ImplementationAssignment,
        *,
        external_id: str,
        correlation_id: str,
    ) -> ProviderStart:
        del correlation_id
        if external_id not in self._runs:
            raise ExecutionNotFound("External execution is unknown.")
        if self.start_errors:
            raise self.start_errors.pop(0)
        self.follow_ups.append(assignment)
        return self._remember(external_id, assignment, phase="creating")

    async def cancel(
        self,
        *,
        external_id: str,
        provider_run_id: str,
        correlation_id: str,
    ) -> None:
        del correlation_id
        found = self._runs.get(external_id)
        if found is None or found.provider_run_id != provider_run_id:
            raise ExecutionNotFound("External execution is unknown.")
        self.cancels.append((external_id, provider_run_id))
        self._runs[external_id] = found.model_copy(update={"phase": "cancelled"})

    def _remember(
        self,
        external_id: str,
        assignment: ImplementationAssignment,
        *,
        phase: Literal["creating", "running"],
    ) -> ProviderStart:
        self._counter += 1
        run_id = f"run-{self._counter:08d}-0000-4000-8000-000000000000"
        start = ProviderStart(external_id=external_id, provider_run_id=run_id, phase=phase)
        self._runs[external_id] = ProviderObservation(
            external_id=external_id,
            provider_run_id=run_id,
            phase=phase,
            branches=(
                ProviderBranch(
                    repository_url=assignment.repository_url,
                    branch=assignment.authorised_branch,
                ),
            ),
            pull_request_opened=False,
        )
        return start
