"""Cursor Cloud Agent client.

Cursor is an external engineering execution provider. Credentials stay on the
authorization header. This client calls the Cloud Agents API v1 and does not
open pull requests, choose a protected branch, or store a transcript.

The public API cannot name the branch Cursor creates when it branches
automatically. Delegation therefore asks Cursor to commit on the platform's
already authorised branch (``workOnCurrentBranch``) and refuses any other
result. v1 does not offer webhooks, so completion is observed with a later
status read rather than a held stream.
"""

import base64
import re
from collections.abc import Callable, Mapping
from typing import Literal
from urllib.parse import urlsplit

import httpx
from agent_framework import SecretString
from pydantic import BaseModel, ConfigDict

from agentbuildkit.external_execution.models import (
    ImplementationAssignment,
    ProviderBranch,
    ProviderObservation,
    ProviderPhase,
    ProviderStart,
    render_assignment,
)
from agentbuildkit.external_execution.provider import (
    ExecutionConflict,
    ExecutionContactError,
    ExecutionMalformed,
    ExecutionNotFound,
    ExecutionRejected,
)
from agentbuildkit.integrations.http import (
    HttpClient,
    HttpPayloadError,
    HttpStatusError,
    HttpTimeoutError,
)
from agentbuildkit.observability import AuditLog

CURSOR_API_BASE_URL = "https://api.cursor.com"
PROVIDER_CURSOR = "cursor"
_AGENT_ID = re.compile(r"^bc-[0-9a-fA-F-]{8,80}$")
_RUN_ID = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._:-]{0,120}$")


class CursorCredentials:
    """Cursor API key. The value leaves this object only on the authorization header."""

    def __init__(self, api_key: SecretString) -> None:
        self.api_key = api_key


class CursorExecutionProvider:
    """Cloud Agents API v1 implementation of the external execution contract."""

    name = PROVIDER_CURSOR

    def __init__(self, *, credentials: CursorCredentials, http: HttpClient) -> None:
        _assert_cursor_base_url(http.base_url)
        self._credentials = credentials
        self._http = http

    async def start(
        self,
        assignment: ImplementationAssignment,
        *,
        external_id: str,
        correlation_id: str,
    ) -> ProviderStart:
        _require_agent_id(external_id)
        repository = _Repo(
            url=_github_url(assignment.repository_url),
            startingRef=assignment.authorised_branch,
        )
        body = _CreateBody(
            prompt=_Prompt(text=self._prompt(assignment)),
            repos=[repository],
            workOnCurrentBranch=True,
            autoCreatePR=False,
            agentId=external_id,
            mode="agent",
        )
        created = await self._request(
            "POST",
            "/v1/agents",
            _CreateResponse,
            json_body=body,
            correlation_id=correlation_id,
        )
        if created.agent.id != external_id or created.run.agentId != external_id:
            raise ExecutionMalformed("External execution response is not allowed.")
        return _start_from_run(created.run, external_id)

    async def inspect(
        self,
        *,
        external_id: str,
        provider_run_id: str | None,
        correlation_id: str,
    ) -> ProviderObservation:
        _require_agent_id(external_id)
        run_id = provider_run_id
        if run_id is None:
            agent = await self._request(
                "GET",
                f"/v1/agents/{external_id}",
                _Agent,
                json_body=None,
                correlation_id=correlation_id,
            )
            if agent.id != external_id or agent.latestRunId is None:
                raise ExecutionMalformed("External execution response is not allowed.")
            run_id = agent.latestRunId
        _require_run_id(run_id)
        run = await self._request(
            "GET",
            f"/v1/agents/{external_id}/runs/{run_id}",
            _Run,
            json_body=None,
            correlation_id=correlation_id,
        )
        return _observation(run, external_id)

    async def follow_up(
        self,
        assignment: ImplementationAssignment,
        *,
        external_id: str,
        correlation_id: str,
    ) -> ProviderStart:
        _require_agent_id(external_id)
        started = await self._request(
            "POST",
            f"/v1/agents/{external_id}/runs",
            _RunResponse,
            json_body=_FollowBody(prompt=_Prompt(text=self._prompt(assignment))),
            correlation_id=correlation_id,
        )
        if started.run.agentId != external_id:
            raise ExecutionMalformed("External execution response is not allowed.")
        return _start_from_run(started.run, external_id)

    async def cancel(
        self,
        *,
        external_id: str,
        provider_run_id: str,
        correlation_id: str,
    ) -> None:
        _require_agent_id(external_id)
        _require_run_id(provider_run_id)
        cancelled = await self._request(
            "POST",
            f"/v1/agents/{external_id}/runs/{provider_run_id}/cancel",
            _CancelResponse,
            json_body=None,
            correlation_id=correlation_id,
        )
        if cancelled.id != provider_run_id:
            raise ExecutionMalformed("External execution response is not allowed.")

    def _prompt(self, assignment: ImplementationAssignment) -> str:
        text = render_assignment(assignment)
        secret = self._credentials.api_key.get_secret_value()
        if secret and secret in text:
            raise ExecutionRejected("External execution is not allowed.")
        return text

    async def _request[ResponseT: BaseModel](
        self,
        method: Literal["GET", "POST"],
        path: str,
        response_type: type[ResponseT],
        *,
        json_body: BaseModel | None,
        correlation_id: str,
    ) -> ResponseT:
        try:
            return await self._http.send(
                method,
                path,
                response_type=response_type,
                json_body=json_body,
                correlation_id=correlation_id,
            )
        except HttpStatusError as exc:
            if exc.status_code == 409:
                raise ExecutionConflict("External execution already exists.") from exc
            if exc.status_code == 404:
                raise ExecutionNotFound("External execution is unknown.") from exc
            if exc.status_code >= 500:
                raise ExecutionContactError("External execution contact was lost.") from exc
            raise ExecutionRejected("External execution did not start.") from exc
        except HttpTimeoutError as exc:
            raise ExecutionContactError("External execution contact was lost.") from exc
        except HttpPayloadError as exc:
            raise ExecutionMalformed("External execution response is not allowed.") from exc


