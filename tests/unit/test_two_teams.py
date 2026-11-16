"""Two installed teams keep runs, approvals, and spreadsheet windows apart."""

import json
import sqlite3
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from agent_framework import ChatResponse, Content, Message

from agentbuildkit.teams import TeamRegistry, load_bundle
from agentbuildkit.testing import ScriptedChatClient
from agentbuildkit.workflows.persistence import PendingRequest
from customer_service.connections import LEGACY_TEAM_ID, SqliteConnectionStore
from customer_service.dashboard import handle_request
from customer_service.evaluation import LOCAL_MOCK_TOKEN, scripted_clients
from customer_service.recovery import SqliteActionRecovery
from customer_service.sheets import SheetDesk
from office.installation import Installation, build_installation
from office.server import serve_installation
from operations_reporting.jobs import DEMO_AS_OF, FIXTURE, classify, parse_as_of
from operations_reporting.service import CONNECTED_FAILURE
from operations_reporting.team import TEAM_ID

_SHEET = "1" * 40
_OTHER = "2" * 40
_ACCOUNT = "ca_account"


class _Gateway:
    def __init__(self) -> None:
        from agentbuildkit.integrations.composio import (
            AccountView,
            ComposioProviderError,
            LinkRequest,
        )

        self._account = AccountView
        self._link = LinkRequest
        self._provider_error = ComposioProviderError
        self.user_id = "operator"
        self.auth_config_id = "ac_readonly_sheets"
        self.fail = False
        self.payload: dict[str, object] = {
            "successful": True,
            "data": {
                "values": [
                    ["job_id", "customer", "due_date", "status"],
                    ["J-900", "Sheet Co", "2026-11-01", "open"],
                ]
            },
        }

    def start_link(self, *, user_id: str, toolkit: str, callback_url: str) -> object:
        del user_id, toolkit, callback_url
        return self._link(id="lnk", redirect_url="https://connect.example.test/link")

    def get_account(self, account_id: str) -> object:
        if account_id != _ACCOUNT:
            raise self._provider_error()
        return self._account(
            id=account_id,
            user_id=self.user_id,
            toolkit="googlesheets",
            status="ACTIVE",
        )

    def execute(self, **_kwargs: object) -> dict[str, object]:
        if self.fail:
            raise self._provider_error()
        return self.payload


def _tool_call(name: str, arguments: dict[str, object]) -> ChatResponse:
    return ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id=f"call-{name}",
                        name=name,
                        arguments=arguments,
                    )
                ],
            )
        ],
        response_id="scripted",
    )


def _reporting_clients(*, summary: str = "J-100 is overdue.") -> dict[str, ScriptedChatClient]:
    records = "job_id, customer, due_date, status\nJ-100, Northwind, 2026-11-01, open"
    return {
        "analyst": ScriptedChatClient(
            [
                _tool_call("read_jobs", {"as_of": DEMO_AS_OF}),
                json.dumps({"as_of": DEMO_AS_OF, "records": records, "summary": summary}),
            ]
        ),
        "reviewer": ScriptedChatClient(
            [
                json.dumps(
                    {
                        "as_of": DEMO_AS_OF,
                        "records": records,
                        "summary": summary,
                        "supported": True,
                        "notes": "",
                    }
                )
            ]
        ),
    }


def _installation(tmp_path: Path | None = None, *, summary: str = "J-100 is overdue."):
    from typing import cast

    from agentbuildkit.integrations.composio import ComposioGateway

    gateway = _Gateway()
    installation = build_installation(
        customer_clients=scripted_clients("billing-routes-to-sam"),
        reporting_clients=_reporting_clients(summary=summary),
        directory=tmp_path,
        gateway=cast(ComposioGateway, gateway),
    )
    gateway.user_id = installation.connections.operator_id()
    return installation, gateway


def test_bundle_id_is_independent_of_the_display_title() -> None:
    bundle = load_bundle(Path("examples/customer_service/team.json"))
    assert bundle.team_id == "customer-service"
    assert bundle.display_name == "Customer Service"
    registry = TeamRegistry()
    registry.add(bundle)
    registry.add(load_bundle(Path("examples/operations_reporting/team.json")))
    assert [item.team_id for item in registry.teams()] == [
        "customer-service",
        "operations-reporting",
    ]


def test_dates_are_classified_without_a_model() -> None:
    as_of = parse_as_of(DEMO_AS_OF)
    overdue = classify(FIXTURE, as_of)
    assert [job.job_id for job in overdue] == ["J-100"]


