"""Stdio sessions for the local drafting server. No live provider is called."""

import argparse
import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from urllib.request import urlopen

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import (
    CallToolResult,
    CancelledNotification,
    CancelledNotificationParams,
    ClientNotification,
)
from pydantic import AnyUrl

from agentbuildkit.cli import run
from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.teams import TeamBundle
from agentbuildkit.testing import ScriptedChatClient
from customer_service.evaluation import scripted_clients
from office.installation import build_installation
from office.server import serve_installation

_ROOT = Path(__file__).resolve().parents[2]
_SENTINEL = "sk-drafting-sentinel"
_ALLOWED = {
    "list_templates",
    "list_teams",
    "read_team",
    "read_draft",
    "read_revision",
    "create_team",
    "open_draft",
    "update_draft",
    "validate_draft",
    "evaluate_draft",
    "read_evaluation",
    "compare_draft",
}
_ABSENT = {
    "activate_team",
    "rollback_team",
    "discard_draft",
    "reset_draft",
    "run_team",
    "decide_approval",
    "connect_account",
    "disconnect_account",
    "grant_tool",
    "execute_sql",
}
_READ_ONLY = _ALLOWED - {
    "create_team",
    "open_draft",
    "update_draft",
    "validate_draft",
    "evaluate_draft",
}


def test_cli_rejects_a_relative_directory_without_reading_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    marker = sentinel / "marker"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"APP_STATE_DIRECTORY={sentinel}\nOPENAI_API_KEY={_SENTINEL}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("APP_STATE_DIRECTORY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", _SENTINEL)

    assert run(["mcp", "serve", "--help"]) == 0
    assert "--state-directory" in capsys.readouterr().out
    assert run(["mcp", "serve", "--state-directory", "relative"]) == 2
    captured = capsys.readouterr()
    assert "absolute" in captured.err
    assert _SENTINEL not in f"{captured.out}{captured.err}"
    assert list(tmp_path.glob("*.sqlite")) == []
    assert marker.read_text(encoding="utf-8") == "keep"
    assert list(sentinel.iterdir()) == [marker]


def test_relative_server_command_keeps_stdout_empty(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "office.mcp", "--state-directory", "relative"],
        cwd=tmp_path,
        env=_child_env(tmp_path),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"absolute" in completed.stderr
    assert _SENTINEL.encode() not in completed.stderr
    assert list(sentinel.iterdir()) == []
    assert list(tmp_path.glob("*.sqlite")) == []


