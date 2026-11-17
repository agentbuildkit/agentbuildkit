"""Typed requests and decisions for the reference customer-service organisation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

OutcomeStatus = Literal[
    "completed",
    "rejected",
    "pending_approval",
    "escalated",
    "route_rejected",
]
QualityVerdict = Literal["approve", "reject", "escalate"]


class CustomerRequest(BaseModel):
    """A customer service request received by the organisation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    message: str


class RouteDecision(BaseModel):
    """Alex's classification. Python routes on ``specialist`` and ignores other text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    specialist: str
    category: str
    reference: str
    message: str


class ProposedResponse(BaseModel):
    """A specialist's draft. ``needs_action`` does not execute anything."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    specialist: str
    reference: str
    response: str
    needs_action: bool
    action: str | None = None


class QualityDecision(BaseModel):
    """Jordan's review of a proposed customer response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: QualityVerdict
    reference: str
    response: str


class ServiceActionRequest(BaseModel):
    """The action a person must approve before the business system is changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    action: str


class FinalOutcome(BaseModel):
    """The typed result returned to the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    status: OutcomeStatus
    specialist: str
    response: str
    correlation_id: str


class ReferenceQuery(BaseModel):
    """Lookup key for a read-only business API call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str


class AccountView(BaseModel):
    """Account data the business API is willing to show."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    account_name: str
    balance: str


class ServiceView(BaseModel):
    """Service data the business API is willing to show."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    service_status: str


class ActionReceipt(BaseModel):
    """Confirmation from the one side-effecting business operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    status: str
