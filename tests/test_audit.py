"""Tests for local/API count reconciliation and mismatch classification."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from presscorner_builder.api import ACTIVE_TYPE_CODES
from presscorner_builder.audit import (
    audit_store,
    local_count,
    mismatches,
    reconcile_windows,
)
from presscorner_builder.cli import main
from presscorner_builder.store import Store


class CountAPI:
    """Return deterministic totals keyed by inclusive ISO date pairs."""

    # Store synthetic count responses for later calls.
    def __init__(self, counts: dict[tuple[str, str], int]) -> None:
        self.request_delay = 1.0
        self.counts = counts
        self.calls: list[dict[str, Any]] = []

    # Return the configured count while accepting the real client's keyword signature.
    def count_window(self, date_from: date, date_to: date, **kwargs: Any) -> int:
        self.calls.append(dict(kwargs))
        return self.counts[(date_from.isoformat(), date_to.isoformat())]


class PhantomAuditAPI:
    """Serve stable counts and changing enumerations for audit-repair tests."""

    # Configure the count and successive full-window result enumerations.
    def __init__(
        self,
        total: int,
        search_results: list[list[dict[str, Any]]],
    ) -> None:
        self.request_delay = 0.0
        self.total = total
        self.search_results = list(search_results)
        self.count_calls: list[dict[str, Any]] = []

    # Return the synthetic inflated total while recording scope filters.
    def count_window(self, date_from: date, date_to: date, **kwargs: Any) -> int:
        del date_from, date_to
        self.count_calls.append(dict(kwargs))
        return self.total

    # Return the next configured enumeration for repair or phantom verification.
    def search_window(
        self, date_from: date, date_to: date, **kwargs: Any
    ) -> list[dict[str, Any]]:
        del date_from, date_to, kwargs
        return self.search_results.pop(0)

    # Fail no detail call silently; these tests enumerate only already-stored refs.
    def get_document(self, reference: str, **kwargs: Any) -> None:
        del reference, kwargs
        return None


# Create a minimal search summary for phantom-count enumeration.
def _summary(reference: str) -> dict[str, Any]:
    return {
        "refCode": reference,
        "eventDate": "2020-01-15",
        "title": reference,
        "leadText": "Lead",
        "docutype": {"code": "IP", "description": "Press release"},
        "languageCode": "en",
    }


# Verify local ISO date comparisons are inclusive at both boundaries.
def test_local_count_is_inclusive() -> None:
    frame = pd.DataFrame({"date": ["2020-01-01", "2020-01-31", "2020-02-01", None]})

    assert local_count(frame, date(2020, 1, 1), date(2020, 1, 31)) == 2


# Verify deficits, surpluses, and exact matches are classified independently.
def test_reconcile_detects_deficient_windows() -> None:
    windows = [
        (date(2020, 3, 1), date(2020, 3, 31)),
        (date(2020, 2, 1), date(2020, 2, 29)),
        (date(2020, 1, 1), date(2020, 1, 31)),
    ]
    frame = pd.DataFrame(
        {
            "date": [
                "2020-03-02",
                "2020-02-02",
                "2020-02-03",
                "2020-01-02",
            ]
        }
    )
    api = CountAPI(
        {
            ("2020-03-01", "2020-03-31"): 2,
            ("2020-02-01", "2020-02-29"): 1,
            ("2020-01-01", "2020-01-31"): 1,
        }
    )

    results = reconcile_windows(api, frame, windows)

    assert [result.status for result in results] == ["deficient", "surplus", "match"]
    assert [result.status for result in mismatches(results)] == ["deficient", "surplus"]


# Verify active-scope audit counts carry the exact pinned type-code filter.
def test_active_scope_audit_filters_count_query(
    tmp_path: Path, record_factory
) -> None:
    store = Store(tmp_path)
    store.save([record_factory(date="2020-01-15")])
    store.write_metadata(scope="active")
    api = CountAPI({("2020-01-01", "2020-01-31"): 1})
    messages: list[str] = []

    audit_store(
        api,
        store,
        date(2020, 1, 1),
        date(2020, 1, 31),
        progress=False,
        reporter=messages.append,
    )

    assert api.calls == [
        {"language": "en", "document_types": list(ACTIVE_TYPE_CODES)}
    ]
    assert messages == [
        "Auditing 1 windows at ~1 request/s — this takes about 1 minute."
    ]


# Verify phantom totals reconcile successfully, while retrievable missing refs stay partial.
@pytest.mark.parametrize(
    ("enumerated_missing", "expected_exit", "expected_status"),
    [
        (False, 0, "reconciled"),
        (True, 2, "deficient"),
    ],
)
def test_audit_fix_reconciles_only_phantom_server_counts(
    tmp_path: Path,
    record_factory,
    monkeypatch,
    capsys,
    enumerated_missing: bool,
    expected_exit: int,
    expected_status: str,
) -> None:
    store = Store(tmp_path)
    store.save(
        [
            record_factory(
                document_id="ip_20_1",
                reference="IP/20/1",
                date="2020-01-15",
            )
        ]
    )
    stored = _summary("IP/20/1")
    enumeration = [stored]
    if enumerated_missing:
        enumeration.append(_summary("IP/20/2"))
    api = PhantomAuditAPI(2, [[stored], enumeration])
    monkeypatch.setattr(
        "presscorner_builder.cli.PressCornerAPI",
        lambda **kwargs: api,
    )

    exit_code = main(
        [
            "audit",
            "--data-dir",
            str(tmp_path),
            "--since",
            "2020-01-01",
            "--until",
            "2020-01-31",
            "--fix",
            "--delay",
            "0",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == expected_exit
    assert f"({expected_status})" in output
    if enumerated_missing:
        assert "remain after repair" in output
    else:
        assert "phantom index entries" in output
        assert "remain after repair" not in output
