"""
PII redaction / rehydration layer.

Sits between a dataset (demo or user-uploaded) and the Claude API: known
PII-bearing fields are replaced with stable, opaque tokens before any
record reaches the model, and the same mapping is used to rehydrate those
tokens back to real values before anything is shown to the human user
(trace panel, tool results, final answer, drafted reports). What goes
back into the model's own conversation history stays tokenized - real
values never re-enter the model, even across a multi-turn tool loop.

Scope (deliberate, stated - see README honesty notes):
- Covers structured fields with a known schema: email/username/displayName
  on accounts, assignee/reporterEmail on tickets.
- Also scans free-text fields (ticket summaries, drafted report bodies) for
  email addresses and phone numbers via regex, and for exact matches of
  names already seen in a structured field.
- This is NOT a general-purpose PII/NER scrubber. A name or identifier that
  appears ONLY in free text, and was never seen in a structured field, and
  isn't an email/phone pattern, will not be caught. That is a real limit on
  what this catches, not a bug to silently paper over.
"""
from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
# Matches phone-number-shaped runs of digits (7+ digits, optionally grouped
# with spaces/hyphens/dots, optional leading +). Deliberately permissive -
# false positives (e.g. an order number) are safer here than false negatives.
PHONE_RE = re.compile(r"\+?\d[\d\-.\s]{6,}\d")

_FIELD_KIND: dict[str, str] = {
    "email": "EMAIL",
    "reporterEmail": "EMAIL",
    "username": "USER",
    "displayName": "PERSON",
    "assignee": "PERSON",
}

ACCOUNT_PII_FIELDS: tuple[str, ...] = ("email", "username", "displayName")
TICKET_PII_FIELDS: tuple[str, ...] = ("assignee", "reporterEmail")
TICKET_FREE_TEXT_FIELDS: tuple[str, ...] = ("summary",)


class RedactionMap:
    """Per-request PII <-> token mapping.

    One instance is created per agent run (see ``agent.run_agent``) so
    tokens are stable within a single conversation - the same real value
    always maps to the same token across every turn and every tool call -
    but never persisted or reused across different requests.
    """

    def __init__(self) -> None:
        self._forward: dict[str, str] = {}  # real value -> token
        self._reverse: dict[str, str] = {}  # token -> real value
        self._counters: dict[str, int] = {}

    def tokenize(self, value: Any, kind: str) -> Any:
        """Replace a single scalar PII value with a stable token.

        Args:
            value: The real value (e.g. an email address). Falsy values
                (None, "") pass through unchanged - there's nothing to redact.
            kind: A short label (e.g. ``"EMAIL"``, ``"PERSON"``) used as the
                token's prefix, purely for readability in the redacted view.

        Returns:
            The existing token if this exact value has been seen before in
            this request, otherwise a newly minted ``[KIND_n]`` token.
        """
        if not value:
            return value
        if value in self._forward:
            return self._forward[value]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        token = f"[{kind}_{self._counters[kind]}]"
        self._forward[value] = token
        self._reverse[token] = value
        return token

    def redact_free_text(self, text: str | None) -> str | None:
        """Redact a free-text field: regex hits, then any already-known names.

        Args:
            text: Free-text content such as a ticket summary or drafted
                report body.

        Returns:
            The text with emails, phone-shaped numbers, and any name already
            known from a structured field replaced with their tokens.
        """
        if not text:
            return text
        text = EMAIL_RE.sub(lambda m: self.tokenize(m.group(0), "EMAIL"), text)
        text = PHONE_RE.sub(lambda m: self.tokenize(m.group(0), "PHONE"), text)
        # Longest-first so "Cian Brennan" doesn't get partially shadowed by
        # a shorter previously-tokenized substring.
        for real in sorted(self._forward, key=len, reverse=True):
            if real and real in text:
                text = text.replace(real, self._forward[real])
        return text

    def rehydrate(self, value: Any) -> Any:
        """Recursively swap tokens back to real values for human display.

        Walks dicts/lists/strings so it can be applied directly to a whole
        tool-result payload or trace event, not just a single string.

        Args:
            value: A string, or a JSON-shaped structure (dict/list) that may
                contain tokenized strings anywhere within it.

        Returns:
            The same shape with every ``[KIND_n]`` token replaced by its
            real value. Non-string, non-container values pass through as-is.
        """
        if isinstance(value, str):
            for token, real in self._reverse.items():
                if token in value:
                    value = value.replace(token, real)
            return value
        if isinstance(value, dict):
            return {k: self.rehydrate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.rehydrate(v) for v in value]
        return value


def redact_accounts(accounts: list[dict[str, Any]], rmap: RedactionMap) -> list[dict[str, Any]]:
    """Return a copy of ``accounts`` with PII fields tokenized via ``rmap``.

    Args:
        accounts: Normalized account records (demo or uploaded).
        rmap: The RedactionMap for this request - reused across calls so
            the same person maps to the same token everywhere.

    Returns:
        New list of dicts; the input records are not mutated.
    """
    redacted = []
    for account in accounts:
        account = dict(account)
        for field in ACCOUNT_PII_FIELDS:
            if account.get(field):
                account[field] = rmap.tokenize(account[field], _FIELD_KIND[field])
        redacted.append(account)
    return redacted


def redact_tickets(tickets: list[dict[str, Any]], rmap: RedactionMap) -> list[dict[str, Any]]:
    """Return a copy of ``tickets`` with PII fields and free text tokenized.

    Args:
        tickets: Normalized ticket records (demo or uploaded).
        rmap: The RedactionMap for this request.

    Returns:
        New list of dicts; the input records are not mutated.
    """
    redacted = []
    for ticket in tickets:
        ticket = dict(ticket)
        for field in TICKET_PII_FIELDS:
            if ticket.get(field):
                ticket[field] = rmap.tokenize(ticket[field], _FIELD_KIND[field])
        for field in TICKET_FREE_TEXT_FIELDS:
            if ticket.get(field):
                ticket[field] = rmap.redact_free_text(ticket[field])
        redacted.append(ticket)
    return redacted
