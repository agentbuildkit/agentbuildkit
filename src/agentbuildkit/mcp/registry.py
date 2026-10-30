"""Allowlisted MCP servers.

Server identity, transport, command, URL, and credentials come from
configuration. A model cannot supply or replace them.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any, cast
from urllib.parse import urlsplit

from agent_framework import Content, MCPStdioTool, MCPStreamableHTTPTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentbuildkit.guardrails import (
    ApprovalDecision,
    ToolEffect,
    execute_tool,
    prepare_tool,
)
from agentbuildkit.observability import AuditEvent, AuditLog, argument_hash, current_correlation_id

_RESERVED_ARGUMENT_NAMES = frozenset(
    {
        "url",
        "command",
        "args",
        "env",
        "headers",
        "authorization",
        "endpoint",
    }
)
_SHELL_MARKERS = ("\n", "\r", ";", "|", "&", "`", "$(")


class McpTransport(StrEnum):
    """Stable transports used by this platform."""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class McpPolicyError(Exception):
    """The MCP server or tool is not on the configured allowlist."""

    def __init__(self, message: str = "MCP server or tool is not allowlisted.") -> None:
        super().__init__(message)


class McpApprovalRequired(Exception):
    """A side-effecting MCP call is waiting for approval."""

    def __init__(self) -> None:
        super().__init__("MCP tool call is waiting for approval.")


class McpRejected(Exception):
    """A person rejected the MCP call."""

    def __init__(self) -> None:
        super().__init__("MCP tool call was rejected.")


class McpCapability(BaseModel):
    """One remote tool the organisation has chosen to expose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    effect: ToolEffect


class McpServerConfig(BaseModel):
    """The fixed connection details for one MCP server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    transport: McpTransport
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    capabilities: tuple[McpCapability, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_transport(self) -> "McpServerConfig":
        names = [capability.name for capability in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("MCP capabilities need unique names.")
        if self.transport is McpTransport.STDIO:
            if not self.command or self.url is not None:
                raise ValueError("A stdio MCP server needs a command and no URL.")
            _check_command(self.command)
            for arg in self.args:
                _check_command(arg)
            return self
        if not self.url or self.command is not None or self.args:
            raise ValueError("An HTTP MCP server needs a URL and no command.")
        _check_url(self.url)
        return self


class McpRegistry:
    """The servers a deployment is allowed to use."""

    def __init__(self) -> None:
        self._servers: dict[str, _Registration] = {}

    def register(
        self,
        config: McpServerConfig,
        *,
        header_provider: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        """Add one server. Later calls cannot replace its connection details."""
        if config.name in self._servers:
            raise McpPolicyError("MCP server is already registered.")
        self._servers[config.name] = _Registration(config, header_provider)

    def require(self, name: str) -> McpServerConfig:
        """Return the configured server, or reject an unknown name."""
        registration = self._servers.get(name)
        if registration is None:
            raise McpPolicyError("MCP server is not allowlisted.")
        return registration.config

    def headers(self, name: str, runtime: Mapping[str, Any] | None = None) -> dict[str, str]:
        """Return configured headers. Values in runtime are ignored."""
        del runtime
        registration = self._servers.get(name)
        if registration is None:
            raise McpPolicyError("MCP server is not allowlisted.")
        if registration.header_provider is None:
            return {}
        return {key: value for key, value in registration.header_provider().items()}

    def connect(self, name: str) -> MCPStdioTool | MCPStreamableHTTPTool:
        """Build the framework MCP tool from the stored configuration."""
        registration = self._servers.get(name)
        if registration is None:
            raise McpPolicyError("MCP server is not allowlisted.")
        config = registration.config
        allowed = [capability.name for capability in config.capabilities]
        approval_mode = _approval_mode(config)
        if config.transport is McpTransport.STDIO:
            return MCPStdioTool(
                name=config.name,
                command=config.command or "",
                args=list(config.args),
                allowed_tools=allowed,
                approval_mode=approval_mode,
                load_prompts=False,
                description=config.name,
            )

        def provide_headers(runtime: dict[str, Any]) -> dict[str, str]:
            return self.headers(name, runtime)

        return MCPStreamableHTTPTool(
            name=config.name,
            url=config.url or "",
            allowed_tools=allowed,
            approval_mode=approval_mode,
            load_prompts=False,
            description=config.name,
            header_provider=provide_headers,
        )

    async def call_tool(
        self,
        server: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        audit_log: AuditLog,
        caller: Callable[[str, Mapping[str, Any]], Awaitable[str]],
        correlation_id: str | None = None,
        decision: ApprovalDecision | None = None,
    ) -> str:
        """Call one allowlisted capability. Unknown tools never reach the caller."""
        config = self.require(server)
        capability = next(
            (item for item in config.capabilities if item.name == tool_name),
            None,
        )
        resolved = correlation_id or current_correlation_id()
        if resolved is None:
            raise McpPolicyError("MCP tool call has no correlation id.")
        safe_arguments = {
            key: value for key, value in arguments.items() if key not in _RESERVED_ARGUMENT_NAMES
        }
        if capability is None:
            audit_log.record(
                AuditEvent(
                    correlation_id=resolved,
                    agent_name=server,
                    tool_name=tool_name,
                    approval="rejected",
                    outcome="failed",
                    argument_hash=argument_hash(safe_arguments),
                )
            )
            raise McpPolicyError("MCP tool is not allowlisted.")

        async def perform(payload: str) -> str:
            parsed = _json_object(json.loads(payload))
            if parsed is None:
                raise McpPolicyError("MCP tool arguments must be an object.")
            return await caller(tool_name, parsed)

        tool = prepare_tool(
            perform,
            effect=capability.effect,
            name=capability.name,
            description=capability.name,
        )
        result = await execute_tool(
            tool,
            {"payload": json.dumps(safe_arguments, sort_keys=True)},
            audit_log=audit_log,
            correlation_id=resolved,
            agent_name=server,
            decision=decision,
        )
        if isinstance(result, Content) and result.type == "function_approval_response":
            raise McpRejected()
        if isinstance(result, Content):
            raise McpApprovalRequired()
        if isinstance(result, list) and result and isinstance(result[0], Content):
            text = result[0].text
            if isinstance(text, str):
                return text
        raise McpPolicyError("MCP tool did not return text.")


class _Registration:
    def __init__(
        self,
        config: McpServerConfig,
        header_provider: Callable[[], Mapping[str, str]] | None,
    ) -> None:
        self.config = config
        self.header_provider = header_provider


def _approval_mode(config: McpServerConfig) -> Any:
    always_require: list[str] = []
    never_require: list[str] = []
    for capability in config.capabilities:
        if capability.effect is ToolEffect.SIDE_EFFECT:
            always_require.append(capability.name)
        else:
            never_require.append(capability.name)
    return {
        "always_require_approval": always_require,
        "never_require_approval": never_require,
    }


def _json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, Any] = {}
    for key, item in cast(Mapping[Any, Any], value).items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed


def _check_command(value: str) -> None:
    if not value or any(marker in value for marker in _SHELL_MARKERS):
        raise ValueError("MCP command arguments must be literal tokens.")


def _check_url(url: str) -> None:
    parsed = urlsplit(url)
    has_credentials = parsed.username is not None or parsed.password is not None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or has_credentials:
        raise ValueError("MCP URL must be an http(s) URL without embedded credentials.")