async def test_both_teams_run_through_the_shared_installation(tmp_path: Path) -> None:
    installation, _gateway = _installation(tmp_path)
    try:
        customer = await installation.customer.submit("A-100", "Please explain the latest invoice.")
        report = await installation.reporting.submit(DEMO_AS_OF, "")
    finally:
        installation.close()
    assert customer.specialist == "sam"
    assert "Northwind" in customer.response or customer.status == "completed"
    assert report.specialist == "reviewer"
    assert "J-100" in report.response
    assert report.status == "completed"
    assert installation.customer.owns_correlation(customer.correlation_id)
    assert not installation.reporting.owns_correlation(customer.correlation_id)
    assert installation.reporting.owns_correlation(report.correlation_id)
    assert not installation.customer.owns_correlation(report.correlation_id)


async def test_an_invented_job_is_rejected() -> None:
    installation, _gateway = _installation(summary="J-100 and J-999 are overdue.")
    try:
        report = await installation.reporting.submit(DEMO_AS_OF, "")
    finally:
        installation.close()
    assert report.status == "rejected"
    assert "Not supported" in report.response


async def test_connected_failure_does_not_return_demo_jobs() -> None:
    installation, gateway = _installation()
    try:
        connection = "local-connection"
        from customer_service.connections import StoredConnection

        installation.connections.save_connection(
            StoredConnection(
                connection_id=connection,
                toolkit="googlesheets",
                connected_account_id=_ACCOUNT,
                user_id=gateway.user_id,
                status="ACTIVE",
                label="Sheet",
            )
        )
        installation.reporting.window.desk.assign(
            "analyst",
            connection,
            _SHEET,
            "Jobs!A1:D2",
            mode="connected",
        )
        gateway.fail = True
        report = await installation.reporting.submit(DEMO_AS_OF, "")
        assert report.response == CONNECTED_FAILURE
        assert "Northwind" not in report.response
        assert "J-100" not in report.response
    finally:
        installation.close()


def test_same_agent_name_does_not_inherit_the_other_window() -> None:
    installation, gateway = _installation()
    try:
        from typing import cast

        from agentbuildkit.integrations.composio import ComposioGateway, ComposioRejected
        from customer_service.connections import StoredConnection

        shared = cast(ComposioGateway, gateway)
        installation.connections.save_connection(
            StoredConnection(
                connection_id="shared",
                toolkit="googlesheets",
                connected_account_id=_ACCOUNT,
                user_id=gateway.user_id,
                status="ACTIVE",
                label="Sheet",
            )
        )
        left = SheetDesk(installation.connections, shared, team_id="customer-service")
        right = SheetDesk(
            installation.connections,
            shared,
            team_id=TEAM_ID,
            agent_name="sam",
            tool_name="read_sheet_range",
        )
        left.assign("sam", "shared", _SHEET, "Accounts!A1:C2", mode="connected")
        right.assign("sam", "shared", _OTHER, "Jobs!A1:D2", mode="connected")
        assert left.store.assignment("customer-service", "sam") is not None
        assert right.store.assignment(TEAM_ID, "sam") is not None
        with pytest.raises(ComposioRejected, match="not assigned"):
            left.read_window("sam", _OTHER, "Jobs!A1:D2")
        with pytest.raises(ComposioRejected, match="not assigned"):
            right.read_window("sam", _SHEET, "Accounts!A1:C2")
    finally:
        installation.close()


def test_unassign_keeps_the_other_team_and_disconnect_clears_both() -> None:
    installation, gateway = _installation()
    binding = serve_installation(installation, port=0)
    try:
        from urllib.parse import urlencode

        from customer_service.connections import StoredConnection

        installation.connections.save_connection(
            StoredConnection(
                connection_id="shared",
                toolkit="googlesheets",
                connected_account_id=_ACCOUNT,
                user_id=gateway.user_id,
                status="ACTIVE",
                label="Sheet",
            )
        )
        customer = installation.customer.sheets
        assert customer is not None
        analyst = installation.reporting.window.desk
        address = binding.server_address
        body = urlencode(
            {
                "agent": "analyst",
                "mode": "connected",
                "connection_id": "shared",
                "spreadsheet_id": _OTHER,
                "cell_range": "Jobs!A1:ZZ9",
                "team_id": TEAM_ID,
            }
        )
        request = Request(
            f"http://{address[0]}:{address[1]}/teams/{TEAM_ID}/connections/assign",
            data=body.encode(),
            method="POST",
        )
        with urlopen(request) as response:
            assert "notice=window" in response.geturl()
        assert installation.connections.assignment(TEAM_ID, "analyst") is None
        customer.assign("sam", "shared", _SHEET, "Accounts!A1:C2", mode="connected")
        analyst.assign("analyst", "shared", _OTHER, "Jobs!A1:D20", mode="connected")
        customer.unassign("sam")
        assert installation.connections.assignment("customer-service", "sam") is None
        kept = installation.connections.assignment(TEAM_ID, "analyst")
        assert kept is not None
        assert kept.spreadsheet_id == _OTHER
        connection = installation.connections.connection("shared")
        assert connection is not None
        assert connection.status == "ACTIVE"
        assert installation.connections.data_mode("customer-service", "sam") == "connected"
        assert customer.uses_sheet("sam")
        unassigned = installation.customer.render("/team/sam", {"notice": "unassigned"})
        assert "spreadsheet window was removed" in unassigned
        assert "stay in place" in unassigned
        listed = installation.reporting.render("/connections")
        assert "Operations Reporting / Analyst" in listed
        assert "Customer Service / Sam" not in listed
        assert "every assignment that uses it" in listed
        customer.disconnect("shared")
        assert installation.connections.assignment(TEAM_ID, "analyst") is None
        disconnected = installation.connections.connection("shared")
        assert disconnected is not None
        assert disconnected.status == "DISCONNECTED"
        assert installation.connections.data_mode(TEAM_ID, "analyst") == "connected"
        assert analyst.uses_sheet("analyst")
        page = installation.customer.render("/connections", {"notice": "disconnected"})
        assert "every assignment that used it" in page
        assert "does not revoke Google access" in page
    finally:
        binding.shutdown()
        installation.close()


