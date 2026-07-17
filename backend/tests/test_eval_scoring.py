"""Unit tests for the eval harness's scoring logic (EvalResult.passed).

Deliberately does not call the real API - see backend/evals/run_evals.py's
own docstring for why that's a separate, manually-run concern. This just
verifies the pass/fail rule itself is correct, since a wrong scoring rule
would silently make every eval run meaningless.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "evals"))

from run_evals import EvalResult  # noqa: E402
from scenarios import EvalScenario  # noqa: E402


def make_scenario(**overrides) -> EvalScenario:
    defaults = dict(
        id="test",
        prompt="test prompt",
        description="test",
        required_tools=frozenset({"query_accounts"}),
        forbidden_tools=frozenset(),
    )
    defaults.update(overrides)
    return EvalScenario(**defaults)


def test_passes_when_required_tool_called_and_nothing_forbidden():
    result = EvalResult(
        scenario=make_scenario(required_tools=frozenset({"query_accounts"})),
        called_tools={"query_accounts"},
        had_error=False,
        error_text=None,
        final_text="done",
    )
    assert result.passed is True
    assert result.failure_reason is None


def test_fails_when_required_tool_missing():
    result = EvalResult(
        scenario=make_scenario(required_tools=frozenset({"query_accounts", "draft_report"})),
        called_tools={"query_accounts"},
        had_error=False,
        error_text=None,
        final_text="done",
    )
    assert result.passed is False
    assert "draft_report" in result.failure_reason


def test_fails_when_forbidden_tool_called():
    result = EvalResult(
        scenario=make_scenario(
            required_tools=frozenset({"query_tickets"}), forbidden_tools=frozenset({"draft_report"})
        ),
        called_tools={"query_tickets", "draft_report"},
        had_error=False,
        error_text=None,
        final_text="done",
    )
    assert result.passed is False
    assert "draft_report" in result.failure_reason


def test_fails_when_agent_errored_even_if_tools_look_right():
    result = EvalResult(
        scenario=make_scenario(required_tools=frozenset({"query_accounts"})),
        called_tools={"query_accounts"},
        had_error=True,
        error_text="Stopped after 6 turns without a final answer.",
        final_text=None,
    )
    assert result.passed is False
    assert "errored" in result.failure_reason


def test_extra_non_forbidden_tool_calls_dont_fail_a_scenario():
    # Calling more tools than strictly required isn't penalized - only
    # missing a required tool or calling an explicitly forbidden one is.
    result = EvalResult(
        scenario=make_scenario(required_tools=frozenset({"query_accounts"}), forbidden_tools=frozenset()),
        called_tools={"query_accounts", "query_tickets"},
        had_error=False,
        error_text=None,
        final_text="done",
    )
    assert result.passed is True
