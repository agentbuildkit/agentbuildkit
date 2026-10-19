"""OpenTelemetry setup for the application process.

Call this once at startup. Sensitive prompt and tool payloads stay disabled unless
the caller passes ``enable_sensitive_data=True``.
"""

from collections.abc import Callable
from typing import Any

from agent_framework.observability import configure_otel_providers

from agent_platform.config.settings import AppSettings

_DEFAULT_SERVICE_NAME = "agent-platform"


def configure_observability(
    settings: AppSettings | None = None,
    *,
    service_name: str | None = None,
    environment: str | None = None,
    enable_console_exporters: bool | None = None,
    otlp_endpoint: str | None = None,
    enable_sensitive_data: bool = False,
    configurator: Callable[..., None] = configure_otel_providers,
) -> None:
    """Configure OpenTelemetry providers from application settings."""
    resolved = settings or AppSettings()
    name = service_name or resolved.get("service_name") or _DEFAULT_SERVICE_NAME
    resolved_environment = environment if environment is not None else resolved.get("environment")
    console = (
        enable_console_exporters
        if enable_console_exporters is not None
        else bool(resolved.get("enable_console_exporters"))
    )
    endpoint = otlp_endpoint if otlp_endpoint is not None else resolved.get("otlp_endpoint")
    attributes: dict[str, Any] = {}
    if resolved_environment:
        attributes["deployment.environment.name"] = resolved_environment

    options: dict[str, Any] = {
        "service_name": name,
        "enable_console_exporters": console,
        "enable_sensitive_data": enable_sensitive_data,
    }
    if attributes:
        options["resource_attributes"] = attributes
    if endpoint:
        options["otlp_endpoint"] = endpoint
    configurator(**options)
