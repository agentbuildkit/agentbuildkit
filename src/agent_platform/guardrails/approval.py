"""Deterministic tool approval policy.

``FunctionTool.invoke`` does not consult ``approval_mode``. The framework enforces
that flag only in its automatic tool loop. ``execute_tool`` is the supported gate:
a side-effecting tool does not run until a matching approval decision is supplied.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, cast

from agent_framework import (
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    FunctionTool,
    tool,
)
from pydantic import BaseModel

from agent_platform.observability.audit import (
    ApprovalState,
    AuditEvent,
    AuditLog,
    AuditOutcome,
    argument_hash,
    redact_mapping,
)

_EFFECT_KEY = "effect"


class ToolEffect(StrEnum):
    """Whether a tool only reads data or changes something outside the process."""

    READ = "read"
    SIDE_EFFECT = "side_effect"


class ToolPolicyError(Exception):
    """The tool has no usable approval classification."""


class ApprovalDecision:
    """A person's decision for one named tool call."""

    def __init__(self, tool_name: str, *, approved: bool) -> None:
        self.tool_name = tool_name
        self.approved = approved


def prepare_tool(
    func: Callable[..., Any],
    *,
    effect: ToolEffect,
    name: str | None = None,
    description: str | None = None,
) -> FunctionTool:
    """Wrap a function and set the framework approval mode from its effect."""
    approval_mode: Literal["always_require", "never_require"] = (
        "always_require" if effect is ToolEffect.SIDE_EFFECT else "never_require"
    )
    return tool(
        name=name,
        description=description,
        approval_mode=approval_mode,
        additional_properties={_EFFECT_KEY: effect.value},
    )(func)


def tool_effect(function_tool: FunctionTool) -> ToolEffect:
    """Read the effect declared on a prepared tool."""
    properties = function_tool.additional_properties or {}
    raw = properties.get(_EFFECT_KEY)
    if raw == ToolEffect.READ.value:
        return ToolEffect.READ
    if raw == ToolEffect.SIDE_EFFECT.value:
        return ToolEffect.SIDE_EFFECT
    raise ToolPolicyError(f"Tool '{function_tool.name}' has no effect classification.")


class _ApprovalMiddleware(FunctionMiddleware):
    """Stop side-effecting calls that do not have a matching approval decision."""

    def __init__(self, decision: ApprovalDecision | None) -> None:
        self._decision = decision

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        effect = tool_effect(context.function)
        _require_consistent_mode(context.function, effect)
        if effect is ToolEffect.READ:
            context.metadata["approval_state"] = "not_required"
            await call_next()
            return

        decision = self._decision
        if decision is None or decision.tool_name != context.function.name or not decision.approved:
            state: ApprovalState = (
                "rejected"
                if decision is not None
                and decision.tool_name == context.function.name
                and not decision.approved
                else "pending"
            )
            context.metadata["approval_state"] = state
            context.result = _approval_content(
                context, approved=False if state == "rejected" else None
            )
            return

        context.metadata["approval_state"] = "approved"
        await call_next()


class _AuditMiddleware(FunctionMiddleware):
    """Record the outcome of a tool call after the approval gate."""

    def __init__(
        self,
        audit_log: AuditLog,
        *,
        correlation_id: str,
        agent_name: str | None,
    ) -> None:
        self._audit_log = audit_log
        self._correlation_id = correlation_id
        self._agent_name = agent_name

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await call_next()
        except Exception:
            self._record(context, outcome="failed")
            raise
        self._record(context, outcome=_outcome(context))

    def _record(self, context: FunctionInvocationContext, *, outcome: AuditOutcome) -> None:
        self._audit_log.record(
            AuditEvent(
                correlation_id=self._correlation_id,
                agent_name=self._agent_name,
                tool_name=context.function.name,
                approval=_approval_state(context),
                outcome=outcome,
                argument_hash=argument_hash(_argument_mapping(context.arguments)),
            )
        )


def tool_audit_middleware(
    audit_log: AuditLog,
    *,
    correlation_id: str,
    agent_name: str | None,
) -> FunctionMiddleware:
    """Audit a framework tool call without recording argument values."""
    return _AuditMiddleware(
        audit_log,
        correlation_id=correlation_id,
        agent_name=agent_name,
    )


