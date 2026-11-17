"""One local installation of the registered team templates.

Connections are listed once for the operator. A spreadsheet window belongs to
one installed team and one agent. Runs, approvals, and audit events stay in
that team's workflow scope. This is not hosted multi-user tenancy.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent_framework import SecretString, SupportsChatGetResponse

from agentbuildkit.integrations.composio import ComposioGateway
from agentbuildkit.observability import AuditLog
from agentbuildkit.revisions import MemoryRevisionStore, RevisionService, SqliteRevisionStore
from agentbuildkit.teams import TeamBundle, TeamRegistry
from agentbuildkit.workflows import WorkflowStateStore, memory_workflow_store, sqlite_workflow_store
from agentbuildkit_templates.customer_service.connections import (
    ConnectionStore,
    MemoryConnectionStore,
    SqliteConnectionStore,
)
from agentbuildkit_templates.customer_service.dashboard import DashboardService
from agentbuildkit_templates.customer_service.evaluation import LOCAL_MOCK_TOKEN
from agentbuildkit_templates.customer_service.recovery import (
    ActionRecovery,
    MemoryActionRecovery,
    SqliteActionRecovery,
)
from agentbuildkit_templates.customer_service.sheets import SheetDesk
from agentbuildkit_templates.customer_service.team import (
    ORGANISATION_SCOPE,
    Organisation,
    build_team,
    customer_team_bundle,
)
from agentbuildkit_templates.office.reporting import ReportingDashboard
from agentbuildkit_templates.office.templates import credential_ready, installed_templates
from agentbuildkit_templates.operations_reporting.sheets import JobWindow
from agentbuildkit_templates.operations_reporting.team import (
    TEAM_ID,
    ReportingTeam,
    reporting_bundle,
)
from agentbuildkit_templates.operations_reporting.team import build_team as build_reporting_team

ClientMap = Mapping[str, SupportsChatGetResponse]
ClientFactory = Callable[[], ClientMap]
TeamService = DashboardService | ReportingDashboard


def _no_teams() -> dict[str, TeamService]:
    return {}


@dataclass
class Installation:
    """The installed teams and the shared local connection store."""

    registry: TeamRegistry
    connections: ConnectionStore
    customer: DashboardService
    reporting: ReportingDashboard
    recovery: ActionRecovery
    revisions: RevisionService
    closed: bool = field(default=False)
    directory: Path | None = None
    gateway: ComposioGateway | None = None
    package_missing: bool = False
    customer_clients: ClientMap | None = None
    reporting_clients: ClientMap | None = None
    customer_client_factory: ClientFactory | None = None
    reporting_client_factory: ClientFactory | None = None
    teams: dict[str, TeamService] = field(default_factory=_no_teams)

    def labels(self) -> tuple[tuple[str, str], ...]:
        return tuple((team_id, title) for team_id, title, _runnable in self.revisions.roster())

    def team(self, team_id: str) -> TeamService | None:
        return self.teams.get(team_id)

    def team_for(self, team_id: str) -> TeamService | None:
        """Return an installed team, opening one created by another process.

        A team with no active revision stays inactive. This does not activate it.
        """
        found = self.teams.get(team_id)
        if found is not None:
            return found
        try:
            if self.revisions.instance(team_id) is None:
                return None
            return self.open_instance(team_id)
        except RuntimeError:
            return None

    def open_instance(self, team_id: str) -> TeamService:
        """Open one installed team. A second call returns the same service."""
        found = self.teams.get(team_id)
        if found is not None:
            return found
        record = self.revisions.instance(team_id)
        if record is None:
            raise RuntimeError("That team is not installed.")
        if record.template_id == ORGANISATION_SCOPE:
            opened: TeamService = self._open_customer(team_id)
        elif record.template_id == TEAM_ID:
            opened = self._open_reporting(team_id)
        else:
            raise RuntimeError("That template is not supported.")
        self.teams[team_id] = opened
        bundle, _title, _runnable = self.revisions.presentation(team_id)
        if self.registry.get(team_id) is None:
            self.registry.add(bundle)
        return opened

    def owner_of_correlation(self, correlation_id: str) -> str | None:
        found = [
            team.team_id for team in self.teams.values() if team.owns_correlation(correlation_id)
        ]
        if len(found) != 1:
            return None
        return found[0]

    def owner_of_request(self, request_id: str) -> str | None:
        found = [team.team_id for team in self.teams.values() if team.owns_request(request_id)]
        if len(found) != 1:
            return None
        return found[0]

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        seen: set[int] = set()
        for team in self.teams.values():
            identity = id(team.store)
            if identity in seen:
                continue
            seen.add(identity)
            team.store.close()
        self.recovery.close()
        self.connections.close()
        self.revisions.close()

    def _customer_clients_for(self, team_id: str) -> ClientMap:
        if team_id == ORGANISATION_SCOPE or self.customer_client_factory is None:
            if self.customer_clients is None:
                raise RuntimeError("Customer clients are not configured.")
            return self.customer_clients
        return self.customer_client_factory()

    def _reporting_clients_for(self, team_id: str) -> ClientMap:
        if team_id == TEAM_ID or self.reporting_client_factory is None:
            if self.reporting_clients is None:
                raise RuntimeError("Reporting clients are not configured.")
            return self.reporting_clients
        return self.reporting_client_factory()

    def _workflow_store(self, team_id: str, log: AuditLog) -> WorkflowStateStore:
        if self.directory is None:
            return memory_workflow_store(team_id, log)
        return sqlite_workflow_store(self.directory / "workflows.sqlite", team_id, log)

    def _open_customer(self, team_id: str) -> DashboardService:
        clients = self._customer_clients_for(team_id)
        log = AuditLog()
        desk = SheetDesk(
            self.connections,
            self.gateway,
            package_missing=self.package_missing,
            team_id=team_id,
            agent_name="sam",
        )
        organisation = build_team(
            clients=clients,
            api_token=SecretString(LOCAL_MOCK_TOKEN),
            audit_log=log,
            connected_reads=desk.tools_for,
            account_guard=desk.guard_account,
            configurator=_quiet,
            bundle=self.revisions.presentation(team_id)[0],
        )

        def factory(bundle: TeamBundle) -> Organisation:
            return build_team(
                clients=clients,
                api_token=SecretString(LOCAL_MOCK_TOKEN),
                audit_log=log,
                connected_reads=desk.tools_for,
                account_guard=desk.guard_account,
                configurator=_quiet,
                bundle=bundle,
            )

        service = DashboardService(
            organisation,
            self._workflow_store(team_id, log),
            recovery=self.recovery,
            sheets=desk,
            revisions=self.revisions,
            team_factory=factory,
        )
        service.base = f"/teams/{team_id}"
        service.close_connection_store = False
        return service

    def _open_reporting(self, team_id: str) -> ReportingDashboard:
        clients = self._reporting_clients_for(team_id)
        log = AuditLog()
        window = JobWindow(self.connections, self.gateway, team_id=team_id)
        team = build_reporting_team(
            clients=clients,
            audit_log=log,
            read_jobs=window.tool(),
            bundle=self.revisions.presentation(team_id)[0],
        )

        def factory(bundle: TeamBundle) -> ReportingTeam:
            return build_reporting_team(
                clients=clients,
                audit_log=log,
                read_jobs=window.tool(),
                bundle=bundle,
            )

        service = ReportingDashboard(
            team,
            self._workflow_store(team_id, log),
            window,
            self.recovery,
            revisions=self.revisions,
            team_factory=factory,
        )
        service.base = f"/teams/{team_id}"
        return service


def _quiet(**_kwargs: object) -> None:
    """Skip a second observability setup when a run rebuilds the team."""


def build_installation(
    *,
    customer_clients: ClientMap,
    reporting_clients: ClientMap,
    directory: Path | None = None,
    gateway: ComposioGateway | None = None,
    package_missing: bool = False,
    customer_client_factory: ClientFactory | None = None,
    reporting_client_factory: ClientFactory | None = None,
) -> Installation:
    """Build both teams. Pass scripted clients in tests. No live sheet is read."""
    registry = TeamRegistry()
    registry.add(customer_team_bundle())
    registry.add(reporting_bundle())
    customer_log = AuditLog()
    reporting_log = AuditLog()
    connections: ConnectionStore
    recovery: ActionRecovery
    customer_store: WorkflowStateStore
    reporting_store: WorkflowStateStore
    if directory is None:
        connections = MemoryConnectionStore()
        recovery = MemoryActionRecovery()
        customer_store = memory_workflow_store(ORGANISATION_SCOPE, customer_log)
        reporting_store = memory_workflow_store(TEAM_ID, reporting_log)
        revision_store = MemoryRevisionStore()
    else:
        directory.mkdir(parents=True, exist_ok=True)
        connections = SqliteConnectionStore(directory / "connections.sqlite")
        recovery = SqliteActionRecovery(directory / "action-recovery.sqlite")
        database = directory / "workflows.sqlite"
        customer_store = sqlite_workflow_store(database, ORGANISATION_SCOPE, customer_log)
        reporting_store = sqlite_workflow_store(database, TEAM_ID, reporting_log)
        revision_store = SqliteRevisionStore(directory / "revisions.sqlite")
    revisions = RevisionService(
        revision_store,
        installed_templates(),
        credentials=credential_ready,
    )
    revisions.seed()
    customer_desk = SheetDesk(connections, gateway, package_missing=package_missing)
    organisation = build_team(
        clients=customer_clients,
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=customer_log,
        connected_reads=customer_desk.tools_for,
        account_guard=customer_desk.guard_account,
    )

    def customer_factory(bundle: TeamBundle) -> Organisation:
        return build_team(
            clients=customer_clients,
            api_token=SecretString(LOCAL_MOCK_TOKEN),
            audit_log=customer_log,
            connected_reads=customer_desk.tools_for,
            account_guard=customer_desk.guard_account,
            configurator=_quiet,
            bundle=bundle,
        )

    customer = DashboardService(
        organisation,
        customer_store,
        recovery=recovery,
        sheets=customer_desk,
        revisions=revisions,
        team_factory=customer_factory,
    )
    window = JobWindow(connections, gateway)
    reporting_team = build_reporting_team(
        clients=reporting_clients,
        audit_log=reporting_log,
        read_jobs=window.tool(),
    )

    def reporting_factory(bundle: TeamBundle) -> ReportingTeam:
        return build_reporting_team(
            clients=reporting_clients,
            audit_log=reporting_log,
            read_jobs=window.tool(),
            bundle=bundle,
        )

    reporting = ReportingDashboard(
        reporting_team,
        reporting_store,
        window,
        recovery,
        revisions=revisions,
        team_factory=reporting_factory,
    )
    installed = tuple((team_id, title) for team_id, title, _flag in revisions.roster())
    customer.base = f"/teams/{customer.team_id}"
    customer.installed = installed
    customer.close_connection_store = False
    reporting.base = f"/teams/{reporting.team_id}"
    reporting.installed = installed
    installation = Installation(
        registry,
        connections,
        customer,
        reporting,
        recovery,
        revisions,
        directory=directory,
        gateway=gateway,
        package_missing=package_missing,
        customer_clients=customer_clients,
        reporting_clients=reporting_clients,
        customer_client_factory=customer_client_factory,
        reporting_client_factory=reporting_client_factory,
    )
    installation.teams[customer.team_id] = customer
    installation.teams[reporting.team_id] = reporting
    for item in revisions.instances():
        installation.open_instance(item.team_id)
    return installation
