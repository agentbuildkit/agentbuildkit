"""Demonstration customer-service team.

The people, the company, and reference ``A-100`` are fixtures. Each role
declares its own provider and model. The runnable default uses OpenAI for
every role, so a live run needs ``OPENAI_API_KEY`` only. A role switches to
Anthropic by replacing that role's ``ProviderConfiguration``. Tests pass a
``ScriptedChatClient`` through ``build_team(clients=...)``. ``production_clients``
asks the provider registry for one client per role.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import cast

from agent_framework import FunctionTool, SecretString, SupportsChatGetResponse
from pydantic import BaseModel

from agentbuildkit.agents import AgentDefinition, PlatformAgent, create_agent
from agentbuildkit.config import AppSettings
from agentbuildkit.observability import AuditLog, configure_observability
from agentbuildkit.providers import ProviderConfiguration, ProviderRegistry, builtin_registry
from agentbuildkit.teams import TeamBundle, load_bundle_json
from agentbuildkit.workflows.diagram import DiagramChoice, DiagramStep, WorkflowDiagram
from agentbuildkit_templates.customer_service.business import BusinessSystem
from agentbuildkit_templates.customer_service.models import (
    CustomerRequest,
    ProposedResponse,
    QualityDecision,
    RouteDecision,
)

ALEX = "alex"
SAM = "sam"
TAYLOR = "taylor"
JORDAN = "jordan"
ROUTE_WORKFLOW = "customer-route"
ACTION_WORKFLOW = "service-action"
CUSTOMER_DIAGRAM = WorkflowDiagram(
    stages=(
        DiagramStep("request", "Request", "entry"),
        DiagramStep(ALEX, "Alex", "agent"),
        DiagramChoice(
            (
                DiagramStep(SAM, "Sam", "agent"),
                DiagramStep(TAYLOR, "Taylor", "agent"),
            )
        ),
        DiagramStep(JORDAN, "Jordan", "agent"),
        DiagramStep("you", "You", "person"),
        DiagramStep("schedule_service", "schedule_service", "effect", mono=True),
    ),
    handling=(
        "Alex classifies the request. Python routes it to Sam or Taylor and rejects any other "
        "name.",
        "The specialist reads the business record with its allowed tool and drafts a response.",
        "Jordan approves, rejects or escalates the draft.",
        "If the specialist proposed a service action, the run pauses until you approve or "
        "reject it. schedule_service is on no agent's tool list.",
        "An approved action is reserved before it is sent. If completion isn't recorded, it stays "
        "unresolved and is not sent again. This is not exactly-once delivery.",
    ),
)
ORGANISATION_SCOPE = "customer-service"
BUNDLE_PATH = files("agentbuildkit_templates.customer_service").joinpath("team.json")
_SCHEMAS: dict[str, type[BaseModel]] = {
    "CustomerRequest": CustomerRequest,
    "RouteDecision": RouteDecision,
    "ProposedResponse": ProposedResponse,
    "QualityDecision": QualityDecision,
}
_ROLE_ORDER = (ALEX, SAM, TAYLOR, JORDAN)


def customer_team_bundle() -> TeamBundle:
    """Load the example bundle. Bindings are checked when the team is built."""
    return load_bundle_json(BUNDLE_PATH.read_bytes())


def _bundle_roles() -> dict[str, ProviderConfiguration]:
    return {
        agent.name: ProviderConfiguration(provider=agent.provider, model=agent.model)
        for agent in customer_team_bundle().agents
    }


ROLE_CONFIGURATION = _bundle_roles()
EXAMPLE_CHAT_MODEL = ROLE_CONFIGURATION[ALEX].model


@dataclass
class Organisation:
    """The demonstration team and its fixture business system."""

    alex: PlatformAgent[CustomerRequest, RouteDecision]
    sam: PlatformAgent[RouteDecision, ProposedResponse]
    taylor: PlatformAgent[RouteDecision, ProposedResponse]
    jordan: PlatformAgent[ProposedResponse, QualityDecision]
    business: BusinessSystem
    audit_log: AuditLog

    async def aclose(self) -> None:
        await self.business.aclose()


def production_clients(
    *,
    env_file_path: str | None = None,
    registry: ProviderRegistry | None = None,
    roles: Mapping[str, ProviderConfiguration] | None = None,
) -> dict[str, SupportsChatGetResponse]:
    """Ask the provider registry for one client per role."""
    selected = _role_configuration(roles)
    resolved = registry if registry is not None else builtin_registry(env_file_path=env_file_path)
    return {name: resolved.create_client(configuration) for name, configuration in selected.items()}


def build_team(
    *,
    clients: Mapping[str, SupportsChatGetResponse],
    api_token: SecretString,
    audit_log: AuditLog | None = None,
    actions: list[str] | None = None,
    settings: AppSettings | None = None,
    configurator: Callable[..., None] | None = None,
    roles: Mapping[str, ProviderConfiguration] | None = None,
    connected_reads: Callable[[str], tuple[FunctionTool, ...]] | None = None,
    account_guard: Callable[[BusinessSystem], FunctionTool] | None = None,
    bundle: TeamBundle | None = None,
) -> Organisation:
    """Create the team from injected clients and a bundle.

    ``clients`` is the test and production seam. A scripted client is used as
    given. The bundle supplies instructions, schemas, and tool allowlists.
    ``roles`` replaces the provider and model for named roles. The side-effect
    tool stays on the approval workflow. ``bundle`` selects a published
    revision. When it is omitted, ``team.json`` is used.
    """
    log = audit_log or AuditLog()
    resolved: AppSettings = settings or {
        "service_name": "customer-service",
        "environment": "local",
        "enable_console_exporters": False,
    }
    if configurator is None:
        configure_observability(resolved)
    else:
        configure_observability(resolved, configurator=configurator)
    business = BusinessSystem(audit_log=log, api_token=api_token, actions=actions)
    account_tool = None if account_guard is None else account_guard(business)
    definitions = _definitions(business, roles, account_tool, bundle)
    if connected_reads is not None:
        definitions = tuple(
            replace(item, tools=item.tools + connected_reads(item.name)) for item in definitions
        )
    built = {
        definition.name: create_agent(
            definition,
            client=_require_client(clients, definition.name),
            audit_log=log,
        )
        for definition in definitions
    }
    return Organisation(
        alex=cast(PlatformAgent[CustomerRequest, RouteDecision], built[ALEX]),
        sam=cast(PlatformAgent[RouteDecision, ProposedResponse], built[SAM]),
        taylor=cast(PlatformAgent[RouteDecision, ProposedResponse], built[TAYLOR]),
        jordan=cast(PlatformAgent[ProposedResponse, QualityDecision], built[JORDAN]),
        business=business,
        audit_log=log,
    )


def _definitions(
    business: BusinessSystem,
    roles: Mapping[str, ProviderConfiguration] | None,
    account_tool: FunctionTool | None = None,
    bundle: TeamBundle | None = None,
) -> tuple[AgentDefinition[BaseModel, BaseModel], ...]:
    """Resolve a bundle against this application's schemas and read tools."""
    selected_bundle = customer_team_bundle() if bundle is None else bundle
    definitions = selected_bundle.definitions(
        schemas=_SCHEMAS,
        tools={
            "read_account": account_tool or business.read_account,
            "read_service": business.read_service,
        },
    )
    if tuple(definition.name for definition in definitions) != _ROLE_ORDER:
        raise ValueError("The customer-service bundle does not match the application.")
    if bundle is None:
        selected = _role_configuration(roles)
    else:
        selected = {
            item.name: ProviderConfiguration(provider=item.provider, model=item.model)
            for item in definitions
        }
        if roles is not None:
            unknown = sorted(set(roles) - set(selected))
            if unknown:
                raise ValueError("A provider configuration was given for an unknown role.")
            selected.update(roles)
    return tuple(
        replace(
            definition,
            provider=selected[definition.name].provider,
            model=selected[definition.name].model,
        )
        for definition in definitions
    )


def _role_configuration(
    roles: Mapping[str, ProviderConfiguration] | None,
) -> dict[str, ProviderConfiguration]:
    selected = dict(ROLE_CONFIGURATION)
    if roles is None:
        return selected
    unknown = sorted(set(roles) - set(selected))
    if unknown:
        raise ValueError("A provider configuration was given for an unknown role.")
    selected.update(roles)
    return selected


def _require_client(
    clients: Mapping[str, SupportsChatGetResponse],
    name: str,
) -> SupportsChatGetResponse:
    client = clients.get(name)
    if client is None:
        raise ValueError("A chat client is not configured for this role.")
    return client
