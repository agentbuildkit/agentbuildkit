"""Load application and OpenAI settings without exposing secret values."""

from collections.abc import Mapping
from typing import Any, TypedDict

from agent_framework import SecretString, load_settings
from agent_framework.openai import OpenAISettings

_REDACTED = "**********"


class CursorSettings(TypedDict, total=False):
    """Cursor Cloud Agent settings loaded from the ``CURSOR_`` prefix.

    ``api_key`` stays a ``SecretString``. Callers unwrap it only when building
    the authorization header.
    """

    api_key: SecretString | None


class GithubSettings(TypedDict, total=False):
    """GitHub App settings loaded from the ``GITHUB_`` prefix.

    ``private_key`` stays a ``SecretString``. Callers unwrap it only when signing
    an installation token.
    """

    app_id: str | None
    installation_id: str | None
    private_key: SecretString | None


class AppSettings(TypedDict, total=False):
    """Process settings loaded from the ``APP_`` prefix.

    These values are not secrets. Provider credentials are not loaded here.
    """

    service_name: str | None
    environment: str | None
    enable_console_exporters: bool | None
    otlp_endpoint: str | None
    state_directory: str | None


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


def load_cursor_settings(
    *,
    env_file_path: str | None = None,
    **overrides: Any,
) -> CursorSettings:
    """Load Cursor API settings. The API key remains a ``SecretString``."""
    return load_settings(
        CursorSettings,
        env_prefix="CURSOR_",
        env_file_path=env_file_path,
        **overrides,
    )


def load_github_settings(
    *,
    env_file_path: str | None = None,
    **overrides: Any,
) -> GithubSettings:
    """Load GitHub App settings. The private key remains a ``SecretString``."""
    return load_settings(
        GithubSettings,
        env_prefix="GITHUB_",
        env_file_path=env_file_path,
        **overrides,
    )


def load_openai_settings(
    *,
    env_file_path: str | None = None,
    **overrides: Any,
) -> OpenAISettings:
    """Load OpenAI provider settings.

    A missing API key stays unset so offline tests can run. These settings
    belong to the OpenAI provider. The rest of the harness does not require them.
    """
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
