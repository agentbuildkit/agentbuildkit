"""Spreadsheet window for the operations analyst.

The window is saved for this team and this agent. It is not Sam's window.
A connected read that fails does not return the demo jobs.
"""

from agent_framework import FunctionTool

from agentbuildkit.guardrails import ToolEffect, prepare_tool
from agentbuildkit.integrations.composio import ComposioGateway
from agentbuildkit_templates.customer_service.connections import ConnectionStore
from agentbuildkit_templates.customer_service.sheets import SheetDesk
from agentbuildkit_templates.operations_reporting.jobs import (
    FIXTURE,
    classify,
    format_jobs,
    jobs_from_sheet,
    parse_as_of,
)
from agentbuildkit_templates.operations_reporting.team import TEAM_ID

ANALYST = "analyst"
READ_JOBS = "read_jobs"


class JobWindow:
    """Demo jobs, or one explicitly assigned sheet window."""

    def __init__(
        self,
        store: ConnectionStore,
        gateway: ComposioGateway | None,
        team_id: str = TEAM_ID,
    ) -> None:
        self.desk = SheetDesk(
            store,
            gateway,
            team_id=team_id,
            agent_name=ANALYST,
            tool_name=READ_JOBS,
        )
        self.called = False
        self.failed = False
        self.last_ids: tuple[str, ...] = ()

    def read(self, as_of: str) -> str:
        """Classify overdue jobs. Connected mode never substitutes the fixture."""
        day = parse_as_of(as_of)
        self.called = True
        try:
            if self.desk.uses_sheet(ANALYST):
                jobs = jobs_from_sheet(self.desk.read_window(ANALYST, "", ""))
            else:
                jobs = FIXTURE
        except Exception:
            self.failed = True
            raise
        overdue = classify(jobs, day)
        self.last_ids = tuple(job.job_id for job in overdue)
        return format_jobs(overdue)

    def tool(self) -> FunctionTool:
        window = self

        async def read_jobs(as_of: str) -> str:
            """Read overdue jobs as of YYYY-MM-DD. Classification is done in Python."""
            return window.read(as_of)

        return prepare_tool(
            read_jobs,
            effect=ToolEffect.READ,
            name=READ_JOBS,
            description=(
                "Read overdue jobs for the saved as-of date. "
                "Another spreadsheet or a failed connected read is refused."
            ),
        )
