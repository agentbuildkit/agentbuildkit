"""Read-only view of the dashboard's recorded state.

Nothing here runs, approves, or retries an action. It groups what the service,
the workflow store, the recovery file, and the audit log already recorded, and
names each state for a person. Missing facts stay missing.
"""

from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from agentbuildkit.observability import AuditEvent
from agentbuildkit.workflows import LedgerRecord, PendingRequest
from customer_service.models import FinalOutcome
from customer_service.service import (
    DECLINED_ACTION_RESPONSE,
    REVIEW_REJECTED_RESPONSE,
    UNSPECIFIED_ACTION_RESPONSE,
)

ALREADY_HANDLED = "The service action was already handled."
UNRESOLVED_RESPONSE = "The service action is unresolved and will not be repeated."
MISSING_ARGUMENTS = "The service action cannot be resumed because its arguments were not recovered."
PENDING_RESPONSE = "The service action is waiting for approval."
COMPLETED_RESPONSE = "The service action was completed."
FAILED_RESPONSE = "The service action ended with an error. It will not be repeated."

State = Literal[
    "waiting",
    "completed",
    "declined",
    "review_rejected",
    "rejected",
    "failed",
    "escalated",
    "unresolved",
    "not_routed",
    "not_run",
]
Tone = Literal["wait", "done", "fail", "neutral", "unres"]
Category = Literal["agent", "tool", "approval", "effect", "workflow"]


@dataclass(frozen=True)
class StateStyle:
    """How a state is named and shown. Colour is never the only signal."""

    label: str
    tone: Tone
    icon: str
    text: str


STATES: dict[State, StateStyle] = {
    "waiting": StateStyle("Waiting for approval", "wait", "clock", PENDING_RESPONSE),
    "completed": StateStyle("Completed", "done", "check", COMPLETED_RESPONSE),
    "declined": StateStyle("Declined", "neutral", "x", DECLINED_ACTION_RESPONSE),
    "review_rejected": StateStyle("Rejected in review", "neutral", "x", REVIEW_REJECTED_RESPONSE),
    "rejected": StateStyle("Rejected", "neutral", "x", "The request was not completed."),
    "failed": StateStyle("Failed", "fail", "alert", FAILED_RESPONSE),
    "escalated": StateStyle("Escalated", "neutral", "person", "The request needs a person."),
    "unresolved": StateStyle("Unresolved", "unres", "help", UNRESOLVED_RESPONSE),
    "not_routed": StateStyle("Not routed", "neutral", "slash", "The request could not be routed."),
    "not_run": StateStyle("Not run", "fail", "alert", MISSING_ARGUMENTS),
}


@dataclass(frozen=True)
class Pause:
    """A stored approval, its effect status, and what recovery kept for it."""

    request: PendingRequest
    effect: str | None
    reference: str
    arguments: dict[str, str] | None

    @property
    def action(self) -> str | None:
        if self.arguments is None:
            return None
        return self.arguments.get("action") or None


@dataclass(frozen=True)
class Run:
    """Every recorded outcome for one correlation ID, newest last."""

    correlation_id: str
    reference: str
    specialist: str
    state: State
    response: str
    history: tuple[FinalOutcome, ...]
    pause: Pause | None


def outcome_state(outcome: FinalOutcome) -> State:
    """Name one recorded outcome from its status and response."""
    if outcome.status == "completed":
        return "completed"
    if outcome.status == "pending_approval":
        return "waiting"
    if outcome.status == "escalated":
        return "escalated"
    if outcome.status == "route_rejected":
        return "not_routed"
    response = outcome.response
    if response == UNRESOLVED_RESPONSE:
        return "unresolved"
    if response == MISSING_ARGUMENTS:
        return "not_run"
    if response in {DECLINED_ACTION_RESPONSE, ALREADY_HANDLED}:
        return "declined"
    if response in {REVIEW_REJECTED_RESPONSE, UNSPECIFIED_ACTION_RESPONSE}:
        return "review_rejected"
    return "rejected"


def pause_state(pause: Pause) -> State:
    """Name a stored approval from the store, which outlives this process."""
    status = pause.request.status
    effect = pause.effect
    if status == "completed" or effect == "completed":
        return "completed"
    if status == "rejected" or effect == "rejected":
        return "declined"
    if effect == "failed":
        return "failed"
    if effect == "started" or status == "failed":
        return "unresolved"
    return "waiting"


def build_runs(
    outcomes: Sequence[FinalOutcome],
    pauses: dict[str, Pause],
) -> tuple[Run, ...]:
    """Group outcomes by correlation ID. The most recently updated run comes first."""
    groups: dict[str, list[FinalOutcome]] = {}
    last: dict[str, int] = {}
    for index, outcome in enumerate(outcomes):
        groups.setdefault(outcome.correlation_id, []).append(outcome)
        last[outcome.correlation_id] = index
    runs: list[Run] = []
    for correlation_id in sorted(groups, key=lambda item: last[item], reverse=True):
        history = tuple(groups[correlation_id])
        latest = history[-1]
        pause = pauses.get(correlation_id)
        state = pause_state(pause) if pause is not None else outcome_state(latest)
        response = latest.response if outcome_state(latest) == state else STATES[state].text
        reference = next((item.reference for item in reversed(history) if item.reference), "")
        if not reference and pause is not None:
            reference = pause.reference
        specialist = next((item.specialist for item in reversed(history) if item.specialist), "")
        runs.append(
            Run(
                correlation_id=correlation_id,
                reference=reference,
                specialist=specialist,
                state=state,
                response=response,
                history=history,
                pause=pause,
            )
        )
    return tuple(runs)


