"""Load application and OpenAI settings without exposing secret values."""

from collections.abc import Mapping
from typing import Any, TypedDict

from agent_framework import SecretString, load_settings
from agent_framework.openai import OpenAISettings

_REDACTED = "**********"


class AppSettings(TypedDict, total=False):
    """Process settings loaded from the ``APP_`` prefix.

    These values are not secrets. Provider credentials stay on the OpenAI settings
    object and remain wrapped in ``SecretString``.
    """

    service_name: str | None
    environment: str | None
    enable_console_exporters: bool | None
    otlp_endpoint: str | None


def load_app_settings(
    *,
    env_file_path: str | None = None,
    **overrides: Any,
) -> AppSettings:
    """Load application settings from overrides, an env file, then the process environment."""
    return load_settings(
        AppSettings,
        env_prefix="APP_",
        env_file_path=env_file_path,
        **overrides,
    )


def load_openai_settings(
    *,
    env_file_path: str | None = None,
    **overrides: Any,
) -> OpenAISettings:
    """Load OpenAI settings. A missing API key stays unset so offline tests can run."""
    return load_settings(
        OpenAISettings,
        env_prefix="OPENAI_",
        env_file_path=env_file_path,
        **overrides,
    )


def redacted_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy safe to log. Secret values are replaced with a mask."""
    redacted: dict[str, Any] = {}
    for key, value in settings.items():
        redacted[str(key)] = _REDACTED if isinstance(value, SecretString) else value
    return redacted
