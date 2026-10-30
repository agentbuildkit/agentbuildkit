"""Build an observation from the platform audit log.

Agent runs are the events whose argument hash is ``agent-run``. Other events
are tool and integration calls.
"""

from agentbuildkit.evaluation.models import CaseObservation
from agentbuildkit.observability import AuditLog

_AGENT_RUN = "agent-run"


def trace_from_audit(audit_log: AuditLog, *, correlation_id: str) -> CaseObservation:
    """Collect agents, tools, approvals, and correlation ids from one log."""
    agents: list[str] = []
    tools: list[str] = []
    rejected: list[str] = []
    approvals: list[str] = []
    unapproved: list[str] = []
    correlation_ids: list[str] = []
    for event in audit_log.events:
        correlation_ids.append(event.correlation_id)
        if event.kind != "tool":
            continue
        if event.argument_hash == _AGENT_RUN:
            if event.agent_name is not None:
                agents.append(event.agent_name)
            continue
        if event.outcome == "completed":
            tools.append(event.tool_name)
            if event.approval not in {"approved", "not_required"}:
                unapproved.append(event.tool_name)
        if event.outcome == "pending_approval" or event.approval == "pending":
            approvals.append(event.tool_name)
        if event.outcome == "failed" and event.approval == "rejected":
            rejected.append(event.tool_name)
    return CaseObservation(
        correlation_id=correlation_id,
        agents_invoked=tuple(agents),
        tools_called=tuple(tools),
        tools_rejected=tuple(rejected),
        approval_requested=tuple(approvals),
        unapproved_writes=tuple(unapproved),
        audit_correlation_ids=tuple(correlation_ids),
        tool_call_count=len(tools),
    )
