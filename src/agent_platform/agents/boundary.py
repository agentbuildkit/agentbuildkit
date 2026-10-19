"""Typed agent creation and execution on Microsoft Agent Framework.

A client deployment supplies the name, purpose, instructions, schemas, client,
and prepared tools. This module does not contain client-specific agents.
"""

import copy
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent_framework import (
    Agent,
    AgentResponse,
    AgentSession,
    ChatOptions,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    FunctionTool,
    SupportsChatGetResponse,
)
from pydantic import BaseModel, ValidationError

from agent_platform.guardrails import (
    PreparedToolGuard,
    ToolEffect,
    ToolPolicyError,
    tool_audit_middleware,
    tool_effect,
)
from agent_platform.observability import (
    AuditEvent,
    AuditLog,
    bind_correlation_id,
    reset_correlation_id,
)
from agent_platform.observability.audit import ApprovalState, AuditOutcome


class AgentInputError(Exception):
    """The caller did not supply the agent's input model."""


class AgentOutputError(Exception):
    """The agent response did not match the output model."""


class AgentApprovalRequired(Exception):
    """A tool call is waiting for approval, so the run has no final output."""

    def __init__(self, requests: Sequence[Content]) -> None:
        super().__init__("Agent run is waiting for tool approval.")
        self.requests = list(requests)


@dataclass(frozen=True)
class AgentDefinition[InputT: BaseModel, OutputT: BaseModel]:
    """Everything a client-specific module needs to declare one agent."""

    name: str
    purpose: str
    instructions: str
    input_type: type[InputT]
    output_type: type[OutputT]
    tools: tuple[FunctionTool, ...] = ()
    model: str | None = None


@dataclass
class PlatformAgent[InputT: BaseModel, OutputT: BaseModel]:
    """A framework agent plus the typed run helper."""

    definition: AgentDefinition[InputT, OutputT]
    agent: Agent
    audit_log: AuditLog

    def create_session(self, *, session_id: str | None = None) -> AgentSession:
        """Create an independent Agent Framework session."""
        return self.agent.create_session(session_id=session_id)

    async def run(
        self,
        data: InputT,
        *,
        session: AgentSession | None = None,
        correlation_id: str | None = None,
    ) -> OutputT:
        """Validate input, run the agent, and return the parsed output model.

        This method does not accept additional tools. Only tools declared on the
        definition are registered.
        """
        if not isinstance(data, self.definition.input_type):
            raise AgentInputError(f"Expected input type {self.definition.input_type.__name__}.")
        resolved_correlation_id = correlation_id or uuid.uuid4().hex
        correlation_token = bind_correlation_id(resolved_correlation_id)
        try:
            response = await self.agent.run(
                data.model_dump_json(),
                session=session,
                options={"response_format": self.definition.output_type},
                middleware=[
                    tool_audit_middleware(
                        self.audit_log,
                        correlation_id=resolved_correlation_id,
                        agent_name=self.definition.name,
                    ),
                ],
            )
            if response.user_input_requests:
                _record_run(
                    self.audit_log,
                    correlation_id=resolved_correlation_id,
                    agent_name=self.definition.name,
                    outcome="pending_approval",
                    approval="pending",
                )
                raise AgentApprovalRequired(
                    _requests_without_arguments(response.user_input_requests)
                )
            output = _validated_output(self.definition, response)
        except AgentApprovalRequired:
            raise
        except Exception:
            _record_run(
                self.audit_log,
                correlation_id=resolved_correlation_id,
                agent_name=self.definition.name,
                outcome="failed",
                approval="not_required",
            )
            raise
        else:
            _record_run(
                self.audit_log,
                correlation_id=resolved_correlation_id,
                agent_name=self.definition.name,
                outcome="completed",
                approval="not_required",
            )
            return output
        finally:
            reset_correlation_id(correlation_token)


def create_agent[InputT: BaseModel, OutputT: BaseModel](
    definition: AgentDefinition[InputT, OutputT],
    *,
    client: SupportsChatGetResponse,
    audit_log: AuditLog | None = None,
) -> PlatformAgent[InputT, OutputT]:
    """Create a framework agent from a client-supplied definition."""
    for function_tool in definition.tools:
        _require_prepared_tool(function_tool)
    options: ChatOptions[None] | None = None
    if definition.model is not None:
        options = {"model": definition.model}
    agent = Agent(
        client,
        definition.instructions,
        name=definition.name,
        description=definition.purpose,
        tools=list(definition.tools),
        default_options=options,
        middleware=[
            _AssignedToolGuard(frozenset(function_tool.name for function_tool in definition.tools)),
            PreparedToolGuard(),
        ],
    )
    return PlatformAgent(
        definition=definition,
        agent=agent,
        audit_log=audit_log or AuditLog(),
    )


class _AssignedToolGuard(FunctionMiddleware):
    """Refuse tool calls that were not declared on the agent."""

    def __init__(self, names: frozenset[str]) -> None:
        self._names = names

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        if context.function.name not in self._names:
            context.metadata["approval_state"] = "rejected"
            context.result = "Tool is not registered for this agent."
            return
        await call_next()


def _requests_without_arguments(requests: Sequence[Content]) -> list[Content]:
    """Keep approval requests without the argument values the model supplied."""
    safe: list[Content] = []
    for request in requests:
        copied = copy.copy(request)
        copied.arguments = None
        function_call = copied.function_call
        if function_call is not None:
            call = copy.copy(function_call)
            call.arguments = None
            copied.function_call = call
        safe.append(copied)
    return safe


def _validated_output[InputT: BaseModel, OutputT: BaseModel](
    definition: AgentDefinition[InputT, OutputT],
    response: AgentResponse[Any],
) -> OutputT:
    try:
        value = response.value
    except (ValidationError, ValueError):
        raise AgentOutputError(
            f"Agent '{definition.name}' returned output that does not match "
            f"{definition.output_type.__name__}."
        ) from None
    if not isinstance(value, definition.output_type):
        raise AgentOutputError(
            f"Agent '{definition.name}' returned output that does not match "
            f"{definition.output_type.__name__}."
        )
    return value


def _record_run(
    audit_log: AuditLog,
    *,
    correlation_id: str,
    agent_name: str,
    outcome: AuditOutcome,
    approval: ApprovalState,
) -> None:
    audit_log.record(
        AuditEvent(
            correlation_id=correlation_id,
            agent_name=agent_name,
            tool_name=agent_name,
            approval=approval,
            outcome=outcome,
            argument_hash="agent-run",
        )
    )


def _require_prepared_tool(function_tool: FunctionTool) -> None:
    effect = tool_effect(function_tool)
    expected = "always_require" if effect is ToolEffect.SIDE_EFFECT else "never_require"
    if function_tool.approval_mode != expected:
        raise ToolPolicyError(
            f"Tool '{function_tool.name}' effect {effect.value} does not match its approval mode."
        )
