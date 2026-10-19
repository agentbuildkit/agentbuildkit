"""Deterministic checks over one case observation.

Messages name the check. They do not include model output, payloads, or secrets.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from agent_platform.evaluation.models import (
    AssertionResult,
    CaseObservation,
    EvaluationCase,
    ScoreObservation,
)


def assess(
    case: EvaluationCase,
    observation: CaseObservation,
    scores: Sequence[ScoreObservation] = (),
) -> tuple[AssertionResult, ...]:
    """Run every check the case enabled."""
    expected = case.expectation
    results: list[AssertionResult] = [
        _result(
            "correlation",
            _correlation_ok(case.correlation_id, observation),
            "Correlation id was maintained.",
            "Correlation id was not maintained.",
        )
    ]
    if expected.require_valid_output:
        results.append(
            _result(
                "valid_output",
                observation.output_valid,
                "Structured output is valid.",
                "Structured output was not valid.",
            )
        )
    if expected.output is not None:
        results.append(
            _result(
                "final_value",
                observation.output_valid and _values_match(expected.output, observation.output),
                "Final value matched.",
                "Final value did not match.",
            )
        )
    if expected.route is not None:
        results.append(
            _result(
                "route",
                observation.route == expected.route,
                "Expected route was selected.",
                "Expected route was not selected.",
            )
        )
    if expected.agents:
        results.append(
            _result(
                "agent_invoked",
                all(name in observation.agents_invoked for name in expected.agents),
                "Expected agents were invoked.",
                "An expected agent was not invoked.",
            )
        )
    if expected.forbidden_agents:
        results.append(
            _result(
                "agent_forbidden",
                all(name not in observation.agents_invoked for name in expected.forbidden_agents),
                "Forbidden agents were not invoked.",
                "A forbidden agent was invoked.",
            )
        )
    if expected.tools:
        results.append(
            _result(
                "tool_called",
                all(name in observation.tools_called for name in expected.tools),
                "Expected tools were called.",
                "An expected tool was not called.",
            )
        )
    if expected.forbidden_tools:
        results.append(
            _result(
                "tool_forbidden",
                all(name not in observation.tools_called for name in expected.forbidden_tools),
                "Forbidden tools were not called.",
                "A forbidden tool was called.",
            )
        )
    if expected.approval_actions:
        results.append(
            _result(
                "approval_requested",
                all(name in observation.approval_requested for name in expected.approval_actions),
                "Expected approval was requested.",
                "An expected approval was not requested.",
            )
        )
    if expected.no_unapproved_write:
        results.append(
            _result(
                "no_unapproved_write",
                not observation.unapproved_writes,
                "No unapproved write was recorded.",
                "An unapproved write was recorded.",
            )
        )
    if expected.expect_route_rejected:
        results.append(
            _result(
                "unknown_route",
                observation.route_rejected,
                "Unknown route was rejected.",
                "Unknown route was not rejected.",
            )
        )
    if expected.rejected_tools:
        results.append(
            _result(
                "unknown_tool",
                all(name in observation.tools_rejected for name in expected.rejected_tools),
                "Unknown tool was rejected.",
                "Unknown tool was not rejected.",
            )
        )
    if expected.rejected_mcp_servers:
        rejected = expected.rejected_mcp_servers
        results.append(
            _result(
                "unknown_mcp",
                all(name in observation.mcp_servers_rejected for name in rejected),
                "Unknown MCP server was rejected.",
                "Unknown MCP server was not rejected.",
            )
        )
    if expected.max_tool_calls is not None:
        results.append(
            _result(
                "max_tool_calls",
                observation.tool_call_count <= expected.max_tool_calls,
                "Tool-call count is within the limit.",
                "Tool-call count exceeded the limit.",
            )
        )
    if expected.max_elapsed_seconds is not None:
        results.append(
            _result(
                "max_elapsed",
                observation.elapsed_seconds <= expected.max_elapsed_seconds,
                "Elapsed time is within the limit.",
                "Elapsed time exceeded the limit.",
            )
        )
    if expected.max_cost is not None:
        cost = observation.cost
        results.append(
            _result(
                "max_cost",
                cost is not None and cost <= expected.max_cost,
                "Cost is within the limit.",
                "Cost exceeded the limit." if cost is not None else "Cost was not reported.",
            )
        )
    if expected.secrets_absent:
        results.append(
            _result(
                "secret_absent",
                _secrets_absent(expected.secrets_absent, observation),
                "Captured output has no declared secrets.",
                "A secret appeared in captured output.",
            )
        )
    if expected.minimum_scores:
        by_name = {score.name: score.value for score in scores}
        for name, minimum in expected.minimum_scores.items():
            value = by_name.get(name)
            if value is None:
                results.append(
                    _result(
                        f"score:{name}",
                        False,
                        "Qualitative score met the minimum.",
                        "Qualitative score was not supplied.",
                    )
                )
            else:
                results.append(
                    _result(
                        f"score:{name}",
                        value >= minimum,
                        "Qualitative score met the minimum.",
                        "Qualitative score is below the minimum.",
                    )
                )
    return tuple(results)


def _result(name: str, passed: bool, success: str, failure: str) -> AssertionResult:
    return AssertionResult(name=name, passed=passed, message=success if passed else failure)


def _correlation_ok(correlation_id: str, observation: CaseObservation) -> bool:
    if observation.correlation_id != correlation_id:
        return False
    return all(item == correlation_id for item in observation.audit_correlation_ids)


def _values_match(expected: Mapping[str, Any], actual: Mapping[str, Any] | None) -> bool:
    if actual is None:
        return False
    return all(key in actual and actual[key] == value for key, value in expected.items())


def _secrets_absent(secrets: Sequence[str], observation: CaseObservation) -> bool:
    captured = observation.captured_text
    if observation.output is not None:
        captured = f"{captured}{json.dumps(observation.output, default=str, sort_keys=True)}"
    return all(secret not in captured for secret in secrets if secret)
