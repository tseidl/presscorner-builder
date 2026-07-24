"""Reconcile per-window API totals against rows stored in the local parquet dataset."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Literal

import pandas as pd
from tqdm import tqdm

from presscorner_builder.api import ACTIVE_TYPE_CODES, PressCornerAPI, WindowFetchError
from presscorner_builder.pipeline import RunResult, SearchSpec, run_windows
from presscorner_builder.records import document_id
from presscorner_builder.store import Reporter, Store
from presscorner_builder.windows import month_windows, year_windows


@dataclass(frozen=True)
class AuditResult:
    """One API/local count comparison for an inclusive date window."""

    date_from: date
    date_to: date
    api_count: int
    local_count: int
    reconciled: bool = False

    # Classify the window, including server-count inflation reconciled by enumeration.
    @property
    def status(self) -> Literal["match", "deficient", "surplus", "reconciled"]:
        if self.reconciled:
            return "reconciled"
        if self.api_count > self.local_count:
            return "deficient"
        if self.local_count > self.api_count:
            return "surplus"
        return "match"

    # Return the signed number of API records missing from local state.
    @property
    def difference(self) -> int:
        return self.api_count - self.local_count


@dataclass(frozen=True)
class RepairResult:
    """Before/after reconciliation for one deficient window repair."""

    before: AuditResult
    after: AuditResult

    # Return the number of phantom index entries for a reconciled repair.
    @property
    def phantom_count(self) -> int:
        return max(self.after.difference, 0) if self.after.reconciled else 0


# Ignore audit messages unless the caller supplies a reporter.
def _quiet_reporter(message: str) -> None:
    del message


# Build API search arguments while omitting the type parameter for archive scope.
def _search_kwargs(
    language: str, document_types: Iterable[str] | None
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"language": language}
    selected_types = list(document_types or ())
    if selected_types:
        kwargs["document_types"] = selected_types
    return kwargs


# Describe the expected duration of the rate-limited audit count loop.
def _report_audit_duration(
    api: PressCornerAPI,
    window_count: int,
    reporter: Reporter,
) -> None:
    seconds_per_request = max(float(getattr(api, "request_delay", 1.0)), 1.0)
    requests_per_second = 1 / seconds_per_request
    minutes = max(1, math.ceil(window_count * seconds_per_request / 60))
    unit = "minute" if minutes == 1 else "minutes"
    reporter(
        f"Auditing {window_count:,} windows at ~{requests_per_second:g} request/s "
        f"— this takes about {minutes:,} {unit}."
    )


# Count rows in an inclusive window using the ISO-formatted date column.
def local_count(frame: pd.DataFrame, date_from: date, date_to: date) -> int:
    """Count local rows while ignoring missing or invalidly absent date values."""
    if "date" not in frame.columns:
        raise ValueError("Dataset does not contain the required 'date' column")
    values = frame["date"].astype("string")
    mask = values.ge(date_from.isoformat()) & values.le(date_to.isoformat())
    return int(mask.fillna(False).sum())


# Reconcile every requested window against a fresh one-result API count.
def reconcile_windows(
    api: PressCornerAPI,
    frame: pd.DataFrame,
    windows: Iterable[tuple[date, date]],
    *,
    language: str = "en",
    document_types: Iterable[str] | None = None,
    progress: bool = False,
    reporter: Reporter | None = None,
) -> list[AuditResult]:
    """Return all comparisons, including matches, in the supplied window order."""
    report = reporter or _quiet_reporter
    requested = list(windows)
    _report_audit_duration(api, len(requested), report)
    kwargs = _search_kwargs(language, document_types)
    results: list[AuditResult] = []
    iterable = tqdm(
        requested,
        desc="Auditing windows",
        unit="window",
        disable=not progress,
    )
    for date_from, date_to in iterable:
        results.append(
            AuditResult(
                date_from=date_from,
                date_to=date_to,
                api_count=api.count_window(date_from, date_to, **kwargs),
                local_count=local_count(frame, date_from, date_to),
            )
        )
    return results


# Return only count comparisons that need user attention.
def mismatches(results: Iterable[AuditResult]) -> list[AuditResult]:
    """Filter out exact API/local matches while retaining deficits and surpluses."""
    return [
        result
        for result in results
        if result.status in {"deficient", "surplus"}
    ]


# Generate requested audit windows and reconcile them against a loaded store.
def audit_store(
    api: PressCornerAPI,
    store: Store,
    since_date: date,
    until_date: date,
    *,
    granularity: Literal["month", "year"] = "month",
    language: str = "en",
    progress: bool = True,
    reporter: Reporter | None = None,
) -> list[AuditResult]:
    """Audit a store at month or year granularity without changing local rows."""
    if granularity not in {"month", "year"}:
        raise ValueError("granularity must be 'month' or 'year'")
    store.load()
    windows = (
        month_windows(since_date, until_date)
        if granularity == "month"
        else year_windows(since_date, until_date)
    )
    document_types = ACTIVE_TYPE_CODES if store.scope == "active" else ()
    return reconcile_windows(
        api,
        store.frame,
        windows,
        language=language,
        document_types=document_types,
        progress=progress,
        reporter=reporter,
    )


# Re-fetch deficient windows and then recompute their API/local counts.
def repair_deficiencies(
    api: PressCornerAPI,
    store: Store,
    results: Iterable[AuditResult],
    *,
    language: str = "en",
    progress: bool = True,
    reporter: Reporter | None = None,
) -> tuple[list[RepairResult], RunResult]:
    """Repair only API-deficient windows; local surplus rows are never deleted."""
    deficient = [result for result in results if result.status == "deficient"]
    windows = [(result.date_from, result.date_to) for result in deficient]
    document_types = ACTIVE_TYPE_CODES if store.scope == "active" else ()
    search_kwargs = _search_kwargs(language, document_types)
    run_result = run_windows(
        api,
        store,
        windows,
        search=SearchSpec(
            language=language,
            document_types=tuple(document_types),
        ),
        retry_pending=False,
        retry_details=False,
        progress=progress,
        reporter=reporter,
    )
    repaired: list[RepairResult] = []
    for before in deficient:
        after = AuditResult(
            date_from=before.date_from,
            date_to=before.date_to,
            api_count=api.count_window(
                before.date_from, before.date_to, **search_kwargs
            ),
            local_count=store.count_window(before.date_from, before.date_to),
        )
        window = (before.date_from, before.date_to)
        if (
            after.status == "deficient"
            and window in run_result.window_additions
            and run_result.window_additions[window] == 0
        ):
            try:
                summaries = api.search_window(
                    before.date_from,
                    before.date_to,
                    **search_kwargs,
                )
            except WindowFetchError:
                summaries = []
                enumeration_ok = False
            else:
                existing_ids = store.existing_ids
                returned_ids: set[str] = set()
                enumeration_ok = True
                for summary in summaries:
                    reference = str(summary.get("refCode") or "").strip()
                    if not reference:
                        enumeration_ok = False
                        break
                    returned_ids.add(document_id(reference))
                enumeration_ok = enumeration_ok and returned_ids <= existing_ids
            if enumeration_ok:
                after = AuditResult(
                    date_from=after.date_from,
                    date_to=after.date_to,
                    api_count=after.api_count,
                    local_count=after.local_count,
                    reconciled=True,
                )
        repaired.append(RepairResult(before=before, after=after))
    store.write_metadata()
    return repaired, run_result