@pytest.mark.asyncio
async def test_drafting_session_creates_validates_and_stays_inactive(tmp_path: Path) -> None:
    state, workspace, sentinel = _dirs(tmp_path)
    async with _session(state, workspace) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert names == _ALLOWED
        assert _ABSENT.isdisjoint(names)
        for tool in tools.tools:
            assert tool.inputSchema.get("type") == "object"
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is (tool.name in _READ_ONLY)
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.openWorldHint is False
        create = next(tool for tool in tools.tools if tool.name == "create_team")
        required = set(create.inputSchema.get("required", []))
        assert {"template_id", "title", "description", "idempotency_key"} <= required
        assert create.inputSchema.get("additionalProperties") is False
        key_schema = create.inputSchema["properties"]["idempotency_key"]
        assert "8 to 80" in str(key_schema.get("description"))

        templates = await _ok(session, "list_templates", {})
        ids = [item["template_id"] for item in _rows(templates, "templates")]
        assert ids == ["customer-service", "operations-reporting"]
        assert "does not measure a live model" in str(templates["note"])
        assert _SENTINEL not in str(templates)

        resources = await session.list_resources()
        assert any(
            str(item.uri) == "agentbuildkit://drafting/guide" for item in resources.resources
        )
        guide = await session.read_resource(AnyUrl("agentbuildkit://drafting/guide"))
        guide_text = "".join(getattr(block, "text", "") for block in guide.contents)
        assert "does not activate" in guide_text
        assert _SENTINEL not in guide_text

        created = await _ok(
            session,
            "create_team",
            {
                "template_id": "operations-reporting",
                "title": "Facilities Operations",
                "description": "Plant jobs for the night shift.",
                "idempotency_key": "facilities-key",
            },
        )
        assert created["runnable"] is False
        team_id = str(created["team_id"])
        draft_id = str(created["draft_id"])
        repeated = await _ok(
            session,
            "create_team",
            {
                "template_id": "operations-reporting",
                "title": "  Facilities Operations  ",
                "description": "  Plant jobs for the night shift.  ",
                "idempotency_key": "facilities-key",
            },
        )
        assert repeated["team_id"] == team_id
        assert repeated["draft_id"] == draft_id
        conflict = await session.call_tool(
            "create_team",
            {
                "template_id": "operations-reporting",
                "title": "Facilities Operations",
                "description": "A different description.",
                "idempotency_key": "facilities-key",
            },
        )
        assert conflict.isError is True
        assert "conflict" in _text(conflict)
        assert _SENTINEL not in _text(conflict)

        draft = await _ok(session, "read_draft", {"team_id": team_id, "draft_id": draft_id})
        reviewer = next(agent for agent in _rows(draft, "agents") if agent["name"] == "reviewer")
        reviewer["instructions"] = "Name only job ids present in the records."
        updated = await _ok(
            session,
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": draft["version"],
                "description": "Plant jobs for the morning shift.",
                "agents": [reviewer],
            },
        )
        assert updated["version"] == 2
        assert updated["readiness_cleared"] is True
        assert updated["runnable"] is False
        stale = await session.call_tool(
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": 1,
                "description": "This should not be stored.",
            },
        )
        assert stale.isError is True
        assert "stale" in _text(stale)
        current = await _ok(session, "read_draft", {"team_id": team_id, "draft_id": draft_id})
        assert current["version"] == 2
        assert current["description"] == "Plant jobs for the morning shift."

        other = await _ok(
            session,
            "create_team",
            {
                "template_id": "customer-service",
                "title": "Desk Extra",
                "description": "Another desk.",
                "idempotency_key": "desk-extra-key",
            },
        )
        denied = await session.call_tool(
            "read_draft",
            {"team_id": other["team_id"], "draft_id": draft_id},
        )
        assert denied.isError is True
        assert "forbidden" in _text(denied)
        shipped = await _ok(session, "read_team", {"team_id": "operations-reporting"})
        hidden = await session.call_tool(
            "read_revision",
            {"team_id": team_id, "revision_id": shipped["active_revision_id"]},
        )
        assert hidden.isError is True
        assert "forbidden" in _text(hidden)

        bad_tools = dict(reviewer)
        bad_tools["tools"] = ["schedule_service"]
        rejected = await session.call_tool(
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": 2,
                "agents": [bad_tools],
            },
        )
        assert rejected.isError is True
        assert "invalid" in _text(rejected)
        assert await _ok(session, "read_draft", {"team_id": team_id, "draft_id": draft_id})
        assert (await _ok(session, "read_draft", {"team_id": team_id, "draft_id": draft_id}))[
            "version"
        ] == 2
        unknown_role = dict(reviewer)
        unknown_role["name"] = "alex"
        role_error = await session.call_tool(
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": 2,
                "agents": [unknown_role],
            },
        )
        assert role_error.isError is True
        extra = await session.call_tool(
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": 2,
                "description": "This description must not be saved.",
                "approval_mode": "never_require",
            },
        )
        assert extra.isError is True
        assert "sk-drafting-should-not-echo" not in _text(extra)
        unchanged = await _ok(session, "read_draft", {"team_id": team_id, "draft_id": draft_id})
        assert unchanged["version"] == 2
        assert unchanged["description"] == "Plant jobs for the morning shift."
        hidden_text = "sk-drafting-should-not-echo"
        leaked = await session.call_tool(
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": 2,
                "agents": [
                    {
                        "name": "reviewer",
                        "purpose": reviewer["purpose"],
                        "instructions": hidden_text,
                        "provider": reviewer["provider"],
                        "model": reviewer["model"],
                        "tools": reviewer["tools"],
                        "input_schema": "CustomerRequest",
                    }
                ],
            },
        )
        assert leaked.isError is True
        assert hidden_text not in _text(leaked)
        assert (await _ok(session, "read_draft", {"team_id": team_id, "draft_id": draft_id}))[
            "version"
        ] == 2
        schema_agent = dict(reviewer)
        schema_agent["input_schema"] = "CustomerRequest"
        schema_error = await session.call_tool(
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": 2,
                "agents": [schema_agent],
            },
        )
        assert schema_error.isError is True
        assert (await _ok(session, "read_draft", {"team_id": team_id, "draft_id": draft_id}))[
            "version"
        ] == 2

        opened = await _ok(session, "open_draft", {"team_id": team_id})
        assert opened["draft_id"] == draft_id
        assert opened["preserved_existing"] is True
        assert opened["version"] == 2

        validated = await _ok(
            session,
            "validate_draft",
            {"team_id": team_id, "draft_id": draft_id, "expected_version": 2},
        )
        assert validated["validation_ok"] is True
        assert validated["runnable"] is False
        evaluated = await _ok(
            session,
            "evaluate_draft",
            {"team_id": team_id, "draft_id": draft_id, "expected_version": 2},
        )
        assert evaluated["evaluation_ok"] is True
        assert evaluated["activation_ready"] is True
        assert evaluated["runnable"] is False
        assert evaluated["content_hash"] == updated["content_hash"]
        assert "does not measure a live model" in str(evaluated["note"])
        compared = await _ok(
            session,
            "compare_draft",
            {"team_id": team_id, "draft_id": draft_id},
        )
        assert compared["compared_with"] == "shipped template"
        assert compared["runnable"] is False
        after = " ".join(str(item["after"]) for item in _rows(compared, "changes"))
        assert "morning shift" in after
        assert "Name only job ids present in the records." in after

        changed = await _ok(
            session,
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": 2,
                "description": "Plant jobs after the evaluation.",
            },
        )
        assert changed["version"] == 3
        assert changed["readiness_cleared"] is True
        stale_result = await _ok(
            session,
            "read_evaluation",
            {"team_id": team_id, "draft_id": draft_id},
        )
        assert stale_result["activation_ready"] is False
        assert stale_result["evaluation_ok"] is None
        assert stale_result["runnable"] is False
        restored = await _ok(
            session,
            "update_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": 3,
                "description": "Plant jobs for the morning shift.",
            },
        )
        await _ok(
            session,
            "validate_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": restored["version"],
            },
        )
        await _ok(
            session,
            "evaluate_draft",
            {
                "team_id": team_id,
                "draft_id": draft_id,
                "expected_version": restored["version"],
            },
        )

    names_on_disk = {path.name for path in state.iterdir() if path.is_file()}
    assert "revisions.sqlite" in names_on_disk
    assert "workflows.sqlite" not in names_on_disk
    assert "connections.sqlite" not in names_on_disk
    assert "action-recovery.sqlite" not in names_on_disk
    assert list(sentinel.iterdir()) == []

    async with _session(state, workspace) as session:
        persisted = await _ok(session, "read_team", {"team_id": team_id})
        assert persisted["runnable"] is False
        assert persisted["description"] == "Plant jobs for the morning shift."
        assert persisted["draft_id"] == draft_id

    binding = serve_installation(
        lambda: build_installation(
            customer_clients=scripted_clients("billing-routes-to-sam"),
            reporting_clients={
                "analyst": ScriptedChatClient([]),
                "reviewer": ScriptedChatClient([]),
            },
            directory=state,
        )
    )
    try:
        overview = _page(binding.server_address, f"/teams/{team_id}/")
        assert "Facilities Operations" in overview
        assert "Not active" in overview
        assert 'name="as_of"' not in overview
        draft_page = _page(binding.server_address, f"/teams/{team_id}/drafts/{draft_id}")
        assert "Plant jobs for the morning shift." in draft_page
        assert "Name only job ids present in the records." in draft_page
        async with _session(state, workspace) as session:
            delivery = await _ok(
                session,
                "create_team",
                {
                    "template_id": "operations-reporting",
                    "title": "Delivery Operations",
                    "description": "Route jobs.",
                    "idempotency_key": "delivery-key",
                },
            )
        delivery_id = str(delivery["team_id"])
        discovered = _page(binding.server_address, f"/teams/{delivery_id}/")
        assert "Delivery Operations" in discovered
        assert "Not active" in discovered
        assert 'name="as_of"' not in discovered
    finally:
        binding.shutdown()
    assert list(sentinel.iterdir()) == []


