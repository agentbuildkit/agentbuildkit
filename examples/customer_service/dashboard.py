"""Local dashboard for the customer-service team.

The page calls the existing request service. It does not approve a side effect
by any path other than ``resume_service_action``.
"""

import asyncio
import threading
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs

from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.observability import AuditEvent
from agentbuildkit.state.models import RegistryRejected, require_argument_hash
from agentbuildkit.workflows import (
    LedgerRecord,
    PendingRequest,
    WorkflowStateStore,
    idempotency_key,
)
from customer_service.evaluation import run_organisation_evaluation
from customer_service.models import CustomerRequest, FinalOutcome
from customer_service.recovery import ActionRecovery, MemoryActionRecovery
from customer_service.service import handle_request, resume_service_action
from customer_service.team import Organisation, customer_team_bundle

Evaluate = Callable[[], Coroutine[Any, Any, EvaluationReport]]
_ALREADY = "The service action was already handled."
_UNRESOLVED = "The service action is unresolved and will not be repeated."
_MISSING = "The service action cannot be resumed because its arguments were not recovered."
_PENDING = "The service action is waiting for approval."
_COMPLETED = "The service action was completed."
_DECLINED = "The service action was not approved."


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

    def _open_approvals(self) -> tuple[tuple[PendingRequest, str, str], ...]:
        open_rows: list[tuple[PendingRequest, str, str]] = []
        for row in self.store.list_pending(status="pending"):
            if self.store.effect_for(_effect_key(row)) is not None:
                continue
            saved = self.recovery.pause_for(row.request_id)
            reference = "" if saved is None else saved[0]
            if saved is None:
                service_action = "arguments were not recovered"
            else:
                service_action = saved[1].get("action", "")
            open_rows.append((row, reference, service_action))
        return tuple(open_rows)

    def _audit_rows(self) -> tuple[tuple[str, str, str, str, str, str], ...]:
        rows: list[tuple[str, str, str, str, str, str]] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        combined: tuple[AuditEvent | LedgerRecord, ...] = (
            *self.store.list_ledger(),
            *self.organisation.audit_log.events,
        )
        for event in combined:
            item = _audit_identity(event)
            if item in seen:
                continue
            seen.add(item)
            rows.append(item)
        return tuple(rows)

    async def run_evaluation(self) -> EvaluationReport:
        """Run the offline suite and keep the report for the page."""
        self.report = await self._evaluate()
        return self.report

    def render(self) -> str:
        """Render the current session. Secrets are not included."""
        bundle = customer_team_bundle()
        rows: list[str] = []
        for agent in bundle.agents:
            tools = ", ".join(agent.tools) if agent.tools else "none"
            rows.append(
                "<li>"
                f"{escape(agent.name)} · {escape(agent.provider)} / {escape(agent.model)}"
                f" · tools: {escape(tools)}"
                "</li>"
            )
        pending = "".join(
            (
                "<form method='post' action='/decide'>"
                f"<input type='hidden' name='request_id' value='{escape(row.request_id)}'>"
                f"<p>{escape(reference)} · {escape(service_action)} · "
                f"{escape(row.action)} · {escape(row.correlation_id)}</p>"
                "<button name='decision' value='approve'>Approve</button>"
                "<button name='decision' value='reject'>Reject</button>"
                "</form>"
            )
            for row, reference, service_action in self._open_approvals()
        )
        outcomes = "".join(
            (
                "<li>"
                f"{escape(item.status)} · {escape(item.specialist or '-')} · "
                f"{escape(item.response)} · {escape(item.correlation_id)}"
                "</li>"
            )
            for item in self.outcomes
        )
        events = "".join(
            (
                "<li>"
                f"{escape(agent)} · {escape(tool)} · {escape(kind)} · "
                f"{escape(approval)} · {escape(outcome)} · {escape(correlation)}"
                "</li>"
            )
            for agent, tool, kind, approval, outcome, correlation in self._audit_rows()
        )
        report = "<p>No evaluation has been run.</p>"
        if self.report is not None:
            cases = "".join(
                f"<li>{escape(case.name)} · {'PASS' if case.passed else 'FAIL'}</li>"
                for case in self.report.cases
            )
            report = f"<p>{self.report.passed} of {self.report.total} passed.</p><ul>{cases}</ul>"
        notice = f"<p>{escape(self.error)}</p>" if self.error else ""
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{escape(bundle.name)}</title></head>
<body>
<h1>{escape(bundle.name)}</h1>
{notice}
<ul>{"".join(rows)}</ul>
<form method="post" action="/run">
<label>Reference <input name="reference" value="A-100"></label>
<label>Message <input name="message" required></label>
<button>Run</button>
</form>
<h2>Outcomes</h2>
<ul>{outcomes}</ul>
<h2>Pending approvals</h2>
{pending or "<p>None.</p>"}
<h2>Audit</h2>
<ul>{events}</ul>
<h2>Evaluation</h2>
<form method="post" action="/evaluate"><button>Run evaluation</button></form>
{report}
</body>
</html>
"""


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

    def invoke(awaitable: Coroutine[Any, Any, object]) -> None:
        future: Future[object] = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            future.result()
        except Exception as exc:
            service.error = describe_error(exc)

    def render() -> bytes:
        future = asyncio.run_coroutine_threadsafe(_render(service), loop)
        return future.result()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = render()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            fields = parse_qs(self.rfile.read(length).decode())
            service.error = ""
            if self.path == "/run":
                reference = _field(fields, "reference")
                message = _field(fields, "message")
                invoke(service.submit(reference, message))
            elif self.path == "/decide":
                request_id = _field(fields, "request_id")
                approved = _field(fields, "decision") == "approve"
                invoke(service.decide(request_id, approved=approved))
            elif self.path == "/evaluate":
                invoke(service.run_evaluation())
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

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


async def _render(service: DashboardService) -> bytes:
    return service.render().encode()


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


def _audit_identity(
    event: AuditEvent | LedgerRecord,
) -> tuple[str, str, str, str, str, str]:
    return (
        event.agent_name or "-",
        event.tool_name,
        event.kind,
        event.approval,
        event.outcome,
        event.correlation_id,
    )


def _field(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name, [])
    return values[0] if values else ""


if __name__ == "__main__":
    main()
