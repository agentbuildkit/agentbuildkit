"""Packaging seams for the installed dashboard and drafting server."""

import builtins
import importlib
from pathlib import Path

import pytest
from agent_framework import SecretString

from agentbuildkit.cli import run
from agentbuildkit.hosting import templates_missing_message
from agentbuildkit.providers import ProviderError
from agentbuildkit.revisions.store import SqliteRevisionStore, StartupLockError
from agentbuildkit_templates.customer_service.dashboard import read_static
from agentbuildkit_templates.customer_service.team import customer_team_bundle
from agentbuildkit_templates.office.clients import (
    DeferredChatClient,
    DeferredGateway,
    dashboard_clients,
    optional_gateway,
)


def test_static_asset_comes_from_the_package() -> None:
    css = read_static("dashboard.css")
    font = read_static("fonts/geist-mono-latin-400-normal.woff2")

    assert css is not None and b"{" in css
    assert font is not None and font.startswith(b"wOF2")
    assert read_static("../team.json") is None
    assert read_static("missing.css") is None


def test_dashboard_clients_wait_for_a_live_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _Registry:
        def create_client(self, configuration: object) -> object:
            calls.append(str(configuration))
            raise ProviderError("missing")

    def registry(**_kwargs: object) -> _Registry:
        return _Registry()

    monkeypatch.setattr("agentbuildkit_templates.office.clients.builtin_registry", registry)
    clients = dashboard_clients(customer_team_bundle(), env_file_path=None)
    client = clients["alex"]

    assert isinstance(client, DeferredChatClient)
    assert calls == []
    with pytest.raises(ProviderError, match="openai is not configured"):
        client.get_response([])
    assert len(calls) == 1


def test_composio_gateway_waits_for_an_account_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def present(name: str) -> object:
        del name
        return object()

    monkeypatch.setattr(
        "agentbuildkit_templates.office.clients.importlib.util.find_spec",
        present,
    )

    def refused(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("The Composio client was constructed at startup.")

    monkeypatch.setattr("agentbuildkit_templates.office.clients.LiveComposioGateway", refused)
    gateway, missing = optional_gateway(SecretString("local-test-key"), "auth-config")

    assert missing is False
    assert isinstance(gateway, DeferredGateway)
    assert gateway.auth_config_id == "auth-config"
    with pytest.raises(AssertionError, match="constructed at startup"):
        gateway.get_account("account-1")


def test_missing_flock_is_reported_without_opening_the_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_import = builtins.__import__

    def blocked(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "fcntl":
            raise ImportError("no flock")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(StartupLockError, match="POSIX flock"):
        SqliteRevisionStore(tmp_path / "revisions.sqlite")
    assert list(tmp_path.iterdir()) == []


def test_missing_templates_package_does_not_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("agentbuildkit.cli.templates_available", lambda: False)
    state = tmp_path / "state"

    assert run(["dashboard", "--state-directory", str(state)]) == 1
    assert run(["mcp", "serve", "--state-directory", str(state.resolve())]) == 1

    captured = capsys.readouterr()
    assert templates_missing_message() in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert list(tmp_path.iterdir()) == []


def test_incompatible_harness_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    class HarnessIncompatible(RuntimeError):
        pass

    def reject(name: str) -> object:
        raise HarnessIncompatible("agentbuildkit-templates cannot use this harness.")

    monkeypatch.setattr("agentbuildkit.cli.templates_available", lambda: True)
    monkeypatch.setattr("agentbuildkit.cli.importlib.import_module", reject)

    assert run(["dashboard", "--state-directory", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "cannot use this harness" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_missing_capability_names_the_release_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentbuildkit_templates import compat

    real_import = importlib.import_module

    def missing(name: str) -> object:
        if name == "agentbuildkit.revisions":
            raise ImportError(name)
        return real_import(name)

    monkeypatch.setattr(importlib, "import_module", missing)

    with pytest.raises(compat.HarnessIncompatible) as caught:
        compat.require_harness()

    message = str(caught.value)
    assert "agentbuildkit.revisions.RevisionService" in message
    assert "published agentbuildkit 0.1.0" in message
    assert "first harness release" in message
    assert compat.HARNESS_RELEASE_BOUND == ""
