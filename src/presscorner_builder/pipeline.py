"""Resumable month-window and fixed-reference pipeline orchestration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd
from tqdm import tqdm

from presscorner_builder.api import (
    ACTIVE_TYPE_CODES,
    DetailFetchOutcome,
    PressCornerAPI,
    WindowFetchError,
)
from presscorner_builder.config import (
    Config,
    DescriptiveDataConfig,
    FixedDataConfig,
    config_hash,
)
from presscorner_builder.records import build_record, document_id
from presscorner_builder.store import DatasetScope, Reporter, Store
from presscorner_builder.windows import month_windows

LONG_FETCH_WINDOW_THRESHOLD = 12

KNOWN_DOCUMENT_TYPES = {
    "IP",
    "MEX",
    "SPEECH",
    "MEMO",
    "STATEMENT",
    "QANDA",
    "READ",
    "INF",
    "FS",
    "AC",
    "BIO",
    "PRES",
    "CJE",
    "PESC",
    "CES",
    "COR",
    "ECA",
    "EO",
    "BEI",
    "OLAF",
    "EDPS",
    "EPSO",
    "STAT",
    "P",
    "DOC",
    "AGENDA",
    "CLDR",
    "WM",
    "DN",
}


# Ignore library progress messages unless a caller supplies a reporter.
def _quiet_reporter(message: str) -> None:
    del message


@dataclass(frozen=True)
class SearchSpec:
    """Search filters shared by every month in one pipeline run."""

    language: str = "en"
    document_types: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    commissioners: tuple[str, ...] = ()
    policy_areas: tuple[str, ...] = ()

    # Expand repeated descriptive filters into independent queries for union semantics.
    def variants(self) -> list[dict[str, str]]:
        variants: list[dict[str, str]] = []
        variants.extend({"keyword": value} for value in self.keywords)
        variants.extend({"commissioner": value} for value in self.commissioners)
        variants.extend({"policy_area": value} for value in self.policy_areas)
        return variants or [{}]


@dataclass
class RunResult:
    """Outcome and pending-work counts from a mutating pipeline run."""

    new_documents: int = 0
    recovered_details: int = 0
    permanently_empty_details: int = 0
    completed_windows: int = 0
    failed_window_attempts: int = 0
    pending_windows: int = 0
    pending_refs: int = 0
    limit_reached: bool = False
    window_additions: dict[tuple[date, date], int] = field(default_factory=dict)

    # Report whether the run ended with work that must be retried.
    @property
    def partial(self) -> bool:
        return self.pending_windows > 0 or self.pending_refs > 0


@dataclass(frozen=True)
class DetailRetryResult:
    """Recovered and permanently empty outcomes from a retry-ledger pass."""

    recovered: int = 0
    permanently_empty: int = 0


@dataclass(frozen=True)
class DetailBatchResult:
    """Stored-record and permanent-empty counts from one detail batch."""

    stored: int = 0
    permanently_empty: int = 0


@dataclass(frozen=True)
class WindowCount:
    """API and local counts for a non-mutating dry-run window."""

    date_from: date
    date_to: date
    api_count: int
    local_count: int

    # Return the signed number of API rows not represented locally.
    @property
    def difference(self) -> int:
        return self.api_count - self.local_count


# Convert an inclusive date pair to the ledger's JSON-safe representation.
def _window_entry(window: tuple[date, date]) -> dict[str, str]:
    return {"date_from": window[0].isoformat(), "date_to": window[1].isoformat()}


# Convert a persisted ledger entry back to an inclusive date pair.
def _entry_window(entry: Mapping[str, str]) -> tuple[date, date]:
    return date.fromisoformat(entry["date_from"]), date.fromisoformat(entry["date_to"])


# Build a stable key for de-duplicating requested and pending windows.
def _window_key(window: tuple[date, date]) -> tuple[str, str]:
    return window[0].isoformat(), window[1].isoformat()


# Convert pandas' nullable scalars to ordinary None for API-shaped retry summaries.
def _plain_scalar(value: Any) -> Any:
    return None if pd.isna(value) else value


# Resolve a dataset scope into an explicit filter only for active datasets.
def _document_types_for_scope(scope: DatasetScope) -> tuple[str, ...]:
    return ACTIVE_TYPE_CODES if scope == "active" else ()


# Format a minute estimate compactly for an upfront long-run notice.
def _format_eta(minutes: int) -> str:
    if minutes < 1:
        return "under a minute"
    if minutes == 1:
        return "about 1 minute"
    if minutes < 60:
        return f"about {minutes:,} minutes"
    hours = math.ceil(minutes / 60)
    return f"about {hours:,} hour(s)"


# Report a conservative request-rate ETA before a long update starts.
def _report_fetch_eta(
    api: PressCornerAPI,
    window_count: int,
    *,
    force: bool,
    reporter: Reporter,
) -> None:
    if not force and window_count < LONG_FETCH_WINDOW_THRESHOLD:
        return
    seconds_per_request = max(float(getattr(api, "request_delay", 1.0)), 1.0)
    requests_per_second = 1 / seconds_per_request
    minutes = math.ceil(window_count * seconds_per_request / 60)
    reporter(
        f"Rough ETA: {window_count:,} window searches at "
        f"~{requests_per_second:g} request/s need {_format_eta(minutes)} at minimum; "
        "pagination and document-detail requests add time."
    )


# Union all configured search variants by reference while preserving API order.
def _search_window(
    api: PressCornerAPI,
    window: tuple[date, date],
    search: SearchSpec,
) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for variant in search.variants():
        results = api.search_window(
            window[0],
            window[1],
            language=search.language,
            document_types=list(search.document_types) or None,
            **variant,
        )
        for summary in results:
            reference = str(summary.get("refCode") or "").strip()
            if not reference:
                raise WindowFetchError(
                    f"Search returned a summary without refCode for {window[0]} to {window[1]}"
                )
            summaries.setdefault(document_id(reference), summary)
    return list(summaries.values())


# Reconstruct the summary fields needed to replace a partial row from its ledger entry.
def _summary_for_retry(
    reference: str, summary: Mapping[str, Any], store: Store
) -> dict[str, Any]:
    reconstructed = dict(summary)
    reconstructed.setdefault("refCode", reference)
    doc_id = document_id(reference)
    matches = store.frame.loc[store.frame["document_id"].eq(doc_id)]
    if matches.empty:
        return reconstructed

    row = matches.iloc[-1]
    reconstructed.setdefault("title", _plain_scalar(row["title"]))
    reconstructed.setdefault("eventDate", _plain_scalar(row["date"]))
    reconstructed.setdefault("languageCode", _plain_scalar(row["language"]))
    reconstructed.setdefault("leadText", _plain_scalar(row["summary"]))
    reconstructed.setdefault(
        "docutype",
        {
            "code": _plain_scalar(row["doc_type"]),
            "description": _plain_scalar(row["doc_type_name"]),
        },
    )
    return reconstructed


# Remove ledger entries whose successfully checkpointed rows are already detail-complete.
def _clear_resolved_reference_failures(store: Store) -> None:
    failed = store.load_failed_refs()
    if not failed:
        return
    complete = store.frame.loc[store.frame["detail_ok"].fillna(False), "document_id"]
    complete_ids = set(complete.dropna().astype(str))
    remaining = {
        reference: summary
        for reference, summary in failed.items()
        if document_id(reference) not in complete_ids
    }
    if len(remaining) != len(failed):
        store.save_failed_refs(remaining)


# Retry partial documents before searching any new windows.
def retry_failed_references(
    api: PressCornerAPI,
    store: Store,
    *,
    default_language: str = "en",
    keep_html: bool = False,
    progress: bool = True,
    reporter: Reporter | None = None,
) -> DetailRetryResult:
    """Replace recovered partial rows and retain only still-failing ledger entries."""
    report = reporter or _quiet_reporter
    failed = store.load_failed_refs()
    if not failed:
        return DetailRetryResult()

    report(f"Retrying {len(failed):,} pending detail request(s).")
    recovered: dict[str, dict[str, Any]] = {}
    permanently_empty: dict[str, dict[str, Any]] = {}
    stale: set[str] = set()
    iterable = tqdm(
        failed.items(), desc="Retrying details", unit="document", disable=not progress
    )

    for reference, raw_summary in iterable:
        doc_id = document_id(reference)
        matches = store.frame.loc[store.frame["document_id"].eq(doc_id)]
        if not matches.empty:
            detail_ok = matches.iloc[-1]["detail_ok"]
            if pd.notna(detail_ok) and bool(detail_ok):
                stale.add(reference)
                continue

        summary = _summary_for_retry(reference, raw_summary, store)
        language = str(summary.get("languageCode") or default_language)
        detail = api.get_document(reference, language=language)
        if detail is DetailFetchOutcome.PERMANENTLY_EMPTY:
            permanently_empty[reference] = build_record(
                summary,
                None,
                language=language,
                keep_html=keep_html,
            )
            continue
        if detail is None:
            continue
        recovered[reference] = build_record(
            summary,
            detail,
            language=language,
            keep_html=keep_html,
        )

    resolved_records = [*recovered.values(), *permanently_empty.values()]
    if resolved_records:
        store.add(resolved_records)
        store.save()
    for reference in recovered.keys() | permanently_empty.keys() | stale:
        failed.pop(reference, None)
    store.save_failed_refs(failed)
    if recovered:
        report(f"Recovered {len(recovered):,} document detail(s).")
    return DetailRetryResult(
        recovered=len(recovered),
        permanently_empty=len(permanently_empty),
    )


# Fetch details for selected summaries and persist failures before their partial rows.
def _process_summaries(
    api: PressCornerAPI,
    store: Store,
    summaries: Iterable[Mapping[str, Any]],
    *,
    language: str,
    keep_html: bool,
    progress: bool,
    description: str,
) -> DetailBatchResult:
    selected = list(summaries)
    failed_refs = store.load_failed_refs()
    records: list[dict[str, Any]] = []
    permanently_empty = 0
    iterable = tqdm(
        selected, desc=description, unit="document", leave=False, disable=not progress
    )

    for summary in iterable:
        reference = str(summary.get("refCode") or "").strip()
        record_language = str(summary.get("languageCode") or language)
        detail = api.get_document(reference, language=record_language)
        if detail is DetailFetchOutcome.PERMANENTLY_EMPTY:
            permanently_empty += 1
            if reference in failed_refs:
                failed_refs.pop(reference)
                store.save_failed_refs(failed_refs)
            detail_payload = None
        elif detail is None:
            failed_refs[reference] = dict(summary)
            store.save_failed_refs(failed_refs)
            detail_payload = None
        else:
            detail_payload = detail
        record = build_record(
            summary,
            detail_payload,
            language=record_language,
            keep_html=keep_html,
        )
        records.append(record)

    if records:
        store.add(records)
    return DetailBatchResult(
        stored=len(records),
        permanently_empty=permanently_empty,
    )


# Queue requested windows durably so interruption cannot turn an unfinished range into success.
def _queued_windows(
    store: Store,
    requested: Iterable[tuple[date, date]],
    *,
    retry_pending: bool,
) -> tuple[list[tuple[date, date]], dict[tuple[str, str], dict[str, str]]]:
    requested_windows = list(requested)
    pending_entries = store.load_failed_windows()
    pending_windows = [_entry_window(entry) for entry in pending_entries]
    processing_order = pending_windows if retry_pending else []
    processing_order.extend(requested_windows)

    unique_order: list[tuple[date, date]] = []
    seen: set[tuple[str, str]] = set()
    for window in processing_order:
        key = _window_key(window)
        if key not in seen:
            unique_order.append(window)
            seen.add(key)

    ledger = {_window_key(window): _window_entry(window) for window in pending_windows}
    for window in requested_windows:
        ledger[_window_key(window)] = _window_entry(window)
    store.save_failed_windows(ledger.values())
    return unique_order, ledger


# Fetch, checkpoint, and reconcile a sequence of calendar windows.
def run_windows(
    api: PressCornerAPI,
    store: Store,
    windows: Iterable[tuple[date, date]],
    *,
    search: SearchSpec | None = None,
    keep_html: bool = False,
    limit: int | None = None,
    retry_pending: bool = True,
    retry_details: bool = True,
    progress: bool = True,
    reporter: Reporter | None = None,
) -> RunResult:
    """Run a durable newest-first window queue and continue past failed searches."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    report = reporter or _quiet_reporter
    search_spec = search or SearchSpec()
    store.load()
    result = RunResult()

    if retry_details:
        retry_result = retry_failed_references(
            api,
            store,
            default_language=search_spec.language,
            keep_html=keep_html,
            progress=progress,
            reporter=report,
        )
        result.recovered_details = retry_result.recovered
        result.permanently_empty_details += retry_result.permanently_empty

    requested = list(windows)
    queue, ledger = _queued_windows(store, requested, retry_pending=retry_pending)
    iterable = tqdm(queue, desc="Windows", unit="window", disable=not progress)

    for window in iterable:
        if limit is not None and result.new_documents >= limit:
            result.limit_reached = True
            break

        report(f"Fetching window {window[0]} to {window[1]}.")
        try:
            summaries = _search_window(api, window, search_spec)
        except WindowFetchError as error:
            result.failed_window_attempts += 1
            report(f"WINDOW FAILED: {error}")
            continue

        existing_ids = store.existing_ids
        missing = [
            summary
            for summary in summaries
            if document_id(str(summary.get("refCode") or "")) not in existing_ids
        ]
        remaining = None if limit is None else limit - result.new_documents
        selected = missing if remaining is None else missing[:remaining]
        report(
            f"Window returned {len(summaries):,}; {len(missing):,} document(s) are new."
        )
        batch_result = _process_summaries(
            api,
            store,
            selected,
            language=search_spec.language,
            keep_html=keep_html,
            progress=progress,
            description=f"Documents {window[0]:%Y-%m}",
        )
        result.new_documents += batch_result.stored
        result.permanently_empty_details += batch_result.permanently_empty
        result.window_additions[window] = batch_result.stored
        store.save()
        _clear_resolved_reference_failures(store)

        if len(selected) == len(missing):
            ledger.pop(_window_key(window), None)
            store.save_failed_windows(ledger.values())
            result.completed_windows += 1
        else:
            result.limit_reached = True
            break

    result.pending_windows = len(store.load_failed_windows())
    result.pending_refs = len(store.load_failed_refs())
    return result


