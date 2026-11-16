"""One local installation of the customer-service and operations-reporting teams.

Connections are listed once for the operator. A spreadsheet window belongs to
one team and one agent. Runs, approvals, and audit events stay in that team's
workflow scope. This is not hosted multi-user tenancy.
"""

from collections.abc import Mapping as ClientMap
from dataclasses import dataclass, field
from pathlib import Path

from agent_framework import SecretString, SupportsChatGetResponse

from agentbuildkit.integrations.composio import ComposioGateway
from agentbuildkit.observability import AuditLog
from agentbuildkit.teams import TeamRegistry
from agentbuildkit.workflows import WorkflowStateStore, memory_workflow_store, sqlite_workflow_store
from customer_service.connections import (
    ConnectionStore,
    MemoryConnectionStore,
    SqliteConnectionStore,
)
from customer_service.dashboard import DashboardService
from customer_service.evaluation import LOCAL_MOCK_TOKEN
from customer_service.recovery import ActionRecovery, MemoryActionRecovery, SqliteActionRecovery
from customer_service.sheets import SheetDesk
from customer_service.team import ORGANISATION_SCOPE, build_team, customer_team_bundle
from office.reporting import ReportingDashboard
from operations_reporting.sheets import JobWindow
from operations_reporting.team import TEAM_ID, reporting_bundle
from operations_reporting.team import build_team as build_reporting_team


@dataclass
class Installation:
    """The two teams and the shared local connection store."""

    registry: TeamRegistry
    connections: ConnectionStore
    customer: DashboardService
    reporting: ReportingDashboard
    recovery: ActionRecovery
    closed: bool = field(default=False)

    def labels(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.team_id, item.title) for item in self.registry.teams())

    def team(self, team_id: str) -> DashboardService | ReportingDashboard | None:
        if team_id == self.customer.team_id:
            return self.customer
        if team_id == self.reporting.team_id:
            return self.reporting
        return None

    def owner_of_correlation(self, correlation_id: str) -> str | None:
        found = [
            team.team_id
            for team in (self.customer, self.reporting)
            if team.owns_correlation(correlation_id)
        ]
        if len(found) != 1:
            return None
        return found[0]

    def owner_of_request(self, request_id: str) -> str | None:
        found = [
            team.team_id
            for team in (self.customer, self.reporting)
            if team.owns_request(request_id)
        ]
        if len(found) != 1:
            return None
        return found[0]

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.customer.store.close()
        self.reporting.store.close()
        self.recovery.close()
        self.connections.close()


def build_installation(
    *,
    customer_clients: ClientMap[str, SupportsChatGetResponse],
    reporting_clients: ClientMap[str, SupportsChatGetResponse],
    directory: Path | None = None,
    gateway: ComposioGateway | None = None,
    package_missing: bool = False,
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
    else:
        directory.mkdir(parents=True, exist_ok=True)
        connections = SqliteConnectionStore(directory / "connections.sqlite")
        recovery = SqliteActionRecovery(directory / "action-recovery.sqlite")
        database = directory / "workflows.sqlite"
        customer_store = sqlite_workflow_store(database, ORGANISATION_SCOPE, customer_log)
        reporting_store = sqlite_workflow_store(database, TEAM_ID, reporting_log)
    customer_desk = SheetDesk(connections, gateway, package_missing=package_missing)
    organisation = build_team(
        clients=customer_clients,
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=customer_log,
        connected_reads=customer_desk.tools_for,
        account_guard=customer_desk.guard_account,
    )
    customer = DashboardService(
        organisation,
        customer_store,
        recovery=recovery,
        sheets=customer_desk,
    )
    window = JobWindow(connections, gateway)
    reporting_team = build_reporting_team(
        clients=reporting_clients,
        audit_log=reporting_log,
        read_jobs=window.tool(),
    )
    reporting = ReportingDashboard(reporting_team, reporting_store, window, recovery)
    installed = tuple((item.team_id, item.title) for item in registry.teams())
    customer.base = f"/teams/{customer.team_id}"
    customer.installed = installed
    customer.close_connection_store = False
    reporting.base = f"/teams/{reporting.team_id}"
    reporting.installed = installed
    return Installation(registry, connections, customer, reporting, recovery)
