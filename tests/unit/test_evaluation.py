"""Offline evaluation suite. No model or network access."""

from typing import Any

import pytest

from agent_platform.evaluation import (
    CaseObservation,
    EvaluationCase,
    Expectation,
    ScoreObservation,
    compare_reports,
    run_evaluation,
    trace_from_audit,
)
from agent_platform.observability import AuditEvent, AuditLog

SECRET = "sk-live-eval-secret"


def _case(
    name: str,
    expectation: Expectation,
    *,
    correlation_id: str = "corr-eval",
) -> EvaluationCase:
    return EvaluationCase(name=name, correlation_id=correlation_id, expectation=expectation)


def _ok(**updates: Any) -> CaseObservation:
    base = CaseObservation(
        correlation_id="corr-eval",
        output_valid=True,
        output={"status": "ready"},
        route="alpha",
        agents_invoked=("classifier", "reviewer"),
        tools_called=("lookup_status",),
        elapsed_seconds=0.2,
        tool_call_count=1,
        cost=0.1,
    )
    return base.model_copy(update=updates)


async def test_passing_suite_reports_full_pass_rate() -> None:
    cases = (
        _case(
            "ready",
            Expectation(require_valid_output=True, output={"status": "ready"}, route="alpha"),
        ),
        _case(
            "reviewed",
            Expectation(agents=("reviewer",), tools=("lookup_status",), max_tool_calls=1),
        ),
    )

    async def execute(case: EvaluationCase) -> CaseObservation:
        del case
        return _ok()

    report = await run_evaluation(cases, execute)

    assert report.total == 2
    assert report.passed == 2
    assert report.failed == 0
    assert report.pass_rate == 1.0
    assert report.failures_by_assertion == {}
    assert report.elapsed_seconds >= 0
    assert [item.name for item in report.cases] == ["ready", "reviewed"]


async def test_failing_case_is_recorded_and_later_cases_still_run() -> None:
    cases = (
        _case("wrong-route", Expectation(route="alpha")),
        _case("later", Expectation(require_valid_output=True)),
    )

    async def execute(case: EvaluationCase) -> CaseObservation:
        if case.name == "wrong-route":
            return _ok(route="beta")
        return _ok()

    report = await run_evaluation(cases, execute)

    assert report.total == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.pass_rate == 0.5
    assert report.cases[0].passed is False
    assert report.cases[1].passed is True
    assert report.failures_by_assertion == {"route": 1}
    failed = next(item for item in report.cases[0].assertions if not item.passed)
    assert failed.message == "Expected route was not selected."


async def test_multiple_assertion_failures_are_listed() -> None:
    case = _case(
        "several",
        Expectation(
            route="alpha",
            forbidden_tools=("send_notice",),
            max_tool_calls=1,
            max_elapsed_seconds=1.0,
        ),
    )

    async def execute(case: EvaluationCase) -> CaseObservation:
        del case
        return _ok(
            route="beta",
            tools_called=("lookup_status", "send_notice"),
            tool_call_count=3,
            elapsed_seconds=2.5,
        )

    report = await run_evaluation((case,), execute)
    failed = {item.name: item.message for item in report.cases[0].assertions if not item.passed}

    assert report.passed == 0
    assert failed == {
        "route": "Expected route was not selected.",
        "tool_forbidden": "A forbidden tool was called.",
        "max_tool_calls": "Tool-call count exceeded the limit.",
        "max_elapsed": "Elapsed time exceeded the limit.",
    }
    assert report.failures_by_assertion == {name: 1 for name in failed}


async def test_malformed_output_is_a_failure_and_does_not_stop_the_suite() -> None:
    cases = (
        _case("malformed", Expectation(require_valid_output=True, output={"status": "ready"})),
        _case("next", Expectation(require_valid_output=True)),
    )

    async def execute(case: EvaluationCase) -> CaseObservation:
        if case.name == "malformed":
            raise ValueError(f"schema rejected {SECRET}")
        return _ok()

    report = await run_evaluation(cases, execute)
    rendered = report.model_dump_json()

    assert report.cases[0].passed is False
    assert report.cases[1].passed is True
    assert "execution" in {item.name for item in report.cases[0].assertions if not item.passed}
    assert "valid_output" in {item.name for item in report.cases[0].assertions if not item.passed}
    assert SECRET not in rendered
    assert "schema rejected" not in rendered


