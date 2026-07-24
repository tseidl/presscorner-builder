"""Tests for help, status, and config initialization without network access."""

from __future__ import annotations

from pathlib import Path

import pytest

from presscorner_builder.cli import _report_run, main
from presscorner_builder.config import load_config
from presscorner_builder.pipeline import RunResult
from presscorner_builder.store import Store


# Verify argparse help exits successfully and lists the primary verbs.
def test_help_runs(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])

    output = capsys.readouterr().out
    assert caught.value.code == 0
    assert "download" in output
    assert "update" in output
    assert "audit" in output


# Verify status summarizes a tiny synthetic parquet and its pending ledgers.
def test_status_on_tiny_parquet(tmp_path: Path, record_factory, capsys) -> None:
    store = Store(tmp_path)
    store.save([record_factory()])
    store.save_failed_windows([])
    store.save_failed_refs({})
    store.write_metadata(dataset_version="vtest")

    exit_code = main(["status", "--data-dir", str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Total documents: 1" in output
    assert "Cutoff: 2026-01-20" in output
    assert "Detail complete: 100.0%" in output
    assert "Scope: all" in output
    assert "IP: 1" in output
    assert "Dataset version: vtest" in output


# Verify init writes the same valid descriptive example used by the repository.
def test_init_writes_valid_config(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config.yaml"

    exit_code = main(["init", str(target)])
    config = load_config(target)

    assert exit_code == 0
    assert config.data.mode == "descriptive"
    assert target.read_text(encoding="utf-8") == Path(
        "configs/example-config.yaml"
    ).read_text(encoding="utf-8")


# Verify permanent empty details are informational and do not produce partial exit 2.
def test_run_summary_reports_permanent_empty_details_as_success(capsys) -> None:
    result = RunResult(new_documents=1, permanently_empty_details=1)

    exit_code = _report_run(result)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "no detail record server-side" in output


# Verify scope cannot be supplied to an ordinary incremental update.
def test_update_scope_requires_full(tmp_path: Path, capsys) -> None:
    exit_code = main(
        ["update", "--data-dir", str(tmp_path), "--scope", "active"]
    )

    assert exit_code == 1
    assert "only valid together with --full" in capsys.readouterr().err
