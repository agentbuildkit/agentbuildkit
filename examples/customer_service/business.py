"""Fixture business API for the customer-service example.

The HTTP client talks to an in-process mock at ``http://business.test``.
It does not call a network, a CRM, or a live customer-service backend.
Reference ``A-100`` and the account name Northwind are fixture data.
Reads are allowlisted tools. The one write is a separate side-effect tool
and is not given to either specialist.
"""

import json
from typing import Any, cast

import httpx
from agent_framework import SecretString

from agentbuildkit.guardrails import ApprovalDecision, ToolEffect, prepare_tool
from agentbuildkit.integrations import HttpClient
from agentbuildkit.observability import AuditLog, current_correlation_id
from customer_service.models import (
    AccountView,
    ActionReceipt,
    ReferenceQuery,
    ServiceActionRequest,
    ServiceView,
)

# Fixture data. A-100 is not a live customer.
_ACCOUNTS = {
    "A-100": {"account_name": "Northwind", "balance": "120.00"},
}
_SERVICES = {
    "A-100": {"service_status": "open"},
}


class BusinessSystem:
    """The mock company system and the tools that may call it."""

    def __init__(
        self,
        *,
        audit_log: AuditLog,
        api_token: SecretString,
        actions: list[str] | None = None,
    ) -> None:
        self.actions = actions if actions is not None else []
        self.account_reads: list[str] = []
        self.service_reads: list[str] = []
        self.api = HttpClient(
            name="business-api",
            base_url="http://business.test",
            audit_log=audit_log,
            header_provider=lambda: {"Authorization": api_token},
            transport=_transport(self, api_token),
        )
        self.read_account = prepare_tool(
            self._read_account,
            effect=ToolEffect.READ,
            name="read_account",
            description="Read one customer account. This cannot change the account.",
        )
        self.read_service = prepare_tool(
            self._read_service,
            effect=ToolEffect.READ,
            name="read_service",
            description="Read one service record. This cannot change the record.",
        )
        self.schedule_service = prepare_tool(
            self._schedule_service,
            effect=ToolEffect.SIDE_EFFECT,
            name="schedule_service",
            description="Schedule a service visit after a person has approved it.",
        )

    async def aclose(self) -> None:
        await self.api.aclose()

    async def _read_account(self, reference: str) -> str:
        """Read one customer account. This cannot change the account."""
        found = await self.api.read(
            "/accounts",
            response_type=AccountView,
            operation="account.read",
            query=ReferenceQuery(reference=reference),
            correlation_id=current_correlation_id(),
        )
        return found.model_dump_json()

    async def _read_service(self, reference: str) -> str:
        """Read one service record. This cannot change the record."""
        found = await self.api.read(
            "/services",
            response_type=ServiceView,
            operation="service.read",
            query=ReferenceQuery(reference=reference),
            correlation_id=current_correlation_id(),
        )
        return found.model_dump_json()

    async def _schedule_service(self, reference: str, action: str) -> str:
        """Perform the service change after the workflow gate has approved it.

        ``HttpClient.write`` asks for its own decision before it sends. This
        function is invoked only by that gate, so the decision is supplied here.
        """
        receipt = await self.api.write(
            "POST",
            "/service-actions",
            response_type=ActionReceipt,
            operation="service.schedule",
            body=ServiceActionRequest(reference=reference, action=action),
            decision=ApprovalDecision("service.schedule", approved=True),
            correlation_id=current_correlation_id(),
        )
        return receipt.status


def _transport(system: BusinessSystem, api_token: SecretString) -> httpx.MockTransport:
    secret = api_token.get_secret_value()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != secret:
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "GET" and request.url.path == "/accounts":
            return _account(system, request.url.params.get("reference", ""))
        if request.method == "GET" and request.url.path == "/services":
            return _service(system, request.url.params.get("reference", ""))
        if request.method == "POST" and request.url.path == "/service-actions":
            return _schedule(system, request)
        return httpx.Response(404, json={"error": "missing"})

    return httpx.MockTransport(handle)


def _account(system: BusinessSystem, reference: str) -> httpx.Response:
    record = _ACCOUNTS.get(reference)
    if record is None:
        return httpx.Response(404, json={"error": "missing"})
    system.account_reads.append(reference)
    return httpx.Response(
        200,
        json={
            "reference": reference,
            "account_name": record["account_name"],
            "balance": record["balance"],
        },
    )


def _service(system: BusinessSystem, reference: str) -> httpx.Response:
    record = _SERVICES.get(reference)
    if record is None:
        return httpx.Response(404, json={"error": "missing"})
    system.service_reads.append(reference)
    return httpx.Response(
        200,
        json={"reference": reference, "service_status": record["service_status"]},
    )


def _schedule(system: BusinessSystem, request: httpx.Request) -> httpx.Response:
    body = _object(json.loads(request.content))
    if body is None:
        return httpx.Response(400, json={"error": "invalid"})
    reference = body.get("reference")
    action = body.get("action")
    if not isinstance(reference, str) or not isinstance(action, str):
        return httpx.Response(400, json={"error": "invalid"})
    system.actions.append(f"{reference}:{action}")
    return httpx.Response(200, json={"reference": reference, "status": "scheduled"})


def _object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, Any] = {}
    for key, item in cast(dict[Any, Any], value).items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed
