"""Local dashboard for the customer-service team.

The page calls the existing request service. It does not approve a side effect
by any path other than ``resume_service_action``.
"""

import asyncio
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.observability import AuditEvent
from agentbuildkit.state.models import RegistryRejected, require_argument_hash
from agentbuildkit.workflows import (
    PendingRequest,
    WorkflowStateStore,
    idempotency_key,
)
from customer_service.dashboard_model import (
    ALREADY_HANDLED,
    COMPLETED_RESPONSE,
    MISSING_ARGUMENTS,
    PENDING_RESPONSE,
    UNRESOLVED_RESPONSE,
    Pause,
    build_runs,
    merge_audit,
)
from customer_service.dashboard_view import AgentInfo, Page, Snapshot, render_page
from customer_service.evaluation import run_organisation_evaluation
from customer_service.models import CustomerRequest, FinalOutcome
from customer_service.recovery import ActionRecovery, MemoryActionRecovery
from customer_service.service import (
    DECLINED_ACTION_RESPONSE,
    handle_request,
    resume_service_action,
)
from customer_service.team import Organisation, customer_team_bundle

Evaluate = Callable[[], Coroutine[Any, Any, EvaluationReport]]
_ALREADY = ALREADY_HANDLED
_UNRESOLVED = UNRESOLVED_RESPONSE
_MISSING = MISSING_ARGUMENTS
_PENDING = PENDING_RESPONSE
_COMPLETED = COMPLETED_RESPONSE
_DECLINED = DECLINED_ACTION_RESPONSE
_NOT_FOUND = "That approval could not be found. It may already have been decided."

# Roles come from the example's workflow, not from team.json.
ROLES: dict[str, tuple[str, str]] = {
    "alex": ("Manager", "Classifies each request as billing or service and names a specialist."),
    "sam": (
        "Account specialist",
        "Answers account questions from the account record. Cannot change data.",
    ),
    "taylor": (
        "Service specialist",
        "Reads the service record and may propose one service action.",
    ),
    "jordan": (
        "Reviewer",
        "Approves, rejects or escalates the draft before the customer sees it.",
    ),
}
STATIC_DIRECTORY = Path(__file__).with_name("static")
_STATIC_TYPES: dict[str, str] = {
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
    "fonts/instrument-sans-latin-400-normal.woff2": "font/woff2",
    "fonts/instrument-sans-latin-500-normal.woff2": "font/woff2",
    "fonts/instrument-sans-latin-600-normal.woff2": "font/woff2",
    "fonts/geist-mono-latin-400-normal.woff2": "font/woff2",
    "fonts/geist-mono-latin-500-normal.woff2": "font/woff2",
}
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'self'; script-src 'self'; font-src 'self'; "
        "img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)


