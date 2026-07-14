"""In-memory store for uploaded datasets.

OpsPilot is a local, single-user demo tool rather than a multi-tenant
service, so a simple process-local dict is the right amount of engineering
here - no database, no persistence across restarts. A dataset lives for the
life of the server process (or until its data is replaced by a new upload
under the same ``dataset_id``).
"""
from __future__ import annotations

import uuid
from typing import Any

# dataset_id -> {"accounts": [...], "tickets": [...], "meta": {...}}
_STORE: dict[str, dict[str, Any]] = {}


def create_dataset() -> str:
    """Create a new empty dataset and return its id.

    Returns:
        A newly generated UUID string identifying the dataset.
    """
    dataset_id = str(uuid.uuid4())
    _STORE[dataset_id] = {"accounts": [], "tickets": [], "meta": {}}
    return dataset_id


def get_dataset(dataset_id: str) -> dict[str, Any] | None:
    """Look up a dataset by id.

    Args:
        dataset_id: The dataset's UUID string.

    Returns:
        The dataset dict (``accounts``, ``tickets``, ``meta`` keys), or
        ``None`` if no dataset exists with that id.
    """
    return _STORE.get(dataset_id)


def set_accounts(dataset_id: str, records: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    """Replace the accounts records for a dataset.

    Args:
        dataset_id: The dataset's UUID string. Created if it doesn't exist yet.
        records: Normalized account records (see ``csv_ingest.ingest_accounts_csv``).
        meta: Ingestion metadata (filename, row count, column mapping, warnings)
            to surface back to the frontend.
    """
    dataset = _STORE.setdefault(dataset_id, {"accounts": [], "tickets": [], "meta": {}})
    dataset["accounts"] = records
    dataset["meta"]["accounts"] = meta


def set_tickets(dataset_id: str, records: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    """Replace the ticket records for a dataset.

    Args:
        dataset_id: The dataset's UUID string. Created if it doesn't exist yet.
        records: Normalized ticket records (see ``csv_ingest.ingest_tickets_csv``).
        meta: Ingestion metadata (filename, row count, column mapping, warnings)
            to surface back to the frontend.
    """
    dataset = _STORE.setdefault(dataset_id, {"accounts": [], "tickets": [], "meta": {}})
    dataset["tickets"] = records
    dataset["meta"]["tickets"] = meta


def describe(dataset_id: str) -> dict[str, Any] | None:
    """Return a summary of a dataset suitable for an API response.

    Args:
        dataset_id: The dataset's UUID string.

    Returns:
        A dict with ``dataset_id``, ``accounts_loaded``, ``tickets_loaded``,
        and the raw ingestion ``meta`` for each uploaded file, or ``None`` if
        the dataset doesn't exist.
    """
    dataset = get_dataset(dataset_id)
    if dataset is None:
        return None
    return {
        "dataset_id": dataset_id,
        "accounts_loaded": len(dataset["accounts"]),
        "tickets_loaded": len(dataset["tickets"]),
        "meta": dataset["meta"],
    }
