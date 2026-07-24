"""Strict Pydantic configuration models and YAML loading helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from presscorner_builder.api import ACTIVE_TYPE_CODES


class StrictModel(BaseModel):
    """Base model that rejects misspelled or misplaced configuration keys."""

    model_config = ConfigDict(extra="forbid")


class MetadataConfig(StrictModel):
    """Optional human-readable metadata copied into the sidecar."""

    project_name: str = ""
    author: str = ""
    description: str = ""


class DescriptiveDataConfig(StrictModel):
    """Search filters for a date-windowed descriptive corpus."""

    mode: Literal["descriptive"] = "descriptive"
    document_types: list[str] = Field(default_factory=list)
    start_date: date = date(1975, 1, 1)
    end_date: date | None = None
    keywords: list[str] = Field(default_factory=list)
    commissioners: list[str] = Field(default_factory=list)
    policy_areas: list[str] = Field(default_factory=list)
    language: str = "en"

    # Normalize document type codes without restricting legacy values.
    @field_validator("document_types", mode="before")
    @classmethod
    def normalize_document_types(cls, values: object) -> object:
        if values is None:
            return []
        if isinstance(values, str) and values.strip().lower() == "active":
            return list(ACTIVE_TYPE_CODES)
        if not isinstance(values, list):
            return values
        normalized = [str(value).strip().upper() for value in values]
        if "ACTIVE" in normalized:
            if any(value != "ACTIVE" for value in normalized):
                raise ValueError(
                    "document_types: active cannot be mixed with literal codes"
                )
            return list(ACTIVE_TYPE_CODES)
        return normalized

    # Ensure an explicitly bounded descriptive date range is coherent.
    @model_validator(mode="after")
    def validate_date_range(self) -> "DescriptiveDataConfig":
        if self.end_date is not None and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class FixedDataConfig(StrictModel):
    """Explicit reference list for a fixed corpus."""

    mode: Literal["fixed"]
    references: list[str] = Field(default_factory=list)
    language: str = "en"

    # Normalize reference codes while retaining arbitrary valid archive prefixes.
    @field_validator("references", mode="before")
    @classmethod
    def normalize_references(cls, values: object) -> object:
        if values is None:
            return []
        if not isinstance(values, list):
            return values
        return [str(value).strip().upper() for value in values]


DataConfig = Annotated[
    Union[DescriptiveDataConfig, FixedDataConfig],
    Field(discriminator="mode"),
]


class ProcessingConfig(StrictModel):
    """Network and record-processing options."""

    keep_html: bool = False
    request_delay: float = Field(default=1.0, ge=0)


class OutputConfig(StrictModel):
    """Location and filename options for a built corpus."""

    output_directory: Path = Path("./output")
    dataset_name: str = "press-corner"

    # Reject names that would escape the configured output directory.
    @field_validator("dataset_name")
    @classmethod
    def validate_dataset_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or Path(cleaned).name != cleaned:
            raise ValueError("dataset_name must be a non-empty filename stem")
        return cleaned


class Config(StrictModel):
    """Complete presscorner-builder configuration."""

    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    data: DataConfig = Field(default_factory=DescriptiveDataConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)


# Load and strictly validate one YAML configuration file.
def load_config(path: str | Path) -> Config:
    """Load YAML with safe parsing and report Pydantic's precise key locations."""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Configuration root must be a YAML mapping")
    return Config.model_validate(raw)


# Hash a validated configuration deterministically for sidecar provenance.
def config_hash(config: Config) -> str:
    """Return a SHA-256 hash of the normalized configuration values."""
    payload = config.model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