class DashboardService:
    """One local session over an already built team and workflow store."""

    def __init__(
        self,
        organisation: Organisation,
        store: WorkflowStateStore,
        *,
        recovery: ActionRecovery | None = None,
        evaluate: Evaluate | None = None,
    ) -> None:
        self.organisation = organisation
        self.store = store
        self.recovery = recovery or MemoryActionRecovery()
        self.report: EvaluationReport | None = None
        self.error = ""
        self.draft: tuple[str, str] | None = None
        self._evaluate = evaluate or run_organisation_evaluation
        organisation.audit_log.add_sink(_LedgerSink(store))
        self.outcomes = self._restored_outcomes()

    async def submit(self, reference: str, message: str) -> FinalOutcome:
        """Run one request. A side effect stays pending until a person decides."""
        result = await handle_request(
            self.organisation,
            CustomerRequest(reference=reference, message=message),
            correlation_id=uuid.uuid4().hex,
            store=self.store,
        )
        pending = result.pending
        if pending is not None and result.action_arguments is not None:
            self.recovery.save_pause(
                pending.request_id,
                result.outcome.reference,
                result.action_arguments,
            )
        return self._remember(result.outcome)

    async def decide(self, request_id: str, *, approved: bool) -> FinalOutcome:
        """Resume one stored approval. A closed or uncertain action is not run again."""
        found = self.store.pending_for(request_id)
        if found is None:
            raise KeyError(request_id)
        pending, _digest = found
        saved = self.recovery.pause_for(request_id)
        reference = saved[0] if saved is not None else ""
        arguments = saved[1] if saved is not None else None
        existing = self.store.effect_for(_effect_key(pending))
        if pending.status != "pending" or existing is not None:
            return self._remember(self._closed_outcome(pending, reference, existing))
        if approved and arguments is None:
            return self._remember(
                FinalOutcome(
                    reference=reference,
                    status="rejected",
                    specialist="taylor",
                    response=_MISSING,
                    correlation_id=pending.correlation_id,
                )
            )
        outcome = await resume_service_action(
            self.organisation,
            self.store,
            pending,
            reference=reference,
            approved=approved,
            arguments=arguments if approved else None,
        )
        if outcome.response == "The service action was already attempted.":
            return self._remember(
                outcome.model_copy(update={"response": _UNRESOLVED, "status": "rejected"})
            )
        return self._remember(outcome)

    def _remember(self, outcome: FinalOutcome) -> FinalOutcome:
        self.recovery.save_outcome(outcome)
        self.outcomes.append(outcome)
        return outcome

    def _restored_outcomes(self) -> list[FinalOutcome]:
        """Reopen saved outcomes and any approval the store still records."""
        restored = list(self.recovery.outcomes())
        for row in self.store.list_pending():
            saved = self.recovery.pause_for(row.request_id)
            reference = saved[0] if saved is not None else ""
            existing = self.store.effect_for(_effect_key(row))
            outcome = _stored_outcome(row, reference, existing)
            if _same_outcome(restored, outcome):
                continue
            restored.append(outcome)
        return restored

    def _closed_outcome(
        self,
        pending: PendingRequest,
        reference: str,
        existing: object,
    ) -> FinalOutcome:
        effect = _effect_status(existing)
        if pending.status == "failed" or effect in {"started", "failed"}:
            response = _UNRESOLVED
            status = "rejected"
        elif pending.status == "completed" or effect == "completed":
            response = _ALREADY
            status = "completed"
        else:
            response = _ALREADY
            status = "rejected"
        return FinalOutcome(
            reference=reference,
            status=status,
            specialist="taylor",
            response=response,
            correlation_id=pending.correlation_id,
        )

    async def run_evaluation(self) -> EvaluationReport:
        """Run the offline suite and keep the report for the page."""
        self.report = await self._evaluate()
        return self.report

    @property
    def persistent(self) -> bool:
        """True when approvals and outcomes are kept outside this process."""
        return not isinstance(self.recovery, MemoryActionRecovery)

    def _pauses(self) -> tuple[Pause, ...]:
        pauses: list[Pause] = []
        for row in self.store.list_pending():
            saved = self.recovery.pause_for(row.request_id)
            pauses.append(
                Pause(
                    request=row,
                    effect=_effect_status(self.store.effect_for(_effect_key(row))),
                    reference="" if saved is None else saved[0],
                    arguments=None if saved is None else saved[1],
                )
            )
        return tuple(pauses)

    def snapshot(self) -> Snapshot:
        """Read the current state for a page. Nothing is changed or run."""
        bundle = customer_team_bundle()
        agents = tuple(
            AgentInfo(
                name=agent.name,
                role=ROLES.get(agent.name, ("Agent", agent.purpose))[0],
                job=ROLES.get(agent.name, ("Agent", agent.purpose))[1],
                provider=agent.provider,
                model=agent.model,
                input_schema=agent.input_schema,
                output_schema=agent.output_schema,
                tools=tuple(agent.tools),
                instructions=agent.instructions,
            )
            for agent in bundle.agents
        )
        pauses = self._pauses()
        by_correlation = {pause.request.correlation_id: pause for pause in pauses}
        open_approvals = tuple(
            pause for pause in pauses if pause.request.status == "pending" and pause.effect is None
        )
        return Snapshot(
            team_name=bundle.name,
            agents=agents,
            runs=build_runs(self.outcomes, by_correlation),
            approvals=open_approvals,
            audit=merge_audit(self.store.list_ledger(), self.organisation.audit_log.events),
            report=self.report,
            error=self.error,
            draft=self.draft,
            persistent=self.persistent,
        )

    def page(self, path: str = "/", query: Mapping[str, str] | None = None) -> Page:
        """Render one page with its HTTP status. Secrets are not included."""
        return render_page(self.snapshot(), path, query)

    def render(self, path: str = "/", query: Mapping[str, str] | None = None) -> str:
        """Render one page. Secrets are not included."""
        return self.page(path, query).body

    def decision_target(self, request_id: str) -> str:
        """Where the page goes after a decision: the run, or the approvals list."""
        found = self.store.pending_for(request_id)
        if found is None:
            return "/approvals"
        return f"/runs/{found[0].correlation_id}"


