"""The customer-service workflow is built from its team bundle."""

from agent_framework import SecretString

from agentbuildkit.observability import AuditLog
from agentbuildkit.workflows import memory_workflow_store
from agentbuildkit_templates.customer_service.evaluation import (
    LOCAL_MOCK_TOKEN,
    scripted_clients,
)
from agentbuildkit_templates.customer_service.models import CustomerRequest
from agentbuildkit_templates.customer_service.service import handle_request
from agentbuildkit_templates.customer_service.team import (
    BUNDLE_PATH,
    ORGANISATION_SCOPE,
    build_team,
    customer_team_bundle,
)


def _silent(**_kwargs: object) -> None:
    return None


def test_customer_bundle_matches_the_application_allowlists() -> None:
    bundle = customer_team_bundle()

    assert bundle.name == "customer-service"
    assert BUNDLE_PATH.name == "team.json"
    by_name = {agent.name: agent for agent in bundle.agents}
    assert by_name["sam"].tools == ("read_account",)
    assert by_name["taylor"].tools == ("read_service",)
    assert by_name["alex"].tools == ()
    assert by_name["jordan"].tools == ()
    assert "schedule_service" not in {tool for agent in bundle.agents for tool in agent.tools}


async def test_built_team_uses_bundle_definitions() -> None:
    bundle = customer_team_bundle()
    organisation = build_team(
        clients=scripted_clients("billing-routes-to-sam"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        audit_log=AuditLog(),
        configurator=_silent,
    )
    try:
        agents = {
            "alex": organisation.alex,
            "sam": organisation.sam,
            "taylor": organisation.taylor,
            "jordan": organisation.jordan,
        }
        for declared in bundle.agents:
            agent = agents[declared.name]
            assert agent.definition.instructions == declared.instructions
            assert agent.definition.provider == declared.provider
            assert agent.definition.model == declared.model
            assert tuple(tool.name for tool in agent.definition.tools) == declared.tools
            assert agent.definition.input_type.__name__ == declared.input_schema
            assert agent.definition.output_type.__name__ == declared.output_schema
        result = await handle_request(
            organisation,
            CustomerRequest(reference="A-100", message="Please explain the latest invoice."),
            correlation_id="bundle-billing",
            store=memory_workflow_store(ORGANISATION_SCOPE, organisation.audit_log),
        )
    finally:
        await organisation.aclose()

    assert result.outcome.status == "completed"
    assert result.outcome.specialist == "sam"
    assert result.pending is None
