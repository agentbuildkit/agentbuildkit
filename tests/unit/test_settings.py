"""Settings load from an env file and the process environment without a live model."""

import json
from pathlib import Path

import pytest
from agent_framework import SecretString

from agent_platform.config import load_app_settings, load_openai_settings, redacted_settings
from agent_platform.runtime import build_runtime

_OPENAI_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_MODEL",
    "OPENAI_EMBEDDING_MODEL",
    "OPENAI_CHAT_MODEL",
    "OPENAI_CHAT_COMPLETION_MODEL",
)
_APP_KEYS = (
    "APP_SERVICE_NAME",
    "APP_ENVIRONMENT",
    "APP_ENABLE_CONSOLE_EXPORTERS",
    "APP_OTLP_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _clear_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (*_OPENAI_KEYS, *_APP_KEYS):
        monkeypatch.delenv(key, raising=False)


def test_env_file_loads_app_and_openai_settings(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "APP_SERVICE_NAME=client-alpha",
                "APP_ENVIRONMENT=local",
                "APP_ENABLE_CONSOLE_EXPORTERS=false",
                "OPENAI_API_KEY=sk-test-secret",
                "OPENAI_CHAT_MODEL=gpt-4.1-mini",
            ]
        ),
        encoding="utf-8",
    )

    app_settings = load_app_settings(env_file_path=str(env_file))
    openai_settings = load_openai_settings(env_file_path=str(env_file))

    assert app_settings.get("service_name") == "client-alpha"
    assert app_settings.get("environment") == "local"
    assert app_settings.get("enable_console_exporters") is False
    assert openai_settings.get("chat_model") == "gpt-4.1-mini"
    assert isinstance(openai_settings.get("api_key"), SecretString)


def test_override_beats_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_SERVICE_NAME=from-file\n", encoding="utf-8")

    settings = load_app_settings(env_file_path=str(env_file), service_name="from-override")

    assert settings.get("service_name") == "from-override"


def test_process_environment_loads_when_no_file_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "ci")

    settings = load_app_settings()

    assert settings.get("environment") == "ci"
    assert settings.get("service_name") is None


def test_missing_env_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"

    with pytest.raises(FileNotFoundError):
        load_app_settings(env_file_path=str(missing))


def test_runtime_loads_without_an_api_key() -> None:
    runtime = build_runtime(configure_telemetry=False)

    assert runtime.openai_settings.get("api_key") is None
    assert runtime.audit_log.events == []


def test_secret_values_are_masked(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test-secret\n", encoding="utf-8")
    settings = load_openai_settings(env_file_path=str(env_file))
    secret = settings.get("api_key")
    assert isinstance(secret, SecretString)

    visible = redacted_settings(settings)
    rendered = json.dumps(visible)

    assert secret.get_secret_value() == "sk-test-secret"
    assert str(secret) == "**********"
    assert "sk-test-secret" not in str(visible)
    assert "sk-test-secret" not in rendered
    with pytest.raises(TypeError):
        json.dumps(dict(settings))
