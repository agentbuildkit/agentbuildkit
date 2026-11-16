"""Typed inputs and reports for the operations-reporting example."""

from pydantic import BaseModel, ConfigDict


class JobsRequest(BaseModel):
    """The date the report is measured against, plus an optional note."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: str
    note: str = ""


class ReportDraft(BaseModel):
    """Analyst's summary. The records are the tool output, not a new source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: str
    records: str
    summary: str


class ReviewedReport(BaseModel):
    """Reviewer's check that the summary stays inside the supplied records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: str
    records: str
    summary: str
    supported: bool
    notes: str = ""