@pytest.mark.asyncio
async def test_concurrent_servers_keep_one_draft(tmp_path: Path) -> None:
    state, workspace, _sentinel = _dirs(tmp_path)
    async with _session(state, workspace) as first, _session(state, workspace) as second:
        created = await _ok(
            first,
            "create_team",
            {
                "template_id": "operations-reporting",
                "title": "Facilities Operations",
                "description": "Plant jobs.",
                "idempotency_key": "facilities-key",
            },
        )
        seen = await _ok(second, "list_teams", {})
        assert any(item["team_id"] == created["team_id"] for item in _rows(seen, "teams"))
        winner = await _ok(
            first,
            "update_draft",
            {
                "team_id": created["team_id"],
                "draft_id": created["draft_id"],
                "expected_version": 1,
                "description": "Plant jobs from the first process.",
            },
        )
        loser = await second.call_tool(
            "update_draft",
            {
                "team_id": created["team_id"],
                "draft_id": created["draft_id"],
                "expected_version": 1,
                "description": "Plant jobs from the second process.",
            },
        )
        assert loser.isError is True
        assert "stale" in _text(loser)
        current = await _ok(
            second,
            "read_draft",
            {"team_id": created["team_id"], "draft_id": created["draft_id"]},
        )
        assert current["version"] == winner["version"]
        assert current["description"] == "Plant jobs from the first process."


