"""Interactive local run of the customer-service example.

This module asks the provider registry for one client per role and runs the
team. It prints the typed outcome and the approval request. It does not print
prompts, credentials, or the audit log.
"""

import asyncio
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from agent_framework import SecretString, SupportsChatGetResponse

from agentbuildkit.agents import AgentInputError, AgentOutputError
from agentbuildkit.config import AppSettings, load_app_settings
from agentbuildkit.observability import AuditLog
from agentbuildkit.providers import ProviderError
from agentbuildkit.workflows import (
    RouteRejected,
    WorkflowInputError,
    WorkflowRunError,
    WorkflowStateStore,
    memory_workflow_store,
    sqlite_workflow_store,
)
from customer_service.evaluation import LOCAL_MOCK_TOKEN
from customer_service.models import CustomerRequest, FinalOutcome
from customer_service.service import ServiceResult, handle_request, resume_service_action
from customer_service.team import ORGANISATION_SCOPE, Organisation, build_team, production_clients

_WORKFLOW_DATABASE = "workflows.sqlite"


def state_directory(env_file: Path | None = None) -> str | None:
    """Return ``APP_STATE_DIRECTORY`` when durable workflow state is configured.

    When an env file is present, its value wins over the process environment.
    This matches ``load_settings`` and is unchanged by an explicit dashboard path.
    """
    path = Path.cwd() / ".env" if env_file is None else env_file
    settings = load_app_settings(env_file_path=str(path)) if path.is_file() else load_app_settings()
    directory = settings.get("state_directory")
    if isinstance(directory, str) and directory:
        return directory
    return None


def selected_state_directory(explicit: str | None, env_file: Path | None = None) -> str | None:
    """Return the state directory for this process.

    An explicit path is used as given. It is not taken from the env file or the
    process environment. Without one, ``state_directory`` applies.
    """
    if explicit is not None:
        chosen = explicit.strip()
        if not chosen or "\x00" in chosen:
            raise ValueError("The state directory is not valid.")
        return chosen
    return state_directory(env_file)


def open_workflow_store(audit_log: AuditLog, directory: str | None) -> WorkflowStateStore:
    """Use SQLite under ``directory`` so a mounted volume survives the container."""
    if directory is None:
        return memory_workflow_store(ORGANISATION_SCOPE, audit_log)
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    return sqlite_workflow_store(path / _WORKFLOW_DATABASE, ORGANISATION_SCOPE, audit_log)


def render_outcome(outcome: FinalOutcome) -> str:
    """Render the typed result without audit data or prompts."""
    specialist = outcome.specialist if outcome.specialist else "-"
    return "\n".join(
        (
            f"Status: {outcome.status}",
            f"Specialist: {specialist}",
            f"Response: {outcome.response}",
            f"Correlation: {outcome.correlation_id}",
        )
    )


def render_approval(result: ServiceResult) -> str:
    """Render the action a person is being asked to approve."""
    arguments = result.action_arguments or {}
    reference = arguments.get("reference", result.outcome.reference)
    service_action = arguments.get("action", "")
    action_name = result.pending.request.action if result.pending is not None else ""
    return "\n".join(
        (
            "Approval required",
            f"Action: {action_name}",
            f"Reference: {reference}",
            f"Service action: {service_action}",
            f"Specialist: {result.outcome.specialist}",
            f"Correlation: {result.outcome.correlation_id}",
        )
    )


def approval_from_text(text: str) -> bool | None:
    """Return a decision for y or n. Any other text is not a decision."""
    cleaned = text.strip().lower()
    if cleaned in {"y", "yes"}:
        return True
    if cleaned in {"n", "no"}:
        return False
    return None


async def run_session(
    organisation: Organisation,
    store: WorkflowStateStore,
    request: CustomerRequest,
    *,
    correlation_id: str,
    approve: Callable[[ServiceResult], bool],
    write: Callable[[str], None],
) -> FinalOutcome:
    """Run one request and resume when the workflow pauses for a person."""
    result = await handle_request(
        organisation,
        request,
        correlation_id=correlation_id,
        store=store,
    )
    paused = result.pending
    if result.outcome.status != "pending_approval" or paused is None:
        write(render_outcome(result.outcome))
        return result.outcome
    write(render_approval(result))
    approved = approve(result)
    pending = next(
        (
            row
            for row in store.list_pending(status="pending")
            if row.request_id == paused.request_id
        ),
        None,
    )
    if pending is None:
        raise RuntimeError("The service action did not pause for approval.")
    outcome = await resume_service_action(
        organisation,
        store,
        pending,
        reference=result.outcome.reference,
        approved=approved,
        arguments=result.action_arguments if approved else None,
    )
    write(render_outcome(outcome))
    return outcome


