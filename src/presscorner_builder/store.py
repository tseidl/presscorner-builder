"""Parquet-backed dataset state, migration, ledgers, metadata, and exports."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from presscorner_builder import __version__
from presscorner_builder.api import ACTIVE_TYPE_CODES
from presscorner_builder.records import COLUMNS, SCHEMA

Reporter = Callable[[str], None]
DatasetScope = Literal["active", "all"]

LEGACY_COLUMNS = {
    "reference_number": "reference",
    "document_type": "doc_type",
    "document_type_name": "doc_type_name",
    "publication_date": "date",
    "authors": "spokespersons",
}

TYPE_FILE_STEMS = {
    "IP": "press-releases",
    "MEX": "daily-news",
    "SPEECH": "speeches",
    "MEMO": "memoranda",
    "STATEMENT": "statements",
    "QANDA": "questions-and-answers",
    "READ": "read-outs",
    "INF": "infringement-decisions",
    "FS": "factsheets",
    "AC": "news-articles",
}


# Ignore library messages unless a CLI or caller explicitly provides a reporter.
def _quiet_reporter(message: str) -> None:
    del message


# Create an empty DataFrame with the exact nullable package schema.
def empty_frame() -> pd.DataFrame:
    """Return a zero-row, schema-complete dataset frame."""
    return pd.DataFrame(
        {column: pd.Series(dtype=dtype) for column, dtype in SCHEMA.items()}
    )


# Coerce arbitrary bool-like values into pandas' nullable boolean dtype.
def _boolean_series(values: pd.Series) -> pd.Series:
    if str(values.dtype) in {"bool", "boolean"}:
        return values.astype("boolean")

    # Preserve missing values while recognizing common serialized booleans.
    def convert(value: Any) -> Any:
        if pd.isna(value):
            return pd.NA
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        return pd.NA

    return values.map(convert).astype("boolean")


# Write JSON atomically beside its final target.
def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.stem}-",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


# Read a JSON file or return the supplied default when it does not exist.
def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class Store:
    """Treat one Parquet file as the complete and durable pipeline state."""

    # Resolve either a parquet path or a data directory into standard sidecar paths.
    def __init__(
        self,
        path: str | Path = Path("data/press-corner.parquet"),
        *,
        reporter: Reporter | None = None,
    ) -> None:
        candidate = Path(path)
        self.path = (
            candidate
            if candidate.suffix.lower() == ".parquet"
            else candidate / "press-corner.parquet"
        )
        self.meta_path = self.path.with_suffix(".meta.json")
        self.failed_windows_path = self.path.parent / "failed-windows.json"
        self.failed_refs_path = self.path.parent / "failed-refs.json"
        self.reporter = reporter or _quiet_reporter
        self._frame: pd.DataFrame | None = None

    # Report whether the durable parquet source of truth already exists.
    @property
    def exists(self) -> bool:
        return self.path.exists()

    # Return the loaded data, loading and migrating it on first access.
    @property
    def frame(self) -> pd.DataFrame:
        if self._frame is None:
            return self.load()
        return self._frame

    # Map predecessor columns into the current schema without losing newer values.
    def _migrate_legacy(self, frame: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
        migrated = (
            any(column in frame.columns for column in LEGACY_COLUMNS)
            or "keywords" in frame.columns
        )
        if not migrated:
            return frame, False

        result = frame.copy()
        for old_name, new_name in LEGACY_COLUMNS.items():
            if old_name not in result.columns:
                continue
            if new_name in result.columns:
                result[new_name] = result[new_name].where(
                    result[new_name].notna(), result[old_name]
                )
                result = result.drop(columns=[old_name])
            else:
                result = result.rename(columns={old_name: new_name})

        if "detail_ok" not in result.columns:
            if "full_text" in result.columns:
                result["detail_ok"] = result["full_text"].fillna("").astype(str).ne("")
            else:
                result["detail_ok"] = False
        if "keywords" in result.columns:
            result = result.drop(columns=["keywords"])
        return result, True

    # Add missing schema columns, discard obsolete extras, and apply stable dtypes.
    def _normalize(self, frame: pd.DataFrame) -> pd.DataFrame:
        normalized = frame.copy()
        for column in COLUMNS:
            if column not in normalized.columns:
                normalized[column] = pd.NA

        normalized = normalized.loc[:, COLUMNS]
        for column, dtype in SCHEMA.items():
            if dtype == "boolean":
                normalized[column] = _boolean_series(normalized[column])
            else:
                normalized[column] = normalized[column].astype("string")
        return normalized

    # Load the parquet source, migrating a legacy schema in memory only.
    def load(self) -> pd.DataFrame:
        """Load the current dataset or return an empty schema when no file exists."""
        if not self.path.exists():
            self._frame = empty_frame()
            return self._frame

        raw = pd.read_parquet(self.path)
        migrated_frame, migrated = self._migrate_legacy(raw)
        self._frame = self._normalize(migrated_frame)
        if migrated:
            self.reporter(
                "Legacy schema detected — migrated in memory; "
                "the file is rewritten in the new schema on the next save."
            )
        return self._frame

    # Append records in memory while making the newest copy win by document ID.
    def add(self, records: pd.DataFrame | Iterable[Mapping[str, Any]]) -> pd.DataFrame:
        """Merge records into loaded state without writing until ``save`` is called."""
        if isinstance(records, pd.DataFrame):
            incoming = records.copy()
        else:
            incoming = pd.DataFrame(list(records))
        if incoming.empty:
            return self.frame

        combined = pd.concat([self.frame, incoming], ignore_index=True)
        combined = self._normalize(combined)
        combined = combined.drop_duplicates(subset=["document_id"], keep="last")
        self._frame = combined.reset_index(drop=True)
        return self._frame

    # Write a normalized frame atomically through a same-directory temporary file.
    def _write_frame(self, frame: pd.DataFrame) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            dir=self.path.parent,
            prefix=f".{self.path.stem}-",
            suffix=".tmp.parquet",
            delete=False,
        )
        temp_path = Path(handle.name)
        handle.close()
        try:
            frame.to_parquet(temp_path, index=False, compression="snappy")
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    # Merge optional records, deduplicate, sort newest-first, and atomically save.
    def save(
        self,
        records: pd.DataFrame | Iterable[Mapping[str, Any]] | None = None,
    ) -> pd.DataFrame:
        """Persist the exact current schema with one row per document."""
        if records is not None:
            self.add(records)
        frame = self._normalize(self.frame)
        frame = frame.drop_duplicates(subset=["document_id"], keep="last")
        frame = frame.sort_values(
            "date", ascending=False, na_position="last", kind="stable"
        )
        self._frame = frame.reset_index(drop=True)
        self._write_frame(self._frame)
        return self._frame

    # Return all non-empty document identifiers in durable state.
    @property
    def existing_ids(self) -> set[str]:
        values = self.frame["document_id"].dropna().astype(str)
        return {value for value in values if value}

    # Return the newest valid date string in durable state.
    @property
    def cutoff(self) -> str | None:
        values = self.frame["date"].dropna().astype(str)
        values = values[values.ne("")]
        return values.max() if not values.empty else None

    # Count local rows whose ISO dates lie inside an inclusive window.
    def count_window(self, date_from: date, date_to: date) -> int:
        values = self.frame["date"].astype("string")
        mask = values.ge(date_from.isoformat()) & values.le(date_to.isoformat())
        return int(mask.fillna(False).sum())

    # Load pending failed/incomplete window entries from their JSON ledger.
    def load_failed_windows(self) -> list[dict[str, str]]:
        payload = _read_json(self.failed_windows_path, [])
        if not isinstance(payload, list):
            raise ValueError(
                f"Invalid failed-window ledger: {self.failed_windows_path}"
            )
        entries: list[dict[str, str]] = []
        for item in payload:
            if (
                not isinstance(item, dict)
                or "date_from" not in item
                or "date_to" not in item
            ):
                raise ValueError(f"Invalid failed-window ledger entry: {item!r}")
            entries.append(
                {"date_from": str(item["date_from"]), "date_to": str(item["date_to"])}
            )
        return entries

    # Atomically replace the failed/incomplete window ledger.
    def save_failed_windows(self, entries: Iterable[Mapping[str, Any]]) -> None:
        unique: dict[tuple[str, str], dict[str, str]] = {}
        for entry in entries:
            normalized = {
                "date_from": str(entry["date_from"]),
                "date_to": str(entry["date_to"]),
            }
            unique[(normalized["date_from"], normalized["date_to"])] = normalized
        _write_json(self.failed_windows_path, list(unique.values()))

    # Load failed references and their original summaries from the JSON ledger.
    def load_failed_refs(self) -> dict[str, dict[str, Any]]:
        payload = _read_json(self.failed_refs_path, {})
        if not isinstance(payload, dict):
            raise ValueError(
                f"Invalid failed-reference ledger: {self.failed_refs_path}"
            )
        entries: dict[str, dict[str, Any]] = {}
        for reference, summary in payload.items():
            entries[str(reference)] = (
                dict(summary) if isinstance(summary, dict) else {"refCode": reference}
            )
        return entries

    # Atomically replace the failed-reference ledger.
    def save_failed_refs(self, entries: Mapping[str, Mapping[str, Any]]) -> None:
        _write_json(
            self.failed_refs_path, {key: dict(value) for key, value in entries.items()}
        )

    # Read sidecar metadata without treating a missing sidecar as an error.
    def load_metadata(self) -> dict[str, Any]:
        payload = _read_json(self.meta_path, {})
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid metadata sidecar: {self.meta_path}")
        scope = payload.get("scope", "all")
        if scope not in {"active", "all"}:
            raise ValueError(
                f"Invalid dataset scope in metadata sidecar {self.meta_path}: {scope!r}"
            )
        payload["scope"] = scope
        return payload

    # Return the immutable active/archive scope, defaulting old datasets to all.
    @property
    def scope(self) -> DatasetScope:
        scope = self.load_metadata()["scope"]
        return "active" if scope == "active" else "all"

    # Build and atomically persist a sidecar snapshot of dataset and failure state.
    def write_metadata(
        self,
        *,
        config_digest: str | None = None,
        project_metadata: Mapping[str, Any] | None = None,
        dataset_version: str | None = None,
        scope: DatasetScope | None = None,
    ) -> dict[str, Any]:
        """Stamp package, date, type, provenance, and pending-work metadata."""
        dataset_scope = self.scope if scope is None else scope
        if dataset_scope not in {"active", "all"}:
            raise ValueError("scope must be 'active' or 'all'")
        frame = self.frame
        type_counts = (
            frame["doc_type"].dropna().astype(str).value_counts().sort_index().to_dict()
        )
        failed_windows = self.load_failed_windows()
        failed_refs = sorted(self.load_failed_refs())
        payload: dict[str, Any] = {
            "package_version": __version__,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "total_documents": int(len(frame)),
            "cutoff": self.cutoff,
            "scope": dataset_scope,
            "per_type_counts": {
                str(key): int(value) for key, value in type_counts.items()
            },
            "config_hash": config_digest,
            "failed_windows": failed_windows,
            "failed_refs": failed_refs,
            "failed_window_count": len(failed_windows),
            "failed_ref_count": len(failed_refs),
        }
        if project_metadata is not None:
            payload["metadata"] = dict(project_metadata)
        if dataset_version is not None:
            payload["dataset_version"] = dataset_version
        _write_json(self.meta_path, payload)
        return payload

    # Delete only this store's exact parquet, sidecar, and pending-work files.
    def reset(self) -> list[Path]:
        """Remove corpus state selected by an explicit fresh-build request."""
        removed: list[Path] = []
        for path in (
            self.path,
            self.meta_path,
            self.failed_windows_path,
            self.failed_refs_path,
        ):
            if path.exists():
                path.unlink()
                removed.append(path)
        self._frame = None
        return removed

    # Export each modern type and one combined legacy/other subset as parquet.
    def export_by_type(self, output_directory: str | Path | None = None) -> list[Path]:
        """Write predecessor-compatible kebab-case subset filenames."""
        output_dir = (
            Path(output_directory) if output_directory is not None else self.path.parent
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        frame = self.frame
        written: list[Path] = []
        for code, stem in TYPE_FILE_STEMS.items():
            subset = frame.loc[frame["doc_type"].eq(code)]
            if subset.empty:
                continue
            target = output_dir / f"{stem}.parquet"
            subset.to_parquet(target, index=False, compression="snappy")
            written.append(target)

        other = frame.loc[~frame["doc_type"].isin(TYPE_FILE_STEMS)]
        if not other.empty:
            target = output_dir / "other.parquet"
            other.to_parquet(target, index=False, compression="snappy")
            written.append(target)
        return written

    # Export a website-visible active-type parquet subset beside the source dataset.
    def export_active(self, path: str | Path | None = None) -> Path:
        """Write rows whose document type is currently active and return the path."""
        target = (
            Path(path)
            if path is not None
            else self.path.with_name(f"{self.path.stem}-active.parquet")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        subset = self.frame.loc[self.frame["doc_type"].isin(ACTIVE_TYPE_CODES)]
        subset.to_parquet(target, index=False, compression="snappy")
        return target

    # Export the complete current dataset as a CSV file beside the parquet source.
    def export_csv(self, path: str | Path | None = None) -> Path:
        """Write a UTF-8 CSV representation and return its path."""
        target = Path(path) if path is not None else self.path.with_suffix(".csv")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.frame.to_csv(target, index=False)
        return target
