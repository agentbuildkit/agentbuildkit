"""Structured MCP results. Draft text in these models is data, not authority."""

from pydantic import BaseModel, ConfigDict, Field


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateAgent(DraftModel):
    name: str
    provider: str
    model: str
    input_schema: str
    output_schema: str
    permitted_tools: list[str]
    required_tools: list[str]


class TemplateInfo(DraftModel):
    template_id: str
    title: str
    description: str
    suite_version: str
    agents: list[TemplateAgent]


class TemplateList(DraftModel):
    templates: list[TemplateInfo]
    note: str


class TeamStatus(DraftModel):
    team_id: str
    template_id: str
    title: str
    runnable: bool
    active_revision_id: str | None
    draft_id: str | None
    draft_version: int | None


class TeamList(DraftModel):
    teams: list[TeamStatus]


class TeamDetail(TeamStatus):
    description: str
    draft_content_hash: str | None


class AgentDraft(DraftModel):
    name: str
    purpose: str
    instructions: str
    provider: str
    model: str
    tools: list[str]


class DraftDetail(DraftModel):
    team_id: str
    draft_id: str
    template_id: str
    version: int
    content_hash: str
    title: str
    description: str
    agents: list[AgentDraft]
    runnable: bool


class RevisionDetail(DraftModel):
    team_id: str
    revision_id: str
    template_id: str
    number: int
    content_hash: str
    title: str
    description: str
    agents: list[AgentDraft]
    suite_version: str
    evaluation_passed: bool
    immutable: bool = True


class CreatedTeam(DraftModel):
    team_id: str
    template_id: str
    title: str
    description: str
    runnable: bool
    draft_id: str | None
    draft_version: int | None
    idempotency_key: str


class OpenedDraft(DraftModel):
    team_id: str
    draft_id: str
    version: int
    content_hash: str
    preserved_existing: bool
    runnable: bool


class UpdatedDraft(DraftModel):
    team_id: str
    draft_id: str
    version: int
    content_hash: str
    runnable: bool
    readiness_cleared: bool


class FindingView(DraftModel):
    code: str
    message: str


class ValidationView(DraftModel):
    team_id: str
    draft_id: str
    version: int
    content_hash: str
    validation_ok: bool
    findings: list[FindingView]
    runnable: bool


class EvaluationView(DraftModel):
    team_id: str
    draft_id: str
    version: int
    content_hash: str
    validation_ok: bool
    evaluation_ok: bool | None
    suite_version: str
    passed: int
    failed: int
    note: str
    runnable: bool
    activation_ready: bool = Field(
        description=(
            "True only when this exact draft hash passed validation and the "
            "scripted suite. The team is not activated."
        )
    )


class FieldDiff(DraftModel):
    label: str
    before: str
    after: str


class ComparisonView(DraftModel):
    team_id: str
    draft_id: str
    version: int
    content_hash: str
    compared_with: str
    changes: list[FieldDiff]
    runnable: bool