@pytest.mark.asyncio
async def test_concurrent_creation_returns_the_original_team(tmp_path: Path) -> None:
    state, workspace, _sentinel = _dirs(tmp_path)
    payload = {
        "template_id": "operations-reporting",
        "title": "Facilities Operations",
        "description": "Plant jobs.",
        "idempotency_key": "facilities-key",
    }
    async with _session(state, workspace) as first, _session(state, workspace) as second:
        one, two = await asyncio.gather(
            _ok(first, "create_team", payload),
            _ok(second, "create_team", payload),
        )
        assert one["team_id"] == two["team_id"]
        assert one["runnable"] is False
        listed = await _ok(first, "list_teams", {})
        matches = [item for item in _rows(listed, "teams") if item["team_id"] == one["team_id"]]
        assert matches == [item for item in matches if item["runnable"] is False]
        assert len(matches) == 1


@pytest.mark.asyncio
async def test_stopped_evaluation_does_not_record_a_pass(tmp_path: Path) -> None:
    state, workspace, _sentinel = _dirs(tmp_path)
    async with _session(state, workspace, mode="slow") as session:
        created = await _create_reporting(session)
        stopped = await session.call_tool(
            "evaluate_draft",
            {
                "team_id": created["team_id"],
                "draft_id": created["draft_id"],
                "expected_version": 1,
                "timeout_seconds": 0.2,
            },
        )
        assert stopped.isError is True
        message = _text(stopped)
        assert "timeout" in message
        assert "No result was stored" in message
        assert _SENTINEL not in message
        ready = await _ok(
            session,
            "read_evaluation",
            {"team_id": created["team_id"], "draft_id": created["draft_id"]},
        )
        assert ready["activation_ready"] is False
        assert ready["evaluation_ok"] is None
    async with _session(state, workspace, mode="fail") as session:
        created = await _create_reporting(session, key="failure-key", title="Failure Desk")
        failed = await session.call_tool(
            "evaluate_draft",
            {
                "team_id": created["team_id"],
                "draft_id": created["draft_id"],
                "expected_version": 1,
            },
        )
        assert failed.isError is True
        message = _text(failed)
        assert "unavailable" in message
        assert "sk-drafting-should-not-leak" not in message
        ready = await _ok(
            session,
            "read_evaluation",
            {"team_id": created["team_id"], "draft_id": created["draft_id"]},
        )
        assert ready["activation_ready"] is False


@pytest.mark.asyncio
async def test_client_cancellation_does_not_record_a_pass(tmp_path: Path) -> None:
    state, workspace, _sentinel = _dirs(tmp_path)
    async with _session(state, workspace, mode="slow") as session:
        created = await _create_reporting(session, key="cancel-key", title="Cancel Desk")
        issued = _request_id(session)
        task = asyncio.create_task(
            session.call_tool(
                "evaluate_draft",
                {
                    "team_id": created["team_id"],
                    "draft_id": created["draft_id"],
                    "expected_version": 1,
                    "timeout_seconds": 30,
                },
            )
        )
        for _ in range(100):
            if _request_id(session) == issued + 1:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("The evaluation request was not sent.")
        await session.send_notification(
            ClientNotification(
                CancelledNotification(
                    params=CancelledNotificationParams(
                        requestId=issued,
                        reason="operator cancelled",
                    )
                )
            )
        )
        try:
            result = await task
        except McpError as exc:
            assert "cancel" in str(exc).lower()
        else:
            assert result.isError is True
            assert "cancel" in _text(result).lower()
        await asyncio.sleep(1.3)
        ready = await _ok(
            session,
            "read_evaluation",
            {"team_id": created["team_id"], "draft_id": created["draft_id"]},
        )
        assert ready["activation_ready"] is False
        assert ready["evaluation_ok"] is None
        assert ready["runnable"] is False


