"""Unit tests for csv_ingest.py.

Deliberately exercises messy, inconsistent input - different header
spellings, mixed date formats, unrecognized vocabularies - since "handles
messy real-world exports gracefully" is the actual claim this module makes,
and it's the one most worth having a regression net around.
"""
from __future__ import annotations

from csv_ingest import (
    ACCOUNT_ALIASES,
    _match_headers,
    _normalize_header,
    ingest_accounts_csv,
    ingest_tickets_csv,
)


def csv_bytes(text: str) -> bytes:
    return text.strip("\n").encode("utf-8")


class TestNormalizeHeader:
    def test_strips_spacing_casing_and_punctuation(self):
        assert _normalize_header("Last Sign-In") == "lastsignin"
        assert _normalize_header(" User Principal Name ") == "userprincipalname"
        assert _normalize_header("MFA_Enabled") == "mfaenabled"


class TestMatchHeaders:
    def test_exact_alias_match(self):
        mapping, report = _match_headers(["Username", "Status", "Last Login"], ACCOUNT_ALIASES)
        assert mapping["username"] == "Username"
        assert mapping["status"] == "Status"
        assert mapping["lastLogin"] == "Last Login"
        statuses = {r["field"]: r["confidence"] for r in report}
        assert statuses["username"] == "exact"

    def test_fuzzy_match_catches_typo(self):
        # "Statuss" is a typo but close enough to fuzzy-match "status".
        mapping, report = _match_headers(["Statuss"], ACCOUNT_ALIASES)
        assert mapping.get("status") == "Statuss"

    def test_unmatched_field_reported_as_none(self):
        mapping, report = _match_headers(["Username"], ACCOUNT_ALIASES)
        dept_entry = next(r for r in report if r["field"] == "department")
        assert dept_entry["header"] is None
        assert dept_entry["confidence"] is None

    def test_each_header_consumed_at_most_once(self):
        # "user" is an alias for both username and displayName - only one
        # canonical field should claim it.
        mapping, _ = _match_headers(["user"], ACCOUNT_ALIASES)
        claimants = [field for field, header in mapping.items() if header == "user"]
        assert len(claimants) == 1


class TestIngestAccountsCsv:
    def test_happy_path_exact_headers(self):
        csv_text = """
Username,Status,Last Login,Title,Groups
alice,Active,2026-07-01,Support Engineer,Everyone;Helpdesk
"""
        result = ingest_accounts_csv(csv_bytes(csv_text))
        assert result["row_count"] == 1
        record = result["records"][0]
        assert record["username"] == "alice"
        assert record["status"] == "ACTIVE"
        assert record["lastLogin"] == "2026-07-01T00:00:00Z"
        assert record["groups"] == ["Everyone", "Helpdesk"]
        assert result["warnings"] == []

    def test_messy_headers_and_status_vocabulary(self):
        csv_text = """
SAMAccountName,AccountStatus,LastSignIn
bob,Disabled,07/01/2026
"""
        result = ingest_accounts_csv(csv_bytes(csv_text))
        record = result["records"][0]
        assert record["username"] == "bob"
        assert record["status"] == "SUSPENDED"
        assert record["lastLogin"] == "2026-07-01T00:00:00Z"

    def test_unparseable_date_is_flagged_not_guessed(self):
        csv_text = """
Username,Status,Last Login
carol,Active,not-a-real-date
"""
        result = ingest_accounts_csv(csv_bytes(csv_text))
        record = result["records"][0]
        assert record["lastLogin"] is None
        assert any("lastLogin" in w for w in result["warnings"])

    def test_missing_required_column_is_warned(self):
        csv_text = """
Username
dave
"""
        result = ingest_accounts_csv(csv_bytes(csv_text))
        assert any("status" in w and "lastLogin" in w for w in result["warnings"])

    def test_id_falls_back_to_username_then_row_number(self):
        csv_text = """
Username,Email
,eve@example.com
,
"""
        result = ingest_accounts_csv(csv_bytes(csv_text))
        assert result["records"][0]["id"] == "eve@example.com"
        assert result["records"][1]["id"] == "row-2"

    def test_unrecognized_status_passed_through_unchanged(self):
        csv_text = """
Username,Status
frank,Pending Review
"""
        result = ingest_accounts_csv(csv_bytes(csv_text))
        assert result["records"][0]["status"] == "Pending Review"


class TestIngestTicketsCsv:
    def test_happy_path_exact_headers(self):
        csv_text = """
Key,Status,Priority,Created
OPS-1,Open,P1,2026-07-01T00:00:00Z
"""
        result = ingest_tickets_csv(csv_bytes(csv_text))
        record = result["records"][0]
        assert record["key"] == "OPS-1"
        assert record["status"] == "Open"
        assert record["priority"] == "P1"
        assert record["slaHours"] == 4
        assert record["elapsedHours"] is not None

    def test_messy_status_and_priority_vocabulary(self):
        csv_text = """
IssueKey,State,Severity,OpenDate
JIRA-9,In Review,Critical,2026-07-01
"""
        result = ingest_tickets_csv(csv_bytes(csv_text))
        record = result["records"][0]
        assert record["status"] == "In Progress"
        assert record["priority"] == "P1"
        assert record["slaHours"] == 4

    def test_unrecognized_priority_defaults_to_p3_and_warns(self):
        csv_text = """
Key,Status,Priority,Created
OPS-2,Open,Whenever,2026-07-01
"""
        result = ingest_tickets_csv(csv_bytes(csv_text))
        record = result["records"][0]
        assert record["priority"] == "P3"
        assert record["slaHours"] == 72
        assert any("priority" in w for w in result["warnings"])

    def test_elapsed_hours_uses_updated_for_resolved_tickets(self):
        csv_text = """
Key,Status,Priority,Created,Updated
OPS-3,Resolved,P2,2026-07-01T00:00:00Z,2026-07-02T00:00:00Z
"""
        result = ingest_tickets_csv(csv_bytes(csv_text))
        record = result["records"][0]
        assert record["elapsedHours"] == 24.0

    def test_elapsed_hours_uses_now_for_open_tickets(self):
        csv_text = """
Key,Status,Priority,Created
OPS-4,Open,P2,2026-07-01T00:00:00Z
"""
        result = ingest_tickets_csv(csv_bytes(csv_text))
        record = result["records"][0]
        # Open ticket with no "Updated" column - elapsed is created -> now,
        # so it should be a large positive number, not None or negative.
        assert record["elapsedHours"] > 0

    def test_missing_created_column_produces_no_elapsed_hours(self):
        csv_text = """
Key,Status,Priority
OPS-5,Open,P1
"""
        result = ingest_tickets_csv(csv_bytes(csv_text))
        record = result["records"][0]
        assert record["created"] is None
        assert record["elapsedHours"] is None

    def test_key_defaults_to_row_number_when_unmatched(self):
        csv_text = """
Status,Priority,Created
Open,P1,2026-07-01
"""
        result = ingest_tickets_csv(csv_bytes(csv_text))
        assert result["records"][0]["key"] == "ROW-1"
