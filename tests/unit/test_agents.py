"""Typed agent runs stay offline and keep tools, sessions, and secrets bounded."""

import json
import logging

import pytest
from agent_framework import ChatResponse, Content, FunctionTool, Message, tool
from pydantic import BaseModel

from agentbuildkit.agents import (
    AgentApprovalRequired,
    AgentDefinition,
    AgentOutputError,
    PlatformAgent,
    create_agent,
)
from agentbuildkit.evaluation import trace_from_audit
from agentbuildkit.guardrails import ToolEffect, ToolPolicyError, prepare_tool
from agentbuildkit.observability import AuditLog
from agentbuildkit.testing import ScriptedChatClient

SECRET = "sk-live-agent-secret"


class Task(BaseModel):
    task: str


class Report(BaseModel):
    summary: str
    score: int


def _report(summary: str, score: int) -> str:
    return json.dumps({"summary": summary, "score": score})


def _definition(
    *tools: FunctionTool,
    instructions: str = "Return only the report schema.",
) -> AgentDefinition[Task, Report]:
    return AgentDefinition(
        name="reporter",
        purpose="Turn one task into a short report.",
        instructions=instructions,
        input_type=Task,
        output_type=Report,
        provider="openai",
        model="offline-model",
        tools=tools,
    )


def _agent(
    client: ScriptedChatClient,
    *tools: FunctionTool,
    instructions: str = "Return only the report schema.",
    audit_log: AuditLog | None = None,
) -> PlatformAgent[Task, Report]:
    return create_agent(
        _definition(*tools, instructions=instructions),
        client=client,
        audit_log=audit_log,
    )


def _read_tool() -> FunctionTool:
    def lookup_status(reference: str) -> str:
        """Return a stored status."""
        return f"status:{reference}"

    return prepare_tool(lookup_status, effect=ToolEffect.READ)


def _other_tool() -> FunctionTool:
    def lookup_archive(reference: str) -> str:
        """Return an archived status."""
        return f"archive:{reference}"

    return prepare_tool(lookup_archive, effect=ToolEffect.READ)


def _side_effect_tool(sink: list[str]) -> FunctionTool:
    def send_notice(recipient: str) -> str:
        """Send a notice."""
        sink.append(recipient)
        return "sent"

    return prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT)


def _message_text(client: ScriptedChatClient, index: int) -> str:
    return " ".join(message.text for message in client.requests[index])


async def test_typed_input_returns_typed_output() -> None:
    audit_log = AuditLog()
    client = ScriptedChatClient([_report("done", 3)])
    agent = _agent(client, audit_log=audit_log)

    result = await agent.run(Task(task="summarise the notes"), correlation_id="run-1")

    assert isinstance(result, Report)
    assert result.summary == "done"
    assert result.score == 3
    event = audit_log.events[0]
    assert event.correlation_id == "run-1"
    assert event.agent_name == "reporter"
    assert event.outcome == "completed"
    assert event.argument_hash == "agent-run"


async def test_malformed_structured_output_is_rejected() -> None:
    client = ScriptedChatClient([f"not-json {SECRET}", '{"summary": "missing score"}'])
    agent = _agent(client)
    audit_log = agent.audit_log

    with pytest.raises(AgentOutputError) as invalid_json:
        await agent.run(Task(task="first"), correlation_id="bad-json")
    with pytest.raises(AgentOutputError) as invalid_schema:
        await agent.run(Task(task="second"), correlation_id="bad-schema")

    for caught in (invalid_json, invalid_schema):
        assert SECRET not in str(caught.value)
        assert "not-json" not in str(caught.value)
        assert "missing score" not in str(caught.value)
        assert caught.value.__cause__ is None
    assert [event.outcome for event in audit_log.events] == ["failed", "failed"]
    assert SECRET not in audit_log_text(audit_log)


async def test_instructions_are_supplied_to_the_client() -> None:
    instructions = "Answer using the report schema and nothing else."
    client = ScriptedChatClient([_report("noted", 1)])
    agent = _agent(client, instructions=instructions)

    await agent.run(Task(task="check the instructions"))

    assert client.invocations[0]["instructions"] == instructions


async def test_only_assigned_tools_are_offered() -> None:
    assigned = _read_tool()
    withheld = _other_tool()
    client = ScriptedChatClient([_report("noted", 1)])
    agent = _agent(client, assigned)

    await agent.run(Task(task="use the allowlist"))

    assert client.invocations[0]["tool_names"] == [assigned.name]
    assert withheld.name not in client.invocations[0]["tool_names"]
    assert agent.agent.default_options["model"] == "offline-model"


def test_unprepared_tools_cannot_be_attached() -> None:
    def raw_lookup(reference: str) -> str:
        """Look up a reference without an approval classification."""
        return reference

    unprepared = tool(approval_mode="never_require")(raw_lookup)
    client = ScriptedChatClient([_report("noted", 1)])

    with pytest.raises(ToolPolicyError):
        _agent(client, unprepared)


async def test_sessions_do_not_share_history() -> None:
    client = ScriptedChatClient(
        [
            _report("one", 1),
            _report("two", 2),
            _report("three", 3),
            _report("four", 4),
        ]
    )
    agent = _agent(client)
    shared = agent.create_session()
    other = agent.create_session()

    await agent.run(Task(task="TOKEN_SHARED_FIRST"), session=shared)
    await agent.run(Task(task="TOKEN_SHARED_SECOND"), session=shared)
    await agent.run(Task(task="TOKEN_OTHER"), session=other)
    await agent.run(Task(task="TOKEN_STATELESS"))

    assert "TOKEN_SHARED_FIRST" in _message_text(client, 1)
    assert "TOKEN_SHARED_FIRST" not in _message_text(client, 2)
    assert "TOKEN_OTHER" in _message_text(client, 2)
    assert "TOKEN_SHARED_FIRST" not in _message_text(client, 3)
    assert "TOKEN_OTHER" not in _message_text(client, 3)


