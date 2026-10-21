"""External engineering execution.

The platform remains the orchestrator. A provider such as Cursor receives one
bounded implementation assignment and works asynchronously. Bindings store
identifiers and status, not transcripts.
"""

from agent_platform.external_execution.models import (
    IMPLEMENTATION_COMPLETION,
    IMPLEMENTATION_PROHIBITIONS,
    ExecutionPolicyRecord,
    ExternalExecutionRecord,
    ImplementationAssignment,
    render_assignment,
)
from agent_platform.external_execution.provider import (
    ExecutionConflict,
    ExecutionContactError,
    ExecutionError,
    ExecutionMalformed,
    ExecutionNotFound,
    ExecutionRejected,
    ExternalExecutionProvider,
    FakeExecutionProvider,
)
from agent_platform.external_execution.service import (
    TOOL_DELEGATE_IMPLEMENTATION,
    TOOL_FOLLOW_UP_IMPLEMENTATION,
    ExecutionService,
    delegation_arguments,
    follow_up_arguments,
    register_execution_tool_permissions,
)

__all__ = [
    "IMPLEMENTATION_COMPLETION",
    "IMPLEMENTATION_PROHIBITIONS",
    "TOOL_DELEGATE_IMPLEMENTATION",
    "TOOL_FOLLOW_UP_IMPLEMENTATION",
    "ExecutionConflict",
    "ExecutionContactError",
    "ExecutionError",
    "ExecutionMalformed",
    "ExecutionNotFound",
    "ExecutionPolicyRecord",
    "ExecutionRejected",
    "ExecutionService",
    "ExternalExecutionProvider",
    "ExternalExecutionRecord",
    "FakeExecutionProvider",
    "ImplementationAssignment",
    "delegation_arguments",
    "follow_up_arguments",
    "register_execution_tool_permissions",
    "render_assignment",
]
