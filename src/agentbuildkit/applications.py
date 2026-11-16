"""The boundary a local dashboard uses to run one installed team.

The harness owns this interface. Each example application implements it in its
own package. The dashboard selects a team for display, then asks that team's
service whether it owns a run or an approval. A route or a form field is not
enough to establish ownership.
"""

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class RenderedPage(BaseModel):
    """One HTML response. The harness does not interpret the body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int
    body: str


class TeamApplication(Protocol):
    """One installed team's dashboard service.

    Implementations keep their business types in the example package. The
    dashboard calls these methods and does not import a team's workflow code
    from a string.
    """

    team_id: str
    title: str

    def render(self, path: str = "/", query: Mapping[str, str] | None = None) -> RenderedPage:
        """Render one page for this team only."""
        ...

    def owns_correlation(self, correlation_id: str) -> bool:
        """True when this team recorded that run."""
        ...

    def owns_request(self, request_id: str) -> bool:
        """True when this team recorded that approval request."""
        ...
