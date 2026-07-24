"""Download and verify a published presscorner-builder parquet release."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

from presscorner_builder import __version__

MANIFEST_URL = (
    "https://raw.githubusercontent.com/tseidl/presscorner-builder/"
    "main/dataset-manifest.json"
)
USER_AGENT = (
    f"presscorner-builder/{__version__} "
    "(https://github.com/tseidl/presscorner-builder; academic research)"
)

Reporter = Callable[[str], None]


@dataclass(frozen=True)
class DownloadResult:
    """Verified download location and published release metadata."""

    path: Path
    version: str | None
    cutoff: str | None
    row_count: int
    sha256: str
    size_bytes: int


# Ignore download messages unless the CLI supplies a reporter.
def _quiet_reporter(message: str) -> None:
    del message


# Atomically write a small JSON sidecar next to a downloaded dataset.
def _write_sidecar(path: Path, payload: Mapping[str, Any]) -> None:
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
            json.dump(dict(payload), handle, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


# Fetch and validate the small release manifest document.
def fetch_manifest(
    *,
    session: requests.Session | None = None,
    manifest_url: str = MANIFEST_URL,
) -> dict[str, Any]:
    """Return a manifest containing the required release and checksum fields."""
    client = session or requests.Session()
    response = client.get(manifest_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    required = {"version", "cutoff", "url", "sha256", "size_bytes"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        missing = sorted(required - set(payload if isinstance(payload, dict) else {}))
        raise ValueError(
            f"Dataset manifest is missing required field(s): {', '.join(missing)}"
        )
    if payload.get("scope", "all") not in {"active", "all"}:
        raise ValueError("Dataset manifest scope must be 'active' or 'all'")
    return payload


# Read row count and cutoff from parquet metadata and its date column.
def _parquet_summary(path: Path) -> tuple[int, str | None]:
    parquet = pq.ParquetFile(path)
    row_count = parquet.metadata.num_rows
    names = set(parquet.schema.names)
    date_column = (
        "date"
        if "date" in names
        else "publication_date"
        if "publication_date" in names
        else None
    )
    if date_column is None or row_count == 0:
        return row_count, None
    dates = (
        pd.read_parquet(path, columns=[date_column])[date_column].dropna().astype(str)
    )
    return row_count, dates.max() if not dates.empty else None


# Count document types from either the current or predecessor parquet schema.
def _parquet_type_counts(path: Path) -> dict[str, int]:
    parquet = pq.ParquetFile(path)
    names = set(parquet.schema.names)
    type_column = (
        "doc_type"
        if "doc_type" in names
        else "document_type"
        if "document_type" in names
        else None
    )
    if type_column is None:
        return {}
    values = (
        pd.read_parquet(path, columns=[type_column])[type_column].dropna().astype(str)
    )
    return {
        str(key): int(value)
        for key, value in values.value_counts().sort_index().items()
    }


# Stream a parquet file to a temporary target while calculating its SHA-256 digest.
def _stream_download(
    url: str,
    temp_path: Path,
    *,
    session: requests.Session,
    progress: bool,
) -> tuple[str, int]:
    response = session.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=60,
        stream=True,
    )
    response.raise_for_status()
    total = int(response.headers.get("content-length", 0)) or None
    digest = hashlib.sha256()
    size = 0
    with (
        temp_path.open("wb") as handle,
        tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            desc="Downloading dataset",
            disable=not progress,
        ) as bar,
    ):
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            bar.update(len(chunk))
    return digest.hexdigest(), size


# Download, verify, atomically install, and describe the published parquet dataset.
def download_dataset(
    data_dir: str | Path = Path("data"),
    *,
    url: str | None = None,
    expected_sha256: str | None = None,
    force: bool = False,
    progress: bool = True,
    reporter: Reporter | None = None,
    session: requests.Session | None = None,
) -> DownloadResult:
    """Install the manifest release or a caller-supplied direct parquet URL."""
    report = reporter or _quiet_reporter
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "press-corner.parquet"
    if target.exists() and not force:
        raise FileExistsError(f"Refusing to overwrite existing dataset: {target}")

    client = session or requests.Session()
    manifest: dict[str, Any] | None = None
    if url is None:
        manifest = fetch_manifest(session=client)
        download_url = str(manifest["url"])
    else:
        download_url = url

    handle = tempfile.NamedTemporaryFile(
        dir=directory,
        prefix=".press-corner-",
        suffix=".download.parquet",
        delete=False,
    )
    temp_path = Path(handle.name)
    handle.close()
    try:
        digest, size = _stream_download(
            download_url,
            temp_path,
            session=client,
            progress=progress,
        )
        expected_digest = (
            str(manifest["sha256"]).lower() if manifest is not None else None
        )
        if expected_sha256 is not None:
            expected_digest = expected_sha256.lower()
        if expected_digest is not None and digest.lower() != expected_digest:
            raise ValueError(
                f"SHA-256 mismatch: expected {expected_digest}, received {digest}"
            )
        if manifest is not None:
            expected_size = int(manifest["size_bytes"])
            if size != expected_size:
                raise ValueError(
                    f"Size mismatch: expected {expected_size}, received {size}"
                )

        row_count, parquet_cutoff = _parquet_summary(temp_path)
        type_counts = _parquet_type_counts(temp_path)
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    version = str(manifest["version"]) if manifest is not None else None
    cutoff = str(manifest["cutoff"]) if manifest is not None else parquet_cutoff
    scope = str(manifest.get("scope", "all")) if manifest is not None else "all"
    sidecar = {
        "package_version": __version__,
        "last_run": datetime.now(timezone.utc).isoformat(),
        "dataset_version": version,
        "total_documents": row_count,
        "cutoff": cutoff,
        "scope": scope,
        "per_type_counts": type_counts,
        "config_hash": None,
        "sha256": digest,
        "size_bytes": size,
        "source_url": download_url,
        "failed_windows": [],
        "failed_refs": [],
        "failed_window_count": 0,
        "failed_ref_count": 0,
    }
    _write_sidecar(target.with_suffix(".meta.json"), sidecar)
    report(
        f"Downloaded dataset {version or 'from custom URL'} through {cutoff or 'unknown'} "
        f"with {row_count:,} rows."
    )
    return DownloadResult(
        path=target,
        version=version,
        cutoff=cutoff,
        row_count=row_count,
        sha256=digest,
        size_bytes=size,
    )
