"""Registered templates for the two installed teams.

The revision service accepts this data. It does not import either example to
discover a workflow.
"""

from collections.abc import Awaitable, Callable

from agent_framework import SecretString, load_settings

from agentbuildkit.config.settings import load_openai_settings
from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.providers.anthropic import AnthropicCredentials
from agentbuildkit.revisions import TeamTemplate
from agentbuildkit.revisions.models import AgentContract
from agentbuildkit.teams import TeamBundle
from agentbuildkit_templates.customer_service.evaluation import (
    run_bundle_evaluation as evaluate_customer,
)
from agentbuildkit_templates.customer_service.team import customer_team_bundle
from agentbuildkit_templates.operations_reporting.evaluation import (
    run_bundle_evaluation as evaluate_reporting,
)
from agentbuildkit_templates.operations_reporting.team import reporting_bundle

Evaluator = Callable[[TeamBundle], Awaitable[EvaluationReport]]
_FORBIDDEN = (
    "schedule_service",
    "service.schedule",
    "GOOGLESHEETS_BATCH_GET",
    "read_sheet_range",
)


def installed_templates() -> tuple[TeamTemplate, ...]:
    """Customer Service and Operations Reporting, with their shipped text."""
    return (_template(customer_team_bundle(), _CUSTOMER), _template(reporting_bundle(), _REPORTING))


def installed_evaluators() -> dict[str, Evaluator]:
    """Scripted suites. They do not call a live model or Composio."""
    return {
        "customer-service": evaluate_customer,
        "operations-reporting": evaluate_reporting,
    }


def credential_ready(provider: str) -> bool:
    """True when that provider has a non-empty key. The key is not returned."""
    try:
        if provider == "openai":
            return _present(load_openai_settings().get("api_key"))
        if provider == "anthropic":
            loaded = load_settings(AnthropicCredentials, env_prefix="ANTHROPIC_")
            return _present(loaded.get("api_key"))
    except Exception:
        return False
    return False


def _present(value: object) -> bool:
    if not isinstance(value, SecretString):
        return False
    return value.get_secret_value() != ""


def _template(bundle: TeamBundle, description: tuple[str, str]) -> TeamTemplate:
    summary, suite = description
    return TeamTemplate(
        template_id=bundle.team_id,
        team_id=bundle.team_id,
        title=bundle.display_name,
        description=summary,
        suite_version=suite,
        agents=tuple(
            AgentContract(
                name=agent.name,
                purpose=agent.purpose,
                instructions=agent.instructions,
                provider=agent.provider,
                model=agent.model,
                input_schema=agent.input_schema,
                output_schema=agent.output_schema,
                permitted_tools=agent.tools,
                required_tools=agent.tools,
            )
            for agent in bundle.agents
        ),
        forbidden_tools=_FORBIDDEN,
    )


_CUSTOMER = (
    "Alex routes a request, one specialist reads a fixture record, and Jordan reviews the draft.",
    "customer-service-offline-1",
)
_REPORTING = (
    "Analyst summarises overdue jobs and Reviewer checks that the summary names only those ids.",
    "operations-reporting-offline-1",
)
