"""Transform Press Corner API responses into the package's flat record schema."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import pandas as pd
from bs4 import BeautifulSoup

BASE_URL = "https://ec.europa.eu/commission/presscorner"

SCHEMA: dict[str, str] = {
    "document_id": "string",
    "reference": "string",
    "doc_type": "string",
    "doc_type_name": "string",
    "title": "string",
    "subtitle": "string",
    "summary": "string",
    "date": "string",
    "publish_datetime": "string",
    "place": "string",
    "language": "string",
    "original_language": "string",
    "commissioners": "string",
    "spokespersons": "string",
    "policy_areas": "string",
    "policy_codes": "string",
    "full_text": "string",
    "html": "string",
    "url": "string",
    "pdf_url": "string",
    "detail_ok": "boolean",
    "scraped_at": "string",
}

COLUMNS = list(SCHEMA)


# Convert a Press Corner reference code to its stable dataset identifier.
def document_id(reference: str) -> str:
    """Normalize a reference such as ``IP/26/301`` to ``ip_26_301``."""
    return reference.strip().lower().replace("/", "_")


# Return a nested mapping value or an empty mapping when the source is absent.
def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# Return a list-like API section or an empty list when it is absent.
def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return []


# Convert empty or missing scalar values to pandas' nullable sentinel.
def _nullable(value: Any) -> Any:
    if value is None or value == "":
        return pd.NA
    return value


# Join non-empty values with the schema-wide semicolon separator.
def _joined(values: Sequence[Any]) -> Any:
    cleaned = [
        str(value).strip()
        for value in values
        if value is not None and str(value).strip()
    ]
    return "; ".join(cleaned) if cleaned else pd.NA


# Format Press Corner contacts as spokesperson names with optional titles.
def _spokespersons(contacts: Sequence[Any]) -> Any:
    formatted: list[str] = []
    for raw_contact in contacts:
        contact = _mapping(raw_contact)
        name = " ".join(
            part.strip()
            for part in (
                str(contact.get("firstName") or ""),
                str(contact.get("lastName") or ""),
            )
            if part.strip()
        )
        title = str(contact.get("title") or "").strip()
        if name:
            formatted.append(f"{name} ({title})" if title else name)
        elif title:
            formatted.append(title)
    return _joined(formatted)


# Convert an HTML document body to clean newline-separated text.
def html_to_text(html: str | None) -> str:
    """Extract readable plain text while retaining block separation as newlines."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


# Build a schema-complete record from summary and optional detail API data.
def build_record(
    summary: Mapping[str, Any],
    detail: Mapping[str, Any] | None,
    *,
    language: str = "en",
    keep_html: bool = False,
    scraped_at: str | None = None,
) -> dict[str, Any]:
    """Flatten one document, preserving a partial summary record when detail failed."""
    detail_data = _mapping(detail)
    detail_language = _mapping(detail_data.get("docuLanguageResource"))
    summary_type = _mapping(summary.get("docutype"))
    detail_type = _mapping(detail_data.get("docutypeResource"))

    reference = str(detail_data.get("refCd") or summary.get("refCode") or "").strip()
    doc_id = document_id(reference)
    record_language = str(summary.get("languageCode") or language).strip() or language
    html_content = detail_language.get("htmlContent") if detail is not None else None
    html_string = str(html_content) if html_content is not None else ""

    commissioners = [
        _mapping(item).get("shortDescription")
        for item in _sequence(detail_data.get("commissionerResource"))
    ]
    policies = [
        _mapping(item) for item in _sequence(detail_data.get("policiesResource"))
    ]
    place = _mapping(detail_data.get("placeResource")).get("description")
    reference_stem = reference.replace("/", "_").upper()

    return {
        "document_id": doc_id,
        "reference": reference,
        "doc_type": str(detail_type.get("code") or summary_type.get("code") or ""),
        "doc_type_name": str(
            detail_type.get("description") or summary_type.get("description") or ""
        ),
        "title": str(detail_language.get("title") or summary.get("title") or ""),
        "subtitle": _nullable(detail_language.get("subtitle")),
        "summary": _nullable(summary.get("leadText")),
        "date": str(detail_data.get("eventDate") or summary.get("eventDate") or ""),
        "publish_datetime": _nullable(detail_data.get("publishDate")),
        "place": _nullable(place),
        "language": record_language,
        "original_language": _nullable(detail_data.get("originalLanguage")),
        "commissioners": _joined(commissioners),
        "spokespersons": _spokespersons(_sequence(detail_data.get("contactsResource"))),
        "policy_areas": _joined([policy.get("description") for policy in policies]),
        "policy_codes": _joined([policy.get("code") for policy in policies]),
        "full_text": html_to_text(html_string),
        "html": _nullable(html_string) if keep_html else pd.NA,
        "url": f"{BASE_URL}/detail/{record_language}/{doc_id}",
        "pdf_url": (
            f"{BASE_URL}/api/files/document/print/{record_language}/{doc_id}/"
            f"{reference_stem}_{record_language.upper()}.pdf"
        ),
        "detail_ok": detail is not None,
        "scraped_at": scraped_at or datetime.now(timezone.utc).isoformat(),
    }