async def test_secrets_do_not_appear_in_errors_logs_or_audit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit_log = AuditLog()
    client = ScriptedChatClient([_report("done", 1)])
    agent = _agent(client, audit_log=audit_log)

    with caplog.at_level(logging.DEBUG):
        await agent.run(Task(task=SECRET), correlation_id="secret-run")

    rendered = audit_log_text(audit_log) + caplog.text
    assert SECRET not in rendered


async def test_side_effects_still_require_framework_approval() -> None:
    sink: list[str] = []
    side_effect = _side_effect_tool(sink)
    script = ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id="call-1",
                        name=side_effect.name,
                        arguments={"recipient": SECRET},
                    )
                ],
            )
        ],
        response_id="scripted",
    )
    client = ScriptedChatClient([script])
    agent = _agent(client, side_effect)

    with pytest.raises(AgentApprovalRequired) as caught:
        await agent.run(Task(task="send the notice"), correlation_id="approval-run")

    assert sink == []
    assert SECRET not in str(caught.value)
    assert caught.value.requests
    assert all(request.arguments is None for request in caught.value.requests)
    assert all(
        request.function_call is None or request.function_call.arguments is None
        for request in caught.value.requests
    )
    event = agent.audit_log.events[0]
    assert event.outcome == "pending_approval"
    assert event.approval == "pending"
    assert SECRET not in audit_log_text(agent.audit_log)


async def test_unprepared_tool_cannot_run_through_the_agent() -> None:
    sink: list[str] = []

    def raw_send(recipient: str) -> str:
        """Send without classification."""
        sink.append(recipient)
        return "sent"

    unprepared = tool(approval_mode="never_require")(raw_send)
    script = ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id="call-2",
                        name=unprepared.name,
                        arguments={"recipient": SECRET},
                    )
                ],
            )
        ],
        response_id="scripted",
    )
    client = ScriptedChatClient([script, _report("should-not-matter", 1)])
    agent = _agent(client)

    response = await agent.agent.run(
        Task(task="bypass").model_dump_json(),
        tools=[unprepared],
        options={"response_format": Report},
    )

    assert sink == []
    assert isinstance(response.value, Report)
    assert SECRET not in audit_log_text(agent.audit_log)


async def test_prepared_tool_outside_the_definition_does_not_run() -> None:
    calls: list[str] = []

    def lookup_archive(reference: str) -> str:
        """Return an archived status."""
        calls.append(reference)
        return f"archive:{reference}"

    extra = prepare_tool(lookup_archive, effect=ToolEffect.READ)
    script = ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id="call-3",
                        name=extra.name,
                        arguments={"reference": SECRET},
                    )
                ],
            )
        ],
        response_id="scripted",
    )
    client = ScriptedChatClient([script, _report("contained", 1)])
    agent = _agent(client, _read_tool())

    response = await agent.agent.run(
        Task(task="try another tool").model_dump_json(),
        tools=[extra],
        options={"response_format": Report},
    )

    assert calls == []
    assert isinstance(response.value, Report)
    assert SECRET not in audit_log_text(agent.audit_log)


async def test_unassigned_tool_rejection_is_observed_from_the_audit() -> None:
    executed: list[str] = []

    def read_service(reference: str) -> str:
        """Read a service record."""
        executed.append(reference)
        return "open"

    foreign = prepare_tool(read_service, effect=ToolEffect.READ, name="read_service")
    assert foreign.name == "read_service"
    script = ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id="call-foreign",
                        name="read_service",
                        arguments={"reference": "A-100"},
                    )
                ],
            )
        ],
        response_id="scripted",
    )
    audit_log = AuditLog()
    client = ScriptedChatClient([script, _report("contained", 1)])
    agent = _agent(client, _read_tool(), audit_log=audit_log)

    result = await agent.run(Task(task="read the service record"), correlation_id="corr-foreign")

    assert result.summary == "contained"
    assert executed == []
    rejected = [event for event in audit_log.events if event.tool_name == "read_service"]
    assert len(rejected) == 1
    assert rejected[0].approval == "rejected"
    assert rejected[0].outcome == "failed"
    assert rejected[0].correlation_id == "corr-foreign"
    observation = trace_from_audit(audit_log, correlation_id="corr-foreign")
    assert observation.tools_rejected == ("read_service",)
    assert "read_service" not in observation.tools_called


async def test_assigned_tool_call_is_not_recorded_as_rejected() -> None:
    calls: list[str] = []

    def lookup_status(reference: str) -> str:
        """Return a stored status."""
        calls.append(reference)
        return f"status:{reference}"

    assigned = prepare_tool(lookup_status, effect=ToolEffect.READ, name="lookup_status")
    script = ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id="call-assigned",
                        name="lookup_status",
                        arguments={"reference": "A-100"},
                    )
                ],
            )
        ],
        response_id="scripted",
    )
    audit_log = AuditLog()
    client = ScriptedChatClient([script, _report("read", 1)])
    agent = _agent(client, assigned, audit_log=audit_log)

    await agent.run(Task(task="read the assigned record"), correlation_id="corr-assigned")

    assert calls == ["A-100"]
    observation = trace_from_audit(audit_log, correlation_id="corr-assigned")
    assert observation.tools_rejected == ()
    assert "lookup_status" in observation.tools_called


def audit_log_text(audit_log: AuditLog) -> str:
    return "".join(event.model_dump_json() for event in audit_log.events)
