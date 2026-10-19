"""Typed orchestration patterns.

These graphs are built with WorkflowBuilder. The conversation-oriented
SequentialBuilder, ConcurrentBuilder, HandoffBuilder, GroupChatBuilder, and
MagenticBuilder pass message transcripts and can let a model choose the next
participant. The typed helpers here keep routing in Python and keep each agent
on the tools from its definition.
"""

from agent_platform.workflows import (
    approval_workflow,
    concurrent_workflow,
    conditional_workflow,
    manager_workflow,
    sequential_workflow,
)

__all__ = [
    "approval_workflow",
    "concurrent_workflow",
    "conditional_workflow",
    "manager_workflow",
    "sequential_workflow",
]
