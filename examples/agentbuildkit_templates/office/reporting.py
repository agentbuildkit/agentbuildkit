"""Dashboard adapter for operations reporting.

The page renderer is shared with customer service. This module supplies that
team's runs, agents, and request form. It does not approve actions.
"""

import uuid
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from agentbuildkit.applications import RenderedPage
from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.observability import AuditEvent
from agentbuildkit.revisions import Revision, RevisionError, RevisionService
from agentbuildkit.state.models import RegistryRejected, require_argument_hash
from agentbuildkit.teams import TeamBundle
from agentbuildkit.workflows import WorkflowStateStore
from agentbuildkit_templates.customer_service.dashboard_model import build_runs, merge_audit
from agentbuildkit_templates.customer_service.dashboard_view import (
    AgentInfo,
    AssignmentInfo,
    ConnectionInfo,
    Snapshot,
    render_page,
)
from agentbuildkit_templates.customer_service.draft_pages import maybe_revision_page
from agentbuildkit_templates.customer_service.models import FinalOutcome
from agentbuildkit_templates.customer_service.recovery import ActionRecovery, MemoryActionRecovery
from agentbuildkit_templates.operations_reporting.jobs import parse_as_of
from agentbuildkit_templates.operations_reporting.service import handle_report
from agentbuildkit_templates.operations_reporting.sheets import ANALYST, JobWindow
from agentbuildkit_templates.operations_reporting.team import (
    REPORTING_DIAGRAM,
    ReportingTeam,
    reporting_bundle,
)

Evaluate = Callable[[], Coroutine[Any, Any, EvaluationReport]]
TeamFactory = Callable[[TeamBundle], ReportingTeam]


