"""Turns an arbitrary, messy CSV export into the normalized schema ``tools.py``
expects - and reports exactly what it did, so nothing is silently guessed.

Design principle: accuracy over coverage. If a column can't be confidently
identified, or a value can't be confidently parsed, we skip/flag it rather
than force a guess that could silently corrupt a filter result (e.g. an
account wrongly excluded from "elevated" because we mis-mapped its groups
column). The mapping report returned alongside the normalized data is meant
to be shown to the user before they run any query against it.
"""
from __future__ import annotations

import csv
import difflib
import io
import re
from datetime import datetime, timezone
from typing import Any


def _normalize_header(header: str) -> str:
    """Collapse a CSV header to lowercase alphanumeric-only for comparison.

    e.g. "Last Sign-In" -> "lastsignin". This lets headers with different
    spacing, casing, or punctuation still match the same alias.
    """
    return re.sub(r"[^a-z0-9]", "", header.strip().lower())


# Canonical field -> list of header aliases (already lowercase/alnum-only,
# matching _normalize_header's output) that map to it.
ACCOUNT_ALIASES: dict[str, list[str]] = {
    "username": ["username", "user", "login", "samaccountname", "userprincipalname", "upn", "loginname"],
    "email": ["email", "emailaddress", "mail", "useremail", "primaryemail"],
    "displayName": ["displayname", "name", "fullname", "employeename", "user"],
    "department": ["department", "dept", "division", "team"],
    "title": ["title", "jobtitle", "role", "position", "jobrole"],
    "status": ["status", "accountstatus", "state", "enabled", "active", "userstatus"],
    "lastLogin": ["lastlogin", "lastsignin", "lastsignon", "lastactivity", "lastlogindate", "lastauthenticated"],
    "created": ["created", "createddate", "datecreated", "accountcreated", "createdon"],
    "groups": ["groups", "group", "roles", "memberof", "grouplist", "grouplistroles"],
    "mfaEnrolled": ["mfa", "mfaenrolled", "mfaenabled", "twofactor", "2fa", "mfastatus"],
}

TICKET_ALIASES: dict[str, list[str]] = {
    "key": ["key", "ticketid", "id", "issuekey", "ticketkey", "number", "ticketnumber"],
    "summary": ["summary", "title", "subject", "shortdescription", "description"],
    "priority": ["priority", "severity", "urgency"],
    "status": ["status", "state"],
    "assignee": ["assignee", "assignedto", "owner"],
    "reporterEmail": ["reporter", "reporteremail", "createdby", "requester", "requestoremail"],
    "created": ["created", "createddate", "opendate", "createdat", "opened"],
    "updated": ["updated", "updateddate", "lastupdated", "resolveddate", "closeddate", "resolved"],
}

# Fields a dataset needs at least a best-guess mapping for; missing any of
# these degrades a specific tool filter rather than the whole ingest.
REQUIRED_ACCOUNT_FIELDS: list[str] = ["status", "lastLogin"]
REQUIRED_TICKET_FIELDS: list[str] = ["status", "priority", "created"]

# Common status vocabularies collapsed to the two buckets tools.py filters on.
ACCOUNT_STATUS_ACTIVE: set[str] = {"active", "enabled", "1", "true", "yes", "current"}
ACCOUNT_STATUS_INACTIVE: set[str] = {
    "suspended", "disabled", "inactive", "0", "false", "no", "deprovisioned", "terminated",
}

TICKET_STATUS_OPEN: set[str] = {"open", "new", "todo", "to do", "backlog", "unassigned"}
TICKET_STATUS_IN_PROGRESS: set[str] = {
    "in progress", "inprogress", "in review", "review", "pending", "on hold", "onhold", "waiting",
}
TICKET_STATUS_RESOLVED: set[str] = {
    "resolved", "done", "closed", "complete", "completed", "cancelled", "canceled",
}

# Priority vocabularies -> our P1-P4 buckets + assumed SLA window (hours),
# matching the convention used by the bundled synthetic dataset so query
# logic (tools.query_tickets) lines up regardless of data source.
PRIORITY_MAP: dict[str, str] = {
    "p1": "P1", "1": "P1", "highest": "P1", "critical": "P1", "urgent": "P1", "blocker": "P1",
    "p2": "P2", "2": "P2", "high": "P2",
    "p3": "P3", "3": "P3", "medium": "P3", "normal": "P3",
    "p4": "P4", "4": "P4", "low": "P4", "lowest": "P4", "minor": "P4", "trivial": "P4",
}
SLA_HOURS_BY_PRIORITY: dict[str, int] = {"P1": 4, "P2": 24, "P3": 72, "P4": 168}

# Date formats tried in order when parsing a free-text date/time column.
# Ambiguous numeric formats (e.g. 03/12/2026) are resolved US-style
# (month/day/year) before falling back to day/month/year - documented here
# since it's the one place behavior is genuinely ambiguous without knowing
# the source system's locale.
DATE_FORMATS: list[str] = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%b %d, %Y",
    "%B %d, %Y",
]


