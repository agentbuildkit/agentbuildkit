"""Customer-request flow for the reference organisation.

Alex's typed decision is routed by the platform's manager workflow. Jordan's
verdict and the specialist's action flag are applied in ordinary Python. A
service change pauses on the platform approval workflow and resumes from the
durable store.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from agentbuildkit.observability import bind_correlation_id, reset_correlation_id
from agentbuildkit.workflows import (
    HumanApprovalResponse,
    PendingHumanInput,
    PendingRequest,
    RouteRejected,
    WorkflowRunError,
    WorkflowStateStore,
    approval_workflow,
    envelope,
    manager_workflow,
    resume_approval,
    run_workflow,
)
from customer_service.models import (
    CustomerRequest,
    FinalOutcome,
    OutcomeStatus,
    ProposedResponse,
    ServiceActionRequest,
)
from customer_service.team import ACTION_WORKFLOW, ROUTE_WORKFLOW, Organisation

_UNROUTED = "The request could not be routed."
_REJECTED = "The proposed response was not accepted."
_ESCALATED = "The request needs a person."
_UNSPECIFIED = "A service action was not specified."
_PENDING = "The service action is waiting for approval."
_COMPLETED_ACTION = "The service action was completed."
_DECLINED_ACTION = "The service action was not approved."


@dataclass(frozen=True)
class ServiceResult:
    """The outcome plus the pause metadata kept only in this process."""

    outcome: FinalOutcome
    pending: PendingHumanInput | None = None
    action_arguments: dict[str, str] | None = None


async def handle_request(
    organisation: Organisation,
    request: CustomerRequest,
    *,
    correlation_id: str,
    store: WorkflowStateStore,
) -> ServiceResult:
    """Classify, route, review, and either finish or pause for a person."""
    token = bind_correlation_id(correlation_id)
    try:
        return await _handle(organisation, request, correlation_id=correlation_id, store=store)
    finally:
        reset_correlation_id(token)


async def resume_service_action(
    organisation: Organisation,
    store: WorkflowStateStore,
    pending: PendingRequest,
    *,
    reference: str,
    approved: bool,
    arguments: Mapping[str, Any] | None = None,
) -> FinalOutcome:
    """Resume one stored service action. A second approval does not run it again."""
    token = bind_correlation_id(pending.correlation_id)
    try:
        workflow = approval_workflow(
            organisation.business.schedule_service,
            organisation.audit_log,
            name=ACTION_WORKFLOW,
            storage=store,
        )
        result = await resume_approval(
            workflow,
            request_id=pending.request_id,
            checkpoint_id=pending.checkpoint_id,
            correlation_id=pending.correlation_id,
            action=pending.action,
            response=HumanApprovalResponse(approved=approved),
            arguments=arguments,
        )
    finally:
        reset_correlation_id(token)
    if result.output is None:
        raise WorkflowRunError("The service action did not finish.")
    status = result.output.body.get("status")
    if status == "completed":
        return _outcome(
            reference=reference,
            status="completed",
            specialist="taylor",
            response=_COMPLETED_ACTION,
            correlation_id=pending.correlation_id,
        )
    if status == "already_attempted":
        return _outcome(
            reference=reference,
            status="rejected",
            specialist="taylor",
            response="The service action was already attempted.",
            correlation_id=pending.correlation_id,
        )
    return _outcome(
        reference=reference,
        status="rejected",
        specialist="taylor",
        response=_DECLINED_ACTION,
        correlation_id=pending.correlation_id,
    )


async def _handle(
    organisation: Organisation,
    request: CustomerRequest,
    *,
    correlation_id: str,
    store: WorkflowStateStore,
) -> ServiceResult:
    workflow = manager_workflow(
        organisation.alex,
        {"sam": organisation.sam, "taylor": organisation.taylor},
        name=ROUTE_WORKFLOW,
    )
    try:
        routed = await run_workflow(workflow, envelope(correlation_id, request))
    except RouteRejected:
        return ServiceResult(
            outcome=_outcome(
                reference=request.reference,
                status="route_rejected",
                specialist="",
                response=_UNROUTED,
                correlation_id=correlation_id,
            )
        )
    if routed.output is None:
        raise WorkflowRunError("Routing did not produce a specialist response.")
    try:
        proposed = ProposedResponse.model_validate(routed.output.body)
    except ValidationError:
        raise WorkflowRunError("Specialist output was not a proposed response.") from None
    review = await organisation.jordan.run(proposed, correlation_id=correlation_id)
    if review.verdict == "reject":
        return ServiceResult(
            outcome=_outcome(
                reference=request.reference,
                status="rejected",
                specialist=proposed.specialist,
                response=_REJECTED,
                correlation_id=correlation_id,
            )
        )
    if review.verdict == "escalate":
        return ServiceResult(
            outcome=_outcome(
                reference=request.reference,
                status="escalated",
                specialist=proposed.specialist,
                response=_ESCALATED,
                correlation_id=correlation_id,
            )
        )
    if not proposed.needs_action:
        return ServiceResult(
            outcome=_outcome(
                reference=request.reference,
                status="completed",
                specialist=proposed.specialist,
                response=review.response,
                correlation_id=correlation_id,
            )
        )
    if proposed.action is None or proposed.action == "":
        return ServiceResult(
            outcome=_outcome(
                reference=request.reference,
                status="rejected",
                specialist=proposed.specialist,
                response=_UNSPECIFIED,
                correlation_id=correlation_id,
            )
        )
    action = ServiceActionRequest(reference=proposed.reference, action=proposed.action)
    approval = approval_workflow(
        organisation.business.schedule_service,
        organisation.audit_log,
        name=ACTION_WORKFLOW,
        storage=store,
    )
    paused = await run_workflow(approval, envelope(correlation_id, action))
    if paused.pending is None:
        raise WorkflowRunError("The service action did not pause for approval.")
    return ServiceResult(
        outcome=_outcome(
            reference=request.reference,
            status="pending_approval",
            specialist=proposed.specialist,
            response=_PENDING,
            correlation_id=correlation_id,
        ),
        pending=paused.pending,
        action_arguments={"reference": action.reference, "action": action.action},
    )


def _outcome(
    *,
    reference: str,
    status: OutcomeStatus,
    specialist: str,
    response: str,
    correlation_id: str,
) -> FinalOutcome:
    return FinalOutcome(
        reference=reference,
        status=status,
        specialist=specialist,
        response=response,
        correlation_id=correlation_id,
    )
