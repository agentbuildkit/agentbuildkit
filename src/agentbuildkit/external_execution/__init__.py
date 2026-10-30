"""External engineering execution.

The platform remains the orchestrator. A provider such as Cursor receives one
bounded implementation assignment and works asynchronously. Bindings store
identifiers and status, not transcripts.

The service is loaded on first use. Importing this package must not pull the
guardrails package while the state registry is still initialising.
"""

from typing import TYPE_CHECKING, Any

from agentbuildkit.external_execution.models import (
    IMPLEMENTATION_COMPLETION,
    IMPLEMENTATION_PROHIBITIONS,
    ExecutionPolicyRecord,
    ExternalExecutionRecord,
    ImplementationAssignment,
    ProviderBranch,
    ProviderObservation,
    render_assignment,
)
from agentbuildkit.external_execution.provider import (
    ExecutionConflict,
    ExecutionContactError,
    ExecutionError,
    ExecutionMalformed,
    ExecutionNotFound,
    ExecutionRejected,
    ExternalExecutionProvider,
    FakeExecutionProvider,
)

if TYPE_CHECKING:
    from agentbuildkit.external_execution.service import (
        TOOL_DELEGATE_IMPLEMENTATION as TOOL_DELEGATE_IMPLEMENTATION,
    )
    from agentbuildkit.external_execution.service import (
        TOOL_FOLLOW_UP_IMPLEMENTATION as TOOL_FOLLOW_UP_IMPLEMENTATION,
    )
    from agentbuildkit.external_execution.service import (
        ExecutionService as ExecutionService,
    )
    from agentbuildkit.external_execution.service import (
        delegation_arguments as delegation_arguments,
    )
    from agentbuildkit.external_execution.service import (
        follow_up_arguments as follow_up_arguments,
    )
    from agentbuildkit.external_execution.service import (
        register_execution_tool_permissions as register_execution_tool_permissions,
    )

_SERVICE_EXPORTS = frozenset(
    {
        "TOOL_DELEGATE_IMPLEMENTATION",
        "TOOL_FOLLOW_UP_IMPLEMENTATION",
        "ExecutionService",
        "delegation_arguments",
        "follow_up_arguments",
        "register_execution_tool_permissions",
    }
)


def __getattr__(name: str) -> Any:
    """Load the governed service without creating an import cycle."""
    if name in _SERVICE_EXPORTS:
        from agentbuildkit.external_execution import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    "ProviderBranch",
    "ProviderObservation",
    "delegation_arguments",
    "follow_up_arguments",
    "register_execution_tool_permissions",
    "render_assignment",
]
