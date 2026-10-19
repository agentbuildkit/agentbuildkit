"""Application composition root.

Builds settings, an audit log, and optional OpenTelemetry setup. It does not
create agents.
"""

from collections.abc import Callable
from dataclasses import dataclass

from agent_framework.openai import OpenAISettings

from agent_platform.config.settings import AppSettings, load_app_settings, load_openai_settings
from agent_platform.observability.audit import AuditLog
from agent_platform.observability.telemetry import configure_observability


@dataclass(frozen=True)
class Runtime:
    """Loaded settings and the process audit log."""

    app_settings: AppSettings
    openai_settings: OpenAISettings
    audit_log: AuditLog


def build_runtime(
    *,
    env_file_path: str | None = None,
    configure_telemetry: bool = True,
    telemetry_configurator: Callable[..., None] | None = None,
) -> Runtime:
    """Load settings and, by default, configure OpenTelemetry once."""
    app_settings = load_app_settings(env_file_path=env_file_path)
    openai_settings = load_openai_settings(env_file_path=env_file_path)
    if configure_telemetry:
        if telemetry_configurator is None:
            configure_observability(app_settings)
        else:
            configure_observability(app_settings, configurator=telemetry_configurator)
    return Runtime(
        app_settings=app_settings,
        openai_settings=openai_settings,
        audit_log=AuditLog(),
    )