def cursor_header_provider(
    credentials: CursorCredentials,
) -> Callable[[], Mapping[str, str | SecretString]]:
    """Build the authorization header. The API key is not returned to the caller."""

    def headers() -> dict[str, str | SecretString]:
        raw = credentials.api_key.get_secret_value()
        if not raw or any(character.isspace() for character in raw):
            raise ExecutionRejected("Cursor client is not configured.")
        encoded = base64.b64encode(f"{raw}:".encode()).decode("ascii")
        return {"Authorization": SecretString(f"Basic {encoded}")}

    return headers


def cursor_http_client(
    *,
    audit_log: AuditLog,
    header_provider: Callable[[], Mapping[str, str | SecretString]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> HttpClient:
    """Build the only HTTP client Cursor operations use."""
    return HttpClient(
        name=PROVIDER_CURSOR,
        base_url=CURSOR_API_BASE_URL,
        audit_log=audit_log,
        header_provider=header_provider,
        transport=transport,
    )


class _Prompt(BaseModel):
    text: str


class _Repo(BaseModel):
    url: str
    startingRef: str


class _CreateBody(BaseModel):
    prompt: _Prompt
    repos: list[_Repo]
    workOnCurrentBranch: Literal[True]
    autoCreatePR: Literal[False]
    agentId: str
    mode: Literal["agent"]


class _FollowBody(BaseModel):
    prompt: _Prompt


class _GitBranch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repoUrl: str
    branch: str | None = None
    prUrl: str | None = None


class _Git(BaseModel):
    model_config = ConfigDict(extra="ignore")

    branches: list[_GitBranch] = []


class _Run(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    agentId: str
    status: str
    git: _Git | None = None


class _Agent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    latestRunId: str | None = None


class _CreateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent: _Agent
    run: _Run


class _RunResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run: _Run


class _CancelResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str


def _start_from_run(run: _Run, external_id: str) -> ProviderStart:
    phase = _phase(run.status)
    _require_run_id(run.id)
    if phase == "creating":
        return ProviderStart(external_id=external_id, provider_run_id=run.id, phase="creating")
    if phase == "running":
        return ProviderStart(external_id=external_id, provider_run_id=run.id, phase="running")
    raise ExecutionMalformed("External execution response is not allowed.")


def _observation(run: _Run, external_id: str) -> ProviderObservation:
    if run.agentId != external_id:
        raise ExecutionMalformed("External execution response is not allowed.")
    _require_run_id(run.id)
    branches: list[ProviderBranch] = []
    pull_request = False
    if run.git is not None:
        for item in run.git.branches:
            if item.prUrl:
                pull_request = True
            if item.branch:
                branches.append(ProviderBranch(repository_url=item.repoUrl, branch=item.branch))
    return ProviderObservation(
        external_id=external_id,
        provider_run_id=run.id,
        phase=_phase(run.status),
        branches=tuple(branches),
        pull_request_opened=pull_request,
    )


def _phase(value: str) -> ProviderPhase:
    if value == "CREATING":
        return "creating"
    if value == "RUNNING":
        return "running"
    if value == "FINISHED":
        return "completed"
    if value == "ERROR":
        return "failed"
    if value == "CANCELLED":
        return "cancelled"
    if value == "EXPIRED":
        return "expired"
    raise ExecutionMalformed("External execution response is not allowed.")


def _github_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path
        or parsed.path == "/"
    ):
        raise ExecutionRejected("External execution is not allowed.")
    return url


def _require_agent_id(value: str) -> None:
    if _AGENT_ID.fullmatch(value) is None:
        raise ExecutionRejected("External execution is not allowed.")


def _require_run_id(value: str) -> None:
    if _RUN_ID.fullmatch(value) is None:
        raise ExecutionMalformed("External execution response is not allowed.")


def _assert_cursor_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.cursor.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ExecutionRejected("Cursor client is not configured.")
