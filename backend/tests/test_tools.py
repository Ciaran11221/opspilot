"""Unit tests for tools.py.

These test the tool *implementations* directly, with hand-built account/
ticket records, independent of the agent loop or the Claude API. That's a
deliberate boundary: agent.py's job is to call these correctly and stream
the results, tools.py's job is to filter/derive data correctly - each is
testable in isolation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from tools import build_tool_implementations, draft_report, query_accounts, query_tickets

NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def make_account(**overrides):
    account = {
        "id": "alice",
        "username": "alice",
        "status": "ACTIVE",
        "title": "Support Engineer",
        "groups": ["Everyone"],
        "lastLogin": "2026-07-13T00:00:00Z",
        "_scenarioTag": "control",
    }
    account.update(overrides)
    return account


def make_ticket(**overrides):
    ticket = {
        "key": "OPS-1",
        "status": "Open",
        "priority": "P2",
        "slaHours": 24,
        "elapsedHours": 2,
        "_scenarioTag": "control",
    }
    ticket.update(overrides)
    return ticket


class TestQueryAccounts:
    def test_status_filter(self):
        accounts = [make_account(status="ACTIVE"), make_account(id="bob", status="SUSPENDED")]
        result = query_accounts(accounts, NOW, status="SUSPENDED")
        assert result["count"] == 1
        assert result["accounts"][0]["id"] == "bob"

    def test_min_inactive_days(self):
        accounts = [
            make_account(id="recent", lastLogin="2026-07-13T00:00:00Z"),  # 1 day ago
            make_account(id="stale", lastLogin="2026-01-01T00:00:00Z"),  # >90 days ago
        ]
        result = query_accounts(accounts, NOW, min_inactive_days=90)
        assert result["count"] == 1
        assert result["accounts"][0]["id"] == "stale"

    def test_min_inactive_days_skips_unparseable_and_notes_it(self):
        accounts = [
            make_account(id="no_date", lastLogin=None),
            make_account(id="bad_date", lastLogin="not-a-date"),
            make_account(id="stale", lastLogin="2026-01-01T00:00:00Z"),
        ]
        result = query_accounts(accounts, NOW, min_inactive_days=90)
        assert result["count"] == 1
        assert result["accounts"][0]["id"] == "stale"
        assert "2 account(s)" in result["note"]

    @pytest.mark.parametrize(
        "title,groups,expected",
        [
            ("Domain Admin", [], True),
            ("Support Engineer", ["Billing-Admins"], True),
            ("Support Engineer", ["Everyone"], False),
            ("Global Admin", [], True),
            ("Root", [], True),
        ],
    )
    def test_elevated_only_matches_keywords_case_insensitively(self, title, groups, expected):
        accounts = [make_account(title=title, groups=groups)]
        result = query_accounts(accounts, NOW, elevated_only=True)
        assert (result["count"] == 1) is expected

    def test_strips_internal_scenario_tag(self):
        accounts = [make_account()]
        result = query_accounts(accounts, NOW)
        assert "_scenarioTag" not in result["accounts"][0]

    def test_no_filters_returns_everything(self):
        accounts = [make_account(id="a"), make_account(id="b")]
        result = query_accounts(accounts, NOW)
        assert result["count"] == 2
        assert "note" not in result


class TestQueryTickets:
    def test_status_and_priority_filters_combine(self):
        tickets = [
            make_ticket(key="A", status="Open", priority="P1"),
            make_ticket(key="B", status="Open", priority="P2"),
            make_ticket(key="C", status="Resolved", priority="P1"),
        ]
        result = query_tickets(tickets, status="Open", priority="P1")
        assert result["count"] == 1
        assert result["tickets"][0]["key"] == "A"

    def test_sla_risk_only_uses_threshold(self):
        tickets = [
            make_ticket(key="under", status="Open", slaHours=24, elapsedHours=10),  # 0.42
            make_ticket(key="over", status="Open", slaHours=24, elapsedHours=20),  # 0.83
        ]
        result = query_tickets(tickets, sla_risk_only=True, sla_risk_threshold=0.8)
        assert result["count"] == 1
        assert result["tickets"][0]["key"] == "over"
        assert result["tickets"][0]["slaRatio"] == pytest.approx(0.83, abs=0.01)

    def test_sla_risk_only_ignores_resolved_tickets(self):
        tickets = [make_ticket(key="done", status="Resolved", slaHours=24, elapsedHours=100)]
        result = query_tickets(tickets, sla_risk_only=True)
        assert result["count"] == 0

    def test_sla_risk_only_skips_and_notes_undeterminable_tickets(self):
        tickets = [
            make_ticket(key="no_sla", status="Open", slaHours=None, elapsedHours=10),
            make_ticket(key="ok", status="Open", slaHours=24, elapsedHours=23),
        ]
        result = query_tickets(tickets, sla_risk_only=True)
        assert result["count"] == 1
        assert result["tickets"][0]["key"] == "ok"
        assert "1 ticket(s)" in result["note"]

    def test_sla_ratio_only_added_when_derivable(self):
        tickets = [make_ticket(key="no_sla_data", slaHours=None, elapsedHours=None)]
        result = query_tickets(tickets)
        assert "slaRatio" not in result["tickets"][0]


class TestDraftReport:
    def test_shape_and_draft_only_status(self):
        result = draft_report(
            NOW,
            title="Offboard alice",
            report_type="offboarding_ticket",
            body_markdown="# Offboard alice\n...",
            related_ids=["alice"],
        )
        assert result["title"] == "Offboard alice"
        assert result["reportType"] == "offboarding_ticket"
        assert result["relatedIds"] == ["alice"]
        assert result["draftedAt"] == "2026-07-14T00:00:00Z"
        assert "DRAFT" in result["status"]

    def test_related_ids_defaults_to_empty_list(self):
        result = draft_report(NOW, title="t", report_type="sla_risk_report", body_markdown="body")
        assert result["relatedIds"] == []


class TestBuildToolImplementations:
    def test_binds_callables_for_all_three_tools(self):
        impls = build_tool_implementations([make_account()], [make_ticket()], now=NOW)
        assert set(impls.keys()) == {"query_accounts", "query_tickets", "draft_report"}
        # Bound callables should be directly invokable with just the tool's
        # own arguments, e.g. as the agent loop calls them: impl(**tool_input)
        assert impls["query_accounts"](status="ACTIVE")["count"] == 1
        assert impls["query_tickets"](status="Open")["count"] == 1

    def test_defaults_now_to_current_time_when_not_given(self):
        impls = build_tool_implementations([], [])
        result = impls["draft_report"](title="t", report_type="sla_risk_report", body_markdown="b")
        drafted_year = int(result["draftedAt"][:4])
        assert drafted_year >= 2026
