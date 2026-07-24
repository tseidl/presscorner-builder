"""Polite, retrying HTTP client for the European Commission Press Corner API."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

import requests

from presscorner_builder import __version__

BASE_URL = "https://ec.europa.eu/commission/presscorner"
DEFAULT_TIMEOUT = 30.0
MAX_ATTEMPTS = 4
ACTIVE_TYPE_CODES: tuple[str, ...] = (
    "IP",
    "SPEECH",
    "STATEMENT",
    "MEX",
    "READ",
    "AC",
    "QANDA",
    "FS",
    "INF",
)


class WindowFetchError(RuntimeError):
    """Raised when a search request cannot safely produce a complete window."""


class _TransientRequestError(RuntimeError):
    """Internal marker for retryable HTTP responses."""


class DetailFetchOutcome(Enum):
    """Non-payload outcomes that callers must distinguish from transient failure."""

    PERMANENTLY_EMPTY = "permanently_empty"


class PressCornerAPI:
    """Sequential Press Corner API client with rate limiting and bounded retries."""

    # Configure a reusable requests session and injectable timing hooks.
    def __init__(
        self,
        *,
        request_delay: float = 1.0,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = MAX_ATTEMPTS,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if request_delay < 0:
            raise ValueError("request_delay must be non-negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self.request_delay = request_delay
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    f"presscorner-builder/{__version__} "
                    "(https://github.com/tseidl/presscorner-builder; academic research)"
                )
            }
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._last_request_at: float | None = None

    # Wait only as long as needed to preserve the configured inter-request delay.
    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.request_delay - (self._monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    # Request and decode JSON with retries for transient and malformed responses.
    def _request_json(
        self,
        endpoint: str,
        params: Mapping[str, Any],
        *,
        allow_empty_body: bool = False,
    ) -> Any:
        url = f"{BASE_URL}{endpoint}"
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            request_params = dict(params)
            request_params["ts"] = int(self._wall_clock() * 1000)
            try:
                self._wait_for_rate_limit()
                self._last_request_at = self._monotonic()
                response = self.session.get(
                    url, params=request_params, timeout=self.timeout
                )
                if response.status_code >= 500:
                    raise _TransientRequestError(f"HTTP {response.status_code}")
                response.raise_for_status()
                if allow_empty_body and not response.content.strip():
                    return DetailFetchOutcome.PERMANENTLY_EMPTY
                return response.json()
            except (
                _TransientRequestError,
                requests.ConnectionError,
                requests.Timeout,
                ValueError,
            ) as error:
                last_error = error
                if attempt < self.max_attempts - 1:
                    self._sleep(float(2 ** (attempt + 1)))
            except requests.RequestException as error:
                raise RuntimeError(f"Press Corner request failed: {error}") from error

        raise RuntimeError(
            f"Press Corner request failed after {self.max_attempts} attempts: {last_error}"
        ) from last_error

    # Format API dates as the verified ddmmyyyy query representation.
    @staticmethod
    def _format_date(value: date | str) -> str:
        if isinstance(value, date):
            return value.strftime("%d%m%Y")
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d%m%Y")
        except ValueError:
            parsed = datetime.strptime(value, "%d%m%Y")
            return parsed.strftime("%d%m%Y")

    # Fetch one validated search result page or raise the window-level exception.
    def search_page(
        self,
        date_from: date | str,
        date_to: date | str,
        *,
        language: str = "en",
        page_size: int = 100,
        page_number: int = 1,
        document_types: list[str] | None = None,
        keyword: str | None = None,
        commissioner: str | None = None,
        policy_area: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one search page, preserving failure as ``WindowFetchError``."""
        params: dict[str, Any] = {
            "language": language,
            "pagesize": page_size,
            "pagenumber": page_number,
            "datefrom": self._format_date(date_from),
            "dateto": self._format_date(date_to),
        }
        if document_types:
            params["documentTypeCodes"] = ",".join(document_types)
        if keyword:
            params["global"] = keyword
        if commissioner:
            params["commissioner"] = commissioner
        if policy_area:
            params["policyarea"] = policy_area

        try:
            payload = self._request_json("/api/search", params)
        except RuntimeError as error:
            raise WindowFetchError(
                f"Search page {page_number} failed for {date_from} to {date_to}: {error}"
            ) from error

        if not isinstance(payload, dict) or not isinstance(
            payload.get("docuLanguageListResources"), list
        ):
            raise WindowFetchError(
                f"Search page {page_number} returned an invalid payload for "
                f"{date_from} to {date_to}"
            )
        return payload

    # Fetch every search page in one bounded window without masking page failures.
    def search_window(
        self,
        date_from: date | str,
        date_to: date | str,
        *,
        language: str = "en",
        document_types: list[str] | None = None,
        keyword: str | None = None,
        commissioner: str | None = None,
        policy_area: str | None = None,
    ) -> list[dict[str, Any]]:
        """Paginate until the reported total is collected or an empty page is seen."""
        documents: list[dict[str, Any]] = []
        page_number = 1

        while True:
            payload = self.search_page(
                date_from,
                date_to,
                language=language,
                page_size=100,
                page_number=page_number,
                document_types=document_types,
                keyword=keyword,
                commissioner=commissioner,
                policy_area=policy_area,
            )
            page = payload["docuLanguageListResources"]
            if not page:
                break
            documents.extend(item for item in page if isinstance(item, dict))
            try:
                total = int(payload.get("totalNumber", 0))
            except (TypeError, ValueError) as error:
                raise WindowFetchError(
                    f"Search page {page_number} returned an invalid totalNumber"
                ) from error
            if len(documents) >= total:
                break
            page_number += 1

        return documents

    # Fetch only the API's reported total for an audit window.
    def count_window(
        self,
        date_from: date | str,
        date_to: date | str,
        *,
        language: str = "en",
        document_types: list[str] | None = None,
        keyword: str | None = None,
        commissioner: str | None = None,
        policy_area: str | None = None,
    ) -> int:
        """Read ``totalNumber`` with a one-result search request."""
        payload = self.search_page(
            date_from,
            date_to,
            language=language,
            page_size=1,
            page_number=1,
            document_types=document_types,
            keyword=keyword,
            commissioner=commissioner,
            policy_area=policy_area,
        )
        try:
            return int(payload.get("totalNumber", 0))
        except (TypeError, ValueError) as error:
            raise WindowFetchError(
                "Search count returned an invalid totalNumber"
            ) from error

    # Fetch details while separating permanent empty successes from transient failures.
    def get_document(
        self, reference: str, *, language: str = "en"
    ) -> dict[str, Any] | DetailFetchOutcome | None:
        """Return details, a permanent-empty outcome, or ``None`` after failure."""
        try:
            payload = self._request_json(
                "/api/documents",
                {"reference": reference, "language": language},
                allow_empty_body=True,
            )
        except RuntimeError:
            return None
        if payload is DetailFetchOutcome.PERMANENTLY_EMPTY:
            return payload
        return payload if isinstance(payload, dict) and payload else None

    # Fetch the active document-type list for diagnostics without using it as validation.
    def get_document_types(self, *, language: str = "en") -> list[dict[str, Any]]:
        """Return the currently advertised document types, or an empty list on failure."""
        try:
            payload = self._request_json("/api/docutypes", {"language": language})
        except RuntimeError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]
