"""Application composition root.

Builds application settings, an audit log, and optional OpenTelemetry setup.
It does not create agents or chat clients, and it does not load a provider key.
"""

from collections.abc import Callable
from dataclasses import dataclass

from agentbuildkit.config.settings import AppSettings, load_app_settings
from agentbuildkit.observability.audit import AuditLog
from agentbuildkit.observability.telemetry import configure_observability


@dataclass(frozen=True)
class Runtime:
    """Loaded application settings and the process audit log."""

    app_settings: AppSettings
    audit_log: AuditLog


def build_runtime(
    *,
    env_file_path: str | None = None,
    configure_telemetry: bool = True,
    telemetry_configurator: Callable[..., None] | None = None,
) -> Runtime:
    """Load application settings and, by default, configure OpenTelemetry once."""
    app_settings = load_app_settings(env_file_path=env_file_path)
    if configure_telemetry:
        if telemetry_configurator is None:
            configure_observability(app_settings)
        else:
            configure_observability(app_settings, configurator=telemetry_configurator)
    return Runtime(
        app_settings=app_settings,
        audit_log=AuditLog(),
    )