# Fetch an explicit list of references without using the search endpoint.
def run_fixed(
    api: PressCornerAPI,
    store: Store,
    references: Iterable[str],
    *,
    language: str = "en",
    keep_html: bool = False,
    limit: int | None = None,
    progress: bool = True,
    reporter: Reporter | None = None,
) -> RunResult:
    """Build or resume a fixed-reference corpus with per-document checkpoints."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    report = reporter or _quiet_reporter
    store.load()
    result = RunResult()
    retry_result = retry_failed_references(
        api,
        store,
        default_language=language,
        keep_html=keep_html,
        progress=progress,
        reporter=report,
    )
    result.recovered_details = retry_result.recovered
    result.permanently_empty_details += retry_result.permanently_empty
    missing = [
        reference
        for reference in references
        if document_id(reference) not in store.existing_ids
    ]
    iterable = tqdm(missing, desc="Documents", unit="document", disable=not progress)

    for reference in iterable:
        if limit is not None and result.new_documents >= limit:
            result.limit_reached = True
            break
        summary = {"refCode": reference, "languageCode": language}
        batch_result = _process_summaries(
            api,
            store,
            [summary],
            language=language,
            keep_html=keep_html,
            progress=False,
            description="Documents",
        )
        result.new_documents += batch_result.stored
        result.permanently_empty_details += batch_result.permanently_empty
        store.save()
        _clear_resolved_reference_failures(store)

    result.pending_windows = len(store.load_failed_windows())
    result.pending_refs = len(store.load_failed_refs())
    return result


# Compare API and local counts without requesting details or changing durable state.
def dry_run_windows(
    api: PressCornerAPI,
    store: Store,
    windows: Iterable[tuple[date, date]],
    *,
    language: str = "en",
    document_types: Iterable[str] | None = None,
    progress: bool = True,
) -> list[WindowCount]:
    """Return one API-versus-local count record for each requested window."""
    store.load()
    results: list[WindowCount] = []
    selected_types = list(document_types or ())
    iterable = tqdm(
        list(windows), desc="Counting windows", unit="window", disable=not progress
    )
    for date_from, date_to in iterable:
        count_kwargs: dict[str, Any] = {"language": language}
        if selected_types:
            count_kwargs["document_types"] = selected_types
        results.append(
            WindowCount(
                date_from=date_from,
                date_to=date_to,
                api_count=api.count_window(date_from, date_to, **count_kwargs),
                local_count=store.count_window(date_from, date_to),
            )
        )
    return results


# Calculate the default overlapping incremental date range and run its month queue.
def update_dataset(
    api: PressCornerAPI,
    store: Store,
    *,
    since_date: date | None = None,
    until_date: date | None = None,
    full: bool = False,
    scope: DatasetScope | None = None,
    limit: int | None = None,
    progress: bool = True,
    reporter: Reporter | None = None,
) -> RunResult:
    """Top up a full dataset, overlapping its cutoff by three days by default."""
    report = reporter or _quiet_reporter
    existed = store.exists
    store.load()
    if scope not in {None, "active", "all"}:
        raise ValueError("scope must be 'active' or 'all'")
    if scope is not None and not full:
        raise ValueError("scope is only valid together with full=True")
    if not existed and not full:
        raise FileNotFoundError(
            f"Dataset not found at {store.path}; run 'presscorner download' first or use update --full"
        )
    recorded_scope = store.scope
    if full:
        selected_scope: DatasetScope = scope or "active"
        if existed and selected_scope != recorded_scope:
            raise ValueError(
                f"Dataset scope is fixed at creation ({recorded_scope}); "
                f"a {selected_scope}-scope dataset needs a fresh directory"
            )
    else:
        selected_scope = recorded_scope

    end = until_date or date.today()
    if since_date is not None:
        start = since_date
    elif full:
        start = date(1975, 1, 1)
    elif store.cutoff:
        start = date.fromisoformat(store.cutoff) - timedelta(days=3)
    else:
        raise ValueError(
            "Existing dataset has no cutoff; specify --since or use --full"
        )

    requested_windows = month_windows(start, end)
    if not existed:
        store.write_metadata(scope=selected_scope)
    _report_fetch_eta(
        api,
        len(requested_windows),
        force=full,
        reporter=report,
    )
    result = run_windows(
        api,
        store,
        requested_windows,
        search=SearchSpec(
            language="en",
            document_types=_document_types_for_scope(selected_scope),
        ),
        limit=limit,
        progress=progress,
        reporter=report,
    )
    store.write_metadata(scope=selected_scope)
    return result


# Warn through the caller's reporter about codes outside the documented archive set.
def warn_unknown_document_types(
    codes: Iterable[str], reporter: Reporter | None = None
) -> None:
    report = reporter or _quiet_reporter
    unknown = sorted({code.upper() for code in codes} - KNOWN_DOCUMENT_TYPES)
    if unknown:
        report(
            "WARNING: Unrecognized document type code(s) accepted without validation: "
            + ", ".join(unknown)
        )


# Run either descriptive month windows or fixed references from validated configuration.
def build_corpus(
    config: Config,
    api: PressCornerAPI,
    store: Store,
    *,
    limit: int | None = None,
    progress: bool = True,
    reporter: Reporter | None = None,
) -> RunResult:
    """Build or resume a config-defined corpus and stamp its provenance sidecar."""
    report = reporter or _quiet_reporter
    metadata_scope: DatasetScope = "all"
    if isinstance(config.data, DescriptiveDataConfig):
        warn_unknown_document_types(config.data.document_types, report)
        end = config.data.end_date or date.today()
        if tuple(config.data.document_types) == ACTIVE_TYPE_CODES:
            metadata_scope = "active"
        search = SearchSpec(
            language=config.data.language,
            document_types=tuple(config.data.document_types),
            keywords=tuple(config.data.keywords),
            commissioners=tuple(config.data.commissioners),
            policy_areas=tuple(config.data.policy_areas),
        )
        result = run_windows(
            api,
            store,
            month_windows(config.data.start_date, end),
            search=search,
            keep_html=config.processing.keep_html,
            limit=limit,
            progress=progress,
            reporter=report,
        )
    elif isinstance(config.data, FixedDataConfig):
        result = run_fixed(
            api,
            store,
            config.data.references,
            language=config.data.language,
            keep_html=config.processing.keep_html,
            limit=limit,
            progress=progress,
            reporter=report,
        )
    else:
        raise TypeError(f"Unsupported data mode: {type(config.data).__name__}")

    store.write_metadata(
        config_digest=config_hash(config),
        project_metadata=config.metadata.model_dump(mode="json"),
        scope=metadata_scope,
    )
    return result


# Resolve a config's output settings to its corpus parquet path.
def output_path(config: Config) -> Path:
    """Return ``output_directory/dataset_name.parquet`` as configured."""
    return config.output.output_directory / f"{config.output.dataset_name}.parquet"
