"""Offline checks for typed multi-agent workflow patterns."""

from typing import Any, cast

import pytest
from agent_framework import FunctionTool, tool
from pydantic import BaseModel

from agent_platform.agents import AgentDefinition, AgentOutputError, PlatformAgent, create_agent
from agent_platform.guardrails import ToolEffect, ToolPolicyError, prepare_tool
from agent_platform.observability import AuditLog
from agent_platform.testing import ScriptedChatClient
from agent_platform.workflows import (
    HumanApprovalResponse,
    RouteRejected,
    WorkflowInputError,
    approval_workflow,
    concurrent_workflow,
    conditional_workflow,
    envelope,
    manager_workflow,
    run_workflow,
    sequential_workflow,
)

SECRET = "sk-live-workflow-secret"


class Draft(BaseModel):
    text: str


class Labelled(BaseModel):
    text: str
    label: str


class Analysis(BaseModel):
    text: str
    label: str
    analysis: str


class FinalReport(BaseModel):
    text: str
    label: str
    analysis: str
    verdict: str


class Choice(BaseModel):
    route: str
    text: str


class PathResult(BaseModel):
    owner: str
    text: str


class Brief(BaseModel):
    text: str


class Decision(BaseModel):
    specialist: str
    text: str


class SpecialistNote(BaseModel):
    owner: str
    text: str


class TaskBody(BaseModel):
    text: str


class TaskResult(BaseModel):
    owner: str
    text: str


def _agent(
    name: str,
    input_type: type[BaseModel],
    output_type: type[BaseModel],
    script: str | list[str],
    *,
    tools: tuple[FunctionTool, ...] = (),
    audit_log: AuditLog | None = None,
) -> tuple[PlatformAgent[Any, Any], ScriptedChatClient]:
    responses = [script] if isinstance(script, str) else script
    client = ScriptedChatClient(responses)
    agent = create_agent(
        AgentDefinition(
            name=name,
            purpose=f"Test agent {name}.",
            instructions=f"You are {name}. Reply with the output schema.",
            input_type=input_type,
            output_type=output_type,
            tools=tools,
        ),
        client=client,
        audit_log=audit_log,
    )
    return cast(PlatformAgent[Any, Any], agent), client


def _dump(model: BaseModel) -> str:
    return model.model_dump_json()


async def test_sequential_typed_handoff() -> None:
    classifier, classifier_client = _agent(
        "classifier",
        Draft,
        Labelled,
        _dump(Labelled(text="TOKEN_A", label="alpha")),
    )
    specialist, specialist_client = _agent(
        "specialist_a",
        Labelled,
        Analysis,
        _dump(Analysis(text="TOKEN_A", label="alpha", analysis="looked")),
    )
    reviewer, reviewer_client = _agent(
        "reviewer",
        Analysis,
        FinalReport,
        _dump(FinalReport(text="TOKEN_A", label="alpha", analysis="looked", verdict="ready")),
    )
    workflow = sequential_workflow([classifier, specialist, reviewer], name="sequential")

    result = await run_workflow(workflow, envelope("corr-seq", Draft(text="TOKEN_A")))

    assert result.output is not None
    assert result.output.body["verdict"] == "ready"
    assert "TOKEN_A" in classifier_client.requests[0][0].text
    assert "alpha" in specialist_client.requests[0][0].text
    assert "looked" in reviewer_client.requests[0][0].text
    assert classifier_client.invocations[0]["instructions"] == (
        "You are classifier. Reply with the output schema."
    )


async def test_concurrent_execution_collects_each_result() -> None:
    specialist_a, _ = _agent(
        "specialist_a",
        TaskBody,
        TaskResult,
        _dump(TaskResult(owner="specialist_a", text="alpha-done")),
    )
    specialist_b, _ = _agent(
        "specialist_b",
        TaskBody,
        TaskResult,
        _dump(TaskResult(owner="specialist_b", text="beta-done")),
    )
    workflow = concurrent_workflow([specialist_a, specialist_b], name="concurrent")
    message = envelope("corr-fan", Brief(text="ignored")).model_copy(
        update={
            "body": {
                "tasks": {
                    "specialist_a": {"text": "alpha"},
                    "specialist_b": {"text": "beta"},
                }
            }
        }
    )

    result = await run_workflow(workflow, message)

    assert result.output is not None
    collected = result.output.body["results"]
    assert collected["specialist_a"]["text"] == "alpha-done"
    assert collected["specialist_b"]["text"] == "beta-done"


async def test_conditional_route_follows_the_allowlisted_path() -> None:
    specialist_a, client_a = _agent(
        "specialist_a",
        Choice,
        PathResult,
        _dump(PathResult(owner="specialist_a", text="from-a")),
    )
    specialist_b, client_b = _agent(
        "specialist_b",
        Choice,
        PathResult,
        _dump(PathResult(owner="specialist_b", text="from-b")),
    )
    workflow = conditional_workflow(
        {"specialist_a": specialist_a, "specialist_b": specialist_b},
        name="conditional",
    )

    result = await run_workflow(
        workflow,
        envelope("corr-route", Choice(route="specialist_b", text="choose-b")),
    )

    assert result.output is not None
    assert result.output.body["owner"] == "specialist_b"
    assert client_a.requests == []
    assert client_b.requests


