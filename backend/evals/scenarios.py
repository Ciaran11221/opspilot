"""Fixed eval scenarios for OpsPilot's agent loop.

Each scenario is a prompt against the bundled synthetic demo dataset, plus
the tool-call behavior a correct run should exhibit. This is a small,
hand-curated set - the point isn't statistical coverage, it's a concrete,
versioned answer to "how do we know the agent behaves correctly," which is
otherwise just a claim.

What a scenario checks (see EvalResult in run_evals.py):
    - required_tools: every tool in this set must be called at least once.
    - forbidden_tools: none of these tools may be called.
    - no_error: the run must not end in an "error" trace event
      (e.g. hitting MAX_TURNS without a final answer).

This deliberately does NOT check exact tool-call arguments or the wording
of the final answer - the model has legitimate freedom in exactly how it
filters or phrases things. What's being scored is the higher-level, more
stable behavior: does it reach for the right tool(s) for this kind of
request, and does it complete without falling over.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalScenario:
    id: str
    prompt: str
    description: str
    required_tools: frozenset[str] = field(default_factory=frozenset)
    forbidden_tools: frozenset[str] = field(default_factory=frozenset)


SCENARIOS: list[EvalScenario] = [
    EvalScenario(
        id="inactive_elevated_accounts",
        prompt="Find inactive accounts with elevated permissions and draft offboarding tickets for each.",
        description="Core demo prompt - should query accounts with the right filters, then draft an artifact.",
        required_tools=frozenset({"query_accounts", "draft_report"}),
    ),
    EvalScenario(
        id="sla_risk_tickets",
        prompt="Which open tickets are at SLA risk this week?",
        description="Core demo prompt - should query tickets with an SLA-risk filter, no artifact expected.",
        required_tools=frozenset({"query_tickets"}),
        forbidden_tools=frozenset({"draft_report"}),
    ),
    EvalScenario(
        id="simple_priority_filter",
        prompt="List all P1 tickets.",
        description="Simple single-filter lookup - should not over-call tools or draft anything unasked.",
        required_tools=frozenset({"query_tickets"}),
        forbidden_tools=frozenset({"query_accounts", "draft_report"}),
    ),
    EvalScenario(
        id="account_count_question",
        prompt="How many accounts are currently suspended?",
        description="A count question still requires querying real data, not guessing a number.",
        required_tools=frozenset({"query_accounts"}),
        forbidden_tools=frozenset({"query_tickets"}),
    ),
    EvalScenario(
        id="hygiene_report_needs_data_first",
        prompt="Draft an account hygiene report covering any accounts without MFA enrolled.",
        description="Should gather account data before drafting, not draft from assumptions.",
        required_tools=frozenset({"query_accounts", "draft_report"}),
    ),
    EvalScenario(
        id="nonexistent_account_not_fabricated",
        prompt="Give me the last login date for the account 'definitely-not-a-real-user'.",
        description=(
            "Tests the system prompt's 'never invent account names or numbers' rule - the agent must "
            "query for the account rather than fabricate a plausible-looking answer."
        ),
        required_tools=frozenset({"query_accounts"}),
    ),
]
