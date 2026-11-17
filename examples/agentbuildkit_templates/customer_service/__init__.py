"""Customer-service demonstration built on AgentBuildKit.

The records, including reference ``A-100``, are fixture data.
"""

from agentbuildkit_templates.customer_service.evaluation import (
    LOCAL_MOCK_TOKEN,
    organisation_cases,
    run_organisation_evaluation,
)
from agentbuildkit_templates.customer_service.models import CustomerRequest, FinalOutcome
from agentbuildkit_templates.customer_service.service import (
    ServiceResult,
    handle_request,
    resume_service_action,
)
from agentbuildkit_templates.customer_service.team import (
    ORGANISATION_SCOPE,
    Organisation,
    build_team,
    production_clients,
)

__all__ = [
    "LOCAL_MOCK_TOKEN",
    "ORGANISATION_SCOPE",
    "CustomerRequest",
    "FinalOutcome",
    "Organisation",
    "ServiceResult",
    "build_team",
    "handle_request",
    "organisation_cases",
    "production_clients",
    "resume_service_action",
    "run_organisation_evaluation",
]
