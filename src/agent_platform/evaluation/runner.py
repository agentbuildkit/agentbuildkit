"""Run an organisation-defined suite and compare it with a baseline.

A failing case is recorded and the suite continues. Exception text is discarded
so a payload or secret in an error cannot enter the report.
"""

import time
from collections.abc import Awaitable, Callable, Sequence

from agent_platform.evaluation.assertions import assess
from agent_platform.evaluation.models import (
    AssertionResult,
    CaseObservation,
    CaseResult,
    EvaluationCase,
    EvaluationReport,
    RegressionChange,
    RegressionReport,
    ScoreObservation,
)

ScoreProvider = Callable[[EvaluationCase, CaseObservation], Awaitable[Sequence[ScoreObservation]]]
CaseExecutor = Callable[[EvaluationCase], Awaitable[CaseObservation]]


async def run_evaluation(
    cases: Sequence[EvaluationCase],
    execute: CaseExecutor,
    *,
    score_provider: ScoreProvider | None = None,
) -> EvaluationReport:
    """Execute every case and return totals, failures, and per-case results."""
    started = time.perf_counter()
    results: list[CaseResult] = []
    for case in cases:
        observation, execution_failed = await _execute_case(case, execute)
        scores = await _scores(case, observation, score_provider)
        assertions = list(assess(case, observation, scores))
        if execution_failed:
            assertions.insert(
                0,
                AssertionResult(
                    name="execution",
                    passed=False,
                    message="Case execution failed.",
                ),
            )
        results.append(
            CaseResult(
                name=case.name,
                passed=all(item.passed for item in assertions),
                assertions=tuple(assertions),
                elapsed_seconds=observation.elapsed_seconds,
                tool_call_count=observation.tool_call_count,
                correlation_id=observation.correlation_id,
                agents_invoked=observation.agents_invoked,
                tools_called=observation.tools_called,
            )
        )
    elapsed = time.perf_counter() - started
    passed = sum(1 for item in results if item.passed)
    total = len(results)
    return EvaluationReport(
        total=total,
        passed=passed,
        failed=total - passed,
        pass_rate=0.0 if total == 0 else passed / total,
        elapsed_seconds=elapsed,
        failures_by_assertion=_failure_counts(results),
        cases=tuple(results),
    )


def compare_reports(baseline: EvaluationReport, current: EvaluationReport) -> RegressionReport:
    """Compare two saved reports. A case regresses when it stops passing."""
    previous_cases = {item.name: item.passed for item in baseline.cases}
    current_cases = {item.name: item.passed for item in current.cases}
    previous_assertions = _assertion_map(baseline)
    current_assertions = _assertion_map(current)
    changes: list[RegressionChange] = []
    for key, previous in previous_assertions.items():
        if key not in current_assertions or current_assertions[key] == previous:
            continue
        changes.append(
            RegressionChange(
                case_name=key[0],
                assertion=key[1],
                previous=previous,
                current=current_assertions[key],
            )
        )
    for key, current_passed in current_assertions.items():
        if key in previous_assertions:
            continue
        if current_passed:
            continue
        changes.append(
            RegressionChange(
                case_name=key[0],
                assertion=key[1],
                previous=True,
                current=False,
            )
        )
    regressed = tuple(
        name
        for name, passed in current_cases.items()
        if previous_cases.get(name) is True and not passed
    )
    improved = tuple(
        name
        for name, passed in current_cases.items()
        if previous_cases.get(name) is False and passed
    )
    return RegressionReport(
        pass_rate_delta=current.pass_rate - baseline.pass_rate,
        regressed=regressed,
        improved=improved,
        changes=tuple(changes),
    )


async def _execute_case(
    case: EvaluationCase,
    execute: CaseExecutor,
) -> tuple[CaseObservation, bool]:
    try:
        return await execute(case), False
    except Exception:
        return (
            CaseObservation(correlation_id=case.correlation_id, output_valid=False),
            True,
        )


async def _scores(
    case: EvaluationCase,
    observation: CaseObservation,
    score_provider: ScoreProvider | None,
) -> tuple[ScoreObservation, ...]:
    if not case.expectation.minimum_scores or score_provider is None:
        return ()
    try:
        provided = await score_provider(case, observation)
    except Exception:
        return ()
    return tuple(provided)


def _failure_counts(results: Sequence[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        for assertion in result.assertions:
            if assertion.passed:
                continue
            counts[assertion.name] = counts.get(assertion.name, 0) + 1
    return counts


def _assertion_map(report: EvaluationReport) -> dict[tuple[str, str], bool]:
    found: dict[tuple[str, str], bool] = {}
    for case in report.cases:
        for assertion in case.assertions:
            found[(case.name, assertion.name)] = assertion.passed
    return found
