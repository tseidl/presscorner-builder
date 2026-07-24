"""Tests for durable window, detail-failure, union, and limit orchestration."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from presscorner_builder.api import (
    ACTIVE_TYPE_CODES,
    DetailFetchOutcome,
    WindowFetchError,
)
from presscorner_builder.pipeline import SearchSpec, run_windows, update_dataset
from presscorner_builder.store import Store


class FakePipelineAPI:
    """Serve per-window summaries and configurable detail outcomes without HTTP."""

    # Configure summaries, failing windows, and details for pipeline tests.
    def __init__(
        self,
        summaries: dict[tuple[str, str], list[dict[str, Any]]],
        details: dict[
            str, dict[str, Any] | DetailFetchOutcome | None
        ],
        failing: set[tuple[str, str]] | None = None,
    ) -> None:
        self.request_delay = 0.0
        self.summaries = summaries
        self.details = details
        self.failing = failing or set()
        self.search_calls: list[dict[str, Any]] = []

    # Return configured summaries or raise the critical window-level exception.
    def search_window(
        self, date_from: date, date_to: date, **kwargs: Any
    ) -> list[dict[str, Any]]:
        key = (date_from.isoformat(), date_to.isoformat())
        self.search_calls.append(dict(kwargs))
        if key in self.failing:
            raise WindowFetchError(f"synthetic failure for {key}")
        return self.summaries.get(key, [])

    # Return a configured detail response for one reference.
    def get_document(
        self, reference: str, **kwargs: Any
    ) -> dict[str, Any] | DetailFetchOutcome | None:
        del kwargs
        return self.details.get(reference)


# Create a minimal but valid summary for a reference and event date.
def _summary(reference: str, event_date: str) -> dict[str, Any]:
    return {
        "refCode": reference,
        "eventDate": event_date,
        "title": reference,
        "leadText": "Lead",
        "docutype": {"code": "IP", "description": "Press release"},
        "languageCode": "en",
    }


# Adapt a full captured fixture to a new reference and event date.
def _detail(
    sample_detail: dict[str, Any], reference: str, event_date: str
) -> dict[str, Any]:
    result = dict(sample_detail)
    result["refCd"] = reference
    result["eventDate"] = event_date
    return result


# Verify one failed search window is queued while later windows still checkpoint.
def test_window_failure_is_queued_and_other_windows_continue(
    tmp_path: Path, sample_detail
) -> None:
    january = (date(2020, 1, 1), date(2020, 1, 31))
    february = (date(2020, 2, 1), date(2020, 2, 29))
    summaries = {
        ("2020-01-01", "2020-01-31"): [_summary("IP/20/1", "2020-01-10")],
        ("2020-02-01", "2020-02-29"): [_summary("IP/20/2", "2020-02-10")],
    }
    api = FakePipelineAPI(
        summaries,
        {
            "IP/20/1": _detail(sample_detail, "IP/20/1", "2020-01-10"),
            "IP/20/2": _detail(sample_detail, "IP/20/2", "2020-02-10"),
        },
        failing={("2020-01-01", "2020-01-31")},
    )
    store = Store(tmp_path)

    first = run_windows(api, store, [february, january], progress=False)

    assert first.new_documents == 1
    assert first.failed_window_attempts == 1
    assert first.pending_windows == 1
    assert store.existing_ids == {"ip_20_2"}

    api.failing.clear()
    second = run_windows(api, store, [], progress=False)

    assert second.new_documents == 1
    assert second.pending_windows == 0
    assert store.existing_ids == {"ip_20_1", "ip_20_2"}


# Verify detail failure saves a partial row and a later retry replaces it.
def test_detail_failure_partial_row_is_retried(tmp_path: Path, sample_detail) -> None:
    window = (date(2020, 1, 1), date(2020, 1, 31))
    key = ("2020-01-01", "2020-01-31")
    summary = _summary("IP/20/1", "2020-01-10")
    api = FakePipelineAPI({key: [summary]}, {"IP/20/1": None})
    store = Store(tmp_path)

    first = run_windows(api, store, [window], progress=False)

    assert first.pending_refs == 1
    assert not store.frame.iloc[0]["detail_ok"]
    assert store.frame.iloc[0]["full_text"] == ""

    api.details["IP/20/1"] = _detail(sample_detail, "IP/20/1", "2020-01-10")
    second = run_windows(api, store, [], progress=False)

    assert second.recovered_details == 1
    assert second.pending_refs == 0
    assert len(store.frame) == 1
    assert store.frame.iloc[0]["detail_ok"]
    assert store.frame.iloc[0]["full_text"] != ""


# Verify a permanent empty detail is stored once without creating retry work.
def test_permanently_empty_detail_saves_partial_without_ledger(tmp_path: Path) -> None:
    window = (date(2020, 1, 1), date(2020, 1, 31))
    key = ("2020-01-01", "2020-01-31")
    summary = _summary("BIO/85/1", "2020-01-10")
    api = FakePipelineAPI(
        {key: [summary]},
        {"BIO/85/1": DetailFetchOutcome.PERMANENTLY_EMPTY},
    )
    store = Store(tmp_path)

    result = run_windows(api, store, [window], progress=False)

    assert result.new_documents == 1
    assert result.permanently_empty_details == 1
    assert result.pending_refs == 0
    assert not result.partial
    assert store.load_failed_refs() == {}
    assert not store.frame.iloc[0]["detail_ok"]


# Verify a later permanent-empty response clears a previously transient retry entry.
def test_permanently_empty_detail_clears_existing_retry(
    tmp_path: Path,
) -> None:
    window = (date(2020, 1, 1), date(2020, 1, 31))
    key = ("2020-01-01", "2020-01-31")
    summary = _summary("BIO/85/1", "2020-01-10")
    api = FakePipelineAPI({key: [summary]}, {"BIO/85/1": None})
    store = Store(tmp_path)

    first = run_windows(api, store, [window], progress=False)
    api.details["BIO/85/1"] = DetailFetchOutcome.PERMANENTLY_EMPTY
    second = run_windows(api, store, [], progress=False)

    assert first.pending_refs == 1
    assert second.permanently_empty_details == 1
    assert second.pending_refs == 0
    assert store.load_failed_refs() == {}


# Verify a canary limit leaves all not-yet-completed windows durably queued.
def test_limit_leaves_remaining_windows_queued(tmp_path: Path, sample_detail) -> None:
    february = (date(2020, 2, 1), date(2020, 2, 29))
    january = (date(2020, 1, 1), date(2020, 1, 31))
    api = FakePipelineAPI(
        {
            ("2020-02-01", "2020-02-29"): [_summary("IP/20/2", "2020-02-10")],
            ("2020-01-01", "2020-01-31"): [_summary("IP/20/1", "2020-01-10")],
        },
        {
            "IP/20/2": _detail(sample_detail, "IP/20/2", "2020-02-10"),
            "IP/20/1": _detail(sample_detail, "IP/20/1", "2020-01-10"),
        },
    )
    store = Store(tmp_path)

    result = run_windows(api, store, [february, january], limit=1, progress=False)

    assert result.limit_reached
    assert result.new_documents == 1
    assert result.pending_windows == 1
    assert store.load_failed_windows() == [
        {"date_from": "2020-01-01", "date_to": "2020-01-31"}
    ]


# Verify keyword and commissioner searches are unioned before one detail request.
def test_search_filter_values_are_unioned(tmp_path: Path, sample_detail) -> None:
    window = (date(2020, 1, 1), date(2020, 1, 31))
    key = ("2020-01-01", "2020-01-31")
    api = FakePipelineAPI(
        {key: [_summary("IP/20/1", "2020-01-10")]},
        {"IP/20/1": _detail(sample_detail, "IP/20/1", "2020-01-10")},
    )

    result = run_windows(
        api,
        Store(tmp_path),
        [window],
        search=SearchSpec(keywords=("trade", "growth"), commissioners=("person",)),
        progress=False,
    )

    assert result.new_documents == 1
    assert [call.get("keyword") for call in api.search_calls[:2]] == ["trade", "growth"]
    assert api.search_calls[2]["commissioner"] == "person"


# Verify fresh full updates persist their selected scope and use its exact filter.
@pytest.mark.parametrize(
    ("requested_scope", "recorded_scope", "expected_types"),
    [
        (None, "active", list(ACTIVE_TYPE_CODES)),
        ("all", "all", None),
    ],
)
def test_full_update_records_chosen_scope(
    tmp_path: Path,
    requested_scope,
    recorded_scope: str,
    expected_types: list[str] | None,
) -> None:
    key = ("2020-01-01", "2020-01-31")
    api = FakePipelineAPI({key: []}, {})
    store = Store(tmp_path / recorded_scope)
    messages: list[str] = []

    update_dataset(
        api,
        store,
        since_date=date(2020, 1, 1),
        until_date=date(2020, 1, 31),
        full=True,
        scope=requested_scope,
        progress=False,
        reporter=messages.append,
    )

    assert Store(store.path).scope == recorded_scope
    assert api.search_calls[0].get("document_types") == expected_types
    assert messages[0].startswith("Rough ETA:")


# Verify full updates cannot change the immutable scope of an existing dataset.
def test_full_update_rejects_existing_scope_mismatch(
    tmp_path: Path, record_factory
) -> None:
    store = Store(tmp_path)
    store.save([record_factory()])
    api = FakePipelineAPI({}, {})

    with pytest.raises(ValueError, match="scope is fixed at creation"):
        update_dataset(
            api,
            store,
            full=True,
            scope="active",
            progress=False,
        )

    assert api.search_calls == []


# Verify incremental updates derive their filter from persisted dataset scope.
def test_incremental_update_uses_recorded_active_scope(
    tmp_path: Path, record_factory
) -> None:
    store = Store(tmp_path)
    store.save([record_factory(date="2020-01-15")])
    store.write_metadata(scope="active")
    key = ("2020-01-01", "2020-01-31")
    api = FakePipelineAPI({key: []}, {})

    update_dataset(
        api,
        store,
        since_date=date(2020, 1, 1),
        until_date=date(2020, 1, 31),
        progress=False,
    )

    assert api.search_calls[0]["document_types"] == list(ACTIVE_TYPE_CODES)
