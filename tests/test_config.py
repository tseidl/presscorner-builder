"""Tests for strict discriminated YAML configuration."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from presscorner_builder.api import ACTIVE_TYPE_CODES
from presscorner_builder.config import (
    DescriptiveDataConfig,
    FixedDataConfig,
    config_hash,
    load_config,
)


# Write a YAML snippet and return its path for validation tests.
def _write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


# Verify the shipped descriptive example parses with typed dates and defaults.
def test_valid_example_yaml_parses() -> None:
    config = load_config(Path("configs/example-config.yaml"))

    assert isinstance(config.data, DescriptiveDataConfig)
    assert config.data.start_date == date(1975, 1, 1)
    assert config.data.end_date is None
    assert config.processing.request_delay == 1.0
    assert config.output.dataset_name == "press-corner"


# Verify strict validation names an unknown nested key in its error.
def test_unknown_key_rejected_with_key_name(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """data:
  mode: descriptive
  mystery_filter: value
""",
    )

    with pytest.raises(ValidationError) as caught:
        load_config(path)

    assert "mystery_filter" in str(caught.value)


# Verify fixed mode accepts references and normalizes their case.
def test_fixed_mode_parses_references(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """data:
  mode: fixed
  references: [ip/26/301, speech/26/1]
  language: en
""",
    )

    config = load_config(path)

    assert isinstance(config.data, FixedDataConfig)
    assert config.data.references == ["IP/26/301", "SPEECH/26/1"]


# Verify search-only fields are forbidden in fixed mode.
def test_fixed_mode_rejects_descriptive_fields(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """data:
  mode: fixed
  references: [IP/26/301]
  keywords: [trade]
""",
    )

    with pytest.raises(ValidationError, match="keywords"):
        load_config(path)


# Verify fixed-only references are forbidden in descriptive mode.
def test_descriptive_mode_rejects_references(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """data:
  mode: descriptive
  references: [IP/26/301]
""",
    )

    with pytest.raises(ValidationError, match="references"):
        load_config(path)


# Verify invalid descriptive date ordering is rejected clearly.
def test_descriptive_mode_rejects_inverted_dates(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """data:
  mode: descriptive
  start_date: 2020-02-01
  end_date: 2020-01-01
""",
    )

    with pytest.raises(ValidationError, match="start_date"):
        load_config(path)


# Verify the active document-type sugar expands to the pinned reproducible tuple.
def test_active_document_types_expand_to_pinned_codes(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """data:
  mode: descriptive
  document_types: active
""",
    )

    config = load_config(path)

    assert config.data.document_types == list(ACTIVE_TYPE_CODES)


# Verify active sugar cannot be combined ambiguously with literal type codes.
def test_active_document_types_reject_mixed_literal_codes(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """data:
  mode: descriptive
  document_types: [active, IP]
""",
    )

    with pytest.raises(ValidationError, match="cannot be mixed"):
        load_config(path)


# Verify normalized configuration hashes are stable across repeated loading.
def test_config_hash_is_deterministic() -> None:
    first = load_config("configs/example-config.yaml")
    second = load_config("configs/example-config.yaml")

    assert config_hash(first) == config_hash(second)
