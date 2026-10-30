"""Offline HTTP integration checks. No live network calls."""

from collections.abc import Callable, Mapping

import httpx
import pytest
from agent_framework import ChatResponse, Content, Message, SecretString
from pydantic import BaseModel

from agentbuildkit.agents import AgentDefinition, create_agent
from agentbuildkit.guardrails import ApprovalDecision, ToolEffect, prepare_tool
from agentbuildkit.integrations import (
    HttpApprovalRequired,
    HttpClient,
    HttpPathError,
    HttpPayloadError,
    HttpRejected,
    HttpStatusError,
    HttpTimeoutError,
)
from agentbuildkit.observability import AuditLog
from agentbuildkit.testing import ScriptedChatClient
from agentbuildkit.workflows import envelope, run_workflow, sequential_workflow

SECRET = "sk-live-http-secret"


class Status(BaseModel):
    state: str


class StatusQuery(BaseModel):
    reference: str


class Note(BaseModel):
    recipient: str


class Created(BaseModel):
    id: str


class Draft(BaseModel):
    reference: str


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    audit_log: AuditLog | None = None,
    *,
    headers: Mapping[str, str | SecretString] | None = None,
    read_attempts: int = 2,
) -> HttpClient:
    return HttpClient(
        name="records",
        base_url="https://records.test",
        audit_log=audit_log or AuditLog(),
        header_provider=lambda: dict(headers or {}),
        transport=httpx.MockTransport(handler),
        read_attempts=read_attempts,
    )


