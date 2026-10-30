"""Text rendering for the offline demo.

Lines are built from the run's models, audit events, and evaluation report.
"""

from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.observability import AuditEvent

_AGENT_RUN = "agent-run"
_LABELS = {
    "alex": "alex · orchestrator",
    "taylor": "taylor · service specialist",
    "jordan": "jordan · reviewer",
}


def agent_heading(event: AuditEvent) -> list[str]:
    """One agent step, using the provider and model recorded for that run."""
    name = event.agent_name or "agent"
    lines = [_LABELS.get(name, name)]
    if event.provider is not None:
        lines.append(f"    provider: {event.provider}")
    if event.model is not None:
        lines.append(f"    model: {event.model}")
    return lines


def summary(
    events: tuple[AuditEvent, ...],
    report: EvaluationReport,
    checks: tuple[tuple[str, bool], ...],
) -> list[str]:
    """Compact totals taken from the audit log and the evaluation report."""
    agent_runs = [event for event in events if event.argument_hash == _AGENT_RUN]
    names = {event.agent_name for event in agent_runs if event.agent_name is not None}
    tool_calls = [
        event
        for event in events
        if event.kind == "tool"
        and event.outcome == "completed"
        and event.argument_hash != _AGENT_RUN
    ]
    rejected = [
        event
        for event in events
        if event.kind == "tool" and event.outcome == "failed" and event.approval == "rejected"
    ]
    gates = [
        event
        for event in events
        if event.kind == "tool"
        and event.tool_name == "schedule_visit"
        and event.approval == "pending"
    ]
    approvals = [event for event in events if event.kind == "approval_accepted"]
    validations = [event for event in agent_runs if event.outcome == "completed"]
    evaluation = "PASS" if report.failed == 0 and report.total > 0 else "FAIL"
    rows = (
        ("Agents", str(len(names))),
        ("Agent transitions", str(len(agent_runs))),
        ("Tool calls", str(len(tool_calls))),
        ("Rejected tool calls", str(len(rejected))),
        ("Approval gates", str(len(gates))),
        ("Human approvals", str(len(approvals))),
        ("Schema validations", str(len(validations))),
        ("Evaluation", evaluation),
    )
    lines = ["", "────────────────────────────────────", "AgentBuildKit Run Complete", ""]
    lines.extend(f"{label:<24}{value}" for label, value in rows)
    lines.extend(["", "Harness checks exercised:"])
    lines.extend(f"{'✓' if passed else '✗'} {name}" for name, passed in checks)
    if report.failed:
        lines.append("")
        for case in report.cases:
            for assertion in case.assertions:
                if not assertion.passed:
                    lines.append(f"Evaluation failed: {assertion.name}")
    lines.append("────────────────────────────────────")
    return lines
