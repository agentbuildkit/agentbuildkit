"""Telemetry configuration stays non-sensitive by default."""

from typing import Any

import pytest

from agentbuildkit.config import AppSettings
from agentbuildkit.observability import configure_observability
from agentbuildkit.runtime import build_runtime


def test_observability_defaults_keep_sensitive_data_off() -> None:
    captured: dict[str, Any] = {}

    def configurator(**options: Any) -> None:
        captured.update(options)

    configure_observability(
        AppSettings(service_name="billing", environment="local", enable_console_exporters=False),
        configurator=configurator,
    )

    assert captured["service_name"] == "billing"
    assert captured["enable_sensitive_data"] is False
    assert captured["enable_console_exporters"] is False
    assert captured["resource_attributes"] == {"deployment.environment.name": "local"}
    assert "otlp_endpoint" not in captured


def test_runtime_passes_loaded_settings_to_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SERVICE_NAME", "field-team")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    captured: dict[str, Any] = {}

    def configurator(**options: Any) -> None:
        captured.update(options)

    build_runtime(configure_telemetry=True, telemetry_configurator=configurator)

    assert captured["service_name"] == "field-team"
    assert captured["enable_sensitive_data"] is False
    assert captured["resource_attributes"]["deployment.environment.name"] == "test"


def test_runtime_can_skip_telemetry_setup() -> None:
    called = False

    def configurator(**options: Any) -> None:
        del options
        nonlocal called
        called = True

    build_runtime(configure_telemetry=False, telemetry_configurator=configurator)

    assert called is False