async def test_unknown_route_is_rejected() -> None:
    specialist_a, client_a = _agent(
        "specialist_a",
        Choice,
        PathResult,
        _dump(PathResult(owner="specialist_a", text="from-a")),
    )
    specialist_b, client_b = _agent(
        "specialist_b",
        Choice,
        PathResult,
        _dump(PathResult(owner="specialist_b", text="from-b")),
    )
    workflow = conditional_workflow(
        {"specialist_a": specialist_a, "specialist_b": specialist_b},
        name="conditional-reject",
    )

    with pytest.raises(RouteRejected) as caught:
        await run_workflow(
            workflow,
            envelope("corr-bad-route", Choice(route="intruder", text="nope")),
        )

    assert "intruder" not in str(caught.value)
    assert client_a.requests == []
    assert client_b.requests == []


async def test_manager_decision_reaches_only_the_allowlisted_specialist() -> None:
    manager, manager_client = _agent(
        "manager",
        Brief,
        Decision,
        _dump(Decision(specialist="specialist_a", text="delegate")),
    )
    specialist_a, client_a = _agent(
        "specialist_a",
        Decision,
        SpecialistNote,
        _dump(SpecialistNote(owner="specialist_a", text="delegate")),
    )
    specialist_b, client_b = _agent(
        "specialist_b",
        Decision,
        SpecialistNote,
        _dump(SpecialistNote(owner="specialist_b", text="delegate")),
    )
    workflow = manager_workflow(
        manager,
        {"specialist_a": specialist_a, "specialist_b": specialist_b},
        name="manager",
    )

    result = await run_workflow(workflow, envelope("corr-manager", Brief(text="delegate")))

    assert result.output is not None
    assert result.output.source == "specialist_a"
    assert manager_client.requests
    assert client_a.requests
    assert client_b.requests == []


async def test_unrecognised_manager_route_is_rejected() -> None:
    manager, _ = _agent(
        "manager",
        Brief,
        Decision,
        _dump(Decision(specialist="intruder", text="delegate")),
    )
    specialist_a, client_a = _agent(
        "specialist_a",
        Decision,
        SpecialistNote,
        _dump(SpecialistNote(owner="specialist_a", text="delegate")),
    )
    specialist_b, client_b = _agent(
        "specialist_b",
        Decision,
        SpecialistNote,
        _dump(SpecialistNote(owner="specialist_b", text="delegate")),
    )
    workflow = manager_workflow(
        manager,
        {"specialist_a": specialist_a, "specialist_b": specialist_b},
        name="manager-reject",
    )

    with pytest.raises(RouteRejected):
        await run_workflow(workflow, envelope("corr-manager-bad", Brief(text="delegate")))

    assert client_a.requests == []
    assert client_b.requests == []


async def test_human_rejection_prevents_the_side_effect_and_approval_can_resume() -> None:
    sink: list[str] = []

    def send_notice(recipient: str) -> str:
        """Send a notice."""
        sink.append(recipient)
        return "sent"

    tool = prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT)
    audit_log = AuditLog()
    rejected = approval_workflow(tool, audit_log, name="approval-reject")
    paused = await run_workflow(
        rejected,
        envelope("corr-reject", Brief(text=SECRET)),
    )

    assert sink == []
    assert paused.pending is not None
    assert paused.pending.request.action == tool.name
    assert SECRET not in paused.pending.request.model_dump_json()
    resumed = await run_workflow(
        rejected,
        responses={paused.pending.request_id: HumanApprovalResponse(approved=False)},
        checkpoint_id=paused.pending.checkpoint_id,
    )
    assert sink == []
    assert resumed.output is not None
    assert resumed.output.body["status"] == "rejected"
    assert SECRET not in "".join(event.model_dump_json() for event in audit_log.events)

    sink.clear()
    approved = approval_workflow(tool, audit_log, name="approval-resume")
    waiting = await run_workflow(
        approved,
        envelope("corr-approve", Brief(text="unused")).model_copy(
            update={"body": {"recipient": SECRET}}
        ),
    )
    assert sink == []
    assert waiting.pending is not None
    completed = await run_workflow(
        approved,
        responses={waiting.pending.request_id: HumanApprovalResponse(approved=True)},
        checkpoint_id=waiting.pending.checkpoint_id,
    )

    assert sink == [SECRET]
    assert completed.output is not None
    assert completed.output.body == {"status": "completed"}
    assert completed.correlation_id == "corr-approve"
    assert SECRET not in completed.output.model_dump_json()