def _parse_date(raw: str | None) -> datetime | None:
    """Best-effort parse of a date/time string into an aware UTC datetime.

    Args:
        raw: The raw cell value, in any of the formats in ``DATE_FORMATS``.

    Returns:
        A UTC ``datetime``, or ``None`` if the value is empty or doesn't
        match any known format. Callers must handle ``None`` explicitly
        rather than assume a value - this module never guesses a date.
    """
    if not raw or not str(raw).strip():
        return None
    raw = str(raw).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _to_iso(dt: datetime) -> str:
    """Format a datetime as the ISO-8601 string tools.py expects."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _match_headers(
    headers: list[str], aliases: dict[str, list[str]]
) -> tuple[dict[str, str], list[dict[str, str | None]]]:
    """Match a CSV's actual headers to our canonical field names.

    Tries an exact match (after normalization) against each field's alias
    list first, then falls back to fuzzy string matching for headers that
    are close but not an exact alias (e.g. a typo, or an unlisted synonym).
    Each header can only be consumed once, so two canonical fields never
    both claim the same column.

    Args:
        headers: The raw header row from the uploaded CSV.
        aliases: Canonical field -> list of normalized alias strings
            (``ACCOUNT_ALIASES`` or ``TICKET_ALIASES``).

    Returns:
        A tuple of:
            - ``mapping``: ``{canonical_field: original_header}`` for every
              field that was matched.
            - ``report``: one entry per canonical field, each
              ``{"field": ..., "header": original_header_or_None,
              "confidence": "exact" | "fuzzy" | None}``, suitable for
              display to the user.
    """
    normalized_to_original = {_normalize_header(h): h for h in headers}
    normalized_headers = list(normalized_to_original.keys())

    mapping: dict[str, str] = {}
    confidence_by_field: dict[str, str] = {}
    used_headers: set[str] = set()

    # Phase 1: exact matches for every field, as a complete pass over all
    # fields before any fuzzy matching runs. Doing exact-then-fuzzy per
    # field (in dict order) would let an earlier field's fuzzy fallback
    # steal a header that a later field would have matched exactly - e.g.
    # reporterEmail's "createdby" alias fuzzy-matches a "Created" column at
    # high similarity; if that ran before the `created` field got its
    # exact-match turn, `created` would end up unmapped even though
    # "Created" was sitting right there as an exact alias for it.
    for field, alias_list in aliases.items():
        for alias in alias_list:
            if alias in normalized_headers and normalized_to_original[alias] not in used_headers:
                matched_header = normalized_to_original[alias]
                mapping[field] = matched_header
                confidence_by_field[field] = "exact"
                used_headers.add(matched_header)
                break

    # Phase 2: fuzzy fallback, only for fields still unmatched, only against
    # headers no exact match already claimed.
    for field, alias_list in aliases.items():
        if field in mapping:
            continue
        candidates = [h for h in normalized_headers if normalized_to_original[h] not in used_headers]
        close = difflib.get_close_matches(field.lower(), candidates, n=1, cutoff=0.72)
        if not close:
            for alias in alias_list:
                close = difflib.get_close_matches(alias, candidates, n=1, cutoff=0.8)
                if close:
                    break
        if close:
            matched_header = normalized_to_original[close[0]]
            mapping[field] = matched_header
            confidence_by_field[field] = "fuzzy"
            used_headers.add(matched_header)

    report: list[dict[str, str | None]] = [
        {"field": field, "header": mapping.get(field), "confidence": confidence_by_field.get(field)}
        for field in aliases
    ]

    return mapping, report


def _normalize_account_status(raw: str | None) -> str | None:
    """Collapse an account status value to ``"ACTIVE"`` / ``"SUSPENDED"``.

    Falls back to returning the original (stripped) value unchanged if it
    doesn't match a known vocabulary, rather than guessing.
    """
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ACCOUNT_STATUS_ACTIVE:
        return "ACTIVE"
    if value in ACCOUNT_STATUS_INACTIVE:
        return "SUSPENDED"
    return raw.strip() if isinstance(raw, str) else raw


def _normalize_ticket_status(raw: str | None) -> str | None:
    """Collapse a ticket status value to ``"Open"`` / ``"In Progress"`` / ``"Resolved"``.

    Falls back to returning the original (stripped) value unchanged if it
    doesn't match a known vocabulary, rather than guessing.
    """
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in TICKET_STATUS_OPEN:
        return "Open"
    if value in TICKET_STATUS_IN_PROGRESS:
        return "In Progress"
    if value in TICKET_STATUS_RESOLVED:
        return "Resolved"
    return raw.strip() if isinstance(raw, str) else raw


def _split_groups(raw: str | None) -> list[str]:
    """Split a delimited group-membership cell into a clean list.

    Handles comma, semicolon, and pipe delimiters, since different identity
    providers export multi-value fields differently.
    """
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[;,|]", str(raw)) if part.strip()]


def ingest_accounts_csv(file_bytes: bytes) -> dict[str, Any]:
    """Parse and normalize an uploaded accounts CSV into ``tools.py``'s schema.

    Args:
        file_bytes: The raw uploaded file contents.

    Returns:
        A dict with:
            - ``records``: normalized account dicts, ready to pass to
              ``tools.build_tool_implementations``.
            - ``row_count``: number of data rows parsed.
            - ``column_mapping``: the header match report from ``_match_headers``.
            - ``warnings``: human-readable strings describing any rows or
              columns that couldn't be confidently parsed/identified.
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    mapping, header_report = _match_headers(headers, ACCOUNT_ALIASES)

    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    date_parse_failures = 0

    for i, row in enumerate(reader):
        record: dict[str, Any] = {}
        for field, header in mapping.items():
            value = row.get(header)
            record[field] = value.strip() if isinstance(value, str) else value

        record["status"] = _normalize_account_status(record.get("status"))
        record["groups"] = _split_groups(record.get("groups"))

        last_login_dt = _parse_date(record.get("lastLogin"))
        if record.get("lastLogin") and not last_login_dt:
            date_parse_failures += 1
        record["lastLogin"] = _to_iso(last_login_dt) if last_login_dt else None

        created_dt = _parse_date(record.get("created"))
        record["created"] = _to_iso(created_dt) if created_dt else None

        record.setdefault("id", record.get("username") or record.get("email") or f"row-{i + 1}")
        records.append(record)

    if date_parse_failures:
        warnings.append(
            f"{date_parse_failures} row(s) had a lastLogin value that couldn't be parsed "
            "as a date - the inactivity filter will skip them."
        )

    missing_required = [field for field in REQUIRED_ACCOUNT_FIELDS if field not in mapping]
    if missing_required:
        warnings.append(
            f"Could not confidently identify a column for: {', '.join(missing_required)}. "
            "Filters using these fields won't work reliably."
        )

    return {
        "records": records,
        "row_count": len(records),
        "column_mapping": header_report,
        "warnings": warnings,
    }


