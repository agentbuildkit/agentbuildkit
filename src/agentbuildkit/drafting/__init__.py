"""Local MCP interface for inactive team drafts.

This package serves drafting tools. It is not the allowlist an agent uses to
call a remote MCP server.
"""

from agentbuildkit.drafting.server import build_server, quiet_diagnostics, require_state_directory

__all__ = ["build_server", "quiet_diagnostics", "require_state_directory"]