@dataclass(frozen=True)
class AuditItem:
    """One audit event. Argument values are never part of it."""

    correlation_id: str
    agent: str | None
    tool: str
    kind: str
    approval: str
    outcome: str
    argument_hash: str
    provider: str | None
    model: str | None
    recorded_at: str | None
    event_id: str | None
    run_id: str | None
    durable: bool

    @property
    def is_agent_run(self) -> bool:
        return self.kind == "tool" and self.argument_hash == "agent-run"

    @property
    def category(self) -> Category:
        if self.kind == "tool":
            return "agent" if self.is_agent_run else "tool"
        if self.kind in {"approval_accepted", "approval_rejected", "workflow_paused"}:
            return "approval"
        if self.kind.startswith("side_effect_"):
            return "effect"
        return "workflow"

    @property
    def label(self) -> str:
        if self.kind == "tool":
            if self.is_agent_run:
                return "Agent run"
            if self.approval == "rejected":
                return "Tool call refused"
            return "Tool call" if self.agent else "API call"
        return _KIND_LABELS.get(self.kind, self.kind)

    @property
    def outcome_label(self) -> str:
        return _OUTCOME_LABELS.get(self.outcome, self.outcome)


_KIND_LABELS = {
    "checkpoint_created": "Checkpoint saved",
    "workflow_paused": "Paused for approval",
    "workflow_resumed": "Workflow resumed",
    "approval_accepted": "Approval accepted",
    "approval_rejected": "Approval rejected",
    "side_effect_attempted": "Side effect started",
    "side_effect_completed": "Side effect completed",
    "side_effect_failed": "Side effect failed",
    "grant_used": "Grant used",
    "child_outcome": "Child workflow outcome",
}
_OUTCOME_LABELS = {
    "completed": "Completed",
    "pending_approval": "Pending approval",
    "rejected": "Rejected",
    "failed": "Failed",
}

_Key = tuple[str, str | None, str, str, str, str, str, str | None, str | None]


def _key(event: AuditEvent | LedgerRecord) -> _Key:
    return (
        event.correlation_id,
        event.agent_name,
        event.tool_name,
        event.kind,
        event.approval,
        event.outcome,
        event.argument_hash,
        event.provider,
        event.model,
    )


def _from_ledger(record: LedgerRecord) -> AuditItem:
    return AuditItem(
        correlation_id=record.correlation_id,
        agent=record.agent_name,
        tool=record.tool_name,
        kind=record.kind,
        approval=record.approval,
        outcome=record.outcome,
        argument_hash=record.argument_hash,
        provider=record.provider,
        model=record.model,
        recorded_at=record.recorded_at,
        event_id=record.event_id,
        run_id=record.run_id,
        durable=True,
    )


def _from_event(event: AuditEvent) -> AuditItem:
    return AuditItem(
        correlation_id=event.correlation_id,
        agent=event.agent_name,
        tool=event.tool_name,
        kind=event.kind,
        approval=event.approval,
        outcome=event.outcome,
        argument_hash=event.argument_hash,
        provider=event.provider,
        model=event.model,
        recorded_at=None,
        event_id=None,
        run_id=None,
        durable=False,
    )


def merge_audit(
    ledger: Sequence[LedgerRecord],
    session: Sequence[AuditEvent],
) -> tuple[AuditItem, ...]:
    """Combine the durable ledger with this process's events, in recorded order.

    A session event that was also written to the ledger appears once, with the
    ledger's time and ID. Repeated identical events stay repeated. Ledger rows
    from earlier processes come first.
    """
    wanted = Counter(_key(event) for event in session)
    positions: dict[_Key, list[int]] = {}
    for index, record in enumerate(ledger):
        positions.setdefault(_key(record), []).append(index)
    queues: dict[_Key, deque[int]] = {}
    matched: set[int] = set()
    for key, indexes in positions.items():
        count = min(wanted.get(key, 0), len(indexes))
        if count:
            taken = indexes[-count:]
            queues[key] = deque(taken)
            matched.update(taken)
    items = [_from_ledger(record) for index, record in enumerate(ledger) if index not in matched]
    for event in session:
        queue = queues.get(_key(event))
        if queue:
            items.append(_from_ledger(ledger[queue.popleft()]))
        else:
            items.append(_from_event(event))
    return tuple(items)


def clock(value: str | None) -> str | None:
    """Format a stored ISO time as HH:MM:SS in UTC. None stays None."""
    parsed = _parse(value)
    return None if parsed is None else parsed.strftime("%H:%M:%S")


def day_and_time(value: str | None) -> str | None:
    """Format a stored ISO time as '12 Nov 2026, 19:14:07'."""
    parsed = _parse(value)
    return None if parsed is None else parsed.strftime("%d %b %Y, %H:%M:%S").lstrip("0")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
