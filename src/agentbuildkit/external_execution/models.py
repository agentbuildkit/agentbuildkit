"""Provider-neutral implementation assignment and external execution records.

These models store identifiers, statuses, and hashes. They do not store prompts,
provider transcripts, credentials, grants, or repository file contents.
"""

import uuid
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agentbuildkit.state.models import RegistryRejected, require_label

ExternalExecutionStatus = Literal[
    "starting",
    "startup_failed",
    "running",
    "lost_contact",
    "completed",
    "failed",
    "cancelled",
    "expired",
]
ProviderPhase = Literal["creating", "running", "completed", "failed", "cancelled", "expired"]
_ASSIGNMENT_NAMESPACE = uuid.UUID("6f1b8e3a-9c4d-4a1e-8b7c-2d5e6f708192")
_POLICY_LIMIT = 20

IMPLEMENTATION_PROHIBITIONS: tuple[str, ...] = (
    "Do not merge to the default branch or any protected branch.",
    "Do not create a pull request.",
    "Do not create a production release.",
    "Do not push to any branch other than the authorised branch.",
    "Do not access any other repository.",
    "Do not modify another work item.",
    "Do not read or write credentials, tokens, or grants.",
)
IMPLEMENTATION_COMPLETION = (
    "Leave the authorised branch updated with the implementation and stop. "
    "Do not open a pull request. The platform validates the branch afterwards."
)


class ImplementationAssignment(BaseModel):
    """The stable contract an engineering execution provider receives.

    Repository, branch, and prohibitions come from platform policy. The model
    cannot add a repository, a branch, a grant, or a credential.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    work_item_id: str
    objective: str
    acceptance_source: str
    repository_url: str
    authorised_branch: str
    constraints: tuple[str, ...]
    validation_commands: tuple[str, ...]
    prohibitions: tuple[str, ...]
    completion_output: str


class ExecutionPolicyRecord(BaseModel):
    """Repository constraints the platform may include in an assignment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    repository: str
    constraints: tuple[str, ...]
    validation_commands: tuple[str, ...]


class ExternalExecutionRecord(BaseModel):
    """Control-plane binding for one external implementation execution.

    The provider keeps the transcript. GitHub keeps the repository contents.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    execution_id: str
    provider: str
    external_id: str
    provider_run_id: str | None = None
    organisation: str
    work_item_id: str
    run_id: str
    repository: str
    branch: str
    grant_id: str
    attempt_id: str
    status: ExternalExecutionStatus
    argument_hash: str
    assignment_hash: str
    observation_hash: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class ProviderBranch(BaseModel):
    """One branch a provider reports. This is not a platform grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_url: str
    branch: str


class ProviderStart(BaseModel):
    """A provider accepted an execution. The ids are the provider's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str
    provider_run_id: str
    phase: Literal["creating", "running"]


class ProviderObservation(BaseModel):
    """A validated status reading. Assistant text is not part of the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str
    provider_run_id: str
    phase: ProviderPhase
    branches: tuple[ProviderBranch, ...]
    pull_request_opened: bool = False


def external_execution_ids(scope: str, grant_id: str, attempt_id: str) -> tuple[str, str]:
    """Return the stable platform id and provider agent id for one attempt."""
    digest = uuid.uuid5(_ASSIGNMENT_NAMESPACE, f"{scope}\n{grant_id}\n{attempt_id}")
    return f"exec-{digest.hex}", f"bc-{digest}"


def require_policy_lines(values: Sequence[object], message: str) -> tuple[str, ...]:
    """Accept a short list of policy lines. Blank and control text fail closed."""
    if len(values) > _POLICY_LIMIT:
        raise RegistryRejected(message)
    lines: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise RegistryRejected(message)
        lines.append(require_label(value, message))
    return tuple(lines)


def render_assignment(assignment: ImplementationAssignment) -> str:
    """Render the assignment the provider receives. The platform does not store it."""
    lines = [
        "Implementation assignment schema 1.",
        f"Work item: {assignment.work_item_id}",
        f"Objective: {assignment.objective}",
        f"Acceptance source: {assignment.acceptance_source}",
        f"Repository: {assignment.repository_url}",
        f"Authorised branch: {assignment.authorised_branch}",
        "Constraints:",
        *[f"- {item}" for item in assignment.constraints],
        "Validation commands:",
        *[f"- {item}" for item in assignment.validation_commands],
        "Prohibitions:",
        *[f"- {item}" for item in assignment.prohibitions],
        f"Completion: {assignment.completion_output}",
    ]
    return "\n".join(lines)
