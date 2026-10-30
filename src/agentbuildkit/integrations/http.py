"""Typed HTTP calls against one configured base URL.

The model never supplies the base URL, credentials, or a free-form URL.
Writes go through the tool approval gate before any network request.
"""

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx
from agent_framework import Content, SecretString
from pydantic import BaseModel, ValidationError

from agentbuildkit.guardrails import (
    ApprovalDecision,
    ToolEffect,
    execute_tool,
    prepare_tool,
)
from agentbuildkit.observability import AuditEvent, AuditLog, argument_hash, current_correlation_id

_READ_METHODS = frozenset({"GET", "HEAD"})
HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


class HttpError(Exception):
    """A configured HTTP call failed. The message does not include the response body."""


class HttpTimeoutError(HttpError):
    """The configured HTTP call timed out."""


class HttpStatusError(HttpError):
    """The server returned an error status."""

    def __init__(self, *, method: str, path: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {method} {path} failed with status {status_code}.")


class HttpPayloadError(HttpError):
    """The response body did not match the declared response model."""

    def __init__(self, *, method: str, path: str) -> None:
        super().__init__(f"HTTP {method} {path} returned an unexpected payload.")


class HttpPathError(HttpError):
    """The path is not a relative path on the configured base URL."""

    def __init__(self) -> None:
        super().__init__("HTTP path must stay on the configured base URL.")


class HttpApprovalRequired(HttpError):
    """A write is waiting for approval, so no request was sent."""

    def __init__(self) -> None:
        super().__init__("HTTP write is waiting for approval.")


class HttpRejected(HttpError):
    """A person rejected the write, so no request was sent."""

    def __init__(self) -> None:
        super().__init__("HTTP write was rejected.")


@dataclass(frozen=True)
class HttpTimeouts:
    """Connection and transfer limits for one configured service."""

    connect: float = 5.0
    read: float = 30.0
    write: float = 30.0
    pool: float = 5.0


class HttpClient:
    """One organisation-configured HTTP service."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        audit_log: AuditLog,
        header_provider: Callable[[], Mapping[str, str | SecretString]] | None = None,
        timeouts: HttpTimeouts | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        read_attempts: int = 2,
    ) -> None:
        if "://" not in base_url:
            raise HttpPathError()
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._audit_log = audit_log
        self._header_provider = header_provider or _no_headers
        self._read_attempts = read_attempts
        limits = timeouts or HttpTimeouts()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=limits.connect,
                read=limits.read,
                write=limits.write,
                pool=limits.pool,
            ),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def read[ResponseT: BaseModel](
        self,
        path: str,
        *,
        response_type: type[ResponseT],
        operation: str,
        query: BaseModel | None = None,
        correlation_id: str | None = None,
    ) -> ResponseT:
        """Perform a GET and validate the response model. Safe failures may be retried."""
        relative = _relative_path(path)
        resolved = _resolve_correlation_id(correlation_id)
        arguments: dict[str, Any] = {
            "path": relative,
            "query": query.model_dump() if query is not None else {},
        }
        try:
            parsed = await self._send(
                "GET",
                relative,
                response_type=response_type,
                json_body=None,
                query=arguments["query"],
                correlation_id=resolved,
            )
        except HttpError:
            _record(
                self._audit_log,
                correlation_id=resolved,
                service=self.name,
                operation=operation,
                arguments=arguments,
                approval="not_required",
                outcome="failed",
            )
            raise
        _record(
            self._audit_log,
            correlation_id=resolved,
            service=self.name,
            operation=operation,
            arguments=arguments,
            approval="not_required",
            outcome="completed",
        )
        return parsed

    async def write[ResponseT: BaseModel](
        self,
        method: Literal["POST", "PUT", "PATCH", "DELETE"],
        path: str,
        *,
        response_type: type[ResponseT],
        operation: str,
        body: BaseModel,
        decision: ApprovalDecision | None,
        correlation_id: str | None = None,
    ) -> ResponseT:
        """Perform one write after the existing approval gate allows it."""
        relative = _relative_path(path)
        resolved = _resolve_correlation_id(correlation_id)
        holder: list[ResponseT] = []

        async def perform(payload: str) -> str:
            parsed_body = _json_object(json.loads(payload))
            if parsed_body is None:
                raise HttpPayloadError(method=method, path=relative)
            holder.append(
                await self._send(
                    method,
                    relative,
                    response_type=response_type,
                    json_body=parsed_body,
                    query=None,
                    correlation_id=resolved,
                )
            )
            return "ok"

        tool = prepare_tool(
            perform,
            effect=ToolEffect.SIDE_EFFECT,
            name=operation,
            description=operation,
        )
        result = await execute_tool(
            tool,
            {"payload": body.model_dump_json()},
            audit_log=self._audit_log,
            correlation_id=resolved,
            agent_name=self.name,
            decision=decision,
        )
        if isinstance(result, Content) and result.type == "function_approval_response":
            raise HttpRejected()
        if isinstance(result, Content):
            raise HttpApprovalRequired()
        return holder[0]

    async def send[ResponseT: BaseModel](
        self,
        method: HttpMethod,
        path: str,
        *,
        response_type: type[ResponseT],
        json_body: BaseModel | None = None,
        query: BaseModel | None = None,
        correlation_id: str | None = None,
    ) -> ResponseT:
        """Send one request on the configured base URL.

        Side-effect callers must already be inside ``execute_tool``. This method
        does not apply a second approval gate.
        """
        relative = _relative_path(path)
        resolved = _resolve_correlation_id(correlation_id)
        body = None
        if json_body is not None:
            body = _json_object(json.loads(json_body.model_dump_json()))
            if body is None:
                raise HttpPayloadError(method=method, path=relative)
        return await self._send(
            method,
            relative,
            response_type=response_type,
            json_body=body,
            query=None if query is None else query.model_dump(),
            correlation_id=resolved,
        )

    async def _send[ResponseT: BaseModel](
        self,
        method: HttpMethod,
        path: str,
        *,
        response_type: type[ResponseT],
        json_body: Mapping[str, Any] | None,
        query: Mapping[str, Any] | None,
        correlation_id: str,
    ) -> ResponseT:
        attempts = self._read_attempts if method in _READ_METHODS else 1
        headers = _request_headers(self._header_provider(), correlation_id)
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method,
                    path,
                    headers=headers,
                    params=_query_params(query, method, path),
                    json=json_body,
                )
            except httpx.TimeoutException:
                if attempt + 1 >= attempts:
                    raise HttpTimeoutError(f"HTTP {method} {path} timed out.") from None
                continue
            if response.status_code >= 500 and method in _READ_METHODS and attempt + 1 < attempts:
                continue
            if response.status_code >= 400:
                raise HttpStatusError(method=method, path=path, status_code=response.status_code)
            break
        if response is None:
            raise HttpTimeoutError(f"HTTP {method} {path} timed out.")
        try:
            payload = response.json()
            return response_type.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError):
            raise HttpPayloadError(method=method, path=path) from None


def _no_headers() -> dict[str, str | SecretString]:
    return {}


def _json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, Any] = {}
    for key, item in cast(Mapping[Any, Any], value).items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed


def _query_params(query: Mapping[str, Any] | None, method: str, path: str) -> dict[str, str] | None:
    if query is None:
        return None
    params: dict[str, str] = {}
    for key, value in query.items():
        if isinstance(value, bool):
            params[key] = "true" if value else "false"
        elif isinstance(value, str | int | float):
            params[key] = str(value)
        else:
            raise HttpPayloadError(method=method, path=path)
    return params


def _relative_path(path: str) -> str:
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise HttpPathError()
    return path


def _resolve_correlation_id(correlation_id: str | None) -> str:
    return correlation_id or current_correlation_id() or uuid.uuid4().hex


def _request_headers(
    configured: Mapping[str, str | SecretString],
    correlation_id: str,
) -> dict[str, str]:
    headers = {"X-Correlation-ID": correlation_id}
    for name, value in configured.items():
        if isinstance(value, SecretString):
            headers[name] = value.get_secret_value()
        else:
            headers[name] = value
    return headers


def _record(
    audit_log: AuditLog,
    *,
    correlation_id: str,
    service: str,
    operation: str,
    arguments: Mapping[str, Any],
    approval: Literal["not_required", "pending", "approved", "rejected"],
    outcome: Literal["completed", "pending_approval", "rejected", "failed"],
) -> None:
    audit_log.record(
        AuditEvent(
            correlation_id=correlation_id,
            agent_name=service,
            tool_name=operation,
            approval=approval,
            outcome=outcome,
            argument_hash=argument_hash(arguments),
        )
    )
