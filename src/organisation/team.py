"""The reference organisation's four agents.

Tests pass a ``ScriptedChatClient`` for each role. A deployment passes one
``OpenAIChatClient`` from ``production_clients`` without changing these
definitions or the workflow.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agent_framework import SecretString, SupportsChatGetResponse
from agent_framework.openai import OpenAIChatClient, OpenAISettings

from agent_platform.agents import AgentDefinition, PlatformAgent, create_agent
from agent_platform.config import AppSettings, load_openai_settings
from agent_platform.observability import AuditLog, configure_observability
from organisation.business import BusinessSystem
from organisation.models import CustomerRequest, ProposedResponse, QualityDecision, RouteDecision

ALEX = "alex"
SAM = "sam"
TAYLOR = "taylor"
JORDAN = "jordan"
ROUTE_WORKFLOW = "customer-route"
ACTION_WORKFLOW = "service-action"
ORGANISATION_SCOPE = "customer-service"

_ALEX_INSTRUCTIONS = (
    "You are Alex, the operations manager for a B2B company. "
    "Classify the request as billing or service. "
    "Billing and account questions go to sam. "
    "Service and support questions go to taylor. "
    "Return only those specialist names."
)
_SAM_INSTRUCTIONS = (
    "You are Sam, the account specialist. "
    "Read the account with read_account and answer from that record. "
    "You cannot change customer or account data. "
    "Set needs_action to false."
)
_TAYLOR_INSTRUCTIONS = (
    "You are Taylor, the service specialist. "
    "Read the service record with read_service. "
    "Propose the customer response. "
    "If a visit or change is required, set needs_action to true and name the action. "
    "You cannot perform the action yourself."
)
_JORDAN_INSTRUCTIONS = (
    "You are Jordan, the quality reviewer. "
    "Approve a response that answers the request from the business record. "
    "Reject a response that is empty, unsupported, or unhelpful. "
    "Escalate when a person must take over and this is not a standard service action. "
    "Return the verdict and the response the customer should see."
)


@dataclass
class Organisation:
    """One built customer-service team and its business system."""

    alex: PlatformAgent[CustomerRequest, RouteDecision]
    sam: PlatformAgent[RouteDecision, ProposedResponse]
    taylor: PlatformAgent[RouteDecision, ProposedResponse]
    jordan: PlatformAgent[ProposedResponse, QualityDecision]
    business: BusinessSystem
    audit_log: AuditLog

    async def aclose(self) -> None:
        await self.business.aclose()


def production_clients(settings: OpenAISettings | None = None) -> dict[str, OpenAIChatClient]:
    """Share one configured OpenAI client across the four roles."""
    resolved = settings or load_openai_settings()
    model = resolved.get("chat_model")
    api_key = resolved.get("api_key")
    client = OpenAIChatClient(
        model=model if isinstance(model, str) else None,
        api_key=api_key if isinstance(api_key, str | SecretString) else None,
    )
    return {name: client for name in (ALEX, SAM, TAYLOR, JORDAN)}


def build_team(
    *,
    clients: Mapping[str, SupportsChatGetResponse],
    api_token: SecretString,
    audit_log: AuditLog | None = None,
    actions: list[str] | None = None,
    settings: AppSettings | None = None,
    configurator: Callable[..., None] | None = None,
    model: str | None = None,
) -> Organisation:
    """Create the team. ``clients`` selects the model adapter for every agent."""
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
    alex = create_agent(
        AgentDefinition(
            name=ALEX,
            purpose="Classify a customer request and choose an allowlisted specialist.",
            instructions=_ALEX_INSTRUCTIONS,
            input_type=CustomerRequest,
            output_type=RouteDecision,
            model=model,
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
            model=model,
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
            model=model,
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
            model=model,
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


def _require_client(
    clients: Mapping[str, SupportsChatGetResponse],
    name: str,
) -> SupportsChatGetResponse:
    client = clients.get(name)
    if client is None:
        raise ValueError("A chat client is not configured for this role.")
    return client
