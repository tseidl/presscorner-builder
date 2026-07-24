"""Mocked tests for manifest resolution, checksums, and atomic download installation."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests

from presscorner_builder.download import MANIFEST_URL, download_dataset

DATASET_URL = "https://example.test/press-corner.parquet"


class FakeDownloadSession:
    """Serve a JSON manifest and binary parquet from an in-memory URL mapping."""

    # Store response objects keyed by the exact URL requested by the downloader.
    def __init__(self, responses_by_url: dict[str, requests.Response]) -> None:
        self.responses_by_url = responses_by_url
        self.calls: list[str] = []

    # Return the configured response while accepting both manifest and streaming kwargs.
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        del kwargs
        self.calls.append(url)
        return self.responses_by_url[url]


# Build a requests.Response containing either JSON or arbitrary bytes.
def _response(
    *, json_payload: Any | None = None, body: bytes = b""
) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = "https://example.test"
    if json_payload is not None:
        response._content = json.dumps(json_payload).encode("utf-8")
        response.headers["content-type"] = "application/json"
    else:
        response._content = body
        response.raw = io.BytesIO(body)
        response.headers["content-length"] = str(len(body))
    return response


# Build a tiny valid parquet payload for HTTP response bodies.
def _parquet_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "source.parquet"
    pd.DataFrame(
        {
            "document_id": ["ip_20_1"],
            "date": ["2020-01-02"],
            "doc_type": ["IP"],
        }
    ).to_parquet(path, index=False)
    return path.read_bytes()


# Verify the manifest URL is resolved and both checksum and size are enforced.
def test_download_from_manifest(tmp_path: Path, monkeypatch) -> None:
    payload = _parquet_bytes(tmp_path)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "version": "vtest",
        "cutoff": "2020-01-02",
        "url": DATASET_URL,
        "sha256": digest,
        "size_bytes": len(payload),
    }
    session = FakeDownloadSession(
        {
            MANIFEST_URL: _response(json_payload=manifest),
            DATASET_URL: _response(body=payload),
        }
    )
    monkeypatch.setattr(
        "presscorner_builder.download.requests.Session", lambda: session
    )
    data_dir = tmp_path / "data"

    result = download_dataset(data_dir, progress=False)
    sidecar = json.loads((data_dir / "press-corner.meta.json").read_text())

    assert result.path.exists()
    assert result.version == "vtest"
    assert result.row_count == 1
    assert result.sha256 == digest
    assert sidecar["dataset_version"] == "vtest"
    assert sidecar["cutoff"] == "2020-01-02"
    assert sidecar["scope"] == "all"


# Verify an explicit manifest scope is copied into the installed sidecar.
def test_download_stamps_manifest_scope(tmp_path: Path, monkeypatch) -> None:
    payload = _parquet_bytes(tmp_path)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "version": "vtest",
        "cutoff": "2020-01-02",
        "scope": "active",
        "url": DATASET_URL,
        "sha256": digest,
        "size_bytes": len(payload),
    }
    session = FakeDownloadSession(
        {
            MANIFEST_URL: _response(json_payload=manifest),
            DATASET_URL: _response(body=payload),
        }
    )
    monkeypatch.setattr(
        "presscorner_builder.download.requests.Session", lambda: session
    )
    data_dir = tmp_path / "data"

    download_dataset(data_dir, progress=False)
    sidecar = json.loads((data_dir / "press-corner.meta.json").read_text())

    assert sidecar["scope"] == "active"


# Verify checksum failure does not install a corrupt target or leave a temp file.
def test_download_checksum_failure_is_atomic(tmp_path: Path, monkeypatch) -> None:
    payload = _parquet_bytes(tmp_path)
    manifest = {
        "version": "vtest",
        "cutoff": "2020-01-02",
        "url": DATASET_URL,
        "sha256": "0" * 64,
        "size_bytes": len(payload),
    }
    session = FakeDownloadSession(
        {
            MANIFEST_URL: _response(json_payload=manifest),
            DATASET_URL: _response(body=payload),
        }
    )
    monkeypatch.setattr(
        "presscorner_builder.download.requests.Session", lambda: session
    )
    data_dir = tmp_path / "data"

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        download_dataset(data_dir, progress=False)

    assert not (data_dir / "press-corner.parquet").exists()
    assert not list(data_dir.glob("*.download.parquet"))


# Verify an existing dataset is protected unless the caller explicitly forces replacement.
def test_download_refuses_existing_dataset(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "press-corner.parquet").write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="Refusing"):
        download_dataset(data_dir, progress=False)
