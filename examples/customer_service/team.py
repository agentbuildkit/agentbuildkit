"""Demonstration customer-service team.

The people, the company, and reference ``A-100`` are fixtures. Each role
declares its own provider and model. The runnable default uses OpenAI for
every role, so a live run needs ``OPENAI_API_KEY`` only. A role switches to
Anthropic by replacing that role's ``ProviderConfiguration``. Tests pass a
``ScriptedChatClient`` through ``build_team(clients=...)``. ``production_clients``
asks the provider registry for one client per role.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agent_framework import SecretString, SupportsChatGetResponse

from agentbuildkit.agents import AgentDefinition, PlatformAgent, create_agent
from agentbuildkit.config import AppSettings
from agentbuildkit.observability import AuditLog, configure_observability
from agentbuildkit.providers import (
    OPENAI_PROVIDER_ID,
    ProviderConfiguration,
    ProviderRegistry,
    builtin_registry,
)
from customer_service.business import BusinessSystem
from customer_service.models import (
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
ORGANISATION_SCOPE = "customer-service"
EXAMPLE_CHAT_MODEL = "gpt-4.1-mini"

# Each entry is that role's provider and model. They are independent. The
# shared model string is the demo's declared OpenAI model, not a process-wide
# default. Pass ``roles`` to ``build_team`` or ``production_clients`` to switch
# one role, including to Anthropic.
ROLE_CONFIGURATION: dict[str, ProviderConfiguration] = {
    ALEX: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model=EXAMPLE_CHAT_MODEL),
    SAM: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model=EXAMPLE_CHAT_MODEL),
    TAYLOR: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model=EXAMPLE_CHAT_MODEL),
    JORDAN: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model=EXAMPLE_CHAT_MODEL),
}

_ALEX_INSTRUCTIONS = (
    "You are Alex, the operations manager in this demonstration. "
    "The customer records are fixtures, including reference A-100. "
    "Classify the request as billing or service. "
    "Billing and account questions go to sam. "
    "Service and support questions go to taylor. "
    "Return only those specialist names."
)
_SAM_INSTRUCTIONS = (
    "You are Sam, the account specialist in this demonstration. "
    "Read the account with read_account and answer from that record. "
    "You cannot change customer or account data. "
    "Set needs_action to false."
)
_TAYLOR_INSTRUCTIONS = (
    "You are Taylor, the service specialist in this demonstration. "
    "Read the service record with read_service. "
    "Propose the customer response. "
    "If a visit or change is required, set needs_action to true and name the action. "
    "You cannot perform the action yourself."
)
_JORDAN_INSTRUCTIONS = (
    "You are Jordan, the quality reviewer in this demonstration. "
    "Approve a response that answers the request from the business record. "
    "Reject a response that is empty, unsupported, or unhelpful. "
    "Escalate when a person must take over and this is not a standard service action. "
    "Return the verdict and the response the customer should see."
)


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
) -> Organisation:
    """Create the team from injected clients and each role's provider configuration.

    ``clients`` is the test and production seam. A scripted client is used as
    given. ``roles`` replaces the declared provider and model for named roles
    without applying one model to the whole team.
    """
    log = audit_log or AuditLog()
    selected = _role_configuration(roles)
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
    alex = create_agent(
        AgentDefinition(
            name=ALEX,
            purpose="Classify a customer request and choose an allowlisted specialist.",
            instructions=_ALEX_INSTRUCTIONS,
            input_type=CustomerRequest,
            output_type=RouteDecision,
            provider=selected[ALEX].provider,
            model=selected[ALEX].model,
        ),
        client=_require_client(clients, ALEX),
        audit_log=log,
    )
    sam = create_agent(
        AgentDefinition(
            name=SAM,
            purpose="Answer account and billing questions from the business system.",
            instructions=_SAM_INSTRUCTIONS,
            input_type=RouteDecision,
            output_type=ProposedResponse,
            tools=(business.read_account,),
            provider=selected[SAM].provider,
            model=selected[SAM].model,
        ),
        client=_require_client(clients, SAM),
        audit_log=log,
    )
    taylor = create_agent(
        AgentDefinition(
            name=TAYLOR,
            purpose="Answer service questions and propose a service action.",
            instructions=_TAYLOR_INSTRUCTIONS,
            input_type=RouteDecision,
            output_type=ProposedResponse,
            tools=(business.read_service,),
            provider=selected[TAYLOR].provider,
            model=selected[TAYLOR].model,
        ),
        client=_require_client(clients, TAYLOR),
        audit_log=log,
    )
    jordan = create_agent(
        AgentDefinition(
            name=JORDAN,
            purpose="Review a proposed response before it is sent or acted on.",
            instructions=_JORDAN_INSTRUCTIONS,
            input_type=ProposedResponse,
            output_type=QualityDecision,
            provider=selected[JORDAN].provider,
            model=selected[JORDAN].model,
        ),
        client=_require_client(clients, JORDAN),
        audit_log=log,
    )
    return Organisation(
        alex=alex,
        sam=sam,
        taylor=taylor,
        jordan=jordan,
        business=business,
        audit_log=log,
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
