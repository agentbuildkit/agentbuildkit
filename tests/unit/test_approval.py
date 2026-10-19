"""Approval classification and the side-effect gate."""

import json

import pytest
from agent_framework import Content, FunctionTool, SecretString, tool

from agent_platform.guardrails import (
    ApprovalDecision,
    ToolEffect,
    ToolPolicyError,
    execute_tool,
    prepare_tool,
)
from agent_platform.observability import AuditLog, argument_hash


def _read_tool() -> FunctionTool:
    def lookup_status(reference: str) -> str:
        """Return a stored status."""
        return f"status:{reference}"

    return prepare_tool(lookup_status, effect=ToolEffect.READ)


def _side_effect_tool(sink: list[str]) -> FunctionTool:
    def send_notice(recipient: str) -> str:
        """Send a notice."""
        sink.append(recipient)
        return "sent"

    return prepare_tool(send_notice, effect=ToolEffect.SIDE_EFFECT)


def test_classification_sets_framework_approval_mode() -> None:
    read_tool = _read_tool()
    side_effect = _side_effect_tool([])

    assert read_tool.approval_mode == "never_require"
    assert read_tool.additional_properties is not None
    assert read_tool.additional_properties["effect"] == "read"
    assert side_effect.approval_mode == "always_require"
    assert side_effect.additional_properties is not None
    assert side_effect.additional_properties["effect"] == "side_effect"


async def test_read_tool_runs_and_records_a_completed_audit_event() -> None:
    audit_log = AuditLog()

    result = await execute_tool(
        _read_tool(),
        {"reference": "A-1"},
        audit_log=audit_log,
        correlation_id="corr-1",
        agent_name="clerk",
    )

    assert result[0].text == "status:A-1"
    event = audit_log.events[0]
    assert event.correlation_id == "corr-1"
    assert event.agent_name == "clerk"
    assert event.tool_name == "lookup_status"
    assert event.approval == "not_required"
    assert event.outcome == "completed"
    assert event.argument_hash == argument_hash({"reference": "A-1"})


async def test_side_effect_does_not_run_without_approval() -> None:
    sink: list[str] = []
    audit_log = AuditLog()

    result = await execute_tool(
        _side_effect_tool(sink),
        {"recipient": "ops@example.com"},
        audit_log=audit_log,
        correlation_id="corr-2",
    )

    assert sink == []
    assert isinstance(result, Content)
    assert result.type == "function_approval_request"
    event = audit_log.events[0]
    assert event.approval == "pending"
    assert event.outcome == "pending_approval"
    assert "ops@example.com" not in event.model_dump_json()


async def test_side_effect_runs_only_after_approval() -> None:
    sink: list[str] = []
    tool_def = _side_effect_tool(sink)
    audit_log = AuditLog()

    await execute_tool(
        tool_def,
        {"recipient": "ops@example.com"},
        audit_log=audit_log,
        correlation_id="corr-3",
        decision=ApprovalDecision("send_notice", approved=True),
    )

    assert sink == ["ops@example.com"]
    assert audit_log.events[0].approval == "approved"
    assert audit_log.events[0].outcome == "completed"


async def test_rejected_side_effect_does_not_run() -> None:
    sink: list[str] = []
    audit_log = AuditLog()

    result = await execute_tool(
        _side_effect_tool(sink),
        {"recipient": "ops@example.com"},
        audit_log=audit_log,
        correlation_id="corr-4",
        decision=ApprovalDecision("send_notice", approved=False),
    )

    assert sink == []
    assert isinstance(result, Content)
    assert result.type == "function_approval_response"
    assert audit_log.events[0].approval == "rejected"
    assert audit_log.events[0].outcome == "rejected"


async def test_approval_for_a_different_tool_does_not_run_the_call() -> None:
    sink: list[str] = []
    audit_log = AuditLog()

    await execute_tool(
        _side_effect_tool(sink),
        {"recipient": "ops@example.com"},
        audit_log=audit_log,
        correlation_id="corr-5",
        decision=ApprovalDecision("other_tool", approved=True),
    )

    assert sink == []
    assert audit_log.events[0].approval == "pending"


async def test_unclassified_tool_fails_before_execution() -> None:
    calls: list[str] = []

    @tool
    def send_notice(recipient: str) -> str:
        calls.append(recipient)
        return "sent"

    audit_log = AuditLog()
    with pytest.raises(ToolPolicyError):
        await execute_tool(
            send_notice,
            {"recipient": "ops@example.com"},
            audit_log=audit_log,
            correlation_id="corr-6",
        )

    assert calls == []
    assert audit_log.events[0].outcome == "failed"
    assert audit_log.events[0].approval == "rejected"


async def test_tool_exception_is_audited_without_storing_the_message() -> None:
    def explode(reference: str) -> str:
        raise RuntimeError(f"boom {reference}")

    audit_log = AuditLog()
    with pytest.raises(RuntimeError, match="boom"):
        await execute_tool(
            prepare_tool(explode, effect=ToolEffect.READ),
            {"reference": "sk-hidden"},
            audit_log=audit_log,
            correlation_id="corr-8",
        )

    assert audit_log.events[0].outcome == "failed"
    assert "sk-hidden" not in audit_log.events[0].model_dump_json()


async def test_secret_argument_is_not_exposed_in_the_audit_or_approval_request() -> None:
    sink: list[str] = []
    audit_log = AuditLog()
    secret = SecretString("sk-live-secret")

    result = await execute_tool(
        _side_effect_tool(sink),
        {"recipient": secret},
        audit_log=audit_log,
        correlation_id="corr-7",
    )

    rendered = json.dumps({"event": audit_log.events[0].model_dump(), "result": str(result)})
    assert sink == []
    assert "sk-live-secret" not in rendered
    assert argument_hash({"recipient": secret}) == argument_hash(
        {"recipient": SecretString("sk-other-secret")}
    )