def describe_error(error: BaseException) -> str:
    """Describe known provider failures without exposing exception payloads."""
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
    )

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, AuthenticationError):
            return "OpenAI authentication failed. Check the API key configured for this dashboard."
        if isinstance(current, RateLimitError):
            if current.code == "insufficient_quota":
                return "OpenAI API quota is exhausted. Check the API project's billing and credits."
            return "OpenAI limited this request. Check API limits and quota before retrying."
        if isinstance(current, PermissionDeniedError):
            return "OpenAI denied access. Check the API project's model permissions."
        if isinstance(current, NotFoundError):
            return "OpenAI resource not found. Check model configuration and access."
        if isinstance(current, BadRequestError):
            return "OpenAI rejected the request format. Check the model options and schema."
        if isinstance(current, APITimeoutError):
            return "The OpenAI request timed out. Check connectivity and retry."
        if isinstance(current, APIConnectionError):
            return "Cannot connect to OpenAI. Check network, proxy, and certificate settings."
        if isinstance(current, APIStatusError):
            return "OpenAI returned an unexpected HTTP error. Check provider availability."
        current = current.__cause__ or current.__context__
    return "The request failed for an unclassified reason. No raw error details are displayed."


class DashboardBinding:
    """HTTP server whose database work stays on one owner thread."""

    def __init__(
        self,
        http: ThreadingHTTPServer,
        loop: asyncio.AbstractEventLoop,
        thread: threading.Thread,
        service: DashboardService,
    ) -> None:
        self._http = http
        self._loop = loop
        self._thread = thread
        self.service = service

    @property
    def server_address(self) -> tuple[str, int]:
        return cast(tuple[str, int], self._http.server_address)

    def shutdown(self) -> None:
        """Stop HTTP, then close databases on the thread that opened them."""
        self._http.shutdown()
        self._http.server_close()
        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_close_service(self.service), self._loop)
            future.result()
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def server_close(self) -> None:
        self._http.server_close()