def _request_id(session: ClientSession) -> int:
    value = session._request_id  # pyright: ignore[reportPrivateUsage]
    assert isinstance(value, int)
    return value


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    sentinel = tmp_path / "sentinel"
    state.mkdir()
    workspace.mkdir()
    sentinel.mkdir()
    (workspace / ".env").write_text(
        f"APP_STATE_DIRECTORY={sentinel}\nOPENAI_API_KEY={_SENTINEL}\n",
        encoding="utf-8",
    )
    return state, workspace, sentinel


def _child_env(workspace: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("APP_STATE_DIRECTORY", None)
    env["PYTHONPATH"] = os.pathsep.join([str(_ROOT), str(_ROOT / "src"), str(_ROOT / "examples")])
    env["OPENAI_API_KEY"] = _SENTINEL
    env["ANTHROPIC_API_KEY"] = _SENTINEL
    env["COMPOSIO_API_KEY"] = _SENTINEL
    env["PWD"] = str(workspace)
    return env


@asynccontextmanager
async def _session(
    state: Path,
    workspace: Path,
    *,
    mode: str = "serve",
) -> AsyncGenerator[ClientSession]:
    err_path = state / f"{mode}.stderr"
    err = err_path.open("w", encoding="utf-8")
    args = ["-m", "office.mcp", "--state-directory", str(state)]
    if mode != "serve":
        args = [
            "-m",
            "tests.unit.test_mcp_drafting",
            "--state-directory",
            str(state),
            f"--{mode}",
        ]
    params = StdioServerParameters(
        command=sys.executable,
        args=args,
        cwd=str(workspace),
        env=_child_env(workspace),
    )
    try:
        async with (
            stdio_client(params, errlog=err) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
    finally:
        err.close()
        logged = err_path.read_text(encoding="utf-8")
        assert _SENTINEL not in logged
        assert "sk-drafting-should-not-leak" not in logged


async def _ok(
    session: ClientSession,
    name: str,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    result = await session.call_tool(name, dict(arguments))
    assert result.isError is False, _text(result)
    body = result.structuredContent
    assert isinstance(body, dict)
    assert _SENTINEL not in str(body)
    return body


def _rows(body: Mapping[str, object], key: str) -> list[dict[str, object]]:
    rows = body[key]
    assert isinstance(rows, list)
    typed: list[dict[str, object]] = []
    for item in cast(list[object], rows):
        assert isinstance(item, dict)
        typed.append(cast(dict[str, object], item))
    return typed


def _text(result: CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content:
        text = getattr(block, "text", "")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


async def _create_reporting(
    session: ClientSession,
    *,
    key: str = "slow-key",
    title: str = "Slow Desk",
) -> dict[str, object]:
    return await _ok(
        session,
        "create_team",
        {
            "template_id": "operations-reporting",
            "title": title,
            "description": "Temporary jobs.",
            "idempotency_key": key,
        },
    )


def _page(address: tuple[str, int], path: str) -> str:
    host = "127.0.0.1" if address[0] in {"0.0.0.0", "::"} else address[0]
    with urlopen(f"http://{host}:{address[1]}{path}", timeout=10) as response:
        return response.read().decode()


def main() -> None:
    """Serve a test evaluator that sleeps or fails. Production uses office.mcp."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--slow", action="store_true")
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args()
    from agentbuildkit.drafting import build_server, quiet_diagnostics, require_state_directory
    from agentbuildkit.revisions import RevisionService, SqliteRevisionStore
    from office.templates import installed_templates

    directory = require_state_directory(str(args.state_directory))
    service = RevisionService(
        SqliteRevisionStore(directory / "revisions.sqlite"),
        installed_templates(),
        credentials=lambda _provider: False,
    )
    service.seed()

    async def evaluator(bundle: TeamBundle) -> EvaluationReport:
        del bundle
        if args.fail:
            raise RuntimeError("sk-drafting-should-not-leak")
        await asyncio.sleep(1)
        return EvaluationReport(
            total=1,
            passed=1,
            failed=0,
            pass_rate=1,
            elapsed_seconds=1,
            failures_by_assertion={},
            cases=(),
        )

    quiet_diagnostics()
    server = build_server(
        service,
        {"customer-service": evaluator, "operations-reporting": evaluator},
    )
    try:
        server.run(transport="stdio")
    finally:
        service.close()


if __name__ == "__main__":
    main()
