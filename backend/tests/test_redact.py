"""Unit tests for redact.py.

These test the redaction/rehydration layer in isolation, independent of the
agent loop - RedactionMap's tokenize/rehydrate contract, and the
account/ticket-level redact_* helpers built on top of it.
"""
from __future__ import annotations

from redact import RedactionMap, redact_accounts, redact_tickets


def test_tokenize_same_value_returns_same_token():
    rmap = RedactionMap()
    first = rmap.tokenize("cian@example.com", "EMAIL")
    second = rmap.tokenize("cian@example.com", "EMAIL")
    assert first == second


def test_tokenize_different_values_get_different_tokens():
    rmap = RedactionMap()
    a = rmap.tokenize("cian@example.com", "EMAIL")
    b = rmap.tokenize("roisin@example.com", "EMAIL")
    assert a != b


def test_tokenize_falsy_value_passes_through():
    rmap = RedactionMap()
    assert rmap.tokenize("", "EMAIL") == ""
    assert rmap.tokenize(None, "EMAIL") is None


def test_rehydrate_reverses_tokenize():
    rmap = RedactionMap()
    token = rmap.tokenize("Cian Brennan", "PERSON")
    assert rmap.rehydrate(f"Please notify {token} about this.") == "Please notify Cian Brennan about this."


def test_rehydrate_walks_nested_structures():
    rmap = RedactionMap()
    token = rmap.tokenize("Cian Brennan", "PERSON")
    nested = {"accounts": [{"displayName": token, "id": "00u1"}], "count": 1}
    rehydrated = rmap.rehydrate(nested)
    assert rehydrated == {"accounts": [{"displayName": "Cian Brennan", "id": "00u1"}], "count": 1}


def test_rehydrate_leaves_non_string_values_untouched():
    rmap = RedactionMap()
    assert rmap.rehydrate(42) == 42
    assert rmap.rehydrate(True) is True
    assert rmap.rehydrate(None) is None


def test_redact_free_text_catches_email_and_phone():
    rmap = RedactionMap()
    redacted = rmap.redact_free_text("Contact john@example.com or 087-123-4567 for details.")
    assert "john@example.com" not in redacted
    assert "087-123-4567" not in redacted
    assert rmap.rehydrate(redacted) == "Contact john@example.com or 087-123-4567 for details."


def test_redact_free_text_catches_already_known_name():
    rmap = RedactionMap()
    rmap.tokenize("Cian Brennan", "PERSON")
    redacted = rmap.redact_free_text("Escalated by Cian Brennan this morning.")
    assert "Cian Brennan" not in redacted
    assert rmap.rehydrate(redacted) == "Escalated by Cian Brennan this morning."


def test_redact_free_text_does_not_catch_unseen_name():
    """Documents the stated scope limit: a name never seen in a structured
    field, and not an email/phone pattern, is not caught by free-text
    scanning alone."""
    rmap = RedactionMap()
    redacted = rmap.redact_free_text("Escalated by Someone Unlisted this morning.")
    assert "Someone Unlisted" in redacted


def test_redact_accounts_tokenizes_pii_fields_and_preserves_others():
    accounts = [
        {
            "id": "00u1",
            "username": "cian.brennan",
            "email": "cian.brennan@example.com",
            "displayName": "Cian Brennan",
            "status": "ACTIVE",
            "title": "Finance Manager",
        }
    ]
    rmap = RedactionMap()
    redacted = redact_accounts(accounts, rmap)

    assert redacted[0]["id"] == "00u1"
    assert redacted[0]["status"] == "ACTIVE"
    assert redacted[0]["title"] == "Finance Manager"
    assert redacted[0]["username"] != "cian.brennan"
    assert redacted[0]["email"] != "cian.brennan@example.com"
    assert redacted[0]["displayName"] != "Cian Brennan"

    # Original records are not mutated.
    assert accounts[0]["displayName"] == "Cian Brennan"

    # Round-trips back to the real values.
    assert rmap.rehydrate(redacted[0]["displayName"]) == "Cian Brennan"


def test_redact_accounts_reuses_token_for_repeat_appearance():
    accounts = [
        {"id": "00u1", "email": "same@example.com", "username": "u1", "displayName": "A"},
        {"id": "00u2", "email": "same@example.com", "username": "u2", "displayName": "B"},
    ]
    rmap = RedactionMap()
    redacted = redact_accounts(accounts, rmap)
    assert redacted[0]["email"] == redacted[1]["email"]


def test_redact_tickets_tokenizes_pii_and_free_text():
    tickets = [
        {
            "key": "OPS-1",
            "status": "Open",
            "priority": "P1",
            "assignee": "Roisin Kelly",
            "reporterEmail": "user9@example.com",
            "summary": "Please contact Roisin Kelly to confirm offboarding.",
        }
    ]
    rmap = RedactionMap()
    redacted = redact_tickets(tickets, rmap)

    assert redacted[0]["key"] == "OPS-1"
    assert redacted[0]["status"] == "Open"
    assert redacted[0]["assignee"] != "Roisin Kelly"
    assert redacted[0]["reporterEmail"] != "user9@example.com"
    assert "Roisin Kelly" not in redacted[0]["summary"]

    assert rmap.rehydrate(redacted[0]["summary"]) == "Please contact Roisin Kelly to confirm offboarding."


def test_redact_tickets_original_records_not_mutated():
    tickets = [{"key": "OPS-1", "assignee": "Roisin Kelly", "summary": "Notify Roisin Kelly."}]
    rmap = RedactionMap()
    redact_tickets(tickets, rmap)
    assert tickets[0]["assignee"] == "Roisin Kelly"
    assert tickets[0]["summary"] == "Notify Roisin Kelly."
