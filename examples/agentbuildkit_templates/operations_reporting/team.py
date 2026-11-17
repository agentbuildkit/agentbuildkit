"""Operations reporting team. Two agents, no side effects."""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import cast

from agent_framework import FunctionTool, SupportsChatGetResponse
from pydantic import BaseModel

from agentbuildkit.agents import PlatformAgent, create_agent
from agentbuildkit.observability import AuditLog
from agentbuildkit.teams import TeamBundle, load_bundle_json
from agentbuildkit.workflows.diagram import DiagramStep, WorkflowDiagram
from agentbuildkit_templates.operations_reporting.models import (
    JobsRequest,
    ReportDraft,
    ReviewedReport,
)

TEAM_ID = "operations-reporting"
WORKFLOW_NAME = "overdue-report"
REPORTING_DIAGRAM = WorkflowDiagram(
    stages=(
        DiagramStep("request", "Request", "entry"),
        DiagramStep("analyst", "Analyst", "agent"),
        DiagramStep("reviewer", "Reviewer", "agent"),
        DiagramStep("report", "Report", "result"),
    ),
    handling=(
        "Analyst reads the jobs for the as-of date. Python classifies overdue dates before the "
        "summary is accepted.",
        "Reviewer checks that the summary names only those job ids.",
        "The report is the reviewer's result. This team does not schedule work, send email, or "
        "edit a sheet.",
    ),
)
BUNDLE_PATH = files("agentbuildkit_templates.operations_reporting").joinpath("team.json")
_SCHEMAS: dict[str, type[BaseModel]] = {
    "JobsRequest": JobsRequest,
    "ReportDraft": ReportDraft,
    "ReviewedReport": ReviewedReport,
}


def reporting_bundle() -> TeamBundle:
    """Load the example bundle. Bindings are checked when the team is built."""
    return load_bundle_json(BUNDLE_PATH.read_bytes())


@dataclass
class ReportingTeam:
    """Analyst drafts. Reviewer checks. Python classifies the dates."""

    analyst: PlatformAgent[JobsRequest, ReportDraft]
    reviewer: PlatformAgent[ReportDraft, ReviewedReport]
    audit_log: AuditLog

    async def aclose(self) -> None:
        """This team holds no network client of its own."""


def build_team(
    *,
    clients: Mapping[str, SupportsChatGetResponse],
    audit_log: AuditLog | None = None,
    read_jobs: FunctionTool,
    bundle: TeamBundle | None = None,
) -> ReportingTeam:
    """Create the team from injected clients and a bundle.

    ``bundle`` selects a published revision. When it is omitted, ``team.json``
    is used.
    """
    log = audit_log or AuditLog()
    bundle = reporting_bundle() if bundle is None else bundle
    definitions = bundle.definitions(schemas=_SCHEMAS, tools={"read_jobs": read_jobs})
    if tuple(item.name for item in definitions) != ("analyst", "reviewer"):
        raise ValueError("The operations-reporting bundle does not match the application.")
    built = {
        item.name: create_agent(item, client=clients[item.name], audit_log=log)
        for item in definitions
    }
    return ReportingTeam(
        analyst=cast(PlatformAgent[JobsRequest, ReportDraft], built["analyst"]),
        reviewer=cast(PlatformAgent[ReportDraft, ReviewedReport], built["reviewer"]),
        audit_log=log,
    )
