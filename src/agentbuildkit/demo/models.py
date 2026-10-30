"""Typed messages for the offline demo. These models are not a business system."""

from pydantic import BaseModel, ConfigDict


class Intake(BaseModel):
    """The request a person typed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    message: str


class RouteDecision(BaseModel):
    """The orchestrator's route. Python accepts only an allowlisted specialist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    specialist: str
    reference: str
    message: str


class Draft(BaseModel):
    """The specialist's proposal. ``needs_action`` does not run the action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    summary: str
    needs_action: bool
    when: str


class ReviewRequest(BaseModel):
    """What the reviewer sees after the approval workflow finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    summary: str
    action_status: str


class Review(BaseModel):
    """The reviewer's structured decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    response: str


class VisitRequest(BaseModel):
    """Arguments for the side-effecting visit tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    when: str
