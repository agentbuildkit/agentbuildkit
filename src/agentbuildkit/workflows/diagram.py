"""Configured route diagrams for the local dashboard.

A diagram describes the trusted workflow a team registered. It does not execute
that workflow, and a final outcome does not mark a later stage as recorded.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

NodeState = Literal["recorded", "waiting", "failed", "unknown", "idle"]
StepKind = Literal["entry", "agent", "person", "effect", "result"]
_ACTIVE: frozenset[NodeState] = frozenset({"recorded", "waiting", "failed", "unknown"})
_DECISIONS = frozenset({"workflow_paused", "approval_accepted", "approval_rejected"})


class RouteEvent(Protocol):
    """The audit fields a diagram needs. Argument values are not included."""

    @property
    def correlation_id(self) -> str: ...

    @property
    def agent(self) -> str | None: ...

    @property
    def tool(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def approval(self) -> str: ...

    @property
    def outcome(self) -> str: ...

    @property
    def is_agent_run(self) -> bool: ...


@dataclass(frozen=True)
class DiagramStep:
    """One configured stage. ``key`` matches an audit agent or effect name."""

    key: str
    label: str
    kind: StepKind
    mono: bool = False


@dataclass(frozen=True)
class DiagramChoice:
    """Mutually exclusive agents. Only a recorded one joins the main path."""

    options: tuple[DiagramStep, ...]

    def __post_init__(self) -> None:
        if len(self.options) < 2:
            raise ValueError("A diagram choice needs at least two agents.")
        keys = [item.key for item in self.options]
        if len(set(keys)) != len(keys):
            raise ValueError("Diagram choice keys must be unique.")
        if any(item.kind != "agent" for item in self.options):
            raise ValueError("A diagram choice contains only agents.")


@dataclass(frozen=True)
class WorkflowDiagram:
    """The configured shape of one team's run."""

    stages: tuple[DiagramStep | DiagramChoice, ...]
    handling: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.stages:
            raise ValueError("A workflow diagram needs at least one stage.")


@dataclass(frozen=True)
class RouteNode:
    name: str
    caption: str
    state: NodeState
    mono: bool = False


@dataclass(frozen=True)
class Route:
    """The configured main path, plus choices this run did not take."""

    main: tuple[RouteNode, ...]
    branches: tuple[tuple[int, RouteNode], ...] = ()


def build_route(
    diagram: WorkflowDiagram,
    events: Iterable[RouteEvent],
    *,
    correlation_id: str,
    reference: str,
) -> Route:
    """Overlay one run's audit events on that team's configured diagram.

    A stage stays idle unless an event for this correlation records it. The
    outcome status is not a substitute for that event.
    """
    recorded = tuple(event for event in events if event.correlation_id == correlation_id)
    main: list[RouteNode] = []
    branches: list[tuple[int, RouteNode]] = []
    previous_completed = False
    for stage in diagram.stages:
        if isinstance(stage, DiagramChoice):
            column = len(main) + 1
            chosen, extras, completed = _choice(stage, recorded)
            main.append(chosen)
            branches.extend((column, extra) for extra in extras)
            previous_completed = completed
            continue
        node, completed = _step(stage, recorded, reference, previous_completed)
        main.append(node)
        previous_completed = completed
    return Route(main=tuple(main), branches=tuple(branches))


def link_on(left: RouteNode, right: RouteNode) -> bool:
    """A connector is solid only between two steps the run reached."""
    return left.state in _ACTIVE and right.state in _ACTIVE


def _step(
    step: DiagramStep,
    events: Sequence[RouteEvent],
    reference: str,
    previous_completed: bool,
) -> tuple[RouteNode, bool]:
    if step.kind == "entry":
        caption = reference or "No reference"
        return RouteNode(step.label, caption, "recorded", step.mono), False
    if step.kind == "agent":
        return _agent(step, events)
    if step.kind == "person":
        return _person(step, events), False
    if step.kind == "effect":
        return _effect(step, events), False
    if previous_completed:
        return RouteNode(step.label, "Produced", "recorded", step.mono), False
    return RouteNode(step.label, "Not recorded", "idle", step.mono), False


def _choice(
    choice: DiagramChoice,
    events: Sequence[RouteEvent],
) -> tuple[RouteNode, tuple[RouteNode, ...], bool]:
    recorded = [step for step in choice.options if _agent_events(events, step.key)]
    chosen = recorded[0] if recorded else choice.options[0]
    node, completed = _agent(chosen, events)
    extras: list[RouteNode] = []
    for step in choice.options:
        if step.key == chosen.key:
            continue
        if _agent_events(events, step.key):
            extra, _completed = _agent(step, events)
        else:
            extra = RouteNode(step.label, "Configured, not used", "idle", step.mono)
        extras.append(extra)
    return node, tuple(extras), completed


def _agent(step: DiagramStep, events: Sequence[RouteEvent]) -> tuple[RouteNode, bool]:
    runs = _agent_events(events, step.key)
    if not runs:
        return RouteNode(step.label, "Not recorded", "idle", step.mono), False
    tools = list(
        dict.fromkeys(
            event.tool
            for event in events
            if _same(event.agent, step.key)
            and not event.is_agent_run
            and event.kind == "tool"
            and event.approval != "rejected"
            and event.tool
        )
    )
    caption = ", ".join(tools) if tools else "Ran"
    if runs[-1].outcome == "failed":
        return RouteNode(step.label, caption, "failed", step.mono), False
    return RouteNode(step.label, caption, "recorded", step.mono), runs[-1].outcome == "completed"


def _person(step: DiagramStep, events: Sequence[RouteEvent]) -> RouteNode:
    decisions = [event for event in events if event.kind in _DECISIONS]
    if not decisions:
        return RouteNode(step.label, "Not reached", "idle", step.mono)
    last = decisions[-1].kind
    if last == "workflow_paused":
        return RouteNode(step.label, "Waiting for you", "waiting", step.mono)
    if last == "approval_rejected":
        return RouteNode(step.label, "Rejected", "recorded", step.mono)
    return RouteNode(step.label, "Approved", "recorded", step.mono)


def _effect(step: DiagramStep, events: Sequence[RouteEvent]) -> RouteNode:
    effects = [
        event
        for event in events
        if event.tool == step.key and event.kind.startswith("side_effect_")
    ]
    kinds = {event.kind for event in effects}
    if "side_effect_failed" in kinds:
        return RouteNode(step.label, "Ended with an error", "failed", step.mono)
    if "side_effect_completed" in kinds:
        return RouteNode(step.label, "Completion recorded", "recorded", step.mono)
    if "side_effect_attempted" in kinds:
        return RouteNode(step.label, "Started, not confirmed", "unknown", step.mono)
    return RouteNode(step.label, "Not reached", "idle", step.mono)


def _agent_events(events: Sequence[RouteEvent], key: str) -> tuple[RouteEvent, ...]:
    return tuple(event for event in events if event.is_agent_run and _same(event.agent, key))


def _same(agent: str | None, key: str) -> bool:
    return bool(agent) and agent.casefold() == key.casefold()