def serve(
    source: DashboardService | Callable[[], DashboardService],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> DashboardBinding:
    """Serve the dashboard.

    A factory runs on the database owner thread, so SQLite connections are
    created, used, and closed there. HTTP threads only dispatch work to it.
    """
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    started: list[DashboardService] = []
    failed: list[BaseException] = []

    def owner() -> None:
        asyncio.set_event_loop(loop)

        async def open_service() -> None:
            try:
                if isinstance(source, DashboardService):
                    started.append(source)
                else:
                    started.append(source())
            except BaseException as exc:
                failed.append(exc)
            ready.set()

        loop.call_soon(lambda: loop.create_task(open_service()))
        loop.run_forever()

    thread = threading.Thread(target=owner, name="dashboard-state", daemon=True)
    thread.start()
    ready.wait()
    if failed:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        raise failed[0]
    service = started[0]

    def invoke(awaitable: Coroutine[Any, Any, object]) -> object | None:
        future: Future[object] = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result()
        except KeyError:
            service.error = _NOT_FOUND
        except Exception as exc:
            service.error = describe_error(exc)
        return None

    def on_owner(call: Callable[[], object]) -> object:
        future = asyncio.run_coroutine_threadsafe(_call(call), loop)
        return future.result()

    def page(path: str, query: dict[str, str]) -> Page:
        result = on_owner(lambda: service.page(path, query))
        return cast(Page, result)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path.startswith("/static/"):
                self._static(parts.path.removeprefix("/static/"))
                return
            query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
            rendered = page(parts.path, query)
            self._send(
                rendered.status,
                rendered.body.encode(),
                "text/html; charset=utf-8",
                cache="no-store",
            )

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            fields = parse_qs(self.rfile.read(length).decode())
            path = urlsplit(self.path).path
            service.error = ""
            if path == "/run":
                reference = _field(fields, "reference")
                message = _field(fields, "message")
                service.draft = None
                outcome = invoke(service.submit(reference, message))
                if isinstance(outcome, FinalOutcome):
                    location = f"/runs/{outcome.correlation_id}"
                else:
                    service.draft = (reference, message)
                    location = "/?new=1"
            elif path == "/decide":
                request_id = _field(fields, "request_id")
                approved = _field(fields, "decision") == "approve"
                location = cast(str, on_owner(lambda: service.decision_target(request_id)))
                invoke(service.decide(request_id, approved=approved))
            elif path == "/evaluate":
                invoke(service.run_evaluation())
                location = "/evaluation"
            else:
                rendered = page("/404", {})
                self._send(
                    404, rendered.body.encode(), "text/html; charset=utf-8", cache="no-store"
                )
                return
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _static(self, name: str) -> None:
            content_type = _STATIC_TYPES.get(name)
            if content_type is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8", cache="no-store")
                return
            body = (STATIC_DIRECTORY / name).read_bytes()
            self._send(200, body, content_type, cache="max-age=300")

        def _send(self, code: int, body: bytes, content_type: str, *, cache: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for name, value in _SECURITY_HEADERS:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer((host, port), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return DashboardBinding(server, loop, thread, service)


def main() -> None:
    """Open the dashboard for a live team. Offline tests construct the service."""
    from pathlib import Path

    from agent_framework import SecretString

    from agentbuildkit.observability import AuditLog
    from customer_service.cli import open_workflow_store, state_directory
    from customer_service.evaluation import LOCAL_MOCK_TOKEN
    from customer_service.recovery import SqliteActionRecovery
    from customer_service.team import ORGANISATION_SCOPE, build_team, production_clients

    env_file = Path.cwd() / ".env"

    def open_dashboard() -> DashboardService:
        clients = production_clients(env_file_path=str(env_file) if env_file.is_file() else None)
        audit_log = AuditLog()
        organisation = build_team(
            clients=clients,
            api_token=SecretString(LOCAL_MOCK_TOKEN),
            audit_log=audit_log,
        )
        directory = state_directory(env_file if env_file.is_file() else None)
        store = open_workflow_store(audit_log, directory)
        recovery = None
        if directory is not None:
            recovery = SqliteActionRecovery(Path(directory) / "action-recovery.sqlite")
        if store.scope != ORGANISATION_SCOPE:
            raise RuntimeError("The dashboard store scope does not match the team.")
        return DashboardService(organisation, store, recovery=recovery)

    binding = serve(open_dashboard)
    address = binding.server_address
    print(f"Customer-service dashboard at http://{address[0]}:{address[1]}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        binding.shutdown()


async def _call(call: Callable[[], object]) -> object:
    return call()


async def _close_service(service: DashboardService) -> None:
    try:
        await service.organisation.aclose()
    finally:
        service.store.close()
        service.recovery.close()


class _LedgerSink:
    """Copy ledger-compatible audit events. Argument values are not accepted."""

    def __init__(self, store: WorkflowStateStore) -> None:
        self._store = store

    def append_ledger(
        self,
        event: AuditEvent,
        *,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> None:
        try:
            require_argument_hash(event.argument_hash)
        except RegistryRejected:
            return
        self._store.append_ledger(event, run_id=run_id, event_id=event_id)


def _effect_key(pending: PendingRequest) -> str:
    return idempotency_key(
        pending.workflow_name,
        pending.correlation_id,
        pending.request_id,
        pending.action,
    )


def _effect_status(existing: object) -> str | None:
    status = getattr(existing, "status", None)
    if not isinstance(status, str):
        return None
    return status


def _stored_outcome(
    row: PendingRequest,
    reference: str,
    existing: object,
) -> FinalOutcome:
    effect = _effect_status(existing)
    if row.status == "completed" or effect == "completed":
        status = "completed"
        response = _COMPLETED
    elif row.status == "rejected" or effect == "rejected":
        status = "rejected"
        response = _DECLINED
    elif row.status == "failed" or effect in {"started", "failed"}:
        status = "rejected"
        response = _UNRESOLVED
    else:
        status = "pending_approval"
        response = _PENDING
    return FinalOutcome(
        reference=reference,
        status=status,
        specialist="taylor",
        response=response,
        correlation_id=row.correlation_id,
    )


def _same_outcome(outcomes: list[FinalOutcome], outcome: FinalOutcome) -> bool:
    return any(
        item.correlation_id == outcome.correlation_id and item.status == outcome.status
        for item in outcomes
    )


def _field(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name, [])
    return values[0] if values else ""


if __name__ == "__main__":
    main()