def test_another_teams_approval_is_rejected() -> None:
    installation, _gateway = _installation()
    try:
        request_id = "request-customer"
        installation.customer.store.note_pending(
            PendingRequest(
                scope="customer-service",
                workflow_name="service-action",
                run_id="run-1",
                correlation_id="corr-approval",
                request_id=request_id,
                action="schedule_service",
                checkpoint_id="checkpoint-1",
                created_at="2026-11-16T00:00:00+00:00",
                status="pending",
            ),
            argument_hash="ab" * 32,
        )
        assert installation.customer.owns_request(request_id)
        assert not installation.reporting.owns_request(request_id)
        assert installation.owner_of_request(request_id) == "customer-service"
        binding = serve_installation(installation, port=0)
        try:
            address = binding.server_address
            body = "request_id=request-customer&decision=approve&team_id=operations-reporting"
            request = Request(
                f"http://{address[0]}:{address[1]}/teams/operations-reporting/decide",
                data=body.encode(),
                method="POST",
            )
            with urlopen(request) as response:
                page = response.read().decode()
            assert "another team" in page
            found = installation.customer.store.pending_for(request_id)
            assert found is not None
            assert found[0].status == "pending"
        finally:
            binding.shutdown()
    finally:
        installation.close()


async def test_switching_teams_during_submit_keeps_the_original_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation, _gateway = _installation()
    try:

        async def flipped(*args: object, **kwargs: object) -> object:
            installation.customer.team_id = TEAM_ID
            return await handle_request(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("customer_service.dashboard.handle_request", flipped)
        outcome = await installation.customer.submit("A-100", "Please explain the latest invoice.")
        assert installation.recovery.outcomes("customer-service")[-1].correlation_id == (
            outcome.correlation_id
        )
        assert installation.recovery.outcomes(TEAM_ID) == ()
        assert not installation.reporting.owns_correlation(outcome.correlation_id)
    finally:
        installation.close()


def test_legacy_rows_are_backed_up_and_kept(tmp_path: Path) -> None:
    path = tmp_path / "connections.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE assignments (
            agent_name TEXT PRIMARY KEY,
            connection_id TEXT NOT NULL,
            tools_json TEXT NOT NULL,
            spreadsheet_id TEXT NOT NULL DEFAULT '',
            cell_range TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'demo'
        );
        CREATE TABLE agent_modes (
            agent_name TEXT PRIMARY KEY,
            mode TEXT NOT NULL
        );
        INSERT INTO assignments (
            agent_name, connection_id, tools_json, spreadsheet_id, cell_range, mode
        ) VALUES (
            'sam', 'keep-me', '["read_sheet_range"]', 'sheet-old', 'Accounts!A1:C2', 'connected'
        );
        INSERT INTO agent_modes (agent_name, mode) VALUES ('sam', 'connected');
        """
    )
    connection.commit()
    connection.close()
    store = SqliteConnectionStore(path)
    try:
        kept = store.assignment(LEGACY_TEAM_ID, "sam")
        assert kept is not None
        assert kept.spreadsheet_id == "sheet-old"
        assert kept.cell_range == "Accounts!A1:C2"
        assert store.assignment(TEAM_ID, "sam") is None
        assert store.data_mode(LEGACY_TEAM_ID, "sam") == "connected"
        assert store.data_mode(TEAM_ID, "analyst") == "demo"
    finally:
        store.close()
    backups = list(tmp_path.glob("connections.sqlite.bak-*"))
    assert len(backups) == 1
    raw = path.read_bytes()
    assert b"sheet-old" in raw
    recovery = tmp_path / "action-recovery.sqlite"
    legacy = sqlite3.connect(recovery)
    legacy.executescript(
        """
        CREATE TABLE pauses (
            request_id TEXT PRIMARY KEY,
            reference TEXT NOT NULL,
            arguments_json TEXT NOT NULL
        );
        CREATE TABLE outcomes (
            outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT NOT NULL,
            status TEXT NOT NULL,
            specialist TEXT NOT NULL,
            response TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        );
        INSERT INTO pauses (request_id, reference, arguments_json)
        VALUES ('pending-1', 'A-100', '{"action":"schedule_service"}');
        INSERT INTO outcomes (reference, status, specialist, response, correlation_id)
        VALUES ('A-100', 'pending_approval', 'taylor', 'waiting', 'corr-old');
        """
    )
    legacy.commit()
    legacy.close()
    opened = SqliteActionRecovery(recovery)
    try:
        pause = opened.pause_for(LEGACY_TEAM_ID, "pending-1")
        assert pause is not None
        assert pause[0] == "A-100"
        assert opened.pause_for(TEAM_ID, "pending-1") is None
        assert opened.outcomes(LEGACY_TEAM_ID)[0].correlation_id == "corr-old"
        assert opened.outcomes(TEAM_ID) == ()
    finally:
        opened.close()
    assert list(tmp_path.glob("action-recovery.sqlite.bak-*"))


def test_dashboard_pages_keep_each_team_visible() -> None:
    installation, _gateway = _installation()
    binding = serve_installation(installation, port=0)
    try:
        address = binding.server_address
        base = f"http://{address[0]}:{address[1]}"

        def get(path: str) -> str:
            with urlopen(base + path) as response:
                return response.read().decode()

        customer = get("/teams/customer-service/?new=1")
        reporting = get("/teams/operations-reporting/?new=1")
        assert "Customer Service" in customer
        assert 'name="reference"' in customer
        assert "Operations Reporting" in reporting
        assert 'name="as_of"' in reporting
        assert "No approval actions" in get("/teams/operations-reporting/approvals")
        assert "customer-service" in customer
        assert LOCAL_MOCK_TOKEN not in customer
        assert LOCAL_MOCK_TOKEN not in reporting
        legacy = urlopen(base + "/runs")
        assert legacy.geturl().endswith("/teams/customer-service/runs")
    finally:
        binding.shutdown()


def test_http_posts_keep_each_team_and_survive_restart(tmp_path: Path) -> None:
    def open_office() -> Installation:
        installation, _gateway = _installation(tmp_path)
        return installation

    def post(base: str, path: str, body: str) -> tuple[str, str]:
        request = Request(base + path, data=body.encode(), method="POST")
        with urlopen(request, timeout=15) as response:
            return response.geturl(), response.read().decode()

    binding = serve_installation(open_office, port=0)
    try:
        host, port = binding.server_address
        base = f"http://{host}:{port}"
        customer_url, customer_page = post(
            base,
            "/teams/customer-service/run",
            "reference=A-100&message=Please+explain+the+latest+invoice.&team_id=customer-service",
        )
        assert "/teams/customer-service/runs/" in customer_url
        assert "completed" in customer_page
        assert "Northwind" in customer_page
        assert "SQLite" not in customer_page
        correlation = customer_url.rstrip("/").rsplit("/", 1)[-1]
        report_url, report_page = post(
            base,
            "/teams/operations-reporting/run",
            f"as_of={DEMO_AS_OF}&note=Weekly+check&team_id=operations-reporting",
        )
        assert "/teams/operations-reporting/runs/" in report_url
        assert "J-100" in report_page
        assert correlation not in report_page
        with urlopen(base + "/teams/operations-reporting/runs", timeout=15) as response:
            operations_runs = response.read().decode()
        assert correlation not in operations_runs
        with urlopen(base + "/teams/operations-reporting/approvals", timeout=15) as response:
            assert "No approval actions" in response.read().decode()
    finally:
        binding.shutdown()

    restored = serve_installation(open_office, port=0)
    try:
        host, port = restored.server_address
        base = f"http://{host}:{port}"
        with urlopen(base + "/teams/customer-service/runs", timeout=15) as response:
            customer_runs = response.read().decode()
        with urlopen(base + "/teams/operations-reporting/runs", timeout=15) as response:
            operations_runs = response.read().decode()
        assert correlation in customer_runs
        assert correlation not in operations_runs
        assert "J-100" in operations_runs
    finally:
        restored.shutdown()