def _capture(
    responses: list[httpx.Response | Exception],
) -> tuple[list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return calls, handler


async def test_typed_read_returns_the_response_model() -> None:
    calls, handler = _capture([httpx.Response(200, json={"state": "open"})])
    audit_log = AuditLog()
    client = _client(handler, audit_log)

    result = await client.read(
        "/status",
        response_type=Status,
        operation="status.read",
        query=StatusQuery(reference="A-1"),
        correlation_id="corr-read",
    )

    assert result == Status(state="open")
    assert calls[0].url.path == "/status"
    assert calls[0].url.params["reference"] == "A-1"
    assert audit_log.events[0].correlation_id == "corr-read"
    assert audit_log.events[0].outcome == "completed"
    await client.aclose()


async def test_malformed_http_response_is_rejected() -> None:
    _, handler = _capture([httpx.Response(200, content=b"not-json")])
    client = _client(handler)

    with pytest.raises(HttpPayloadError) as caught:
        await client.read(
            "/status",
            response_type=Status,
            operation="status.read",
            correlation_id="corr-bad",
        )

    assert "not-json" not in str(caught.value)
    assert caught.value.__cause__ is None
    await client.aclose()


async def test_timeout_is_reported_without_a_retry_on_a_single_attempt() -> None:
    calls, handler = _capture([httpx.ReadTimeout("slow")])
    client = _client(handler, read_attempts=1)

    with pytest.raises(HttpTimeoutError) as caught:
        await client.read(
            "/status",
            response_type=Status,
            operation="status.read",
            correlation_id="corr-time",
        )

    assert len(calls) == 1
    assert "slow" not in str(caught.value)
    await client.aclose()


async def test_http_errors_do_not_retry_client_errors_and_do_not_echo_the_body() -> None:
    calls, handler = _capture([httpx.Response(404, json={"detail": SECRET})])
    audit_log = AuditLog()
    client = _client(handler, audit_log)

    with pytest.raises(HttpStatusError) as caught:
        await client.read(
            "/status",
            response_type=Status,
            operation="status.read",
            correlation_id="corr-404",
        )

    assert len(calls) == 1
    assert caught.value.status_code == 404
    assert SECRET not in str(caught.value)
    assert SECRET not in audit_log.events[0].model_dump_json()
    await client.aclose()


async def test_server_errors_are_retried_for_reads_only() -> None:
    calls, handler = _capture(
        [
            httpx.Response(503, json={"detail": "later"}),
            httpx.Response(200, json={"state": "open"}),
        ]
    )
    client = _client(handler)

    result = await client.read(
        "/status",
        response_type=Status,
        operation="status.read",
        correlation_id="corr-retry",
    )

    assert result.state == "open"
    assert len(calls) == 2
    await client.aclose()


async def test_credentials_do_not_appear_in_errors_or_audit_events() -> None:
    calls, handler = _capture([httpx.Response(500, json={"token": SECRET})])
    audit_log = AuditLog()
    client = _client(
        handler,
        audit_log,
        headers={"Authorization": SecretString(f"Bearer {SECRET}")},
        read_attempts=1,
    )

    with pytest.raises(HttpStatusError) as caught:
        await client.read(
            "/status",
            response_type=Status,
            operation="status.read",
            correlation_id="corr-secret",
        )

    assert calls[0].headers["authorization"] == f"Bearer {SECRET}"
    rendered = str(caught.value) + "".join(event.model_dump_json() for event in audit_log.events)
    assert SECRET not in rendered
    await client.aclose()


async def test_write_requires_approval_and_runs_once_when_approved() -> None:
    calls, handler = _capture([httpx.Response(201, json={"id": "n-1"})])
    client = _client(handler)
    body = Note(recipient="ops@example.com")

    with pytest.raises(HttpApprovalRequired):
        await client.write(
            "POST",
            "/notes",
            response_type=Created,
            operation="notes.create",
            body=body,
            decision=None,
            correlation_id="corr-wait",
        )
    assert calls == []

    with pytest.raises(HttpRejected):
        await client.write(
            "POST",
            "/notes",
            response_type=Created,
            operation="notes.create",
            body=body,
            decision=ApprovalDecision("notes.create", approved=False),
            correlation_id="corr-no",
        )
    assert calls == []

    created = await client.write(
        "POST",
        "/notes",
        response_type=Created,
        operation="notes.create",
        body=body,
        decision=ApprovalDecision("notes.create", approved=True),
        correlation_id="corr-yes",
    )

    assert created.id == "n-1"
    assert len(calls) == 1
    assert calls[0].method == "POST"
    await client.aclose()


async def test_failed_write_is_not_retried() -> None:
    calls, handler = _capture([httpx.Response(500, json={"detail": "nope"})])
    client = _client(handler, read_attempts=3)

    with pytest.raises(HttpStatusError):
        await client.write(
            "POST",
            "/notes",
            response_type=Created,
            operation="notes.create",
            body=Note(recipient="ops@example.com"),
            decision=ApprovalDecision("notes.create", approved=True),
            correlation_id="corr-once",
        )

    assert len(calls) == 1
    await client.aclose()


async def test_absolute_url_is_rejected_before_the_transport() -> None:
    calls, handler = _capture([httpx.Response(200, json={"state": "open"})])
    client = _client(handler)

    with pytest.raises(HttpPathError):
        await client.read(
            "https://evil.test/status",
            response_type=Status,
            operation="status.read",
            correlation_id="corr-path",
        )

    assert calls == []
    await client.aclose()


async def test_correlation_survives_from_workflow_to_http_audit() -> None:
    calls, handler = _capture([httpx.Response(200, json={"state": "open"})])
    audit_log = AuditLog()
    http = _client(handler, audit_log)

    async def lookup_status(reference: str) -> str:
        """Read one stored status."""
        found = await http.read(
            "/status",
            response_type=Status,
            operation="status.read",
            query=StatusQuery(reference=reference),
        )
        return found.state

    reader = create_agent(
        AgentDefinition(
            name="classifier",
            purpose="Read a status.",
            instructions="Use the lookup tool and return the state.",
            input_type=Draft,
            output_type=Status,
            provider="openai",
            model="offline-model",
            tools=(prepare_tool(lookup_status, effect=ToolEffect.READ),),
        ),
        client=ScriptedChatClient(
            [
                ChatResponse(
                    messages=[
                        Message(
                            role="assistant",
                            contents=[
                                Content.from_function_call(
                                    call_id="call-1",
                                    name="lookup_status",
                                    arguments={"reference": "A-1"},
                                )
                            ],
                        )
                    ]
                ),
                '{"state": "open"}',
            ]
        ),
        audit_log=audit_log,
    )
    reviewer = create_agent(
        AgentDefinition(
            name="reviewer",
            purpose="Repeat the status.",
            instructions="Repeat the status.",
            input_type=Status,
            output_type=Status,
            provider="openai",
            model="offline-model",
        ),
        client=ScriptedChatClient(['{"state": "open"}']),
        audit_log=audit_log,
    )
    workflow = sequential_workflow([reader, reviewer], name="http-chain")

    result = await run_workflow(workflow, envelope("corr-http", Draft(reference="A-1")))

    assert result.output is not None
    assert result.output.body["state"] == "open"
    assert calls[0].headers["x-correlation-id"] == "corr-http"
    assert {event.correlation_id for event in audit_log.events} == {"corr-http"}
    assert "status.read" in {event.tool_name for event in audit_log.events}
    await http.aclose()
