"""Tests for parquet state, migration, deduplication, and atomic outputs."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from presscorner_builder.api import ACTIVE_TYPE_CODES
from presscorner_builder.records import COLUMNS
from presscorner_builder.store import Store


# Verify a schema-complete record survives a parquet round trip.
def test_store_round_trip(tmp_path: Path, record_factory) -> None:
    path = tmp_path / "press-corner.parquet"
    store = Store(path)
    store.save([record_factory()])

    loaded = Store(path).load()

    assert list(loaded.columns) == COLUMNS
    assert len(loaded) == 1
    assert loaded.iloc[0]["document_id"] == "ip_26_301"
    assert loaded.iloc[0]["detail_ok"]


# Verify a later row for the same document ID replaces the earlier row.
def test_store_deduplicates_keep_last(tmp_path: Path, record_factory) -> None:
    path = tmp_path / "press-corner.parquet"
    store = Store(path)
    store.save([record_factory(title="First")])
    store.save([record_factory(title="Replacement")])

    loaded = Store(path).load()

    assert len(loaded) == 1
    assert loaded.iloc[0]["title"] == "Replacement"


# Verify predecessor columns migrate in place with row count and semantics preserved.
def test_legacy_schema_migration(tmp_path: Path) -> None:
    path = tmp_path / "press-corner.parquet"
    legacy = pd.DataFrame(
        [
            {
                "document_id": "ip_20_1",
                "reference_number": "IP/20/1",
                "document_type": "IP",
                "document_type_name": "Press release",
                "title": "Legacy title",
                "subtitle": "",
                "publication_date": "2020-01-02",
                "language": "en",
                "summary": "Legacy summary",
                "url": "https://example.test/detail",
                "scraped_at": "2020-01-03T00:00:00",
                "full_text": "Legacy body",
                "authors": "Anna Example",
                "policy_areas": "Trade",
                "keywords": "",
                "pdf_url": "https://example.test/file.pdf",
            },
            {
                "document_id": "speech_20_2",
                "reference_number": "SPEECH/20/2",
                "document_type": "SPEECH",
                "document_type_name": "Speech",
                "title": "Partial legacy title",
                "subtitle": "",
                "publication_date": "2020-01-01",
                "language": "en",
                "summary": "",
                "url": "https://example.test/detail-2",
                "scraped_at": "2020-01-03T00:00:00",
                "full_text": "",
                "authors": "Ben Sample",
                "policy_areas": "",
                "keywords": "unused",
                "pdf_url": "",
            },
        ]
    )
    legacy.to_parquet(path, index=False)
    messages: list[str] = []

    store = Store(path, reporter=messages.append)
    migrated = store.load()
    untouched = pd.read_parquet(path)

    assert len(migrated) == len(legacy) == len(untouched)
    assert list(migrated.columns) == COLUMNS
    # Loading is read-only: the file keeps the legacy schema until a save.
    assert "reference_number" in untouched.columns
    assert migrated.loc[0, "reference"] == "IP/20/1"
    assert migrated.loc[0, "spokespersons"] == "Anna Example"
    assert migrated.loc[0, "detail_ok"]
    assert not migrated.loc[1, "detail_ok"]
    assert "keywords" not in migrated
    assert len(messages) == 1 and "Legacy schema" in messages[0]

    store.save()
    persisted = pd.read_parquet(path)
    assert list(persisted.columns) == COLUMNS
    assert len(persisted) == len(legacy)


# Verify successful atomic saves leave no same-directory temporary files behind.
def test_atomic_save_leaves_no_temp_files(tmp_path: Path, record_factory) -> None:
    store = Store(tmp_path / "press-corner.parquet")

    store.save([record_factory()])

    assert [path for path in tmp_path.iterdir() if ".tmp" in path.name] == []


# Verify sidecar metadata contains type counts, cutoff, and pending ledger counts.
def test_write_metadata_summarizes_store(tmp_path: Path, record_factory) -> None:
    store = Store(tmp_path / "press-corner.parquet")
    store.save([record_factory()])
    store.save_failed_windows([{"date_from": "2020-01-01", "date_to": "2020-01-31"}])
    store.save_failed_refs({"IP/20/1": {"refCode": "IP/20/1"}})

    metadata = store.write_metadata(config_digest="abc")

    assert metadata["total_documents"] == 1
    assert metadata["cutoff"] == "2026-01-20"
    assert metadata["per_type_counts"] == {"IP": 1}
    assert metadata["failed_window_count"] == 1
    assert metadata["failed_ref_count"] == 1
    assert metadata["config_hash"] == "abc"
    assert metadata["scope"] == "all"


# Verify scope persists through metadata and old or absent fields mean full archive.
def test_scope_round_trip_and_missing_field_defaults_to_all(
    tmp_path: Path, record_factory
) -> None:
    store = Store(tmp_path / "press-corner.parquet")
    store.save([record_factory()])

    assert store.scope == "all"

    store.write_metadata(scope="active")
    assert Store(store.path).scope == "active"

    payload = json.loads(store.meta_path.read_text(encoding="utf-8"))
    payload.pop("scope")
    store.meta_path.write_text(json.dumps(payload), encoding="utf-8")

    assert Store(store.path).scope == "all"


# Verify predecessor-compatible type stems and the other bucket are exported.
def test_export_by_type_uses_expected_stems(tmp_path: Path, record_factory) -> None:
    store = Store(tmp_path / "press-corner.parquet")
    store.save(
        [
            record_factory(),
            record_factory(
                document_id="bio_20_1",
                reference="BIO/20/1",
                doc_type="BIO",
                date="2020-01-01",
            ),
        ]
    )

    written = store.export_by_type()

    assert {path.name for path in written} == {
        "press-releases.parquet",
        "other.parquet",
    }


# Verify active export is a strict type-filtered parquet subset.
def test_export_active_filters_to_pinned_type_codes(
    tmp_path: Path, record_factory
) -> None:
    store = Store(tmp_path / "press-corner.parquet")
    store.save(
        [
            record_factory(),
            record_factory(
                document_id="bio_20_1",
                reference="BIO/20/1",
                doc_type="BIO",
                date="2020-01-01",
            ),
        ]
    )

    target = store.export_active()
    exported = pd.read_parquet(target)

    assert target.name == "press-corner-active.parquet"
    assert set(exported["doc_type"]) <= set(ACTIVE_TYPE_CODES)
    assert exported["document_id"].tolist() == ["ip_26_301"]
