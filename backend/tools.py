"""Tool implementations for OpsPilot's agent loop.

Each function here is the *execution* side of a Claude tool-use tool. The
tool *definitions* (JSON schemas sent to the API) live in ``agent.py``, next
to the loop that calls them, so the schema and implementation are easy to
keep in sync.

Tools operate on a "dataset" - plain ``accounts``/``tickets`` lists of dicts -
rather than reading a fixed file directly. This indirection is what lets the
same query logic run against either the bundled synthetic demo data or a
user's own uploaded CSV, once ``csv_ingest.py`` has normalized it into this
common shape. See ``dataset_store.py`` for how datasets are registered and
looked up per request.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from functools import partial
from typing import Any

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

with open(os.path.join(DATA_DIR, "accounts.json"), encoding="utf-8") as f:
    DEMO_ACCOUNTS: list[dict[str, Any]] = json.load(f)

with open(os.path.join(DATA_DIR, "tickets.json"), encoding="utf-8") as f:
    DEMO_TICKETS: list[dict[str, Any]] = json.load(f)

# Fixed reference time for the bundled demo dataset, so its seeded
# "inactive N days" / "SLA risk" scenarios always evaluate the same way
# regardless of when the demo is actually run.
NOW_DEMO = datetime(2026, 7, 14, tzinfo=timezone.utc)

# Keywords used to flag an account as "elevated" - matched case-insensitively
# against both the account's title and its group memberships. Keyword-based
# rather than an exact list of group names, so this generalizes to real
# exports with whatever naming convention a given org uses (Domain-Admins,
# admin_role, IT-Root, Super_User, etc. all match).
ELEVATED_KEYWORDS: list[str] = [
    "admin", "root", "super", "owner", "global admin", "domain admin", "billing",
]


def _strip_ground_truth(record: dict[str, Any]) -> dict[str, Any]:
    """Remove internal bookkeeping fields before handing a record to the model.

    The bundled demo dataset tags each record with a ``_scenarioTag`` field
    (see ``data/generate_data.py``) purely so the generator can guarantee a
    reliable number of "interesting" records for the demo. It is not part of
    a real export and must never reach the model or the UI.

    Args:
        record: A single account or ticket dict, possibly containing
            underscore-prefixed internal fields.

    Returns:
        A copy of ``record`` with any key starting with ``_`` removed.
    """
    return {k: v for k, v in record.items() if not k.startswith("_")}


def _is_elevated(account: dict[str, Any]) -> bool:
    """Return True if an account's title or group memberships look elevated.

    Args:
        account: A normalized account record with ``title`` and ``groups`` keys.

    Returns:
        True if any ``ELEVATED_KEYWORDS`` keyword appears (case-insensitively)
        in the account's title or any of its group names.
    """
    title = account.get("title") or ""
    groups = account.get("groups") or []
    haystack = " ".join([str(title), *[str(g) for g in groups]]).lower()
    return any(keyword in haystack for keyword in ELEVATED_KEYWORDS)


def query_accounts(
    accounts: list[dict[str, Any]],
    now: datetime,
    status: str | None = None,
    min_inactive_days: int | None = None,
    elevated_only: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """Query the account directory (Okta/M365-style export).

    Works against either the bundled synthetic demo data or a user-uploaded
    CSV, once normalized to a common schema by ``csv_ingest.py``.

    Args:
        accounts: List of normalized account records to filter.
        now: Reference timestamp used to compute inactivity in days. Passed
            in explicitly (rather than calling ``datetime.now()`` inline) so
            results are deterministic for the demo dataset and testable.
        status: Filter by account status, e.g. ``"ACTIVE"``.
        min_inactive_days: Only return accounts whose ``lastLogin`` is at
            least this many days before ``now``.
        elevated_only: Only return accounts with an elevated/admin-style
            title or membership in an elevated-sounding group.

    Returns:
        A dict with:
            - ``count``: number of matching accounts.
            - ``accounts``: the matching records (ground-truth fields stripped).
            - ``note`` (optional): present if any records were skipped
              because their ``lastLogin`` couldn't be parsed as a date.
    """
    results: list[dict[str, Any]] = []
    skipped_unparseable = 0

    for account in accounts:
        if status and account.get("status") != status:
            continue

        if min_inactive_days is not None:
            last_login_raw = account.get("lastLogin")
            if not last_login_raw:
                skipped_unparseable += 1
                continue
            try:
                last_login = datetime.strptime(last_login_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                skipped_unparseable += 1
                continue
            if (now - last_login).days < min_inactive_days:
                continue

        if elevated_only and not _is_elevated(account):
            continue

        results.append(_strip_ground_truth(account))

    result: dict[str, Any] = {"count": len(results), "accounts": results}
    if skipped_unparseable:
        result["note"] = (
            f"{skipped_unparseable} account(s) had no usable lastLogin date and were "
            "skipped for the inactivity filter."
        )
    return result


def query_tickets(
    tickets: list[dict[str, Any]],
    status: str | None = None,
    priority: str | None = None,
    sla_risk_only: bool = False,
    sla_risk_threshold: float = 0.8,
    **_: Any,
) -> dict[str, Any]:
    """Query the ticket export (Jira-style export).

    Works against either the bundled synthetic demo data or a user-uploaded
    CSV, once normalized to a common schema by ``csv_ingest.py``.

    Args:
        tickets: List of normalized ticket records to filter.
        status: Filter by status, e.g. ``"Open"``, ``"In Progress"``, ``"Resolved"``.
        priority: Filter by priority, e.g. ``"P1"``.
        sla_risk_only: Only return open/in-progress tickets whose elapsed
            time has crossed ``sla_risk_threshold`` of their SLA window.
        sla_risk_threshold: Fraction (0-1+) of the SLA window elapsed to
            count as "at risk". Defaults to 0.8 (80% of the SLA time used).

    Returns:
        A dict with:
            - ``count``: number of matching tickets.
            - ``tickets``: the matching records, each with an added
              ``slaRatio`` (elapsed / SLA window) where derivable.
            - ``note`` (optional): present if any records were skipped
              because no SLA window could be derived for them.
    """
    results: list[dict[str, Any]] = []
    skipped_no_sla = 0

    for ticket in tickets:
        if status and ticket.get("status") != status:
            continue
        if priority and ticket.get("priority") != priority:
            continue

        sla_hours = ticket.get("slaHours")
        elapsed_hours = ticket.get("elapsedHours")

        if sla_risk_only:
            if ticket.get("status") not in ("Open", "In Progress"):
                continue
            if not sla_hours or elapsed_hours is None:
                skipped_no_sla += 1
                continue
            if (elapsed_hours / sla_hours) < sla_risk_threshold:
                continue

        record = _strip_ground_truth(ticket)
        if sla_hours and elapsed_hours is not None:
            record["slaRatio"] = round(elapsed_hours / sla_hours, 2)
        results.append(record)

    result: dict[str, Any] = {"count": len(results), "tickets": results}
    if skipped_no_sla:
        result["note"] = (
            f"{skipped_no_sla} ticket(s) had no derivable SLA window and were skipped "
            "for the risk filter."
        )
    return result


def draft_report(
    now: datetime,
    title: str,
    report_type: str,
    body_markdown: str,
    related_ids: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Draft an output artifact (offboarding ticket, SLA-risk report, etc.).

    This is the tool the agent calls once it has gathered enough information
    and is ready to produce the "wow" output artifact for the demo. It only
    ever produces a draft in memory - nothing is submitted to a real ticketing
    or identity system.

    Args:
        now: Timestamp to record as the artifact's ``draftedAt`` time.
        title: Short title for the artifact.
        report_type: One of ``"offboarding_ticket"``, ``"sla_risk_report"``,
            or ``"account_hygiene_report"``.
        body_markdown: The full drafted content, in markdown.
        related_ids: Account IDs and/or ticket keys this artifact references.

    Returns:
        A dict describing the drafted artifact, including an explicit
        ``status`` field stating it is a draft only.
    """
    return {
        "title": title,
        "reportType": report_type,
        "body": body_markdown,
        "relatedIds": related_ids or [],
        "draftedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "DRAFT - not submitted to any real system",
    }


