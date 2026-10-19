"""Typed evaluation cases and reports.

A client organisation supplies the cases. This module has no business datasets.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Expectation(BaseModel):
    """The checks one case asks the runner to apply.

    Unset checks are skipped. The correlation id on the case is always checked.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    require_valid_output: bool = False
    output: dict[str, Any] | None = None
    route: str | None = None
    agents: tuple[str, ...] = ()
    forbidden_agents: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    approval_actions: tuple[str, ...] = ()
    no_unapproved_write: bool = False
    expect_route_rejected: bool = False
    rejected_tools: tuple[str, ...] = ()
    rejected_mcp_servers: tuple[str, ...] = ()
    max_tool_calls: int | None = None
    max_elapsed_seconds: float | None = None
    max_cost: float | None = None
    secrets_absent: tuple[str, ...] = ()
    minimum_scores: dict[str, float] = Field(default_factory=dict)


class EvaluationCase(BaseModel):
    """One organisation-defined case.

    ``input`` is for the executor. The report does not copy it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    correlation_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    expectation: Expectation = Field(default_factory=Expectation)


class ScoreObservation(BaseModel):
    """A qualitative score supplied by the caller. The runner does not call a model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float = Field(ge=0, le=1)


class CaseObservation(BaseModel):
    """What one execution produced. Assertion inputs stay here, not in the report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    output_valid: bool = False
    output: dict[str, Any] | None = None
    route: str | None = None
    agents_invoked: tuple[str, ...] = ()
    tools_called: tuple[str, ...] = ()
    tools_rejected: tuple[str, ...] = ()
    approval_requested: tuple[str, ...] = ()
    unapproved_writes: tuple[str, ...] = ()
    route_rejected: bool = False
    mcp_servers_rejected: tuple[str, ...] = ()
    audit_correlation_ids: tuple[str, ...] = ()
    elapsed_seconds: float = 0.0
    tool_call_count: int = 0
    cost: float | None = None
    captured_text: str = ""


class AssertionResult(BaseModel):
    """One deterministic or scored check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    message: str


class CaseResult(BaseModel):
    """The pass/fail record for one case. It omits input, output, and secrets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    assertions: tuple[AssertionResult, ...]
    elapsed_seconds: float
    tool_call_count: int
    correlation_id: str
    agents_invoked: tuple[str, ...] = ()
    tools_called: tuple[str, ...] = ()


class EvaluationReport(BaseModel):
    """Totals for one suite run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    passed: int
    failed: int
    pass_rate: float
    elapsed_seconds: float
    failures_by_assertion: dict[str, int]
    cases: tuple[CaseResult, ...]


class RegressionChange(BaseModel):
    """One assertion whose pass/fail changed between two reports."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_name: str
    assertion: str
    previous: bool
    current: bool


class RegressionReport(BaseModel):
    """How a new run compares with a saved baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pass_rate_delta: float
    regressed: tuple[str, ...]
    improved: tuple[str, ...]
    changes: tuple[RegressionChange, ...]
