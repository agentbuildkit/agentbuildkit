"""Offline Google Sheets connection tests. Composio is mocked."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.request import Request, urlopen

import pytest
from agent_framework import ChatResponse, Content, Message, SecretString

from agentbuildkit.guardrails import execute_tool
from agentbuildkit.integrations.composio import (
    AccountView,
    ComposioProviderError,
    ComposioRejected,
    LinkRequest,
    LiveComposioGateway,
    resolve_read_tool,
    run_allowlisted_read,
)
from agentbuildkit.observability import AuditLog
from agentbuildkit.testing import ScriptedChatClient
from agentbuildkit.workflows import memory_workflow_store
from customer_service.business import BusinessSystem
from customer_service.connections import SqliteConnectionStore
from customer_service.dashboard import DashboardService, serve
from customer_service.models import CustomerRequest
from customer_service.service import handle_request
from customer_service.sheets import CATALOG, READ_SPEC, SheetDesk
from customer_service.team import ORGANISATION_SCOPE, build_team

_SHEET = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
_OTHER_SHEET = "1ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
_RANGE = "Accounts!A1:B2"
_OUTSIDE = "Accounts!A1:C2"
_SESSION = "session-token-session-token-session-1"
_STATE = "a" * 43
_ACCOUNT = "ca_sheet_1"
_OTHER = "ca_other_account"
_SECRET = "sk-composio-secret"
_CELL = "SECRET-CELL"
_CALLBACK = f"http://127.0.0.1:8765/connections/callback?state={_STATE}"


class FakeGateway:
    """Records calls and returns canned account state. It does not use the network."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.auth_config_id = "ac_readonly_sheets"
        self.calls: list[tuple[object, ...]] = []
        self.status = "ACTIVE"
        self.fail = False
        self.payload: dict[str, object] = {
            "successful": True,
            "data": {"values": [["Balance"], ["45.00"]]},
        }

    def start_link(self, *, user_id: str, toolkit: str, callback_url: str) -> LinkRequest:
        self.calls.append(("link", user_id, toolkit, callback_url))
        return LinkRequest(id="lnk_1", redirect_url="https://connect.example.test/link")

    def get_account(self, account_id: str) -> AccountView:
        self.calls.append(("account", account_id))
        if account_id not in {_ACCOUNT, _OTHER}:
            raise ComposioProviderError()
        return AccountView(
            id=account_id,
            user_id=self.user_id if account_id == _ACCOUNT else "someone-else",
            toolkit="googlesheets",
            status=self.status,
        )

    def execute(
        self,
        *,
        slug: str,
        version: str,
        account_id: str,
        user_id: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        self.calls.append(("execute", slug, version, account_id, user_id, arguments))
        if self.fail:
            raise RuntimeError(f"{_SECRET} {_CELL}")
        return self.payload


def _silent(**_kwargs: object) -> None:
    return None


def _clients(
    *,
    alex: list[str | ChatResponse] | None = None,
    sam: list[str | ChatResponse] | None = None,
    jordan: list[str | ChatResponse] | None = None,
) -> dict[str, ScriptedChatClient]:
    return {
        "alex": ScriptedChatClient(alex or []),
        "sam": ScriptedChatClient(sam or []),
        "taylor": ScriptedChatClient([]),
        "jordan": ScriptedChatClient(jordan or []),
    }


def _decision(specialist: str, category: str) -> str:
    return json.dumps(
        {
            "specialist": specialist,
            "category": category,
            "reference": "A-100",
            "message": "classified",
        }
    )


def _proposal(specialist: str, response: str) -> str:
    return json.dumps(
        {
            "specialist": specialist,
            "reference": "A-100",
            "response": response,
            "needs_action": False,
            "action": None,
        }
    )


def _quality(response: str) -> str:
    return json.dumps({"verdict": "approve", "reference": "A-100", "response": response})


def _call(name: str, arguments: dict[str, object]) -> ChatResponse:
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
    return None


def _desk(path: Path | None = None) -> tuple[SheetDesk, FakeGateway]:
    if path is None:
        from customer_service.connections import MemoryConnectionStore

        store: MemoryConnectionStore | SqliteConnectionStore = MemoryConnectionStore()
    else:
        store = SqliteConnectionStore(path)
    gateway = FakeGateway("")
    desk = SheetDesk(store, gateway)
    gateway.user_id = store.operator_id()
    return desk, gateway


def _assign(
    desk: SheetDesk,
    connection_id: str,
    *,
    mode: str = "connected",
    sheet: str = _SHEET,
    cell_range: str = _RANGE,
) -> None:
    desk.assign("sam", connection_id, sheet, cell_range, mode=mode)


def _connect(desk: SheetDesk) -> str:
    desk.begin(session_token=_SESSION, callback_url=_CALLBACK)
    desk.complete(session_token=_SESSION, state=_STATE, account_id=_ACCOUNT)
    connection = desk.store.connections()[0]
    assert connection.connected_account_id == _ACCOUNT
    assert desk.store.assignments() == ()
    return connection.connection_id


def _executes(gateway: FakeGateway) -> list[tuple[object, ...]]:
    return [call for call in gateway.calls if call[0] == "execute"]


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        items = cast(list[object], value)
        return "\n".join(_text(item) for item in items)
    result = getattr(value, "result", None)
    if isinstance(result, str):
        return result
    text = getattr(value, "text", None)
    return text if isinstance(text, str) else ""


async def test_assigned_read_uses_the_saved_account_and_distinguishes_demo_data() -> None:
    desk, gateway = _desk()
    connection_id = _connect(desk)
    _assign(desk, connection_id)
    audit_log = AuditLog()
    organisation = build_team(
        clients=_clients(
            alex=[_decision("sam", "billing")],
            sam=[
                _call(
                    "read_sheet_range",
                    {"spreadsheet_id_or_url": _SHEET, "cell_range": _RANGE},
                ),
                _proposal("sam", "The connected spreadsheet shows a balance of 45.00."),
            ],
            jordan=[_quality("The connected spreadsheet shows a balance of 45.00.")],
        ),
        api_token=SecretString("local-mock-business-token"),
        audit_log=audit_log,
        configurator=_silent,
        connected_reads=desk.tools_for,
        account_guard=desk.guard_account,
    )
    try:
        result = await handle_request(
            organisation,
            CustomerRequest(reference="A-100", message="What does the sheet say?"),
            correlation_id="sheet-read",
            store=memory_workflow_store(ORGANISATION_SCOPE, audit_log),
        )
    finally:
        await organisation.aclose()

    assert result.outcome.status == "completed"
    assert "45.00" in result.outcome.response
    assert "connected spreadsheet" in result.outcome.response
    assert "120.00" not in result.outcome.response
    assert "Northwind" not in result.outcome.response
    assert organisation.business.account_reads == []
    assert _executes(gateway) == [
        (
            "execute",
            "GOOGLESHEETS_BATCH_GET",
            "20261101_00",
            _ACCOUNT,
            desk.store.operator_id(),
            {"spreadsheet_id": _SHEET, "ranges": [_RANGE]},
        )
    ]
    exported = json.dumps([event.model_dump() for event in audit_log.events])
    tool_events = [
        event
        for event in audit_log.events
        if event.tool_name == "read_sheet_range" and event.kind == "tool"
    ]
    assert tool_events
    assert tool_events[0].agent_name == "sam"
    assert tool_events[0].provider == "openai"
    assert tool_events[0].model == "gpt-4.1-mini"
    assert tool_events[0].correlation_id == "sheet-read"
    assert "45.00" not in exported
    assert _CELL not in exported
    assert _SECRET not in exported


async def test_substitution_bounds_and_unassigned_reads_make_no_external_call() -> None:
    desk, gateway = _desk()
    connection_id = _connect(desk)
    _assign(desk, connection_id)
    sam_read = next(tool for tool in desk.tools_for("sam") if tool.name == "read_sheet_range")
    taylor = next(tool for tool in desk.tools_for("taylor") if tool.name == "read_sheet_range")
    with pytest.raises(ComposioRejected, match="not assigned"):
        await execute_tool(
            sam_read,
            {"spreadsheet_id_or_url": _OTHER_SHEET, "cell_range": _RANGE},
            audit_log=AuditLog(),
            correlation_id="other-sheet",
            agent_name="sam",
        )
    with pytest.raises(ComposioRejected, match="outside the assigned window"):
        await execute_tool(
            sam_read,
            {"spreadsheet_id_or_url": _SHEET, "cell_range": _OUTSIDE},
            audit_log=AuditLog(),
            correlation_id="outside",
            agent_name="sam",
        )
    with pytest.raises(ComposioRejected, match="not assigned"):
        await execute_tool(
            taylor,
            {"spreadsheet_id_or_url": _SHEET, "cell_range": _RANGE},
            audit_log=AuditLog(),
            correlation_id="taylor",
            agent_name="taylor",
        )
    with pytest.raises(ComposioRejected, match="not allowed"):
        desk.assign("sam", connection_id, _SHEET, "Accounts!A1:Z100", mode="connected")
    with pytest.raises(ComposioRejected, match="Write tools"):
        resolve_read_tool("GOOGLESHEETS_VALUES_UPDATE", CATALOG)
    with pytest.raises(ComposioRejected, match="not allowlisted"):
        run_allowlisted_read(
            gateway,
            READ_SPEC.model_copy(update={"slug": "GOOGLESHEETS_VALUES_UPDATE"}),
            CATALOG,
            account_id=_ACCOUNT,
            user_id=desk.store.operator_id(),
            arguments={"spreadsheet_id": _SHEET, "ranges": [_RANGE]},
        )
    with pytest.raises(ComposioRejected, match="not allowed"):
        run_allowlisted_read(
            gateway,
            READ_SPEC,
            CATALOG,
            account_id=_ACCOUNT,
            user_id=desk.store.operator_id(),
            arguments={
                "spreadsheet_id": _SHEET,
                "ranges": [_RANGE],
                "connected_account_id": _OTHER,
            },
        )
    assert gateway.calls == [
        ("link", desk.store.operator_id(), "googlesheets", _CALLBACK),
        ("account", _ACCOUNT),
    ]


async def test_connected_mode_uses_sheet_values_and_does_not_fall_back() -> None:
    desk, gateway = _desk()
    connection_id = _connect(desk)
    _assign(desk, connection_id)
    business = BusinessSystem(
        audit_log=AuditLog(),
        api_token=SecretString("local-mock-business-token"),
    )
    tool = desk.guard_account(business)
    text = _text(
        await execute_tool(
            tool,
            {"reference": "A-100"},
            audit_log=AuditLog(),
            correlation_id="connected-account",
            agent_name="sam",
        )
    )
    assert "45.00" in text
    assert "120.00" not in text
    assert "Northwind" not in text
    assert business.account_reads == []

    gateway.fail = True
    with pytest.raises(ComposioProviderError, match="could not complete"):
        await execute_tool(
            tool,
            {"reference": "A-100"},
            audit_log=AuditLog(),
            correlation_id="failed-read",
            agent_name="sam",
        )
    assert business.account_reads == []

    gateway.fail = False
    gateway.status = "EXPIRED"
    with pytest.raises(ComposioRejected, match="not available"):
        await execute_tool(
            tool,
            {"reference": "A-100"},
            audit_log=AuditLog(),
            correlation_id="expired-read",
            agent_name="sam",
        )
    assert business.account_reads == []

    gateway.status = "ACTIVE"
    desk.store.mark(connection_id, "ACTIVE")
    desk.disconnect(connection_id)
    with pytest.raises(ComposioRejected, match="not available"):
        await execute_tool(
            tool,
            {"reference": "A-100"},
            audit_log=AuditLog(),
            correlation_id="disconnected-account",
            agent_name="sam",
        )
    assert business.account_reads == []
    assert desk.store.data_mode("sam") == "connected"
    desk.assign("sam", connection_id, _SHEET, _RANGE, mode="demo")
    demo = _text(
        await execute_tool(
            tool,
            {"reference": "A-100"},
            audit_log=AuditLog(),
            correlation_id="demo-account",
            agent_name="sam",
        )
    )
    assert "Northwind" in demo
    assert "120.00" in demo
    assert business.account_reads == ["A-100"]
    sheet = next(item for item in desk.tools_for("sam") if item.name == "read_sheet_range")
    before = list(gateway.calls)
    with pytest.raises(ComposioRejected, match="not assigned"):
        await execute_tool(
            sheet,
            {"spreadsheet_id_or_url": _SHEET, "cell_range": _RANGE},
            audit_log=AuditLog(),
            correlation_id="demo-sheet",
            agent_name="sam",
        )
    assert gateway.calls == before


async def test_oversized_sheet_result_is_rejected_without_a_partial_answer() -> None:
    desk, gateway = _desk()
    connection_id = _connect(desk)
    _assign(desk, connection_id)
    long_cell = "LONG-" + ("x" * 500)
    gateway.payload = {
        "successful": True,
        "data": {
            "values": [
                ["Balance", long_cell, "EXTRA-CELL"],
                ["45.00", "ok", "EXTRA-CELL"],
                ["EXTRA-CELL", "EXTRA-CELL", "EXTRA-CELL"],
            ]
        },
    }
    tool = next(item for item in desk.tools_for("sam") if item.name == "read_sheet_range")
    with pytest.raises(ComposioRejected, match="was not returned") as caught:
        await execute_tool(
            tool,
            {"spreadsheet_id_or_url": _SHEET, "cell_range": _RANGE},
            audit_log=AuditLog(),
            correlation_id="oversized",
            agent_name="sam",
        )
    message = str(caught.value)
    assert "Balance" not in message
    assert "45.00" not in message
    assert "EXTRA-CELL" not in message
    assert "LONG-" not in message
    gateway.payload = {
        "successful": True,
        "data": {"values": [["reference", "customer"], ["A-100", "x" * 201]]},
    }
    with pytest.raises(ComposioRejected, match="was not returned") as long_cell:
        await execute_tool(
            tool,
            {"spreadsheet_id_or_url": _SHEET, "cell_range": _RANGE},
            audit_log=AuditLog(),
            correlation_id="long-cell",
            agent_name="sam",
        )
    assert "x" * 201 not in str(long_cell.value)
    gateway.payload = {
        "successful": True,
        "data": {"values": [["customer", "balance"], ["Example Test Ltd", "47.50"]]},
    }
    text = _text(
        await execute_tool(
            tool,
            {"spreadsheet_id_or_url": _SHEET, "cell_range": _RANGE},
            audit_log=AuditLog(),
            correlation_id="exact",
            agent_name="sam",
        )
    )
    assert "Example Test Ltd" in text
    assert "47.50" in text


def test_quoted_acceptance_range_fits_the_window() -> None:
    from customer_service.sheets import parse_window

    window = parse_window("'Accounts'!A1:C2")
    assert window.rows == 2
    assert window.columns == 3
    assert window.rows * window.columns <= 200


def test_unknown_and_write_tools_are_denied() -> None:
    with pytest.raises(ComposioRejected, match="not allowlisted"):
        resolve_read_tool("GOOGLESHEETS_NOT_A_TOOL", CATALOG)
    with pytest.raises(ComposioRejected, match="Write tools"):
        resolve_read_tool("GOOGLESHEETS_VALUES_UPDATE", CATALOG)
    with pytest.raises(ComposioRejected, match="Write tools"):
        resolve_read_tool("GOOGLESHEETS_EXECUTE_SQL", CATALOG)
    with pytest.raises(ComposioRejected, match="not allowlisted"):
        resolve_read_tool("GOOGLESHEETS_SEARCH_SPREADSHEETS", CATALOG)


async def test_expired_disconnected_and_provider_failures_hide_secrets() -> None:
    desk, gateway = _desk()
    connection_id = _connect(desk)
    _assign(desk, connection_id)
    tool = next(item for item in desk.tools_for("sam") if item.name == "read_sheet_range")
    arguments = {"spreadsheet_id_or_url": _SHEET, "cell_range": _RANGE}
    gateway.status = "EXPIRED"
    with pytest.raises(ComposioRejected, match="not available"):
        await execute_tool(tool, arguments, audit_log=AuditLog(), correlation_id="expired")
    found = desk.store.connection(connection_id)
    assert found is not None
    assert found.status == "EXPIRED"
    assert _executes(gateway) == []

    gateway.status = "ACTIVE"
    desk.store.mark(connection_id, "ACTIVE")
    desk.disconnect(connection_id)
    with pytest.raises(ComposioRejected, match="not assigned"):
        await execute_tool(tool, arguments, audit_log=AuditLog(), correlation_id="disconnected")
    disconnected = desk.store.connection(connection_id)
    assert disconnected is not None
    assert disconnected.status == "DISCONNECTED"
    assert _executes(gateway) == []

    connection_id = _connect_again(desk)
    _assign(desk, connection_id)
    gateway.fail = True
    with pytest.raises(ComposioProviderError, match="could not complete") as caught:
        await execute_tool(tool, arguments, audit_log=AuditLog(), correlation_id="failed")
    assert _SECRET not in str(caught.value)
    assert _CELL not in str(caught.value)
    assert caught.value.__cause__ is None


def test_assignment_survives_restart_and_the_file_has_no_secrets(tmp_path: Path) -> None:
    path = tmp_path / "connections.sqlite"
    desk, gateway = _desk(path)
    connection_id = _connect(desk)
    _assign(desk, connection_id)
    operator = desk.store.operator_id()
    desk.store.close()

    reopened = SqliteConnectionStore(path)
    gateway.user_id = operator
    again = SheetDesk(reopened, gateway)
    assert again.store.operator_id() == operator
    assignment = again.store.assignment("sam")
    assert assignment is not None
    assert assignment.tools == ("read_sheet_range",)
    assert assignment.spreadsheet_id == _SHEET
    assert assignment.cell_range == _RANGE
    assert assignment.mode == "connected"
    assert again.store.data_mode("sam") == "connected"
    assert again.account_for("sam", "read_sheet_range") == _ACCOUNT
    raw = path.read_bytes()
    assert _SECRET.encode() not in raw
    assert _CELL.encode() not in raw
    assert b"https://connect.example.test" not in raw
    assert b"ya29" not in raw
    again.store.close()


def test_callback_rejects_a_different_session_and_account() -> None:
    desk, _gateway = _desk()
    desk.begin(session_token=_SESSION, callback_url=_CALLBACK)
    with pytest.raises(ComposioRejected, match="not completed"):
        desk.complete(
            session_token="other-session-token-other-session-99",
            state=_STATE,
            account_id=_ACCOUNT,
        )
    assert desk.store.connections() == ()
    with pytest.raises(ComposioRejected, match="not completed"):
        desk.complete(session_token=_SESSION, state=_STATE, account_id=_OTHER)
    assert desk.store.connections() == ()


def test_dashboard_connection_pages_do_not_show_secrets() -> None:
    desk, gateway = _desk()
    connection_id = _connect(desk)
    _assign(desk, connection_id)
    service = _dashboard(desk)
    try:
        connections = service.render("/connections")
        agent = service.render("/team/sam")
    finally:
        pass
    assert "Connect Google Sheets" in connections
    assert "GOOGLESHEETS_BATCH_GET" in connections
    assert "spreadsheets.readonly" in connections
    assert "does not revoke Google access" in connections
    assert "SEARCH_SPREADSHEETS" not in connections
    assert "search_spreadsheets" not in agent
    assert "Demo mode" in agent
    assert "Connected spreadsheet" in agent
    assert "Sam" in agent
    assert _SECRET not in connections
    assert _SECRET not in agent
    assert _ACCOUNT not in connections
    rejected = service.assign_connection(
        "sam", connection_id, _SHEET, "Accounts!A1:Z100", "connected"
    )
    assert rejected == "window"
    window = service.render("/team/sam", {"notice": "window"})
    assert "saved assignment was not changed" in window
    assert "Accounts!A1:B2" in window
    assert service.test_connection(connection_id) == "checked"
    assert _executes(gateway) == []
    checked = service.render("/connections", {"notice": "checked"})
    assert "cells were not read" in checked
    assert _CELL not in checked
    assert "45.00" not in checked
    del gateway


def test_http_callback_is_bound_to_the_browser_session() -> None:
    desk, _gateway = _desk()
    desk.begin(session_token=_SESSION, callback_url=_CALLBACK)
    service = _dashboard(desk)
    binding = serve(service, port=0)
    try:
        host, port = binding.server_address
        request = Request(
            f"http://{host}:{port}/connections/callback?state={_STATE}&connected_account_id={_ACCOUNT}",
            headers={"Cookie": "abk_session=other-session-token-other-session-99"},
        )
        with urlopen(request, timeout=10) as response:
            page = response.read().decode()
    finally:
        binding.shutdown()

    assert "No account was saved" in page
    assert desk.store.connections() == ()
    assert _SECRET not in page


def test_missing_auth_config_disables_connect_and_does_not_link() -> None:
    desk, gateway = _desk()
    gateway.auth_config_id = ""
    with pytest.raises(ComposioRejected, match="managed Composio app is not used"):
        desk.begin(session_token=_SESSION, callback_url=_CALLBACK)
    assert gateway.calls == []
    service = _dashboard(desk)
    page = service.render("/connections")
    assert "Connect Google Sheets</button>" not in page
    assert "COMPOSIO_GOOGLESHEETS_AUTH_CONFIG_ID" in page
    assert "managed Google app is not used" in page
    assert service.start_connection(_SESSION, _CALLBACK) == ("", "authconfig")


def test_missing_composio_package_is_not_described_as_a_missing_key() -> None:
    from customer_service.connections import MemoryConnectionStore

    desk = SheetDesk(MemoryConnectionStore(), None, package_missing=True)
    page = _dashboard(desk).render("/connections")
    assert "uv sync --extra composio" in page
    assert "Connect Google Sheets</button>" not in page
    assert "until the server has a Composio API key" not in page


def test_live_sign_in_selects_the_configured_auth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_composio(monkeypatch, _readonly_config())
    gateway = _live_gateway("ac_readonly_sheets")
    link = gateway.start_link(user_id="operator-1", toolkit="googlesheets", callback_url=_CALLBACK)
    assert link.redirect_url == "https://connect.example.test/link"
    created = [item for item in calls if item[0] == "POST"]
    assert created == [
        (
            "POST",
            "/connected_accounts/link",
            {
                "auth_config_id": "ac_readonly_sheets",
                "user_id": "operator-1",
                "callback_url": _CALLBACK,
            },
        )
    ]
    assert ("GET", "/auth_configs/ac_readonly_sheets", {}) in calls
    assert not any(item[0] in {"create", "link"} for item in calls)
    assert _CLIENT_SECRET not in str(calls)
    assert _CLIENT_SECRET not in link.redirect_url


def test_live_sign_in_never_uses_the_managed_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_composio(monkeypatch, _readonly_config())
    missing = _live_gateway("")
    with pytest.raises(ComposioRejected, match="not configured"):
        missing.start_link(user_id="operator-1", toolkit="googlesheets", callback_url=_CALLBACK)
    assert not any(item[0] in {"create", "link", "GET", "POST"} for item in calls)

    managed = {
        "toolkit": {"slug": "googlesheets"},
        "is_composio_managed": True,
        "type": "default",
        "credentials": {"scopes": _READONLY, "client_secret": _CLIENT_SECRET},
    }
    calls = _install_composio(monkeypatch, managed)
    gateway = _live_gateway("ac_managed_default")
    with pytest.raises(ComposioRejected, match="not a custom") as rejected:
        gateway.start_link(user_id="operator-1", toolkit="googlesheets", callback_url=_CALLBACK)
    assert not any(item[0] in {"create", "link", "POST"} for item in calls)
    assert _CLIENT_SECRET not in str(calls)
    assert _CLIENT_SECRET not in str(rejected.value)


def test_auth_config_metadata_must_show_only_the_read_scope() -> None:
    from agentbuildkit.integrations.composio import require_sheets_auth_config

    broad = _readonly_config()
    broad["credentials"] = {
        "scopes": f"{_READONLY},https://www.googleapis.com/auth/drive",
        "client_secret": _CLIENT_SECRET,
    }
    with pytest.raises(ComposioRejected, match="not a custom") as rejected:
        require_sheets_auth_config(broad)
    assert _CLIENT_SECRET not in str(rejected.value)

    hidden = _readonly_config()
    hidden["credentials"] = {"client_secret": _CLIENT_SECRET}
    with pytest.raises(ComposioRejected, match="requested scopes") as unverified:
        require_sheets_auth_config(hidden)
    assert _CLIENT_SECRET not in str(unverified.value)
    assert "https://www.googleapis.com/auth/drive" not in str(unverified.value)


_READONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"
_CLIENT_SECRET = "google-client-secret-value"


def _readonly_config() -> dict[str, object]:
    return {
        "toolkit": {"slug": "googlesheets"},
        "is_composio_managed": False,
        "type": "custom",
        "auth_scheme": "OAUTH2",
        "status": "ENABLED",
        "credentials": {"scopes": _READONLY, "client_secret": _CLIENT_SECRET},
    }


def _install_composio(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    class _Accounts:
        def link(
            self,
            user_id: str,
            auth_config_id: str,
            *,
            callback_url: str | None = None,
        ) -> object:
            calls.append(("link", user_id, auth_config_id, callback_url))
            return type(
                "Link",
                (),
                {"id": "lnk_1", "redirect_url": "https://connect.example.test/link"},
            )()

    class _AuthConfigs:
        def get(self, config_id: str) -> object:
            calls.append(("get", config_id))
            return payload

    class _Composio:
        def __init__(self, api_key: str, **_kwargs: object) -> None:
            calls.append(("init", len(api_key)))
            self.auth_configs = _AuthConfigs()
            self.connected_accounts = _Accounts()

        def create(self, **kwargs: object) -> object:
            calls.append(("create", kwargs))
            raise AssertionError("session create")

    from agentbuildkit.integrations import composio as composio_module

    def _read(
        api_key: str,
        method: str,
        path: str,
        body: object = None,
    ) -> object:
        del api_key
        recorded: dict[str, object] = {}
        if isinstance(body, dict):
            raw = cast(dict[object, object], body)
            recorded = {str(key): value for key, value in raw.items()}
        calls.append((method, path, recorded))
        if method == "GET":
            return payload
        return {
            "connected_account_id": "ca_test",
            "redirect_url": "https://connect.example.test/link",
        }

    monkeypatch.setattr(composio_module, "_read_json", _read)

    real_import = composio_module.importlib.import_module

    def _import(name: str) -> object:
        if name == "composio":
            return type("Module", (), {"Composio": _Composio})()
        return real_import(name)

    monkeypatch.setattr("agentbuildkit.integrations.composio.importlib.import_module", _import)
    return calls


def _live_gateway(auth_config_id: str) -> LiveComposioGateway:
    return LiveComposioGateway(SecretString("local-test-key"), auth_config_id=auth_config_id)


def _connect_again(desk: SheetDesk) -> str:
    state = "b" * 43
    desk.begin(
        session_token=_SESSION,
        callback_url=f"http://127.0.0.1:8765/connections/callback?state={state}",
    )
    desk.complete(session_token=_SESSION, state=state, account_id=_ACCOUNT)
    active = [item.connection_id for item in desk.store.connections() if item.status == "ACTIVE"]
    return active[-1]


def _dashboard(desk: SheetDesk) -> DashboardService:
    audit_log = AuditLog()
    organisation = build_team(
        clients=_clients(),
        api_token=SecretString("local-mock-business-token"),
        audit_log=audit_log,
        configurator=_silent,
        connected_reads=desk.tools_for,
    )
    return DashboardService(
        organisation,
        memory_workflow_store(ORGANISATION_SCOPE, audit_log),
        sheets=desk,
    )
