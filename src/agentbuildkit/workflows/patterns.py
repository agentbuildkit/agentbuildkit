"""Reusable workflow graphs on Microsoft Agent Framework's WorkflowBuilder.

Sequential, concurrent, and conditional graphs use typed executors. Routing
predicates compare an allowlisted value. They do not resolve an arbitrary name
to an agent or a tool.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, cast

from agent_framework import (
    Case,
    CheckpointStorage,
    Default,
    Executor,
    FunctionTool,
    InMemoryCheckpointStorage,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)

from agentbuildkit.agents import PlatformAgent
from agentbuildkit.observability import AuditLog
from agentbuildkit.workflows.runtime import (
    AgentStep,
    ApprovalGate,
    CompiledWorkflow,
    RejectRoute,
    ResultAggregator,
    RouteRejected,
    WorkflowEnvelope,
    WorkflowInputError,
)


def sequential_workflow(
    agents: Sequence[PlatformAgent[Any, Any]],
    *,
    name: str,
) -> CompiledWorkflow:
    """Run agents in order. Each output is the next agent's input."""
    if len(agents) < 2:
        raise ValueError("A sequential workflow needs at least two agents.")
    _require_unique(agent.definition.name for agent in agents)
    steps = [
        AgentStep(agent, terminal=index == len(agents) - 1) for index, agent in enumerate(agents)
    ]
    return _compile(name, steps[0], lambda builder: builder.add_chain(steps))


def concurrent_workflow(
    agents: Sequence[PlatformAgent[Any, Any]],
    *,
    name: str,
) -> CompiledWorkflow:
    """Give each allowlisted agent its own typed task and collect the results."""
    if len(agents) < 2:
        raise ValueError("A concurrent workflow needs at least two agents.")
    _require_unique(agent.definition.name for agent in agents)
    steps = {agent.definition.name: AgentStep(agent, terminal=False) for agent in agents}
    dispatcher = _TaskDispatcher(steps)
    aggregator = ResultAggregator()
    participants = list(steps.values())
    return _compile(
        name,
        dispatcher,
        lambda builder: builder.add_fan_out_edges(dispatcher, participants).add_fan_in_edges(
            participants,
            aggregator,
        ),
    )


def conditional_workflow(
    routes: Mapping[str, PlatformAgent[Any, Any]],
    *,
    name: str,
    route_field: str = "route",
) -> CompiledWorkflow:
    """Route with a Python predicate. An unknown value fails the workflow."""
    if len(routes) < 2:
        raise ValueError("A conditional workflow needs at least two routes.")
    _require_unique(agent.definition.name for agent in routes.values())
    forwarder = _RouteForwarder(route_field)
    reject = RejectRoute()
    steps = {route: AgentStep(agent, terminal=True) for route, agent in routes.items()}
    cases = [
        Case(condition=_route_is(route_field, route), target=step) for route, step in steps.items()
    ]
    return _compile(
        name,
        forwarder,
        lambda builder: builder.add_switch_case_edge_group(
            forwarder,
            [*cases, Default(target=reject)],
        ),
    )


def manager_workflow(
    manager: PlatformAgent[Any, Any],
    specialists: Mapping[str, PlatformAgent[Any, Any]],
    *,
    name: str,
    route_field: str = "specialist",
) -> CompiledWorkflow:
    """Let a manager emit a typed decision, then route only to an allowlisted specialist.

    The workflow result keeps the specialist output in ``body`` and the manager's
    validated route field on ``route``.
    """
    if not specialists:
        raise ValueError("A manager workflow needs at least one specialist.")
    names = [manager.definition.name, *[agent.definition.name for agent in specialists.values()]]
    _require_unique(names)
    manager_step = AgentStep(manager, terminal=False, route_field=route_field)
    reject = RejectRoute()
    steps = {route: AgentStep(agent, terminal=True) for route, agent in specialists.items()}
    cases = [
        Case(condition=_route_is(route_field, route), target=step) for route, step in steps.items()
    ]
    return _compile(
        name,
        manager_step,
        lambda builder: builder.add_switch_case_edge_group(
            manager_step,
            [*cases, Default(target=reject)],
        ),
    )


def approval_workflow(
    tool: FunctionTool,
    audit_log: AuditLog,
    *,
    name: str,
    storage: CheckpointStorage | None = None,
) -> CompiledWorkflow:
    """Pause before a side effect and resume only from an explicit human response."""
    gate = ApprovalGate(tool, audit_log)
    return _compile(name, gate, lambda builder: builder, storage=storage)


class _TaskDispatcher(Executor):
    """Send each named task to the agent declared for that name."""

    def __init__(self, agents: Mapping[str, AgentStep]) -> None:
        super().__init__(id="dispatch")
        self._agents = dict(agents)

    @handler
    async def dispatch(
        self,
        message: WorkflowEnvelope,
        ctx: WorkflowContext[WorkflowEnvelope],
    ) -> None:
        tasks = message.body.get("tasks")
        if not isinstance(tasks, dict):
            raise WorkflowInputError("Concurrent work requires a tasks object.")
        task_map = cast(Mapping[Any, Any], tasks)
        names = set(self._agents)
        if set(task_map) != names:
            raise RouteRejected()
        for name in names:
            payload = task_map[name]
            if not isinstance(payload, dict):
                raise WorkflowInputError("Each concurrent task must be an object.")
            body: dict[str, Any] = {}
            for key, value in cast(Mapping[Any, Any], payload).items():
                body[str(key)] = value
            await ctx.send_message(
                WorkflowEnvelope(
                    correlation_id=message.correlation_id,
                    source="dispatch",
                    body=body,
                ),
                target_id=name,
            )


class _RouteForwarder(Executor):
    """Require a string route before the switch edge runs."""

    def __init__(self, route_field: str) -> None:
        super().__init__(id="router")
        self._route_field = route_field

    @handler
    async def forward(
        self,
        message: WorkflowEnvelope,
        ctx: WorkflowContext[WorkflowEnvelope],
    ) -> None:
        route = message.body.get(self._route_field)
        if not isinstance(route, str) or not route:
            raise WorkflowInputError("Workflow route is missing.")
        await ctx.send_message(message)


def _route_is(field: str, expected: str) -> Callable[[Any], bool]:
    def condition(message: Any) -> bool:
        return isinstance(message, WorkflowEnvelope) and message.body.get(field) == expected

    condition.__name__ = f"route_{expected}"
    return condition


def _compile(
    name: str,
    start: Executor,
    connect: Callable[[WorkflowBuilder], WorkflowBuilder],
    storage: CheckpointStorage | None = None,
) -> CompiledWorkflow:
    checkpoints = storage if storage is not None else InMemoryCheckpointStorage()
    builder = WorkflowBuilder(
        name=name,
        start_executor=start,
        checkpoint_storage=checkpoints,
        max_iterations=20,
    )
    workflow = connect(builder).build()
    return CompiledWorkflow(name=name, workflow=workflow, checkpoints=checkpoints)


def _require_unique(names: Iterable[str]) -> None:
    collected = list(names)
    if len(collected) != len(set(collected)):
        raise ValueError("Workflow participants need unique names.")
