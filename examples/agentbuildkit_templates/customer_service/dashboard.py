"""Local dashboard for the customer-service team.

The page calls the existing request service. It does not approve a side effect
by any path other than ``resume_service_action``.
"""

import asyncio
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from agentbuildkit.applications import RenderedPage
from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.integrations.composio import (
    AUTH_CONFIG_MISSING,
    AUTH_CONFIG_REJECTED,
    AUTH_CONFIG_UNREADABLE,
    ComposioError,
    ComposioProviderError,
    ComposioRejected,
)
from agentbuildkit.observability import AuditEvent
from agentbuildkit.revisions import Revision, RevisionError, RevisionService
from agentbuildkit.state.models import RegistryRejected, require_argument_hash
from agentbuildkit.teams import TeamBundle
from agentbuildkit.workflows import (
    PendingRequest,
    WorkflowStateStore,
    idempotency_key,
)
from agentbuildkit_templates.customer_service.dashboard_model import (
    ALREADY_HANDLED,
    COMPLETED_RESPONSE,
    MISSING_ARGUMENTS,
    PENDING_RESPONSE,
    UNRESOLVED_RESPONSE,
    Pause,
    build_runs,
    merge_audit,
)
from agentbuildkit_templates.customer_service.dashboard_view import (
    AgentInfo,
    AssignmentInfo,
    ConnectionInfo,
    Page,
    Snapshot,
    render_page,
)
from agentbuildkit_templates.customer_service.draft_pages import maybe_revision_page
from agentbuildkit_templates.customer_service.evaluation import run_organisation_evaluation
from agentbuildkit_templates.customer_service.models import CustomerRequest, FinalOutcome
from agentbuildkit_templates.customer_service.recovery import ActionRecovery, MemoryActionRecovery
from agentbuildkit_templates.customer_service.service import (
    DECLINED_ACTION_RESPONSE,
    handle_request,
    resume_service_action,
)
from agentbuildkit_templates.customer_service.sheets import SheetDesk
from agentbuildkit_templates.customer_service.team import (
    CUSTOMER_DIAGRAM,
    Organisation,
    customer_team_bundle,
)

Evaluate = Callable[[], Coroutine[Any, Any, EvaluationReport]]
TeamFactory = Callable[[TeamBundle], Organisation]
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
STATIC_TYPES: dict[str, str] = {
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
    "fonts/instrument-sans-latin-400-normal.woff2": "font/woff2",
    "fonts/instrument-sans-latin-500-normal.woff2": "font/woff2",
    "fonts/instrument-sans-latin-600-normal.woff2": "font/woff2",
    "fonts/geist-mono-latin-400-normal.woff2": "font/woff2",
    "fonts/geist-mono-latin-500-normal.woff2": "font/woff2",
}


def read_static(name: str) -> bytes | None:
    """Read one packaged dashboard file. Unknown names and parent segments are refused."""
    if name not in STATIC_TYPES or ".." in name.split("/"):
        return None
    resource = files("agentbuildkit_templates.customer_service").joinpath("static")
    for part in name.split("/"):
        resource = resource.joinpath(part)
    try:
        return resource.read_bytes()
    except OSError:
        return None