async def test_security_assertions_detect_policy_failures_without_echoing_the_secret() -> None:
    case = _case(
        "policy",
        Expectation(
            no_unapproved_write=True,
            expect_route_rejected=True,
            rejected_tools=("unknown_tool",),
            rejected_mcp_servers=("unknown-server",),
            secrets_absent=(SECRET,),
            forbidden_agents=("outsider",),
            approval_actions=("send_notice",),
        ),
    )
    observation = _ok(
        route_rejected=False,
        tools_rejected=(),
        mcp_servers_rejected=(),
        unapproved_writes=("send_notice",),
        agents_invoked=("classifier", "outsider"),
        approval_requested=(),
        captured_text=f"token {SECRET}",
        output={"note": SECRET},
        correlation_id="other-run",
        audit_correlation_ids=("other-run",),
    )

    async def execute(ignored: EvaluationCase) -> CaseObservation:
        del ignored
        return observation

    report = await run_evaluation((case,), execute)
    failed = {item.name for item in report.cases[0].assertions if not item.passed}

    assert failed == {
        "correlation",
        "no_unapproved_write",
        "unknown_route",
        "unknown_tool",
        "unknown_mcp",
        "secret_absent",
        "agent_forbidden",
        "approval_requested",
    }
    assert SECRET not in report.model_dump_json()


async def test_optional_score_uses_the_supplied_provider() -> None:
    case = _case("tone", Expectation(minimum_scores={"relevance": 0.8}))

    async def execute(ignored: EvaluationCase) -> CaseObservation:
        del ignored
        return _ok()

    async def score(
        ignored: EvaluationCase,
        observation: CaseObservation,
    ) -> tuple[ScoreObservation, ...]:
        del ignored, observation
        return (ScoreObservation(name="relevance", value=0.4),)

    report = await run_evaluation((case,), execute, score_provider=score)
    assertion = next(item for item in report.cases[0].assertions if item.name == "score:relevance")

    assert assertion.passed is False
    assert assertion.message == "Qualitative score is below the minimum."


async def test_regression_compares_a_saved_baseline() -> None:
    baseline_case = _case("ready", Expectation(route="alpha"))
    current_case = _case("ready", Expectation(route="alpha"))

    async def pass_run(case: EvaluationCase) -> CaseObservation:
        del case
        return _ok()

    async def fail_run(case: EvaluationCase) -> CaseObservation:
        del case
        return _ok(route="beta")

    baseline = await run_evaluation((baseline_case,), pass_run)
    current = await run_evaluation((current_case,), fail_run)
    comparison = compare_reports(baseline, current)

    assert comparison.pass_rate_delta == pytest.approx(-1.0)
    assert comparison.regressed == ("ready",)
    assert comparison.improved == ()
    assert comparison.changes[0].assertion == "route"
    assert comparison.changes[0].previous is True
    assert comparison.changes[0].current is False


def test_audit_trace_feeds_tool_approval_and_correlation_checks() -> None:
    log = AuditLog()
    log.record(
        AuditEvent(
            correlation_id="corr-eval",
            agent_name="classifier",
            tool_name="classifier",
            approval="not_required",
            outcome="completed",
            argument_hash="agent-run",
        )
    )
    log.record(
        AuditEvent(
            correlation_id="corr-eval",
            agent_name="classifier",
            tool_name="lookup_status",
            approval="not_required",
            outcome="completed",
            argument_hash="abc",
        )
    )
    log.record(
        AuditEvent(
            correlation_id="corr-other",
            agent_name="classifier",
            tool_name="send_notice",
            approval="pending",
            outcome="completed",
            argument_hash="def",
        )
    )
    log.record(
        AuditEvent(
            correlation_id="corr-eval",
            agent_name="records",
            tool_name="delete_note",
            approval="rejected",
            outcome="failed",
            argument_hash="ghi",
        )
    )

    observation = trace_from_audit(log, correlation_id="corr-eval")

    assert observation.agents_invoked == ("classifier",)
    assert observation.tools_called == ("lookup_status", "send_notice")
    assert observation.unapproved_writes == ("send_notice",)
    assert observation.tools_rejected == ("delete_note",)
    assert observation.tool_call_count == 2
    assert "corr-other" in observation.audit_correlation_ids
