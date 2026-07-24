"""Shared synthetic Press Corner fixtures for the mocked test suite."""

from __future__ import annotations

from typing import Any

import pytest

from presscorner_builder.records import build_record


# Provide an abbreviated real-shaped search result from the Press Corner API.
@pytest.fixture
def sample_summary() -> dict[str, Any]:
    return {
        "refCode": "IP/26/301",
        "eventDate": "2026-01-20",
        "title": "Summary title",
        "leadText": "Summary lead",
        "docutype": {"code": "IP", "description": "Press release"},
        "languageCode": "en",
    }


# Provide an abbreviated real-shaped detail response covering every repeated section.
@pytest.fixture
def sample_detail() -> dict[str, Any]:
    return {
        "docuLanguageResource": {
            "title": "Detailed title",
            "subtitle": "Detailed subtitle",
            "htmlContent": "<h1>Heading</h1><p>First <b>paragraph</b>.</p>",
            "language": "en",
            "attachmentResources": [],
            "linkResources": [],
            "original": True,
        },
        "refCd": "IP/26/301",
        "eventDate": "2026-01-20",
        "publishDate": "2026-01-20T11:42:00Z",
        "docutypeResource": {"code": "IP", "description": "Press release"},
        "contactsResource": [
            {"firstName": "Anna", "lastName": "Example", "title": "Spokesperson"},
            {"firstName": "Ben", "lastName": "Sample", "title": ""},
        ],
        "commissionerResource": [
            {"code": "commissioner-a", "shortDescription": "Commissioner Alpha"},
            {"code": "commissioner-b", "shortDescription": "Commissioner Beta"},
        ],
        "placeResource": {"description": "Brussels"},
        "policiesResource": [
            {"code": "TRADE", "description": "Trade"},
            {"code": "ECON", "description": "Economy, finance and the euro"},
        ],
        "countriesResource": [],
        "originalLanguage": "en",
    }


# Build a schema-complete synthetic record with easily overridden identifying fields.
@pytest.fixture
def record_factory(sample_summary, sample_detail):
    # Construct one record without relying on wall-clock values in assertions.
    def make_record(**overrides: Any) -> dict[str, Any]:
        record = build_record(
            sample_summary,
            sample_detail,
            scraped_at="2026-01-21T00:00:00+00:00",
        )
        record.update(overrides)
        return record

    return make_record
