"""Mocked HTTP tests for retry, pagination, and failure semantics."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
import requests

from presscorner_builder.api import (
    DetailFetchOutcome,
    PressCornerAPI,
    WindowFetchError,
)


class FakeSession:
    """Serve queued response objects while recording every requested parameter mapping."""

    # Initialize a requests-like session with queued responses or exceptions.
    def __init__(self, queued: list[requests.Response | Exception]) -> None:
        self.queued = list(queued)
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # Return or raise the next queued item exactly as requests.Session.get would.
    def get(
        self, url: str, *, params: dict[str, Any], timeout: float
    ) -> requests.Response:
        del timeout
        self.calls.append((url, dict(params)))
        item = self.queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# Build a minimal requests.Response with optional JSON or raw text content.
def _response(
    status: int = 200,
    *,
    json_payload: Any | None = None,
    body: str = "",
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://example.test/api"
    response.encoding = "utf-8"
    if json_payload is not None:
        response._content = json.dumps(json_payload).encode("utf-8")
        response.headers["content-type"] = "application/json"
    else:
        response._content = body.encode("utf-8")
        response.headers["content-type"] = "text/plain"
    return response


# Verify a transient server failure is retried and the next valid page succeeds.
def test_search_retry_then_succeed(monkeypatch) -> None:
    session = FakeSession(
        [
            _response(503),
            _response(json_payload={"totalNumber": 0, "docuLanguageListResources": []}),
        ]
    )
    monkeypatch.setattr("presscorner_builder.api.requests.Session", lambda: session)
    sleeps: list[float] = []
    api = PressCornerAPI(request_delay=0, sleep=sleeps.append)

    payload = api.search_page(date(2020, 1, 1), date(2020, 1, 31))

    assert payload["totalNumber"] == 0
    assert len(session.calls) == 2
    assert sleeps == [2.0]


# Verify all four exhausted search attempts become WindowFetchError.
def test_search_retry_exhausted_raises_window_fetch_error(monkeypatch) -> None:
    session = FakeSession([_response(500) for _ in range(4)])
    monkeypatch.setattr("presscorner_builder.api.requests.Session", lambda: session)
    sleeps: list[float] = []
    api = PressCornerAPI(request_delay=0, sleep=sleeps.append)

    with pytest.raises(WindowFetchError, match="after 4 attempts"):
        api.search_page(date(2020, 1, 1), date(2020, 1, 31))

    assert len(session.calls) == 4
    assert sleeps == [2.0, 4.0, 8.0]


# Verify a failed later page is never mistaken for successful end-of-results.
def test_failed_pagination_page_does_not_return_truncated_results(monkeypatch) -> None:
    session = FakeSession(
        [
            _response(
                json_payload={
                    "totalNumber": 2,
                    "docuLanguageListResources": [{"refCode": "IP/20/1"}],
                }
            ),
            *[_response(502) for _ in range(4)],
        ]
    )
    monkeypatch.setattr("presscorner_builder.api.requests.Session", lambda: session)
    api = PressCornerAPI(request_delay=0, sleep=lambda _: None)

    with pytest.raises(WindowFetchError, match="page 2"):
        api.search_window(date(2020, 1, 1), date(2020, 1, 31))


# Verify non-JSON detail bodies are retried and then represented as detail failure.
def test_non_json_detail_returns_none_after_retries(monkeypatch) -> None:
    session = FakeSession([_response(body="not JSON") for _ in range(4)])
    monkeypatch.setattr("presscorner_builder.api.requests.Session", lambda: session)
    api = PressCornerAPI(request_delay=0, sleep=lambda _: None)

    assert api.get_document("IP/20/1") is None
    assert len(session.calls) == 4


# Verify a whitespace-only successful detail response is permanent and is not retried.
def test_empty_detail_returns_permanent_outcome_without_retry(monkeypatch) -> None:
    session = FakeSession([_response(body=" \n\t")])
    monkeypatch.setattr("presscorner_builder.api.requests.Session", lambda: session)
    api = PressCornerAPI(request_delay=0, sleep=lambda _: None)

    outcome = api.get_document("BIO/85/1")

    assert outcome is DetailFetchOutcome.PERMANENTLY_EMPTY
    assert len(session.calls) == 1


# Verify pagination follows a changing total until the current total is collected.
def test_pagination_accepts_changing_total_number(monkeypatch) -> None:
    queued = [
        _response(
            json_payload={
                "totalNumber": total,
                "docuLanguageListResources": [{"refCode": reference}],
            }
        )
        for total, reference in [(2, "IP/20/3"), (3, "IP/20/2"), (3, "IP/20/1")]
    ]
    session = FakeSession(queued)
    monkeypatch.setattr("presscorner_builder.api.requests.Session", lambda: session)
    api = PressCornerAPI(request_delay=0)

    documents = api.search_window(date(2020, 1, 1), date(2020, 1, 31))

    assert [item["refCode"] for item in documents] == ["IP/20/3", "IP/20/2", "IP/20/1"]


# Verify every request has cache busting and API dates use ddmmyyyy.
def test_search_request_formats_dates_and_adds_timestamp(monkeypatch) -> None:
    session = FakeSession(
        [_response(json_payload={"totalNumber": 0, "docuLanguageListResources": []})]
    )
    monkeypatch.setattr("presscorner_builder.api.requests.Session", lambda: session)
    api = PressCornerAPI(request_delay=0, wall_clock=lambda: 1234.567)

    api.search_page(
        date(2020, 2, 3),
        date(2020, 2, 29),
        document_types=["IP", "SPEECH"],
        keyword="trade",
    )

    query = session.calls[0][1]
    assert query["datefrom"] == "03022020"
    assert query["dateto"] == "29022020"
    assert query["documentTypeCodes"] == "IP,SPEECH"
    assert query["global"] == "trade"
    assert query["ts"] == 1234567


# Verify an unfiltered archive search still omits documentTypeCodes entirely.
def test_unfiltered_search_omits_document_type_parameter(monkeypatch) -> None:
    session = FakeSession(
        [_response(json_payload={"totalNumber": 0, "docuLanguageListResources": []})]
    )
    monkeypatch.setattr("presscorner_builder.api.requests.Session", lambda: session)
    api = PressCornerAPI(request_delay=0)

    api.search_page(date(2020, 1, 1), date(2020, 1, 31))

    assert "documentTypeCodes" not in session.calls[0][1]