def ingest_tickets_csv(file_bytes: bytes) -> dict[str, Any]:
    """Parse and normalize an uploaded tickets CSV into ``tools.py``'s schema.

    Also derives the two fields the bundled dataset ships pre-computed but a
    real export won't have: ``slaHours`` (from the normalized priority) and
    ``elapsedHours`` (from ``created``/``updated`` timestamps, or ``created``
    to "now" for still-open tickets).

    Args:
        file_bytes: The raw uploaded file contents.

    Returns:
        A dict with:
            - ``records``: normalized ticket dicts, ready to pass to
              ``tools.build_tool_implementations``.
            - ``row_count``: number of data rows parsed.
            - ``column_mapping``: the header match report from ``_match_headers``.
            - ``warnings``: human-readable strings describing any rows or
              columns that couldn't be confidently parsed/identified.
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    mapping, header_report = _match_headers(headers, TICKET_ALIASES)

    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    priority_defaulted = 0
    date_parse_failures = 0

    for i, row in enumerate(reader):
        record: dict[str, Any] = {}
        for field, header in mapping.items():
            value = row.get(header)
            record[field] = value.strip() if isinstance(value, str) else value

        record["status"] = _normalize_ticket_status(record.get("status"))

        raw_priority = str(record.get("priority") or "").strip().lower()
        priority = PRIORITY_MAP.get(raw_priority)
        if not priority:
            priority = "P3"
            if raw_priority:
                priority_defaulted += 1
        record["priority"] = priority
        record["slaHours"] = SLA_HOURS_BY_PRIORITY[priority]

        created_dt = _parse_date(record.get("created"))
        updated_dt = _parse_date(record.get("updated"))
        if record.get("created") and not created_dt:
            date_parse_failures += 1

        record["created"] = _to_iso(created_dt) if created_dt else None
        record["updated"] = _to_iso(updated_dt) if updated_dt else (record["created"] if created_dt else None)

        if created_dt:
            end = updated_dt if (updated_dt and record["status"] == "Resolved") else datetime.now(timezone.utc)
            record["elapsedHours"] = round((end - created_dt).total_seconds() / 3600, 1)
        else:
            record["elapsedHours"] = None

        record.setdefault("key", f"ROW-{i + 1}")
        records.append(record)

    if priority_defaulted:
        warnings.append(
            f"{priority_defaulted} ticket(s) had a priority value we didn't recognize - "
            "defaulted to P3 (72h SLA window) for those rows."
        )
    if date_parse_failures:
        warnings.append(
            f"{date_parse_failures} row(s) had a created-date value that couldn't be "
            "parsed - SLA math will skip them."
        )

    missing_required = [field for field in REQUIRED_TICKET_FIELDS if field not in mapping]
    if missing_required:
        warnings.append(
            f"Could not confidently identify a column for: {', '.join(missing_required)}. "
            "Filters using these fields won't work reliably."
        )

    return {
        "records": records,
        "row_count": len(records),
        "column_mapping": header_report,
        "warnings": warnings,
    }