def render_failure(
    exc: BaseException,
    audit_log: AuditLog | None,
    correlation_id: str,
) -> str:
    """Describe a failure without prompts, credentials, or model text."""
    stage = "request"
    agent = "-"
    if audit_log is not None and correlation_id:
        for event in reversed(audit_log.events):
            if event.correlation_id != correlation_id or event.outcome != "failed":
                continue
            agent = event.agent_name or "-"
            stage = "agent-run" if event.argument_hash == "agent-run" else "tool"
            break
    return "\n".join(
        (
            f"Type: {type(exc).__name__}",
            f"Stage: {stage}",
            f"Agent: {agent}",
            f"Correlation: {correlation_id or '-'}",
            f"Reason: {_safe_reason(exc)}",
        )
    )


def main() -> None:
    """Prompt for one customer request and print the typed outcome."""
    env_file = Path.cwd() / ".env"
    try:
        clients = production_clients(env_file_path=str(env_file) if env_file.is_file() else None)
    except ProviderError as exc:
        print(str(exc))
        raise SystemExit(1) from None
    raise SystemExit(asyncio.run(_interactive(clients)))


async def _interactive(clients: Mapping[str, SupportsChatGetResponse]) -> int:
    organisation: Organisation | None = None
    correlation_id = ""
    try:
        organisation = build_team(
            clients=clients,
            api_token=SecretString(LOCAL_MOCK_TOKEN),
            settings=_local_app_settings(),
        )
        _print_agents(organisation)
        reference = input("Reference [A-100]: ").strip() or "A-100"
        message = input("Message: ").strip()
        if not message:
            print("A message is required.")
            return 1
        correlation_id = uuid.uuid4().hex
        print(f"Correlation: {correlation_id}")
        store = open_workflow_store(organisation.audit_log, state_directory())
        await run_session(
            organisation,
            store,
            CustomerRequest(reference=reference, message=message),
            correlation_id=correlation_id,
            approve=_prompt_approval,
            write=print,
        )
    except Exception as exc:
        audit_log = organisation.audit_log if organisation is not None else None
        print(render_failure(exc, audit_log, correlation_id))
        return 1
    else:
        return 0
    finally:
        if organisation is not None:
            await organisation.aclose()


def _prompt_approval(_result: ServiceResult) -> bool:
    while True:
        decision = approval_from_text(input("Approve this service action? [y/n]: "))
        if decision is not None:
            return decision
        print("Enter y or n.")


def _safe_reason(exc: BaseException) -> str:
    if isinstance(
        exc,
        AgentInputError | AgentOutputError | RouteRejected | WorkflowInputError | WorkflowRunError,
    ):
        return _plain_reason(str(exc))
    message = _service_message(exc)
    if message is not None:
        plain = _plain_reason(message)
        if plain != "The request failed.":
            return plain
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return f"The model service returned HTTP {status}."
    return "The request failed."


def _service_message(exc: BaseException) -> str | None:
    error = _mapping(getattr(exc, "body", None)).get("error")
    message = _mapping(error).get("message")
    if isinstance(message, str):
        return message
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        return {}
    parsed: dict[str, Any] = {}
    for key, item in cast(Mapping[Any, Any], value).items():
        if isinstance(key, str):
            parsed[key] = item
    return parsed


def _plain_reason(text: str) -> str:
    lowered = text.lower()
    if (
        len(text) > 160
        or any(marker in lowered for marker in ("sk-", "bearer", "authorization", "api_key"))
        or any(character in text for character in "{}\n\r")
    ):
        return "The request failed."
    if text and all(character.isprintable() for character in text):
        return text
    return "The request failed."


def _print_agents(organisation: Organisation) -> None:
    for agent in (organisation.alex, organisation.sam, organisation.taylor, organisation.jordan):
        definition = agent.definition
        print(f"{definition.name}: {definition.provider} / {definition.model}")


def _local_app_settings() -> AppSettings:
    return {
        "service_name": "customer-service",
        "environment": "local",
        "enable_console_exporters": False,
    }