def build_tool_implementations(
    accounts: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind the tool functions to a specific dataset for one agent run.

    Each call to the agent loop needs its tool functions bound to the right
    dataset (bundled demo data, or a specific uploaded dataset) and a fixed
    ``now`` for deterministic date math. This factory returns that binding as
    a ``{tool_name: callable}`` dict matching the tool names in
    ``agent.TOOLS``.

    Args:
        accounts: Account records for this run.
        tickets: Ticket records for this run.
        now: Reference timestamp for inactivity/SLA math. Defaults to the
            current UTC time if not provided (used for uploaded datasets;
            the demo dataset always passes its own fixed ``NOW_DEMO``).

    Returns:
        A dict mapping each tool name to a callable matching the signature
        the agent loop expects: ``fn(**tool_input) -> dict``.
    """
    now = now or datetime.now(timezone.utc)
    return {
        "query_accounts": partial(query_accounts, accounts, now),
        "query_tickets": partial(query_tickets, tickets),
        "draft_report": partial(draft_report, now),
    }


# Default binding used when a chat request has no dataset_id - the bundled
# synthetic demo data, built once at import time.
DEMO_TOOL_IMPLEMENTATIONS: dict[str, Any] = build_tool_implementations(
    DEMO_ACCOUNTS, DEMO_TICKETS, NOW_DEMO
)
