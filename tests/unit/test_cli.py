"""Offline tests for the AgentBuildKit command and demonstration."""

import os
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import agentbuildkit
from agentbuildkit.cli import doctor, run
from agentbuildkit.demo import DemoError, DemoResult, run_demo
from agentbuildkit.observability import AuditEvent

_SECRET = "doctor-secret-token"
_ROOT = Path(__file__).resolve().parents[2]


def test_help_describes_agentbuildkit(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--help"]) == 0
    output = capsys.readouterr().out
    assert "AgentBuildKit" in output
    assert "demo" in output
    assert "doctor" in output
    assert "mcp" in output
    assert "version" in output
    assert "customer-service" not in output
    assert "Northwind" not in output


def test_version_comes_from_the_package(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"AgentBuildKit {agentbuildkit.__version__}"


def test_doctor_is_ready_without_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert doctor() == 0

    output = capsys.readouterr().out
    assert "AgentBuildKit Doctor" in output
    assert "not configured" in output
    assert "Status: ready for offline demo" in output
    assert "OpenAI" in output
    assert "Anthropic" in output


def test_doctor_does_not_print_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"anthropic-{_SECRET}")

    assert doctor() == 0

    captured = capsys.readouterr()
    combined = f"{captured.out}{captured.err}"
    assert _SECRET not in combined
    assert "configured" in captured.out
    assert "not configured" not in captured.out


def test_doctor_fails_when_sqlite_cannot_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(sqlite3, "connect", broken)

    assert doctor() == 1
    assert "installation check failed" in capsys.readouterr().out


def test_approve_and_reject_are_mutually_exclusive() -> None:
    assert run(["demo", "--approve", "--reject"]) == 2


async def test_demo_approve_uses_the_harness(tmp_path: Path) -> None:
    result = await run_demo(decision=True, store_path=tmp_path / "demo.sqlite")

    assert result.ok
    assert result.route == "service"
    assert f"route: {result.route}" in result.text
    assert _assertion(result, "route")
    assert result.reads == ("T-100",)
    assert result.visits == ("T-100",)
    assert result.visits_before_approval == 0
    assert result.report.failed == 0
    assert result.report.passed == 1
    assert _assertion(result, "unknown_tool")
    assert _assertion(result, "tool_called")
    assert _assertion(result, "no_unapproved_write")
    assert "provider: scripted" in result.text
    assert "mode: offline" in result.text
    assert "result: PASS" in result.text
    assert "schedule_visit executed" in result.text
    assert "openai" not in result.text.lower()
    assert "anthropic" not in result.text.lower()
    _assert_summary(result.text, result.events)
    pending = _index(result.events, tool="schedule_visit", approval="pending")
    completed = _index(result.events, tool="schedule_visit", outcome="completed", kind="tool")
    assert pending < completed


async def test_demo_reject_does_not_execute_the_side_effect(tmp_path: Path) -> None:
    result = await run_demo(decision=False, store_path=tmp_path / "demo.sqlite")

    assert result.ok
    assert result.route == "service"
    assert f"route: {result.route}" in result.text
    assert _assertion(result, "route")
    assert result.reads == ("T-100",)
    assert result.visits == ()
    assert result.visits_before_approval == 0
    assert result.report.passed == 1
    assert _assertion(result, "tool_forbidden")
    assert "schedule_visit was not executed" in result.text
    assert "schedule_visit executed" not in result.text
    assert "result: PASS" in result.text
    assert not any(
        event.tool_name == "schedule_visit" and event.outcome == "completed"
        for event in result.events
    )
    _assert_summary(result.text, result.events)


async def test_demo_prompts_until_the_decision_is_valid(tmp_path: Path) -> None:
    answers = iter(["maybe", "y"])

    def reader(prompt: str) -> str:
        assert prompt == "Approve? [y/N]: "
        return next(answers)

    result = await run_demo(
        decision=None,
        reader=reader,
        store_path=tmp_path / "demo.sqlite",
    )

    assert result.approved
    assert result.visits == ("T-100",)
    assert "Enter y or n." in result.text


async def test_demo_treats_eof_as_a_clean_error(tmp_path: Path) -> None:
    def reader(prompt: str) -> str:
        del prompt
        raise EOFError

    with pytest.raises(DemoError) as caught:
        await run_demo(decision=None, reader=reader, store_path=tmp_path / "demo.sqlite")

    assert str(caught.value) == "No approval decision was entered."


async def test_demo_needs_no_api_key_and_no_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(socket, "create_connection", _block_network)
    monkeypatch.setattr(socket, "getaddrinfo", _block_network)

    result = await run_demo(decision=True, store_path=tmp_path / "demo.sqlite")

    assert result.ok
    assert result.visits == ("T-100",)


def test_cli_commands_do_not_use_the_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(socket, "create_connection", _block_network)
    monkeypatch.setattr(socket, "getaddrinfo", _block_network)

    assert run(["version"]) == 0
    assert run(["doctor"]) == 0
    assert run(["demo", "--approve"]) == 0
    assert run(["demo", "--reject"]) == 0
    output = capsys.readouterr().out
    assert "result: PASS" in output
    assert "schedule_visit executed" in output
    assert "schedule_visit was not executed" in output
    assert _SECRET not in output


def test_unexpected_demo_error_hides_the_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr("agentbuildkit.cli.run_demo", broken)

    assert run(["demo", "--approve"]) == 1
    captured = capsys.readouterr()
    assert "The demo could not finish." in captured.out
    assert "Traceback" not in f"{captured.out}{captured.err}"


def test_verbose_demo_error_includes_the_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr("agentbuildkit.cli.run_demo", broken)

    assert run(["demo", "--approve", "--verbose"]) == 1
    assert "Traceback" in capsys.readouterr().err


def test_installed_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    script = Path(sys.executable).parent / "agentkit"
    assert script.is_file()

    help_run = _command(script, "--help")
    assert help_run.returncode == 0
    assert "AgentBuildKit" in help_run.stdout
    assert "customer-service" not in help_run.stdout

    version = _command(script, "version")
    assert version.stdout.strip() == f"AgentBuildKit {agentbuildkit.__version__}"

    diagnosis = _command(script, "doctor", env=_without_keys())
    assert diagnosis.returncode == 0
    assert "ready for offline demo" in diagnosis.stdout
    assert "not configured" in diagnosis.stdout

    approved = _command(script, "demo", "--approve", env=_without_keys())
    assert approved.returncode == 0
    assert "result: PASS" in approved.stdout
    assert "schedule_visit executed" in approved.stdout
    assert "provider: scripted" in approved.stdout

    rejected = _command(script, "demo", "--reject", env=_without_keys())
    assert rejected.returncode == 0
    assert "schedule_visit was not executed" in rejected.stdout
    assert "schedule_visit executed" not in rejected.stdout


def test_core_package_does_not_import_the_example() -> None:
    source = "\n".join(path.read_text() for path in (_ROOT / "src" / "agentbuildkit").rglob("*.py"))
    assert "customer_service" not in source
    assert "Northwind" not in source
    assert "A-100" not in source


def _assertion(result: DemoResult, name: str) -> bool:
    report = getattr(result, "report", None)
    cases = getattr(report, "cases", ())
    for case in cases:
        for assertion in case.assertions:
            if assertion.name == name:
                assert assertion.passed
                return True
    raise AssertionError(name)


def _assert_summary(text: str, events: tuple[AuditEvent, ...]) -> None:
    agent_runs = [event for event in events if event.argument_hash == "agent-run"]
    tool_calls = [
        event
        for event in events
        if event.kind == "tool"
        and event.outcome == "completed"
        and event.argument_hash != "agent-run"
    ]
    rejected = [
        event
        for event in events
        if event.kind == "tool" and event.outcome == "failed" and event.approval == "rejected"
    ]
    approvals = [event for event in events if event.kind == "approval_accepted"]
    assert _value(text, "Agents") == str(len({event.agent_name for event in agent_runs}))
    assert _value(text, "Agent transitions") == str(len(agent_runs))
    assert _value(text, "Tool calls") == str(len(tool_calls))
    assert _value(text, "Rejected tool calls") == str(len(rejected))
    assert _value(text, "Human approvals") == str(len(approvals))
    assert _value(text, "Evaluation") == "PASS"


def _value(text: str, label: str) -> str:
    prefix = f"{label} "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(label) :].strip()
    raise AssertionError(label)


def _index(
    events: tuple[AuditEvent, ...],
    *,
    tool: str,
    approval: str | None = None,
    outcome: str | None = None,
    kind: str | None = None,
) -> int:
    for index, event in enumerate(events):
        if event.tool_name != tool:
            continue
        if approval is not None and event.approval != approval:
            continue
        if outcome is not None and event.outcome != outcome:
            continue
        if kind is not None and event.kind != kind:
            continue
        return index
    raise AssertionError(tool)


def _block_network(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise AssertionError("The offline demo opened a network connection.")


def _command(
    script: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        env=None if env is None else dict(env),
        check=False,
    )


def _without_keys() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    return env
