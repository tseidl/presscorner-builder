"""Tests for detail-to-record schema mapping."""

from __future__ import annotations

import pandas as pd

from presscorner_builder.records import COLUMNS, build_record, document_id


# Verify stable reference normalization.
def test_document_id_normalization() -> None:
    assert document_id(" IP/26/301 ") == "ip_26_301"


# Verify all detail, summary, text, URL, and repeated-section mappings.
def test_build_record_from_full_detail(sample_summary, sample_detail) -> None:
    record = build_record(
        sample_summary,
        sample_detail,
        keep_html=True,
        scraped_at="2026-01-21T00:00:00+00:00",
    )

    assert list(record) == COLUMNS
    assert record["document_id"] == "ip_26_301"
    assert record["reference"] == "IP/26/301"
    assert record["doc_type"] == "IP"
    assert record["doc_type_name"] == "Press release"
    assert record["title"] == "Detailed title"
    assert record["subtitle"] == "Detailed subtitle"
    assert record["summary"] == "Summary lead"
    assert record["date"] == "2026-01-20"
    assert record["publish_datetime"] == "2026-01-20T11:42:00Z"
    assert record["place"] == "Brussels"
    assert record["commissioners"] == "Commissioner Alpha; Commissioner Beta"
    assert record["spokespersons"] == "Anna Example (Spokesperson); Ben Sample"
    assert record["policy_areas"] == "Trade; Economy, finance and the euro"
    assert record["policy_codes"] == "TRADE; ECON"
    assert record["full_text"] == "Heading\nFirst\nparagraph\n."
    assert record["html"].startswith("<h1>")
    assert record["detail_ok"] is True
    assert record["url"].endswith("/detail/en/ip_26_301")
    assert record["pdf_url"].endswith("/en/ip_26_301/IP_26_301_EN.pdf")


# Verify missing detail sections become nullable fields and an empty text body.
def test_build_record_with_missing_sections(sample_summary) -> None:
    detail = {
        "refCd": "IP/26/301",
        "eventDate": "2026-01-20",
        "docuLanguageResource": {"title": "Only a title"},
    }
    record = build_record(sample_summary, detail)

    assert pd.isna(record["subtitle"])
    assert pd.isna(record["commissioners"])
    assert pd.isna(record["spokespersons"])
    assert pd.isna(record["policy_areas"])
    assert pd.isna(record["place"])
    assert record["full_text"] == ""
    assert pd.isna(record["html"])
    assert record["detail_ok"] is True


# Verify a failed detail request still yields a complete summary-only schema row.
def test_build_partial_record_uses_summary_fallbacks(sample_summary) -> None:
    record = build_record(sample_summary, None, scraped_at="fixed")

    assert record["title"] == "Summary title"
    assert record["summary"] == "Summary lead"
    assert record["doc_type"] == "IP"
    assert record["full_text"] == ""
    assert record["detail_ok"] is False
    assert record["scraped_at"] == "fixed"


# Verify commissioners never leak into the spokesperson contact field.
def test_spokespersons_and_commissioners_are_separate(
    sample_summary, sample_detail
) -> None:
    record = build_record(sample_summary, sample_detail)

    assert "Commissioner" not in record["spokespersons"]
    assert "Anna Example" not in record["commissioners"]
