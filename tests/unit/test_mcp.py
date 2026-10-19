"""Offline MCP allowlist checks. No MCP server process is started."""

from collections.abc import Mapping
from typing import Any

import pytest
from agent_framework import MCPStdioTool, MCPStreamableHTTPTool
from pydantic import ValidationError

from agent_platform.guardrails import ApprovalDecision, ToolEffect
from agent_platform.mcp import (
    McpApprovalRequired,
    McpCapability,
    McpPolicyError,
    McpRegistry,
    McpRejected,
    McpServerConfig,
    McpTransport,
)
from agent_platform.observability import AuditLog

SECRET = "sk-live-mcp-secret"


def _stdio_config() -> McpServerConfig:
    return McpServerConfig(
        name="local-docs",
        transport=McpTransport.STDIO,
        command="/usr/bin/docs-server",
        args=("--stdio",),
        capabilities=(
            McpCapability(name="read_note", effect=ToolEffect.READ),
            McpCapability(name="publish_note", effect=ToolEffect.SIDE_EFFECT),
        ),
    )


def _http_config() -> McpServerConfig:
    return McpServerConfig(
        name="remote-docs",
        transport=McpTransport.STREAMABLE_HTTP,
        url="https://mcp.records.test/mcp",
        capabilities=(McpCapability(name="read_note", effect=ToolEffect.READ),),
    )


def test_allowlisted_server_is_accepted_and_unknown_server_is_rejected() -> None:
    registry = McpRegistry()
    registry.register(_stdio_config())

    assert registry.require("local-docs").command == "/usr/bin/docs-server"
    with pytest.raises(McpPolicyError) as caught:
        registry.require("other-server")
    assert "other-server" not in str(caught.value)


def test_server_connection_details_are_fixed_on_the_framework_tool() -> None:
    registry = McpRegistry()
    registry.register(_stdio_config())
    registry.register(_http_config(), header_provider=lambda: {"Authorization": f"Bearer {SECRET}"})

    stdio_tool = registry.connect("local-docs")
    http_tool = registry.connect("remote-docs")
    assert isinstance(stdio_tool, MCPStdioTool)
    assert isinstance(http_tool, MCPStreamableHTTPTool)

    assert stdio_tool.command == "/usr/bin/docs-server"
    assert stdio_tool.args == ["--stdio"]
    assert list(stdio_tool.allowed_tools or []) == ["read_note", "publish_note"]
    assert http_tool.url == "https://mcp.records.test/mcp"
    headers = registry.headers(
        "remote-docs",
        {"url": "https://evil.test/mcp", "command": "rm", "Authorization": "Bearer stolen"},
    )
    assert headers == {"Authorization": f"Bearer {SECRET}"}
    assert "evil.test" not in headers.values()


def test_registered_config_cannot_be_reassigned() -> None:
    registry = McpRegistry()
    config = _http_config()
    registry.register(config)

    with pytest.raises(ValidationError):
        config.url = "https://evil.test/mcp"  # type: ignore[misc]
    assert registry.require("remote-docs").url == "https://mcp.records.test/mcp"
    with pytest.raises(McpPolicyError):
        registry.register(config)


async def test_allowlisted_tool_is_called_and_unknown_tool_is_not() -> None:
    registry = McpRegistry()
    registry.register(_stdio_config())
    audit_log = AuditLog()
    seen: list[tuple[str, Mapping[str, Any]]] = []

    async def caller(tool_name: str, arguments: Mapping[str, Any]) -> str:
        seen.append((tool_name, arguments))
        return "note"

    result = await registry.call_tool(
        "local-docs",
        "read_note",
        {"reference": "A-1", "url": "https://evil.test", "command": "rm", "authorization": SECRET},
        audit_log=audit_log,
        caller=caller,
        correlation_id="corr-mcp",
    )

    assert result == "note"
    assert seen == [("read_note", {"reference": "A-1"})]
    assert audit_log.events[0].correlation_id == "corr-mcp"
    assert SECRET not in audit_log.events[0].model_dump_json()

    with pytest.raises(McpPolicyError):
        await registry.call_tool(
            "local-docs",
            "delete_everything",
            {"reference": "A-1"},
            audit_log=audit_log,
            caller=caller,
            correlation_id="corr-mcp",
        )
    assert seen == [("read_note", {"reference": "A-1"})]


async def test_mcp_side_effect_follows_the_approval_policy() -> None:
    registry = McpRegistry()
    registry.register(_stdio_config())
    audit_log = AuditLog()
    calls: list[str] = []

    async def caller(tool_name: str, arguments: Mapping[str, Any]) -> str:
        del arguments
        calls.append(tool_name)
        return "published"

    with pytest.raises(McpApprovalRequired):
        await registry.call_tool(
            "local-docs",
            "publish_note",
            {"reference": "A-1"},
            audit_log=audit_log,
            caller=caller,
            correlation_id="corr-side",
            decision=None,
        )
    assert calls == []

    with pytest.raises(McpRejected):
        await registry.call_tool(
            "local-docs",
            "publish_note",
            {"reference": "A-1"},
            audit_log=audit_log,
            caller=caller,
            correlation_id="corr-side",
            decision=ApprovalDecision("publish_note", approved=False),
        )
    assert calls == []

    result = await registry.call_tool(
        "local-docs",
        "publish_note",
        {"reference": "A-1"},
        audit_log=audit_log,
        caller=caller,
        correlation_id="corr-side",
        decision=ApprovalDecision("publish_note", approved=True),
    )

    assert result == "published"
    assert calls == ["publish_note"]


def test_stdio_and_http_configs_reject_mixed_connection_details() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig(
            name="broken",
            transport=McpTransport.STDIO,
            command="bash",
            args=("-c", "echo hi"),
            url="https://mcp.records.test/mcp",
            capabilities=(McpCapability(name="read_note", effect=ToolEffect.READ),),
        )
    with pytest.raises(ValidationError):
        McpServerConfig(
            name="embedded",
            transport=McpTransport.STREAMABLE_HTTP,
            url="https://user:secret@mcp.records.test/mcp",
            capabilities=(McpCapability(name="read_note", effect=ToolEffect.READ),),
        )
    with pytest.raises(ValidationError):
        McpServerConfig(
            name="shell",
            transport=McpTransport.STDIO,
            command="docs-server",
            args=("ok;rm",),
            capabilities=(McpCapability(name="read_note", effect=ToolEffect.READ),),
        )
