"""Allowlisted MCP server registration.

WebSocket MCP is available in the framework and is not exposed here. Stdio
and streamable HTTP cover local and remote servers.
"""

from agent_platform.mcp.registry import (
    McpApprovalRequired,
    McpCapability,
    McpPolicyError,
    McpRegistry,
    McpRejected,
    McpServerConfig,
    McpTransport,
)

__all__ = [
    "McpApprovalRequired",
    "McpCapability",
    "McpPolicyError",
    "McpRegistry",
    "McpRejected",
    "McpServerConfig",
    "McpTransport",
]
