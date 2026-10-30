"""Offline evaluation for agents and workflows.

Cases are supplied by the caller. The runner applies deterministic checks and
can accept qualitative scores from a caller-supplied provider. It does not
call a model.
"""

from agentbuildkit.evaluation.models import (
    AssertionResult,
    CaseObservation,
    CaseResult,
    EvaluationCase,
    EvaluationReport,
    Expectation,
    RegressionChange,
    RegressionReport,
    ScoreObservation,
)
from agentbuildkit.evaluation.runner import compare_reports, run_evaluation
from agentbuildkit.evaluation.trace import trace_from_audit

__all__ = [
    "AssertionResult",
    "CaseObservation",
    "CaseResult",
    "EvaluationCase",
    "EvaluationReport",
    "Expectation",
    "RegressionChange",
    "RegressionReport",
    "ScoreObservation",
    "compare_reports",
    "run_evaluation",
    "trace_from_audit",
]