def _security_headers(port: int) -> tuple[tuple[str, str], ...]:
    """Headers for one response.

    Chrome applies ``form-action`` to the redirect that follows a form post, and
    its console message names the form address rather than the blocked redirect.
    Connect answers with the hosted sign-in page, so https redirects are allowed.
    Posts to any other http host stay blocked.
    """
    forms = f"http://127.0.0.1:{port} http://localhost:{port} https:"
    policy = (
        "default-src 'none'; style-src 'self'; script-src 'self'; font-src 'self'; "
        f"img-src 'self'; form-action 'self' {forms}; base-uri 'none'; frame-ancestors 'none'"
    )
    return (
        ("Content-Security-Policy", policy),
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
        sheets: SheetDesk | None = None,
        revisions: RevisionService | None = None,
        team_factory: TeamFactory | None = None,
    ) -> None:
        self.organisation = organisation
        self.store = store
        self.sheets = sheets
        self.revisions = revisions
        self._team_factory = team_factory
        self.last_revision_id = ""
        self.last_built_instructions = ""
        self.team_id = store.scope
        self.title = customer_team_bundle().display_name
        self.base = ""
        self.installed: tuple[tuple[str, str], ...] = ()
        self.close_connection_store = True
        self.recovery = recovery or MemoryActionRecovery()
        self.report: EvaluationReport | None = None
        self.error = ""
        self.draft: tuple[str, str] | None = None
        self._evaluate = evaluate or run_organisation_evaluation
        organisation.audit_log.add_sink(_LedgerSink(store))
        self.outcomes = self._restored_outcomes()

    async def submit(self, reference: str, message: str) -> FinalOutcome:
        """Run one request. A side effect stays pending until a person decides."""
        team_id = self.team_id
        if self.revisions is not None and not self.revisions.runnable(team_id):
            raise RevisionError(
                "inactive",
                "This team is not active. Activate a revision before running it.",
            )
        revision = self.revisions.active(team_id) if self.revisions is not None else None
        organisation = self._for_revision(revision)
        correlation_id = uuid.uuid4().hex
        if self.revisions is not None and revision is not None:
            self.revisions.pin_run(team_id, correlation_id, "", revision.revision_id)
        result = await handle_request(
            organisation,
            CustomerRequest(reference=reference, message=message),
            correlation_id=correlation_id,
            store=self.store,
        )
        if self.revisions is not None and revision is not None and result.pending is not None:
            self.revisions.pin_run(
                team_id,
                result.outcome.correlation_id,
                result.pending.request_id,
                revision.revision_id,
            )
        pending = result.pending
        if pending is not None and result.action_arguments is not None:
            self.recovery.save_pause(
                pending.request_id,
                result.outcome.reference,
                result.action_arguments,
                team_id=team_id,
            )
        return self._remember(result.outcome, team_id=team_id)

    async def decide(self, request_id: str, *, approved: bool) -> FinalOutcome:
        """Resume one stored approval. A closed or uncertain action is not run again."""
        team_id = self.team_id
        found = self.store.pending_for(request_id)
        if found is None:
            raise KeyError(request_id)
        pending, _digest = found
        saved = self.recovery.pause_for(team_id, request_id)
        reference = saved[0] if saved is not None else ""
        arguments = saved[1] if saved is not None else None
        existing = self.store.effect_for(_effect_key(pending))
        if pending.status != "pending" or existing is not None:
            return self._remember(
                self._closed_outcome(pending, reference, existing), team_id=team_id
            )
        if approved and arguments is None:
            return self._remember(
                FinalOutcome(
                    reference=reference,
                    status="rejected",
                    specialist="taylor",
                    response=_MISSING,
                    correlation_id=pending.correlation_id,
                ),
                team_id=team_id,
            )
        revision = (
            None
            if self.revisions is None
            else self.revisions.revision_for_pending(team_id, request_id, pending.correlation_id)
        )
        outcome = await resume_service_action(
            self._for_revision(revision),
            self.store,
            pending,
            reference=reference,
            approved=approved,
            arguments=arguments if approved else None,
        )
        if outcome.response == "The service action was already attempted.":
            return self._remember(
                outcome.model_copy(update={"response": _UNRESOLVED, "status": "rejected"}),
                team_id=team_id,
            )
        return self._remember(outcome, team_id=team_id)

    def _remember(self, outcome: FinalOutcome, *, team_id: str) -> FinalOutcome:
        self.recovery.save_outcome(outcome, team_id=team_id)
        self.outcomes.append(outcome)
        return outcome

    def _restored_outcomes(self) -> list[FinalOutcome]:
        """Reopen saved outcomes and any approval the store still records."""
        restored = list(self.recovery.outcomes(self.team_id))
        for row in self.store.list_pending():
            saved = self.recovery.pause_for(self.team_id, row.request_id)
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
            saved = self.recovery.pause_for(self.team_id, row.request_id)
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
        bundle, title, installed, runnable = self._shown_bundle()
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
            team_id=self.team_id,
            title=title,
            base=self.base,
            installed=installed,
            agents=agents,
            runs=build_runs(self.outcomes, by_correlation),
            approvals=open_approvals,
            audit=merge_audit(self.store.list_ledger(), self.organisation.audit_log.events),
            report=self.report,
            error=self.error,
            draft=self.draft,
            persistent=self.persistent,
            connections=self._connections(),
            assignments=self._assignments(),
            composio_configured=self.sheets is not None and self.sheets.connect_ready,
            composio_auth_missing=(
                self.sheets is not None and self.sheets.configured and not self.sheets.connect_ready
            ),
            composio_package_missing=self.sheets is not None and self.sheets.package_missing,
            sam_mode=(
                "demo"
                if self.sheets is None
                else self.sheets.store.data_mode(self.sheets.team_id, "sam")
            ),
            diagram=CUSTOMER_DIAGRAM,
            actor_label="Specialist",
            runnable=runnable,
            activity=tuple(
                (team_id, "Active" if flag else "Draft")
                for team_id, _title, flag in (
                    () if self.revisions is None else self.revisions.roster()
                )
            ),
        )

    def page(self, path: str = "/", query: Mapping[str, str] | None = None) -> Page:
        """Render one page with its HTTP status. Secrets are not included."""
        params = query or {}
        snap = self.snapshot()
        rendered = maybe_revision_page(snap, self.revisions, path, params)
        if rendered is not None:
            return rendered
        return render_page(snap, path, params)

    def _for_revision(self, revision: Revision | None) -> Organisation:
        factory = self._team_factory
        service = self.revisions
        if revision is None or factory is None or service is None:
            return self.organisation
        self.last_revision_id = revision.revision_id
        organisation = factory(service.bundle_for(revision))
        self.last_built_instructions = organisation.alex.definition.instructions
        return organisation

    def _shown_bundle(self) -> tuple[TeamBundle, str, tuple[tuple[str, str], ...], bool]:
        if self.revisions is None:
            bundle = customer_team_bundle()
            return bundle, bundle.display_name, self.installed, True
        bundle, title, runnable = self.revisions.presentation(self.team_id)
        installed = tuple((team_id, label) for team_id, label, _runnable in self.revisions.roster())
        return bundle, title, installed, runnable

    def render(self, path: str = "/", query: Mapping[str, str] | None = None) -> str:
        """Render one page. Secrets are not included."""
        return self.page(path, query).body

    def rendered_page(
        self, path: str = "/", query: Mapping[str, str] | None = None
    ) -> RenderedPage:
        """Return the status and HTML for the shared team interface."""
        rendered = self.page(path, query)
        return RenderedPage(status=rendered.status, body=rendered.body)

    def owns_correlation(self, correlation_id: str) -> bool:
        """True when this team's history contains that run."""
        return any(item.correlation_id == correlation_id for item in self.outcomes)

    def owns_request(self, request_id: str) -> bool:
        """True when this team's store holds that approval."""
        return self.store.pending_for(request_id) is not None

    def prepare_sign_in(self, session_token: str, callback_url: str) -> tuple[str, str]:
        """Reserve a callback on the owner thread. Composio is not called."""
        if self.sheets is None:
            return "", "unconfigured"
        try:
            return self.sheets.prepare(session_token=session_token, callback_url=callback_url), ""
        except ComposioProviderError:
            return "", "unavailable"
        except ComposioError as exc:
            return "", _connection_notice(exc)

    def request_sign_in(self, user_id: str, callback_url: str) -> tuple[str, str]:
        """Fetch the hosted URL. Call this off the database owner thread."""
        if self.sheets is None:
            return "", "unconfigured"
        try:
            return self.sheets.sign_in(user_id=user_id, callback_url=callback_url), ""
        except ComposioProviderError:
            return "", "unavailable"
        except ComposioError as exc:
            return "", _connection_notice(exc)

    def start_connection(self, session_token: str, callback_url: str) -> tuple[str, str]:
        """Begin hosted sign-in. The redirect is empty when the link was not started."""
        if self.sheets is None:
            return "", "unconfigured"
        try:
            return self.sheets.begin(session_token=session_token, callback_url=callback_url), ""
        except ComposioProviderError:
            return "", "unavailable"
        except ComposioError as exc:
            return "", _connection_notice(exc)

    def finish_connection(self, session_token: str, state: str, account_id: str) -> str:
        """Validate the hosted return and save the account, or refuse it."""
        if self.sheets is None:
            return "unconfigured"
        try:
            self.sheets.complete(session_token=session_token, state=state, account_id=account_id)
        except ComposioProviderError:
            return "unavailable"
        except ComposioError:
            return "denied"
        return "connected"

    def test_connection(self, connection_id: str) -> str:
        """Confirm one saved account is active. Cell values are not read."""
        if self.sheets is None:
            return "unconfigured"
        try:
            self.sheets.test(connection_id)
        except ComposioError:
            return "unavailable"
        return "checked"

    def disconnect_connection(self, connection_id: str) -> str:
        if self.sheets is None:
            return "unconfigured"
        try:
            self.sheets.disconnect(connection_id)
        except ComposioError:
            return "denied"
        return "disconnected"

    def assign_connection(
        self,
        agent_name: str,
        connection_id: str,
        spreadsheet_id: str,
        cell_range: str,
        mode: str,
    ) -> str:
        if self.sheets is None or agent_name != "sam":
            return "denied"
        try:
            self.sheets.assign(
                agent_name,
                connection_id,
                spreadsheet_id,
                cell_range,
                mode=mode,
            )
        except ComposioRejected as exc:
            if str(exc) == "The read arguments are not allowed.":
                return "window"
            return "denied"
        except ComposioError:
            return "denied"
        return "demo" if mode == "demo" else "assigned"

    def unassign_connection(self, agent_name: str) -> str:
        if self.sheets is None or agent_name not in ROLES:
            return "denied"
        self.sheets.unassign(agent_name)
        return "unassigned"

    def _connections(self) -> tuple[ConnectionInfo, ...]:
        if self.sheets is None:
            return ()
        assigned = self.sheets.store.assignments()
        return tuple(
            ConnectionInfo(
                connection_id=item.connection_id,
                label=item.label,
                status=item.status,
                agents=tuple(
                    f"{row.team_id}/{row.agent_name}"
                    for row in assigned
                    if row.connection_id == item.connection_id
                ),
            )
            for item in self.sheets.store.connections()
        )

    def _assignments(self) -> tuple[AssignmentInfo, ...]:
        if self.sheets is None:
            return ()
        by_id = {item.connection_id: item for item in self.sheets.store.connections()}
        views: list[AssignmentInfo] = []
        for row in self.sheets.store.assignments(self.sheets.team_id):
            connection = by_id.get(row.connection_id)
            views.append(
                AssignmentInfo(
                    agent_name=row.agent_name,
                    connection_id=row.connection_id,
                    label="" if connection is None else connection.label,
                    tools=row.tools,
                    spreadsheet_id=row.spreadsheet_id,
                    cell_range=row.cell_range,
                    mode=row.mode,
                )
            )
        return tuple(views)

    def decision_target(self, request_id: str) -> str:
        """Where the page goes after a decision: the run, or the approvals list."""
        found = self.store.pending_for(request_id)
        if found is None:
            return f"{self.base}/approvals" if self.base else "/approvals"
        prefix = self.base or ""
        return f"{prefix}/runs/{found[0].correlation_id}"


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
            self._new_cookie: str | None = None
            parts = urlsplit(self.path)
            if parts.path.startswith("/static/"):
                self._static(parts.path.removeprefix("/static/"))
                return
            query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
            if parts.path == "/connections/callback":
                notice = self._finish(query)
                self._redirect(f"/connections?notice={notice}")
                return
            rendered = page(parts.path, query)
            self._send(
                rendered.status,
                rendered.body.encode(),
                "text/html; charset=utf-8",
                cache="no-store",
            )

        def do_POST(self) -> None:
            self._new_cookie = None
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
            elif path == "/connections/connect":
                location = self._connect()
            elif path == "/connections/test":
                notice = cast(
                    str,
                    on_owner(lambda: service.test_connection(_field(fields, "connection_id"))),
                )
                location = _notice_path("/connections", notice)
            elif path == "/connections/disconnect":
                notice = cast(
                    str,
                    on_owner(
                        lambda: service.disconnect_connection(_field(fields, "connection_id"))
                    ),
                )
                location = _notice_path("/connections", notice)
            elif path == "/connections/assign":
                agent = _field(fields, "agent")
                connection_id = _field(fields, "connection_id")
                spreadsheet_id = _field(fields, "spreadsheet_id")
                cell_range = _field(fields, "cell_range")
                mode = _field(fields, "mode")
                notice = cast(
                    str,
                    on_owner(
                        lambda: service.assign_connection(
                            agent, connection_id, spreadsheet_id, cell_range, mode
                        )
                    ),
                )
                target = f"/team/{agent}" if agent in ROLES else "/team"
                location = _notice_path(target, notice)
            elif path == "/connections/unassign":
                agent = _field(fields, "agent")
                notice = cast(str, on_owner(lambda: service.unassign_connection(agent)))
                target = f"/team/{agent}" if agent in ROLES else "/team"
                location = _notice_path(target, notice)
            else:
                rendered = page("/404", {})
                self._send(
                    404, rendered.body.encode(), "text/html; charset=utf-8", cache="no-store"
                )
                return
            self._redirect(location)

        def _connect(self) -> str:
            token = self._session()
            address = cast(tuple[str, int], self.server.server_address)
            host = "127.0.0.1" if address[0] in {"0.0.0.0", "::"} else address[0]
            state = secrets.token_urlsafe(32)
            callback = f"http://{host}:{address[1]}/connections/callback?state={state}"
            user_id, notice = cast(
                tuple[str, str],
                on_owner(lambda: service.prepare_sign_in(token, callback)),
            )
            if user_id:
                redirect, notice = service.request_sign_in(user_id, callback)
            else:
                redirect = ""
            if redirect.startswith("https://") and "\n" not in redirect and "\r" not in redirect:
                return redirect
            return _notice_path("/connections", notice or "denied")

        def _finish(self, query: dict[str, str]) -> str:
            token = self._session()
            notice = cast(
                str,
                on_owner(
                    lambda: service.finish_connection(
                        token,
                        query.get("state", ""),
                        query.get("connected_account_id", ""),
                    )
                ),
            )
            return notice if notice in _NOTICE_CODES else "denied"

        def _session(self) -> str:
            raw = self.headers.get("Cookie")
            if raw:
                for part in raw.split(";"):
                    key, _, value = part.strip().partition("=")
                    if key == _SESSION_COOKIE and _SESSION.fullmatch(value):
                        return value
            if self._new_cookie is None:
                self._new_cookie = secrets.token_urlsafe(32)
            return self._new_cookie

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self._cookie()
            self._security()
            self.end_headers()

        def _static(self, name: str) -> None:
            content_type = STATIC_TYPES.get(name)
            if content_type is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8", cache="no-store")
                return
            body = read_static(name)
            if body is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8", cache="no-store")
                return
            self._send(200, body, content_type, cache="max-age=300")

        def _send(self, code: int, body: bytes, content_type: str, *, cache: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self._cookie()
            self._security()
            self.end_headers()
            self.wfile.write(body)

        def _security(self) -> None:
            address = cast(tuple[str, int], self.server.server_address)
            for name, value in _security_headers(address[1]):
                self.send_header(name, value)

        def _cookie(self) -> None:
            token = getattr(self, "_new_cookie", None)
            if not isinstance(token, str):
                return
            self.send_header(
                "Set-Cookie",
                f"{_SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400",
            )

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer((host, port), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return DashboardBinding(server, loop, thread, service)


def main() -> None:
    """Open the local dashboard for every installed team."""
    from agentbuildkit_templates.office.server import main as office_main

    office_main()


async def _call(call: Callable[[], object]) -> object:
    return call()


async def _close_service(service: DashboardService) -> None:
    try:
        await service.organisation.aclose()
    finally:
        service.store.close()
        service.recovery.close()
        if service.sheets is not None and service.close_connection_store:
            service.sheets.store.close()


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


def form_field(fields: dict[str, list[str]], name: str) -> str:
    """Return one posted form value, or an empty string."""
    return _field(fields, name)


def security_headers(port: int) -> tuple[tuple[str, str], ...]:
    """Response headers for the local dashboard, including the page policy."""
    return _security_headers(port)


def _field(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name, [])
    return values[0] if values else ""


_NOTICE_CODES = frozenset(
    {
        "connected",
        "denied",
        "checked",
        "unavailable",
        "disconnected",
        "assigned",
        "demo",
        "window",
        "unassigned",
        "unconfigured",
        "exists",
        "authconfig",
        "authscopes",
        "authmeta",
    }
)


def _notice_path(path: str, notice: str) -> str:
    code = notice if notice in _NOTICE_CODES else "denied"
    return f"{path}?notice={code}"


def _connection_notice(exc: ComposioError) -> str:
    text = str(exc)
    if text == "Composio is not configured on this server.":
        return "unconfigured"
    if text == AUTH_CONFIG_MISSING:
        return "authconfig"
    if text == AUTH_CONFIG_REJECTED:
        return "authscopes"
    if text == AUTH_CONFIG_UNREADABLE or text.startswith(
        "The auth config metadata did not include "
    ):
        return "authmeta"
    if text == "A Google Sheets account is already connected.":
        return "exists"
    if text == "The connected account is not available.":
        return "unavailable"
    return "denied"


_SESSION_COOKIE = "abk_session"
_SESSION = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


if __name__ == "__main__":
    main()
