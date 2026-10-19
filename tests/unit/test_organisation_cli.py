"""Offline tests for the local organisation command."""

from pathlib import Path

import pytest
from agent_framework import SecretString

from agent_platform.agents import AgentOutputError
from agent_platform.observability import AuditEvent, AuditLog
from agent_platform.workflows import memory_workflow_store
from organisation.cli import (
    DEFAULT_CHAT_MODEL,
    OrganisationConfigError,
    approval_from_text,
    render_approval,
    render_failure,
    render_outcome,
    resolve_settings,
    run_session,
)
from organisation.evaluation import LOCAL_MOCK_TOKEN, scripted_clients
from organisation.models import CustomerRequest, FinalOutcome
from organisation.service import ServiceResult
from organisation.team import ORGANISATION_SCOPE, build_team

_OPENAI_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_CHAT_MODEL",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
)


def _silent(**_kwargs: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _clear_openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _OPENAI_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_missing_api_key_is_rejected_without_echoing_a_secret(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_CHAT_MODEL=gpt-4.1-mini\n", encoding="utf-8")

    with pytest.raises(OrganisationConfigError) as caught:
        resolve_settings(env_file)

    assert "OPENAI_API_KEY" in str(caught.value)
    assert "sk-" not in str(caught.value)
    assert LOCAL_MOCK_TOKEN not in str(caught.value)


def test_chat_model_comes_from_configuration(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=local-test-key\nOPENAI_CHAT_MODEL=gpt-test-local\n",
        encoding="utf-8",
    )

    settings = resolve_settings(env_file)

    assert settings.get("chat_model") == "gpt-test-local"
    assert isinstance(settings.get("api_key"), SecretString)


def test_chat_model_defaults_when_unset(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=local-test-key\n", encoding="utf-8")

    settings = resolve_settings(env_file)

    assert settings.get("chat_model") == DEFAULT_CHAT_MODEL


def test_failure_line_names_the_agent_and_hides_secrets() -> None:
    audit_log = AuditLog()
    audit_log.record(
        AuditEvent(
            correlation_id="corr-live",
            agent_name="alex",
            tool_name="alex",
            approval="not_required",
            outcome="failed",
            argument_hash="agent-run",
        )
    )
    safe = render_failure(
        AgentOutputError("Agent 'alex' returned output that does not match RouteDecision."),
        audit_log,
        "corr-live",
    )
    leaked = render_failure(
        _ServiceError(
            "Error code: 400 - {'error': {'message': 'see sk-live-secret'}}",
            status_code=400,
            body={"error": {"message": "see sk-live-secret"}},
        ),
        audit_log,
        "corr-live",
    )
    service = render_failure(
        _ServiceError(
            "raw body",
            status_code=400,
            body={"error": {"message": "Encrypted content is not supported with this model."}},
        ),
        audit_log,
        "corr-live",
    )

    assert "Type: AgentOutputError" in safe
    assert "Stage: agent-run" in safe
    assert "Agent: alex" in safe
    assert "Correlation: corr-live" in safe
    assert "RouteDecision" in safe
    assert "sk-live-secret" not in leaked
    assert "Reason: The model service returned HTTP 400." in leaked
    assert "Encrypted content is not supported with this model." in service
    assert "raw body" not in service


def test_approval_text_accepts_yes_and_no() -> None:
    assert approval_from_text("y") is True
    assert approval_from_text("YES") is True
    assert approval_from_text("n") is False
    assert approval_from_text("no") is False
    assert approval_from_text("maybe") is None


def test_rendered_outcome_keeps_the_typed_fields_only() -> None:
    rendered = render_outcome(
        FinalOutcome(
            reference="A-100",
            status="completed",
            specialist="sam",
            response="Northwind's account is current.",
            correlation_id="cli-info",
        )
    )

    assert "Status: completed" in rendered
    assert "Specialist: sam" in rendered
    assert "Correlation: cli-info" in rendered
    assert LOCAL_MOCK_TOKEN not in rendered


async def test_informational_session_does_not_ask_for_approval() -> None:
    lines: list[str] = []
    organisation = build_team(
        clients=scripted_clients("informational-completes-without-approval"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        configurator=_silent,
    )
    try:
        outcome = await run_session(
            organisation,
            memory_workflow_store(ORGANISATION_SCOPE, organisation.audit_log),
            CustomerRequest(reference="A-100", message="Please explain the latest invoice."),
            correlation_id="cli-info",
            approve=_reject_approval,
            write=lines.append,
        )
    finally:
        await organisation.aclose()

    assert outcome.status == "completed"
    assert outcome.specialist == "sam"
    assert lines
    assert "Approval required" not in lines[0]
    assert LOCAL_MOCK_TOKEN not in lines[0]


async def test_service_action_session_resumes_after_approval() -> None:
    actions: list[str] = []
    lines: list[str] = []
    audit_log = AuditLog()
    organisation = build_team(
        clients=scripted_clients("service-action-pauses-for-approval"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=audit_log,
        actions=actions,
        configurator=_silent,
    )
    try:
        outcome = await run_session(
            organisation,
            memory_workflow_store(ORGANISATION_SCOPE, audit_log),
            CustomerRequest(reference="A-100", message="The service request is still open."),
            correlation_id="cli-action",
            approve=_approve,
            write=lines.append,
        )
    finally:
        await organisation.aclose()

    assert outcome.status == "completed"
    assert actions == ["A-100:schedule_visit"]
    assert "Approval required" in lines[0]
    assert "schedule_visit" in lines[0]
    assert "Status: completed" in lines[1]
    assert LOCAL_MOCK_TOKEN not in "\n".join(lines)


async def test_rejected_service_action_is_not_run() -> None:
    actions: list[str] = []
    audit_log = AuditLog()
    organisation = build_team(
        clients=scripted_clients("service-action-pauses-for-approval"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=audit_log,
        actions=actions,
        configurator=_silent,
    )
    try:
        outcome = await run_session(
            organisation,
            memory_workflow_store(ORGANISATION_SCOPE, audit_log),
            CustomerRequest(reference="A-100", message="The service request is still open."),
            correlation_id="cli-reject",
            approve=_reject_approval,
            write=lambda _text: None,
        )
    finally:
        await organisation.aclose()

    assert outcome.status == "rejected"
    assert actions == []


def _approve(result: ServiceResult) -> bool:
    assert result.action_arguments == {"reference": "A-100", "action": "schedule_visit"}
    assert render_approval(result)
    return True


def _reject_approval(_result: ServiceResult) -> bool:
    return False


class _ServiceError(Exception):
    def __init__(self, message: str, *, status_code: int, body: dict[str, object]) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
