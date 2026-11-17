"""Drafts and immutable team revisions.

A draft is editable. A revision is the configuration that was activated or
shipped. Neither record holds credentials, connection assignments, or customer
records. The content hash binds validation and evaluation to one exact draft.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agentbuildkit.teams import TeamBundle

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
PurposeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=280)]
InstructionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
ModelText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
DescriptionText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
ProviderId = Literal["openai", "anthropic"]


class RevisionModel(BaseModel):
    """Reject unknown fields, including approval-policy overrides."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentContract(RevisionModel):
    """One role a template requires. The name and schemas are not editable."""

    name: ShortText
    purpose: PurposeText
    instructions: InstructionText
    provider: ProviderId
    model: ModelText
    input_schema: ShortText
    output_schema: ShortText
    permitted_tools: tuple[ShortText, ...] = ()
    required_tools: tuple[ShortText, ...] = ()


class TeamTemplate(RevisionModel):
    """A trusted, registered application. Drafts cannot invent a new one."""

    template_id: ShortText
    team_id: ShortText
    title: ShortText
    description: DescriptionText = ""
    schema_version: Literal["1"] = "1"
    suite_version: ShortText
    agents: tuple[AgentContract, ...] = Field(min_length=1)
    forbidden_tools: tuple[ShortText, ...] = ()


class AgentConfig(RevisionModel):
    """The editable part of one agent. Role, schemas, and workflow stay fixed."""

    name: ShortText
    purpose: PurposeText
    instructions: InstructionText
    provider: ProviderId
    model: ModelText
    tools: tuple[str, ...] = ()


class TeamConfig(RevisionModel):
    """Editable team configuration. Identifiers and schemas are not included."""

    title: ShortText
    description: DescriptionText = ""
    agents: tuple[AgentConfig, ...] = Field(min_length=1)


class Finding(RevisionModel):
    """One validation result. The message does not repeat a secret value."""

    code: str
    message: str


class Draft(RevisionModel):
    """One open working copy. ``version`` rejects a stale save."""

    draft_id: str
    team_id: str
    template_id: str
    version: int
    config: TeamConfig
    content_hash: str
    updated_at: str


class Revision(RevisionModel):
    """One immutable published configuration."""

    revision_id: str
    team_id: str
    template_id: str
    number: int
    content_hash: str
    config: TeamConfig
    suite_version: str
    evaluation_passed: bool
    evaluation_note: str
    created_at: str


class Readiness(RevisionModel):
    """Validation and offline evaluation for one draft hash."""

    draft_id: str
    version: int
    content_hash: str
    validation_ok: bool
    findings: tuple[Finding, ...] = ()
    evaluation_ok: bool | None = None
    suite_version: str = ""
    evaluated_at: str = ""
    passed: int = 0
    failed: int = 0
    note: str = ""


class Activation(RevisionModel):
    """An audited pointer change. Instruction text is not stored here."""

    activation_id: str
    team_id: str
    revision_id: str
    previous_revision_id: str
    content_hash: str
    kind: Literal["baseline", "activate", "rollback"]
    created_at: str


class RunPin(RevisionModel):
    """The revision a run started with. Later activations do not move it."""

    team_id: str
    correlation_id: str
    request_id: str
    revision_id: str


class TeamInstance(RevisionModel):
    """One installed team. Its id is not the template id and not the display name."""

    team_id: str
    template_id: str
    title: str
    description: str
    idempotency_key: str
    created_at: str


class FieldChange(RevisionModel):
    """One readable difference between the active revision and a draft."""

    label: str
    before: str
    after: str


class PreparedEvaluation(RevisionModel):
    """A candidate that passed structural validation and may be evaluated."""

    draft_id: str
    team_id: str
    version: int
    content_hash: str
    bundle: TeamBundle


class RevisionError(Exception):
    """A draft operation was rejected. ``code`` is safe to show in the page."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
