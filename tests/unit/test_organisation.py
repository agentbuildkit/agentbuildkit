"""Offline tests for the reference customer-service organisation."""

import json
from pathlib import Path

from agent_framework import Message, SecretString
from agent_framework.openai import OpenAIChatClient

from agent_platform.config import load_openai_settings
from agent_platform.observability import AuditLog
from agent_platform.workflows import memory_workflow_store, sqlite_workflow_store
from organisation import (
    LOCAL_MOCK_TOKEN,
    ORGANISATION_SCOPE,
    CustomerRequest,
    build_team,
    handle_request,
    production_clients,
    resume_service_action,
    run_organisation_evaluation,
)
from organisation.evaluation import RAW_MODEL_OUTPUT, scripted_clients
from organisation.models import RouteDecision
from organisation.team import SAM, TAYLOR


def _silent(**_kwargs: object) -> None:
    return None


async def test_organisation_evaluation_suite() -> None:
    report = await run_organisation_evaluation()

    assert report.failed == 0
    assert report.passed == report.total
    assert report.pass_rate == 1.0
    rendered = report.model_dump_json()
    assert LOCAL_MOCK_TOKEN not in rendered
    assert RAW_MODEL_OUTPUT not in rendered
    names = {case.name for case in report.cases}
    assert "billing-routes-to-sam" in names
    assert "approved-service-action-executes-once" in names
    assert "correlation-survives-the-organisation" in names


async def test_informational_request_completes_without_approval() -> None:
    actions: list[str] = []
    audit_log = AuditLog()
    organisation = build_team(
        clients=scripted_clients("informational-completes-without-approval"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=audit_log,
        actions=actions,
        configurator=_silent,
    )
    try:
        result = await handle_request(
            organisation,
            CustomerRequest(reference="A-100", message="Please explain the latest invoice."),
            correlation_id="demo-info",
            store=memory_workflow_store(ORGANISATION_SCOPE, audit_log),
        )
    finally:
        await organisation.aclose()

    assert result.outcome.status == "completed"
    assert result.outcome.specialist == "sam"
    assert result.outcome.response == "Northwind's account is current."
    assert result.pending is None
    assert actions == []
    assert organisation.business.account_reads == ["A-100"]
    assert organisation.business.service_reads == []
    rendered = json.dumps([event.model_dump() for event in audit_log.events], default=str)
    assert LOCAL_MOCK_TOKEN not in rendered
    assert RAW_MODEL_OUTPUT not in rendered
    assert {event.correlation_id for event in audit_log.events} == {"demo-info"}
    assert "schedule_service" not in {event.tool_name for event in audit_log.events}
    assert "workflow_paused" not in {event.kind for event in audit_log.events}


async def test_service_action_survives_restart_and_runs_once(tmp_path: Path) -> None:
    actions: list[str] = []
    first_log = AuditLog()
    path = tmp_path / "organisation.sqlite"
    organisation = build_team(
        clients=scripted_clients("service-action-pauses-for-approval"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=first_log,
        actions=actions,
        configurator=_silent,
    )
    try:
        paused = await handle_request(
            organisation,
            CustomerRequest(reference="A-100", message="The service request is still open."),
            correlation_id="demo-action",
            store=sqlite_workflow_store(path, ORGANISATION_SCOPE, first_log),
        )
    finally:
        await organisation.aclose()

    assert paused.outcome.status == "pending_approval"
    assert paused.pending is not None
    assert actions == []
    stored = path.read_bytes()
    assert LOCAL_MOCK_TOKEN.encode() not in stored
    assert RAW_MODEL_OUTPUT.encode() not in stored
    assert b"schedule_visit" not in stored

    second_log = AuditLog()
    restored = sqlite_workflow_store(path, ORGANISATION_SCOPE, second_log)
    pending = restored.list_pending(status="pending")
    assert len(pending) == 1
    assert pending[0].correlation_id == "demo-action"
    resumed = build_team(
        clients=scripted_clients("billing-routes-to-sam"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=second_log,
        actions=actions,
        configurator=_silent,
    )
    try:
        completed = await resume_service_action(
            resumed,
            restored,
            pending[0],
            reference="A-100",
            approved=True,
            arguments={"reference": "A-100", "action": "schedule_visit"},
        )
        repeated = await resume_service_action(
            resumed,
            restored,
            pending[0],
            reference="A-100",
            approved=True,
            arguments={"reference": "A-100", "action": "schedule_visit"},
        )
    finally:
        await resumed.aclose()

    assert completed.status == "completed"
    assert repeated.status == "completed"
    assert actions == ["A-100:schedule_visit"]
    combined = [*first_log.events, *second_log.events]
    assert {event.correlation_id for event in combined} == {"demo-action"}
    kinds = {event.kind for event in combined}
    assert {"workflow_paused", "approval_accepted", "workflow_resumed"} <= kinds
    assert {"side_effect_attempted", "side_effect_completed"} <= kinds
    rendered = json.dumps([event.model_dump() for event in combined], default=str)
    assert LOCAL_MOCK_TOKEN not in rendered
    assert "The service request is open." not in rendered


async def test_specialists_receive_only_their_own_tools() -> None:
    organisation = build_team(
        clients=scripted_clients("billing-routes-to-sam"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        configurator=_silent,
    )
    try:
        sam_tools = {tool.name for tool in organisation.sam.definition.tools}
        taylor_tools = {tool.name for tool in organisation.taylor.definition.tools}
        assert sam_tools == {"read_account"}
        assert taylor_tools == {"read_service"}
        assert "schedule_service" not in sam_tools | taylor_tools
        assert organisation.sam.definition.name == SAM
        assert organisation.taylor.definition.name == TAYLOR
    finally:
        await organisation.aclose()


async def test_production_client_does_not_request_encrypted_reasoning(tmp_path: Path) -> None:
    env_file = tmp_path / "openai.env"
    env_file.write_text("", encoding="utf-8")
    settings = load_openai_settings(
        api_key="local-test-key",
        chat_model="gpt-4.1-mini",
        env_file_path=str(env_file),
    )
    client = production_clients(settings)["alex"]

    prepared = await client.request_options(
        [Message(role="user", contents=["hello"])],
        {"response_format": RouteDecision},
    )
    opted_in = await client.request_options(
        [Message(role="user", contents=["hello"])],
        {"response_format": RouteDecision, "include": ["reasoning.encrypted_content"]},
    )

    assert prepared.get("model") == "gpt-4.1-mini"
    assert prepared.get("text_format") is RouteDecision
    assert "reasoning.encrypted_content" not in (prepared.get("include") or [])
    assert "reasoning.encrypted_content" in opted_in.get("include", [])


def test_production_clients_use_the_openai_adapter(tmp_path: Path) -> None:
    env_file = tmp_path / "openai.env"
    env_file.write_text("", encoding="utf-8")
    settings = load_openai_settings(
        api_key="local-test-key",
        chat_model="local-test-model",
        env_file_path=str(env_file),
    )
    clients = production_clients(settings)

    assert set(clients) == {"alex", "sam", "taylor", "jordan"}
    assert all(isinstance(client, OpenAIChatClient) for client in clients.values())
    assert len({id(client) for client in clients.values()}) == 1