class ReportingDashboard:
    """One local operations-reporting service."""

    def __init__(
        self,
        team: ReportingTeam,
        store: WorkflowStateStore,
        window: JobWindow,
        recovery: ActionRecovery,
        *,
        evaluate: Evaluate | None = None,
        revisions: RevisionService | None = None,
        team_factory: TeamFactory | None = None,
    ) -> None:
        if window.desk.team_id != store.scope:
            raise RuntimeError("The operations store does not match the team.")
        self.team = team
        self.team_id = store.scope
        self.title = reporting_bundle().display_name
        self.store = store
        self.window = window
        self.recovery = recovery
        self.base = ""
        self.installed: tuple[tuple[str, str], ...] = ()
        self.revisions = revisions
        self._team_factory = team_factory
        self.last_revision_id = ""
        self.last_built_instructions = ""
        self.report: EvaluationReport | None = None
        self.error = ""
        self.draft: tuple[str, str] | None = None
        self._evaluate = evaluate
        team.audit_log.add_sink(_LedgerSink(store))
        self.outcomes: list[FinalOutcome] = list(recovery.outcomes(self.team_id))

    async def submit(self, as_of: str, note: str) -> FinalOutcome:
        """Draft one report. The team id is fixed before the model runs."""
        team_id = self.team_id
        parse_as_of(as_of)
        if self.revisions is not None and not self.revisions.runnable(team_id):
            raise RevisionError(
                "inactive",
                "This team is not active. Activate a revision before running it.",
            )
        revision = self.revisions.active(team_id) if self.revisions is not None else None
        team = self._for_revision(revision)
        correlation_id = uuid.uuid4().hex
        if self.revisions is not None and revision is not None:
            self.revisions.pin_run(team_id, correlation_id, "", revision.revision_id)
        outcome = await handle_report(
            team,
            _request(as_of, note),
            correlation_id=correlation_id,
            store=self.store,
            window=self.window,
        )
        self.recovery.save_outcome(outcome, team_id=team_id)
        self.outcomes.append(outcome)
        self.draft = None
        return outcome

    async def decide(self, request_id: str, *, approved: bool) -> FinalOutcome:
        """This team has no approval actions."""
        del request_id, approved
        raise KeyError(self.team_id)

    async def run_evaluation(self) -> EvaluationReport:
        if self._evaluate is None:
            from agentbuildkit_templates.operations_reporting.evaluation import (
                run_reporting_evaluation,
            )

            self._evaluate = run_reporting_evaluation
        self.report = await self._evaluate()
        return self.report

    def owns_correlation(self, correlation_id: str) -> bool:
        return any(item.correlation_id == correlation_id for item in self.outcomes)

    def owns_request(self, request_id: str) -> bool:
        del request_id
        return False

    def page(self, path: str = "/", query: Mapping[str, str] | None = None) -> object:
        params = query or {}
        snap = self.snapshot()
        rendered = maybe_revision_page(snap, self.revisions, path, params)
        if rendered is not None:
            return rendered
        return render_page(snap, path, params)

    def _for_revision(self, revision: Revision | None) -> ReportingTeam:
        factory = self._team_factory
        service = self.revisions
        if revision is None or factory is None or service is None:
            return self.team
        self.last_revision_id = revision.revision_id
        team = factory(service.bundle_for(revision))
        self.last_built_instructions = team.analyst.definition.instructions
        return team

    def render(self, path: str = "/", query: Mapping[str, str] | None = None) -> str:
        rendered = self.page(path, query)
        return str(getattr(rendered, "body", ""))

    def rendered_page(
        self, path: str = "/", query: Mapping[str, str] | None = None
    ) -> RenderedPage:
        rendered = self.page(path, query)
        return RenderedPage(
            status=int(getattr(rendered, "status", 500)),
            body=str(getattr(rendered, "body", "")),
        )

    def snapshot(self) -> Snapshot:
        runnable = True
        activity: tuple[tuple[str, str], ...] = ()
        if self.revisions is None:
            bundle = reporting_bundle()
            title = bundle.display_name
            installed = self.installed
        else:
            bundle, title, runnable = self.revisions.presentation(self.team_id)
            installed = tuple((team_id, label) for team_id, label, _flag in self.revisions.roster())
            activity = tuple(
                (team_id, "Active" if flag else "Draft")
                for team_id, _label, flag in self.revisions.roster()
            )
        mode = self.window.desk.store.data_mode(self.team_id, ANALYST)
        return Snapshot(
            team_name=bundle.team_id,
            team_id=self.team_id,
            title=title,
            base=self.base,
            installed=installed,
            approvals_enabled=False,
            summary=(
                "Two agents draft and review an overdue-jobs report. "
                "Dates are classified in Python. Nothing is emailed or edited."
            ),
            subject_label="As of",
            form="operations",
            sheet_agent=ANALYST,
            footnote="One local operator. This team cannot see another team's runs.",
            agents=tuple(
                AgentInfo(
                    name=agent.name,
                    role=agent.purpose,
                    job=agent.purpose,
                    provider=agent.provider,
                    model=agent.model,
                    input_schema=agent.input_schema,
                    output_schema=agent.output_schema,
                    tools=tuple(agent.tools),
                    instructions=agent.instructions,
                )
                for agent in bundle.agents
            ),
            runs=build_runs(self.outcomes, {}),
            approvals=(),
            audit=merge_audit(self.store.list_ledger(), self.team.audit_log.events),
            report=self.report,
            error=self.error,
            draft=self.draft,
            persistent=not isinstance(self.recovery, MemoryActionRecovery),
            connections=self._connections(),
            assignments=self._assignments(),
            composio_configured=self.window.desk.connect_ready,
            composio_auth_missing=(
                self.window.desk.configured and not self.window.desk.connect_ready
            ),
            composio_package_missing=self.window.desk.package_missing,
            sam_mode=mode,
            diagram=REPORTING_DIAGRAM,
            actor_label="Handled by",
            runnable=runnable,
            activity=activity,
        )

    def _connections(self) -> tuple[ConnectionInfo, ...]:
        assigned = self.window.desk.store.assignments()
        return tuple(
            ConnectionInfo(
                connection_id=item.connection_id,
                label=item.label,
                status=item.status,
                agents=tuple(
                    f"{row.team_id}/{row.agent_name}"
                    for row in assigned
                    if row.connection_id == item.connection_id
                ),
            )
            for item in self.window.desk.store.connections()
        )

    def _assignments(self) -> tuple[AssignmentInfo, ...]:
        by_id = {item.connection_id: item for item in self.window.desk.store.connections()}
        views: list[AssignmentInfo] = []
        for row in self.window.desk.store.assignments(self.team_id):
            connection = by_id.get(row.connection_id)
            views.append(
                AssignmentInfo(
                    agent_name=row.agent_name,
                    connection_id=row.connection_id,
                    label="" if connection is None else connection.label,
                    tools=row.tools,
                    spreadsheet_id=row.spreadsheet_id,
                    cell_range=row.cell_range,
                    mode=row.mode,
                )
            )
        return tuple(views)


def _request(as_of: str, note: str) -> Any:
    from agentbuildkit_templates.operations_reporting.models import JobsRequest

    return JobsRequest(as_of=as_of, note=note)


class _LedgerSink:
    """Copy ledger-compatible audit events. Argument values are not accepted."""

    def __init__(self, store: WorkflowStateStore) -> None:
        self._store = store

    def append_ledger(
        self,
        event: AuditEvent,
        *,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> None:
        try:
            require_argument_hash(event.argument_hash)
        except RegistryRejected:
            return
        self._store.append_ledger(event, run_id=run_id, event_id=event_id)
