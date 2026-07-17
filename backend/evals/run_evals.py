"""Runs OpsPilot's eval scenarios against the real Claude API and reports
tool-selection accuracy.

This is intentionally separate from the pytest suite (backend/tests/):
pytest verifies the agent *loop's* control flow using a mocked client, fast
and free, on every push. This script verifies actual model *behavior* -
does it pick the right tool for a given request - which requires a real API
call and therefore real (small) cost. Run it manually when you change the
system prompt, tool descriptions, or model, not on every commit.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python backend/evals/run_evals.py
    python backend/evals/run_evals.py --model claude-sonnet-5
    python backend/evals/run_evals.py --scenario sla_risk_tickets --verbose

Cost: each scenario is one short agent run (typically 1-3 tool-use turns)
against Haiku by default - a handful of scenarios costs a fraction of a
cent. Sonnet costs more; pass --model explicitly if you want to eval a
different model than OPSPILOT_MODEL/agent.py's default.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

# backend/ isn't a package (main.py, agent.py, etc. are flat modules run
# with backend/ as the working directory) - add it to sys.path so this
# script can `import agent` the same way main.py and the test suite do,
# regardless of what directory it's invoked from.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent  # noqa: E402
from evals.scenarios import SCENARIOS, EvalScenario  # noqa: E402


@dataclass
class EvalResult:
    scenario: EvalScenario
    called_tools: set[str]
    had_error: bool
    error_text: str | None
    final_text: str | None

    @property
    def passed(self) -> bool:
        if self.had_error:
            return False
        if not self.scenario.required_tools.issubset(self.called_tools):
            return False
        if self.scenario.forbidden_tools & self.called_tools:
            return False
        return True

    @property
    def failure_reason(self) -> str | None:
        if self.passed:
            return None
        if self.had_error:
            return f"agent errored: {self.error_text}"
        missing = self.scenario.required_tools - self.called_tools
        if missing:
            return f"missing required tool(s): {', '.join(sorted(missing))}"
        extra = self.scenario.forbidden_tools & self.called_tools
        if extra:
            return f"called forbidden tool(s): {', '.join(sorted(extra))}"
        return "unknown"  # pragma: no cover - passed/failure_reason should be exhaustive above


async def run_scenario(scenario: EvalScenario, api_key: str) -> EvalResult:
    called_tools: set[str] = set()
    had_error = False
    error_text: str | None = None
    final_text: str | None = None

    async for event in agent.run_agent(scenario.prompt, api_key, dataset_id=None):
        if event["type"] == "tool_call":
            called_tools.add(event["name"])
        elif event["type"] == "error":
            had_error = True
            error_text = event["text"]
        elif event["type"] == "final":
            final_text = event["text"]

    return EvalResult(
        scenario=scenario,
        called_tools=called_tools,
        had_error=had_error,
        error_text=error_text,
        final_text=final_text,
    )


async def run_all(scenarios: list[EvalScenario], api_key: str, verbose: bool) -> list[EvalResult]:
    results = []
    for scenario in scenarios:
        print(f"  running: {scenario.id} ...", end=" ", flush=True)
        result = await run_scenario(scenario, api_key)
        print("PASS" if result.passed else "FAIL")
        if verbose or not result.passed:
            print(f"    prompt:   {scenario.prompt}")
            print(f"    required: {sorted(scenario.required_tools) or '(none)'}")
            print(f"    called:   {sorted(result.called_tools) or '(none)'}")
            if result.final_text:
                print(f"    answer:   {result.final_text[:160]}{'...' if len(result.final_text) > 160 else ''}")
            if not result.passed:
                print(f"    reason:   {result.failure_reason}")
        results.append(result)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=None, help="Override OPSPILOT_MODEL/agent.py's default model for this run")
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"), help="Anthropic API key")
    parser.add_argument("--scenario", default=None, help="Run only the scenario with this id")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Print full detail for every scenario, not just failures"
    )
    args = parser.parse_args()

    if not args.api_key:
        print("Error: no API key. Set ANTHROPIC_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    if args.model:
        agent.MODEL = args.model

    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [s for s in SCENARIOS if s.id == args.scenario]
        if not scenarios:
            known = ", ".join(s.id for s in SCENARIOS)
            print(f"Error: no scenario '{args.scenario}'. Known scenarios: {known}", file=sys.stderr)
            return 2

    print(f"OpsPilot eval run - model: {agent.MODEL} - {len(scenarios)} scenario(s)")
    print("This makes real Claude API calls and will incur a small cost.\n")

    results = asyncio.run(run_all(scenarios, args.api_key, args.verbose))

    passed = sum(r.passed for r in results)
    total = len(results)
    print(f"\n{passed}/{total} scenarios passed ({100 * passed / total:.0f}%)")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
