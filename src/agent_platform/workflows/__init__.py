"""Typed multi-agent workflows on Microsoft Agent Framework's graph engine."""

from agent_platform.workflows.patterns import (
    approval_workflow,
    concurrent_workflow,
    conditional_workflow,
    manager_workflow,
    sequential_workflow,
)
from agent_platform.workflows.runtime import (
    HumanApprovalRequest,
    HumanApprovalResponse,
    PendingHumanInput,
    RouteRejected,
    WorkflowEnvelope,
    WorkflowInputError,
    WorkflowRun,
    WorkflowRunError,
    envelope,
    run_workflow,
)

__all__ = [
    "HumanApprovalRequest",
    "HumanApprovalResponse",
    "PendingHumanInput",
    "RouteRejected",
    "WorkflowEnvelope",
    "WorkflowInputError",
    "WorkflowRun",
    "WorkflowRunError",
    "approval_workflow",
    "concurrent_workflow",
    "conditional_workflow",
    "envelope",
    "manager_workflow",
    "run_workflow",
    "sequential_workflow",
]