class PreparedToolGuard(FunctionMiddleware):
    """Refuse tools that were not prepared with an effect and matching approval mode."""

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            effect = tool_effect(context.function)
            _require_consistent_mode(context.function, effect)
        except ToolPolicyError:
            context.metadata["approval_state"] = "rejected"
            context.result = "Tool is not registered for this agent."
            return
        await call_next()


async def execute_tool(
    function_tool: FunctionTool,
    arguments: Mapping[str, Any],
    *,
    audit_log: AuditLog,
    correlation_id: str | None = None,
    agent_name: str | None = None,
    decision: ApprovalDecision | None = None,
) -> Any:
    """Run a prepared tool, or stop when approval is required and absent."""
    resolved_correlation_id = correlation_id or uuid.uuid4().hex
    try:
        effect = tool_effect(function_tool)
        _require_consistent_mode(function_tool, effect)
    except ToolPolicyError:
        audit_log.record(
            AuditEvent(
                correlation_id=resolved_correlation_id,
                agent_name=agent_name,
                tool_name=function_tool.name,
                approval="rejected",
                outcome="failed",
                argument_hash=argument_hash(arguments),
            )
        )
        raise

    context = FunctionInvocationContext(function=function_tool, arguments=dict(arguments))
    middleware: Sequence[FunctionMiddleware] = (
        tool_audit_middleware(
            audit_log,
            correlation_id=resolved_correlation_id,
            agent_name=agent_name,
        ),
        _ApprovalMiddleware(decision),
    )
    return await _run_middleware(
        context,
        middleware,
        _invoke,
    )


async def _invoke(context: FunctionInvocationContext) -> Any:
    return await context.function.invoke(arguments=context.arguments)


async def _run_middleware(
    context: FunctionInvocationContext,
    middleware: Sequence[FunctionMiddleware],
    final: Callable[[FunctionInvocationContext], Awaitable[Any]],
) -> Any:
    async def call_at(index: int) -> None:
        if index >= len(middleware):
            context.result = await final(context)
            return

        async def call_next() -> None:
            await call_at(index + 1)

        await middleware[index].process(context, call_next)

    await call_at(0)
    return context.result


def _require_consistent_mode(function_tool: FunctionTool, effect: ToolEffect) -> None:
    expected = "always_require" if effect is ToolEffect.SIDE_EFFECT else "never_require"
    if function_tool.approval_mode != expected:
        raise ToolPolicyError(
            f"Tool '{function_tool.name}' effect {effect.value} does not match its approval mode."
        )


def _approval_content(context: FunctionInvocationContext, *, approved: bool | None) -> Content:
    if not isinstance(context.arguments, Mapping):
        raise ToolPolicyError(f"Tool '{context.function.name}' arguments must be a mapping.")
    safe_arguments = redact_mapping(context.arguments)
    call_id = uuid.uuid4().hex
    function_call = Content.from_function_call(
        call_id=call_id,
        name=context.function.name,
        arguments=safe_arguments,
    )
    if approved is None:
        return Content.from_function_approval_request(id=call_id, function_call=function_call)
    return Content.from_function_approval_response(
        approved=approved,
        id=call_id,
        function_call=function_call,
    )


def _approval_state(context: FunctionInvocationContext) -> ApprovalState:
    recorded = context.metadata.get("approval_state")
    if recorded in {"not_required", "pending", "approved", "rejected"}:
        return cast(ApprovalState, recorded)
    if context.function.approval_mode == "never_require":
        return "not_required"
    result = context.result
    if isinstance(result, Content) and result.type == "function_approval_request":
        return "pending"
    if isinstance(result, Content) and result.type == "function_approval_response":
        return "rejected"
    return "approved"


def _argument_mapping(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, BaseModel):
        dumped: dict[str, Any] = arguments.model_dump()
        return dumped
    if isinstance(arguments, Mapping):
        mapped: dict[str, Any] = {}
        items = cast(Mapping[Any, Any], arguments)
        for key, value in items.items():
            mapped[str(key)] = value
        return mapped
    return {}


def _outcome(context: FunctionInvocationContext) -> AuditOutcome:
    result = context.result
    if isinstance(result, Content) and result.type == "function_approval_request":
        return "pending_approval"
    if isinstance(result, Content) and result.type == "function_approval_response":
        return "rejected"
    approval = context.metadata.get("approval_state")
    if approval == "rejected":
        return "rejected"
    return "completed"
