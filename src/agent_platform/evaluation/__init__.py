"""Offline evaluation for agents and workflows.

Cases are organisation-defined. The runner applies deterministic checks and
can accept qualitative scores from a caller-supplied provider. It does not
call a model.
"""

from agent_platform.evaluation.models import (
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
from agent_platform.evaluation.runner import compare_reports, run_evaluation
from agent_platform.evaluation.trace import trace_from_audit

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