async def test_independent_runs_do_not_share_state() -> None:
    first_agent, first_client = _agent(
        "classifier",
        Draft,
        Labelled,
        [
            _dump(Labelled(text="TOKEN_ONE", label="one")),
            _dump(Labelled(text="TOKEN_TWO", label="two")),
        ],
    )
    second_agent, second_client = _agent(
        "reviewer",
        Labelled,
        Labelled,
        [
            _dump(Labelled(text="TOKEN_ONE", label="one")),
            _dump(Labelled(text="TOKEN_TWO", label="two")),
        ],
    )
    workflow = sequential_workflow([first_agent, second_agent], name="isolated")

    first = await run_workflow(workflow, envelope("corr-one", Draft(text="TOKEN_ONE")))
    second = await run_workflow(workflow, envelope("corr-two", Draft(text="TOKEN_TWO")))

    assert first.output is not None and first.output.body["label"] == "one"
    assert second.output is not None and second.output.body["label"] == "two"
    assert "TOKEN_ONE" not in second_client.requests[1][0].text
    assert "TOKEN_ONE" not in first_client.requests[1][0].text
    other = sequential_workflow(
        [
            _agent(
                "classifier",
                Draft,
                Labelled,
                _dump(Labelled(text="TOKEN_OTHER", label="other")),
            )[0],
            _agent(
                "reviewer",
                Labelled,
                Labelled,
                _dump(Labelled(text="TOKEN_OTHER", label="other")),
            )[0],
        ],
        name="isolated-other",
    )
    separate = await run_workflow(other, envelope("corr-other", Draft(text="TOKEN_OTHER")))
    assert separate.output is not None
    assert separate.output.body["text"] == "TOKEN_OTHER"
    assert first.correlation_id != separate.correlation_id


async def test_correlation_survives_across_agents() -> None:
    audit_log = AuditLog()
    classifier, _ = _agent(
        "classifier",
        Draft,
        Labelled,
        _dump(Labelled(text="note", label="alpha")),
        audit_log=audit_log,
    )
    reviewer, _ = _agent(
        "reviewer",
        Labelled,
        Labelled,
        _dump(Labelled(text="note", label="alpha")),
        audit_log=audit_log,
    )
    workflow = sequential_workflow([classifier, reviewer], name="audit-chain")

    await run_workflow(workflow, envelope("corr-shared", Draft(text="note")))

    assert [event.correlation_id for event in audit_log.events] == ["corr-shared", "corr-shared"]
    assert [event.agent_name for event in audit_log.events] == ["classifier", "reviewer"]


async def test_malformed_structured_output_does_not_continue() -> None:
    classifier, _ = _agent("classifier", Draft, Labelled, "MALFORMED_PAYLOAD_TOKEN")
    reviewer, reviewer_client = _agent(
        "reviewer",
        Labelled,
        Labelled,
        _dump(Labelled(text="should-not-run", label="no")),
    )
    workflow = sequential_workflow([classifier, reviewer], name="malformed")

    with pytest.raises(AgentOutputError) as caught:
        await run_workflow(workflow, envelope("corr-bad", Draft(text="note")))

    assert "MALFORMED_PAYLOAD_TOKEN" not in str(caught.value)
    assert reviewer_client.requests == []


async def test_workflow_cannot_add_a_tool_the_agent_was_not_assigned() -> None:
    calls: list[str] = []

    def lookup_status(reference: str) -> str:
        """Return a stored status."""
        return f"status:{reference}"

    def raw_send(recipient: str) -> str:
        """Send without being assigned."""
        calls.append(recipient)
        return "sent"

    assigned = prepare_tool(lookup_status, effect=ToolEffect.READ)
    unassigned = tool(approval_mode="never_require")(raw_send)
    classifier, client = _agent(
        "classifier",
        Draft,
        Labelled,
        _dump(Labelled(text="note", label="alpha")),
        tools=(assigned,),
    )
    reviewer, _ = _agent(
        "reviewer",
        Labelled,
        Labelled,
        _dump(Labelled(text="note", label="alpha")),
    )
    workflow = sequential_workflow([classifier, reviewer], name="tools")

    await workflow.workflow.run(
        envelope("corr-tools", Draft(text="note")),
        tools=[unassigned],
    )

    assert calls == []
    assert client.invocations[0]["tool_names"] == [assigned.name]
    assert unassigned.name not in client.invocations[0]["tool_names"]


async def test_missing_route_field_fails_before_a_specialist_runs() -> None:
    specialist_a, client_a = _agent(
        "specialist_a",
        Choice,
        PathResult,
        _dump(PathResult(owner="specialist_a", text="from-a")),
    )
    specialist_b, client_b = _agent(
        "specialist_b",
        Choice,
        PathResult,
        _dump(PathResult(owner="specialist_b", text="from-b")),
    )
    workflow = conditional_workflow(
        {"specialist_a": specialist_a, "specialist_b": specialist_b},
        name="missing-route",
    )

    with pytest.raises(WorkflowInputError):
        await run_workflow(workflow, envelope("corr-missing", Brief(text="no-route")))

    assert client_a.requests == []
    assert client_b.requests == []


def test_unprepared_tool_cannot_be_placed_behind_the_approval_gate() -> None:
    def raw_send(recipient: str) -> str:
        """Send without classification."""
        return recipient

    unprepared = tool(approval_mode="never_require")(raw_send)
    with pytest.raises(ToolPolicyError):
        approval_workflow(unprepared, AuditLog(), name="bad-tool")
