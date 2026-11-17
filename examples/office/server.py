"""Local dashboard for the installed teams.

A URL names a team so the page can be rendered. Ownership of a run or an
approval is checked against that team's store. The selector is not the check.
"""

import argparse
import asyncio
import re
import secrets
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from agentbuildkit.applications import RenderedPage
from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.revisions import PreparedEvaluation, RevisionError
from customer_service.dashboard import (
    STATIC_DIRECTORY,
    STATIC_TYPES,
    describe_error,
    form_field,
    security_headers,
)
from customer_service.draft_pages import create_team_page
from customer_service.models import FinalOutcome
from office.installation import Installation, build_installation
from office.reporting import ReportingDashboard
from office.templates import installed_evaluators
from operations_reporting.jobs import parse_as_of

_TEAM_PATH = re.compile(r"^/teams/(?P<team>[a-z0-9]+(?:-[a-z0-9]+)*)(?P<rest>/.*)?$")
_RUN_PATH = re.compile(r"^/runs/(?P<correlation>[A-Za-z0-9]+)$")
_LEGACY = frozenset({"/", "/runs", "/approvals", "/audit", "/evaluation", "/team", "/connections"})
_SESSION_COOKIE = "abk_session"
_SESSION = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class OfficeBinding:
    """HTTP server whose database work stays on one owner thread."""

    def __init__(
        self,
        http: ThreadingHTTPServer,
        loop: asyncio.AbstractEventLoop,
        thread: threading.Thread,
        installation: Installation,
    ) -> None:
        self._http = http
        self._loop = loop
        self._thread = thread
        self.installation = installation

    @property
    def server_address(self) -> tuple[str, int]:
        return cast(tuple[str, int], self._http.server_address)

    def shutdown(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_close(self.installation), self._loop)
            future.result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def serve_installation(
    source: Installation | Callable[[], Installation],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> OfficeBinding:
    """Serve both teams.

    A factory runs on the database owner thread, so SQLite files are opened,
    used, and closed there. HTTP threads only dispatch work to that thread.
    """
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    started: list[Installation] = []
    failed: list[BaseException] = []

    def run_loop() -> None:
        asyncio.set_event_loop(loop)

        async def open_installation() -> None:
            try:
                if isinstance(source, Installation):
                    started.append(source)
                else:
                    started.append(source())
            except BaseException as exc:
                failed.append(exc)
            ready.set()

        loop.call_soon(lambda: loop.create_task(open_installation()))
        loop.run_forever()

    thread = threading.Thread(target=run_loop, name="office-state", daemon=True)
    thread.start()
    ready.wait()
    if failed:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        raise failed[0]
    installation = started[0]

    def invoke(awaitable: Coroutine[Any, Any, object]) -> object | None:
        future: Future[object] = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result()
        except KeyError:
            return None
        except Exception as exc:
            installation.customer.error = describe_error(exc)
            installation.reporting.error = describe_error(exc)
            return exc

    def on_owner(call: Callable[[], object]) -> object:
        future = asyncio.run_coroutine_threadsafe(_call(call), loop)
        return future.result()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._new_cookie: str | None = None
            parts = urlsplit(self.path)
            if parts.path.startswith("/static/"):
                name = parts.path.removeprefix("/static/")
                content_type = STATIC_TYPES.get(name)
                if content_type is None:
                    self._send(404, b"Not found", "text/plain; charset=utf-8")
                    return
                self._send(200, (STATIC_DIRECTORY / name).read_bytes(), content_type)
                return
            query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
            path_only = parts.path
            if path_only == "/connections/callback":
                notice = cast(
                    str,
                    on_owner(
                        lambda: installation.customer.finish_connection(
                            self._session(),
                            query.get("state", ""),
                            query.get("connected_account_id", ""),
                        )
                    ),
                )
                self._redirect(
                    f"/teams/{installation.customer.team_id}/connections?notice={notice}"
                )
                return
            if _RUN_PATH.fullmatch(path_only):
                target = cast(
                    str | None,
                    on_owner(lambda: _legacy_target(installation, path_only)),
                )
            else:
                target = _legacy_target(installation, path_only)
            if target is not None:
                self._redirect(target)
                return
            rendered = cast(RenderedPage, on_owner(lambda: _render(installation, path_only, query)))
            self._send(rendered.status, rendered.body.encode(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            self._new_cookie = None
            length = int(self.headers.get("Content-Length", "0"))
            fields = parse_qs(self.rfile.read(length).decode())
            path = urlsplit(self.path).path
            if path == "/teams/new":
                location = cast(str, on_owner(lambda: _create_team(installation, fields)))
                self._redirect(location)
                return
            if path.endswith("/connections/connect"):
                self._redirect(self._connect(path))
                return
            match = _TEAM_PATH.fullmatch(path)
            if match is not None:
                team_key = match.group("team")
                on_owner(lambda team_id=team_key: installation.team_for(team_id))
            rest = (match.group("rest") if match else "") or "/"
            if _draft_rest(rest) and rest.endswith("/evaluate"):
                location = _evaluate_draft(installation, path, fields, invoke, on_owner)
            elif _draft_rest(rest):
                location = cast(
                    str,
                    on_owner(lambda: _draft_post(installation, path, fields)),
                )
            elif rest in {"/run", "/decide", "/evaluate"}:
                location = _post(installation, path, fields, invoke, on_owner)
            else:
                location = cast(
                    str,
                    on_owner(lambda: _post(installation, path, fields, invoke, _inline)),
                )
            self._redirect(location)

        def _connect(self, path: str) -> str:
            match = _TEAM_PATH.fullmatch(path)
            team_id = installation.customer.team_id if match is None else match.group("team")
            token = self._session()
            address = cast(tuple[str, int], self.server.server_address)
            host = "127.0.0.1" if address[0] in {"0.0.0.0", "::"} else address[0]
            state = secrets.token_urlsafe(32)
            callback = f"http://{host}:{address[1]}/connections/callback?state={state}"
            user_id, notice = cast(
                tuple[str, str],
                on_owner(lambda: installation.customer.prepare_sign_in(token, callback)),
            )
            redirect = ""
            if user_id:
                redirect, notice = installation.customer.request_sign_in(user_id, callback)
            if redirect.startswith("https://") and "\n" not in redirect and "\r" not in redirect:
                return redirect
            return f"/teams/{team_id}/connections?notice={notice or 'denied'}"

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

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cookie()
            self._security()
            self.end_headers()
            self.wfile.write(body)

        def _cookie(self) -> None:
            token = getattr(self, "_new_cookie", None)
            if not isinstance(token, str):
                return
            self.send_header(
                "Set-Cookie",
                f"{_SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400",
            )

        def _security(self) -> None:
            address = cast(tuple[str, int], self.server.server_address)
            for name, value in security_headers(address[1]):
                self.send_header(name, value)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer((host, port), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return OfficeBinding(server, loop, thread, installation)


def _legacy_target(installation: Installation, path: str) -> str | None:
    match = _RUN_PATH.fullmatch(path)
    if match is not None:
        owner = installation.owner_of_correlation(match.group("correlation"))
        if owner is None:
            return None
        return f"/teams/{owner}/runs/{match.group('correlation')}"
    if path in _LEGACY or path.startswith("/team/"):
        return f"/teams/{installation.customer.team_id}{path if path != '/' else '/'}"
    return None


def _render(installation: Installation, path: str, query: dict[str, str]) -> RenderedPage:
    if path == "/teams/new":
        page = create_team_page(
            installation.customer.snapshot(),
            installation.revisions.templates(),
            query,
        )
        return RenderedPage(status=page.status, body=page.body)
    match = _TEAM_PATH.fullmatch(path)
    if match is None:
        return installation.customer.rendered_page("/404")
    team = installation.team_for(match.group("team"))
    if team is None:
        return installation.customer.rendered_page("/404")
    rest = match.group("rest") or "/"
    if rest != "/" and rest.endswith("/") and rest != "/":
        rest = rest.rstrip("/")
    return team.rendered_page(rest, query)


def _inline(call: Callable[[], object]) -> object:
    return call()


def _post(
    installation: Installation,
    path: str,
    fields: dict[str, list[str]],
    invoke: Callable[[Coroutine[Any, Any, object]], object | None],
    on_owner: Callable[[Callable[[], object]], object],
) -> str:
    match = _TEAM_PATH.fullmatch(path)
    if match is None:
        return f"/teams/{installation.customer.team_id}/"
    team_id = match.group("team")
    team = installation.team(team_id)
    if team is None:
        return f"/teams/{installation.customer.team_id}/"
    posted = form_field(fields, "team_id")
    if posted and posted != team_id:
        team.error = "That request belongs to another team."
        return f"/teams/{team_id}/"
    rest = match.group("rest") or "/"
    if rest == "/run":
        return _run(installation, team, fields, invoke, on_owner)
    if rest == "/decide":
        return _decide(installation, team, fields, invoke, on_owner)
    if rest == "/evaluate":
        invoke(team.run_evaluation())
        return f"/teams/{team_id}/evaluation"
    if rest == "/connections/test":
        notice = installation.customer.test_connection(form_field(fields, "connection_id"))
        return f"/teams/{team_id}/connections?notice={notice}"
    if rest == "/connections/disconnect":
        notice = installation.customer.disconnect_connection(form_field(fields, "connection_id"))
        return f"/teams/{team_id}/connections?notice={notice}"
    if rest == "/connections/assign":
        return _assign(team, fields)
    if rest == "/connections/unassign":
        notice = _unassign(team, form_field(fields, "agent"))
        return f"/teams/{team_id}/team/{form_field(fields, 'agent')}?notice={notice}"
    return f"/teams/{team_id}/"


def _create_team(installation: Installation, fields: dict[str, list[str]]) -> str:
    try:
        instance, draft = installation.revisions.create_instance(
            template_id=form_field(fields, "template_id"),
            title=form_field(fields, "title"),
            description=form_field(fields, "description"),
            idempotency_key=form_field(fields, "idempotency_key"),
        )
        installation.open_instance(instance.team_id)
    except RevisionError as exc:
        return f"/teams/new?notice={exc.code}"
    if draft is None:
        return f"/teams/{instance.team_id}/drafts"
    return f"/teams/{instance.team_id}/drafts/{draft.draft_id}"


def _run(
    installation: Installation,
    team: Any,
    fields: dict[str, list[str]],
    invoke: Callable[[Coroutine[Any, Any, object]], object | None],
    on_owner: Callable[[Callable[[], object]], object],
) -> str:
    runnable = on_owner(lambda: installation.revisions.runnable(team.team_id))
    if not runnable:
        team.error = "This team is not active. Activate a revision before running it."
        return f"/teams/{team.team_id}/"
    if isinstance(team, ReportingDashboard):
        as_of = form_field(fields, "as_of")
        note = form_field(fields, "note")
        try:
            parse_as_of(as_of)
        except ValueError as exc:
            team.error = str(exc)
            team.draft = (as_of, note)
            return f"/teams/{team.team_id}/?new=1"
        outcome = invoke(team.submit(as_of, note))
    else:
        reference = form_field(fields, "reference")
        message = form_field(fields, "message")
        team.draft = None
        outcome = invoke(team.submit(reference, message))
        if not isinstance(outcome, FinalOutcome):
            team.draft = (reference, message)
            return f"/teams/{team.team_id}/?new=1"
    if isinstance(outcome, FinalOutcome):
        return f"/teams/{team.team_id}/runs/{outcome.correlation_id}"
    return f"/teams/{team.team_id}/?new=1"


def _decide(
    installation: Installation,
    team: Any,
    fields: dict[str, list[str]],
    invoke: Callable[[Coroutine[Any, Any, object]], object | None],
    on_owner: Callable[[Callable[[], object]], object],
) -> str:
    request_id = form_field(fields, "request_id")
    owner = on_owner(lambda: installation.owner_of_request(request_id))
    if owner != team.team_id:
        team.error = "That approval belongs to another team."
        return f"/teams/{team.team_id}/approvals"
    approved = form_field(fields, "decision") == "approve"
    invoke(team.decide(request_id, approved=approved))
    return f"/teams/{team.team_id}/approvals"


def _assign(team: Any, fields: dict[str, list[str]]) -> str:
    from agentbuildkit.integrations.composio import ComposioError, ComposioRejected

    agent = form_field(fields, "agent")
    mode = form_field(fields, "mode")
    try:
        if isinstance(team, ReportingDashboard):
            team.window.desk.assign(
                agent,
                form_field(fields, "connection_id"),
                form_field(fields, "spreadsheet_id"),
                form_field(fields, "cell_range"),
                mode=mode,
            )
            notice = "demo" if mode == "demo" else "assigned"
        else:
            notice = str(
                team.assign_connection(
                    agent,
                    form_field(fields, "connection_id"),
                    form_field(fields, "spreadsheet_id"),
                    form_field(fields, "cell_range"),
                    mode,
                )
            )
    except ComposioRejected as exc:
        notice = "window" if str(exc) == "The read arguments are not allowed." else "denied"
    except ComposioError:
        notice = "denied"
    return f"/teams/{team.team_id}/team/{agent}?notice={notice}"


def _draft_rest(rest: str) -> bool:
    return rest == "/drafts" or rest.startswith("/drafts/") or rest.startswith("/revisions/")


def _draft_post(installation: Installation, path: str, fields: dict[str, list[str]]) -> str:
    """Save, validate, activate, discard, or roll back. Evaluation is separate."""
    team_id, rest = _team_rest(path)
    service = installation.revisions
    posted = form_field(fields, "team_id")
    if posted and posted != team_id:
        return f"/teams/{team_id}/drafts?notice=forbidden"
    try:
        if rest == "/drafts":
            draft = service.create_draft(team_id)
            return f"/teams/{team_id}/drafts/{draft.draft_id}"
        if rest == "/drafts/reset":
            draft = service.reset_from_template(team_id, _version(fields))
            return f"/teams/{team_id}/drafts/{draft.draft_id}?notice=reset"
        if rest.startswith("/revisions/") and rest.endswith("/rollback"):
            revision_id = rest.removeprefix("/revisions/").removesuffix("/rollback")
            service.rollback(team_id, revision_id)
            return f"/teams/{team_id}/revisions?notice=rolledback"
        draft_id, action = _draft_action(rest)
        if action == "discard":
            service.discard_draft(team_id, draft_id)
            return f"/teams/{team_id}/drafts?notice=discarded"
        version = _version(fields)
        if action == "validate":
            service.validate(team_id, draft_id, version)
            return f"/teams/{team_id}/drafts/{draft_id}?notice=validated"
        if action == "activate":
            service.activate(team_id, draft_id, version, form_field(fields, "expected_hash"))
            return f"/teams/{team_id}/revisions?notice=activated"
        service.update_draft(team_id, draft_id, version, _payload(service, team_id, fields))
        return f"/teams/{team_id}/drafts/{draft_id}?notice=saved"
    except RevisionError as exc:
        return _draft_target(team_id, rest, exc.code)


def _evaluate_draft(
    installation: Installation,
    path: str,
    fields: dict[str, list[str]],
    invoke: Callable[[Coroutine[Any, Any, object]], object | None],
    on_owner: Callable[[Callable[[], object]], object],
) -> str:
    """Validate on the owner thread, run the scripted suite, then store the hash."""
    team_id, rest = _team_rest(path)
    draft_id, _action = _draft_action(rest)
    version = _version(fields)
    try:
        prepared = cast(
            PreparedEvaluation,
            on_owner(lambda: installation.revisions.prepare_evaluation(team_id, draft_id, version)),
        )
    except RevisionError as exc:
        return _draft_target(team_id, rest, exc.code)

    def _template_id() -> str:
        found = installation.revisions.instance(team_id)
        return "" if found is None else found.template_id

    template_id = cast(str, on_owner(_template_id))
    evaluator = installed_evaluators().get(template_id)
    pending = None if evaluator is None else evaluator(prepared.bundle)
    report = None if pending is None else invoke(cast(Coroutine[Any, Any, object], pending))
    if not isinstance(report, EvaluationReport):
        return _draft_target(team_id, rest, "failed")
    try:
        on_owner(
            lambda: installation.revisions.record_evaluation(
                team_id,
                draft_id,
                prepared.version,
                prepared.content_hash,
                report,
            )
        )
    except RevisionError as exc:
        return _draft_target(team_id, rest, exc.code)
    return f"/teams/{team_id}/drafts/{draft_id}?notice=evaluated"


def _payload(service: Any, team_id: str, fields: dict[str, list[str]]) -> dict[str, object]:
    template = service.template(team_id)
    agents: list[dict[str, object]] = []
    for contract in template.agents:
        tools = [
            tool
            for tool in contract.permitted_tools
            if tool in contract.required_tools
            or form_field(fields, f"tool-{contract.name}-{tool}") == "on"
        ]
        extra = form_field(fields, f"tools-{contract.name}")
        if extra:
            tools.extend(part for part in extra.split(",") if part and part not in tools)
        agents.append(
            {
                "name": contract.name,
                "purpose": form_field(fields, f"purpose-{contract.name}"),
                "instructions": form_field(fields, f"instructions-{contract.name}"),
                "provider": form_field(fields, f"provider-{contract.name}"),
                "model": form_field(fields, f"model-{contract.name}"),
                "tools": tools,
            }
        )
    return {
        "title": form_field(fields, "title"),
        "description": form_field(fields, "description"),
        "agents": agents,
    }


def _team_rest(path: str) -> tuple[str, str]:
    match = _TEAM_PATH.fullmatch(path)
    if match is None:
        return "", "/"
    return match.group("team"), match.group("rest") or "/"


def _draft_action(rest: str) -> tuple[str, str]:
    parts = [part for part in rest.removeprefix("/drafts/").split("/") if part]
    draft_id = parts[0] if parts else ""
    action = parts[1] if len(parts) > 1 else ""
    return draft_id, action


def _version(fields: dict[str, list[str]]) -> int:
    raw = form_field(fields, "expected_version")
    if not raw.isdigit():
        return -1
    return int(raw)


def _draft_target(team_id: str, rest: str, code: str) -> str:
    if rest == "/drafts/reset":
        return f"/teams/{team_id}/drafts?notice={code}"
    if rest.startswith("/drafts/"):
        draft_id = rest.removeprefix("/drafts/").split("/")[0]
        if draft_id and code != "discarded":
            return f"/teams/{team_id}/drafts/{draft_id}?notice={code}"
    if rest.startswith("/revisions/"):
        return f"/teams/{team_id}/revisions?notice={code}"
    return f"/teams/{team_id}/drafts?notice={code}"


def _unassign(team: Any, agent: str) -> str:
    if hasattr(team, "sheets") and team.sheets is not None:
        return str(team.unassign_connection(agent))
    team.window.desk.unassign(agent)
    return "unassigned"


async def _call(call: Callable[[], object]) -> object:
    return call()


async def _close(installation: Installation) -> None:
    try:
        await installation.customer.organisation.aclose()
        await installation.reporting.team.aclose()
    finally:
        installation.close()


@dataclass(frozen=True)
class DashboardOptions:
    """Arguments for one local dashboard process."""

    state_directory: str | None
    host: str
    port: int


def dashboard_options(argv: list[str] | None = None) -> DashboardOptions:
    """Parse the dashboard command. ``--help`` exits before any state is opened."""
    parser = argparse.ArgumentParser(prog="customer_service.dashboard")
    parser.add_argument(
        "--state-directory",
        default=None,
        help="Directory for this process. Overrides APP_STATE_DIRECTORY from .env and the shell.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parsed = parser.parse_args(argv)
    state = parsed.state_directory
    host = parsed.host
    port = parsed.port
    if state is not None and not isinstance(state, str):
        raise SystemExit(2)
    if not isinstance(host, str) or not isinstance(port, int):
        raise SystemExit(2)
    return DashboardOptions(state_directory=state, host=host, port=port)


def dashboard_state_directory(argv: list[str] | None = None) -> str | None:
    """Resolve the state directory the dashboard will open."""
    return _state_from_options(dashboard_options(argv))


def _state_from_options(options: DashboardOptions) -> str | None:
    from customer_service.cli import selected_state_directory

    env_file = Path.cwd() / ".env"
    try:
        return selected_state_directory(
            options.state_directory,
            env_file if env_file.is_file() else None,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def main(argv: list[str] | None = None) -> None:
    """Open the local dashboard for both installed teams."""
    options = dashboard_options(argv)

    def open_installation() -> Installation:
        from agentbuildkit.config import load_composio_settings
        from agentbuildkit.integrations.composio import ComposioUnavailable, LiveComposioGateway
        from agentbuildkit.providers import ProviderConfiguration, builtin_registry
        from customer_service.team import production_clients
        from operations_reporting.team import reporting_bundle

        env_file = Path.cwd() / ".env"
        env_path = str(env_file) if env_file.is_file() else None
        directory = _state_from_options(options)
        composio = load_composio_settings(env_file_path=env_path)
        gateway = None
        package_missing = False
        key = composio.get("api_key")
        if key is not None:
            try:
                gateway = LiveComposioGateway(
                    key,
                    auth_config_id=composio.get("googlesheets_auth_config_id") or "",
                )
            except ComposioUnavailable:
                package_missing = True
        bundle = reporting_bundle()
        providers = builtin_registry(env_file_path=env_path)
        reporting_clients = {
            agent.name: providers.create_client(
                ProviderConfiguration(provider=agent.provider, model=agent.model)
            )
            for agent in bundle.agents
        }
        return build_installation(
            customer_clients=production_clients(env_file_path=env_path),
            reporting_clients=reporting_clients,
            directory=Path(directory) if directory is not None else None,
            gateway=gateway,
            package_missing=package_missing,
        )

    binding = serve_installation(open_installation, host=options.host, port=options.port)
    address = binding.server_address
    print(f"Dashboard at http://{address[0]}:{address[1]}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        binding.shutdown()
