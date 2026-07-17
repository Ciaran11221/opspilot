"""Unit tests for dataset_store.py."""
from __future__ import annotations

import dataset_store


def test_create_dataset_returns_a_usable_empty_dataset():
    dataset_id = dataset_store.create_dataset()
    dataset = dataset_store.get_dataset(dataset_id)
    assert dataset == {"accounts": [], "tickets": [], "meta": {}}


def test_unknown_dataset_id_returns_none():
    assert dataset_store.get_dataset("does-not-exist") is None
    assert dataset_store.describe("does-not-exist") is None


def test_set_accounts_then_tickets_combine_into_one_dataset():
    dataset_id = dataset_store.create_dataset()
    dataset_store.set_accounts(dataset_id, [{"id": "a"}], {"filename": "a.csv"})
    dataset_store.set_tickets(dataset_id, [{"key": "T-1"}], {"filename": "t.csv"})

    summary = dataset_store.describe(dataset_id)
    assert summary["accounts_loaded"] == 1
    assert summary["tickets_loaded"] == 1
    assert summary["meta"]["accounts"]["filename"] == "a.csv"
    assert summary["meta"]["tickets"]["filename"] == "t.csv"


def test_set_accounts_on_a_brand_new_id_creates_the_dataset_implicitly():
    # main.py's upload route may pass a dataset_id that doesn't exist yet
    # (first file of a pair) - set_accounts/set_tickets must handle that.
    dataset_id = "not-created-yet"
    dataset_store.set_accounts(dataset_id, [{"id": "a"}], {})
    assert dataset_store.get_dataset(dataset_id)["accounts"] == [{"id": "a"}]


def test_replacing_accounts_overwrites_not_appends():
    dataset_id = dataset_store.create_dataset()
    dataset_store.set_accounts(dataset_id, [{"id": "a"}], {})
    dataset_store.set_accounts(dataset_id, [{"id": "b"}], {})
    assert dataset_store.get_dataset(dataset_id)["accounts"] == [{"id": "b"}]
