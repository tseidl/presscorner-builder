"""Argparse command-line interface for presscorner-builder."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from presscorner_builder import __version__
from presscorner_builder.api import ACTIVE_TYPE_CODES, PressCornerAPI
from presscorner_builder.audit import (
    AuditResult,
    audit_store,
    mismatches,
    repair_deficiencies,
)
from presscorner_builder.config import load_config
from presscorner_builder.download import download_dataset
from presscorner_builder.pipeline import (
    RunResult,
    build_corpus,
    dry_run_windows,
    output_path,
    update_dataset,
)
from presscorner_builder.store import DatasetScope, Store
from presscorner_builder.windows import month_windows

EXAMPLE_CONFIG = """metadata:
  project_name: ""
  author: ""
  description: ""

data:
  mode: descriptive
  document_types: []
  start_date: 1975-01-01
  end_date: null
  keywords: []
  commissioners: []
  policy_areas: []
  language: en

processing:
  keep_html: false
  request_delay: 1.0

output:
  output_directory: ./output
  dataset_name: press-corner
"""


# Parse one ISO calendar date for an argparse option.
def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD format") from error


# Parse a strictly positive integer for canary limits.
def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


# Parse a non-negative request delay.
def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


# Construct the full command parser and all verb-specific options.
def build_parser() -> argparse.ArgumentParser:
    """Return the CLI parser for reuse in tests and the console entry point."""
    parser = argparse.ArgumentParser(
        prog="presscorner",
        description="Build and maintain research-ready EC Press Corner datasets.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")

    download_parser = subparsers.add_parser(
        "download", help="Download the published full dataset."
    )
    download_parser.add_argument("--data-dir", type=Path, default=Path("./data"))
    download_parser.add_argument(
        "--url", help="Direct parquet URL, bypassing the manifest."
    )
    download_parser.add_argument(
        "--sha256", help="Expected SHA-256 checksum when using --url."
    )
    download_parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing dataset."
    )

    update_parser = subparsers.add_parser(
        "update", help="Incrementally update the full local dataset."
    )
    update_parser.add_argument("--data-dir", type=Path, default=Path("./data"))
    update_parser.add_argument("--delay", type=_nonnegative_float, default=1.0)
    update_parser.add_argument("--since", type=_date_argument)
    update_parser.add_argument("--until", type=_date_argument)
    update_parser.add_argument("--limit", type=_positive_int)
    update_parser.add_argument("--dry-run", action="store_true")
    update_parser.add_argument(
        "--full", action="store_true", help="Scrape from 1975 when needed."
    )
    update_parser.add_argument(
        "--scope",
        choices=["active", "all"],
        help="Dataset scope for --full (default: active).",
    )
    update_parser.add_argument(
        "--yes", action="store_true", help="Skip the full-scrape confirmation."
    )

    build_command = subparsers.add_parser("build", help="Build a YAML-defined corpus.")
    build_command.add_argument("config", type=Path)
    build_command.add_argument(
        "--fresh", action="store_true", help="Delete and rebuild this corpus."
    )
    build_command.add_argument("--limit", type=_positive_int)
    build_command.add_argument("--delay", type=_nonnegative_float)

    audit_parser = subparsers.add_parser(
        "audit", help="Reconcile API and local window counts."
    )
    audit_parser.add_argument("--data-dir", type=Path, default=Path("./data"))
    audit_parser.add_argument("--delay", type=_nonnegative_float, default=1.0)
    audit_parser.add_argument("--fix", action="store_true")
    audit_parser.add_argument("--since", type=_date_argument)
    audit_parser.add_argument("--until", type=_date_argument)
    audit_parser.add_argument(
        "--granularity", choices=["month", "year"], default="month"
    )

    status_parser = subparsers.add_parser(
        "status", help="Summarize the local full dataset."
    )
    status_parser.add_argument("--data-dir", type=Path, default=Path("./data"))

    init_parser = subparsers.add_parser(
        "init", help="Write an example YAML configuration."
    )
    init_parser.add_argument(
        "path", type=Path, nargs="?", default=Path("./config.yaml")
    )

    export_parser = subparsers.add_parser("export", help="Export type subsets or CSV.")
    export_parser.add_argument("--data-dir", type=Path, default=Path("./data"))
    export_parser.add_argument("--by-type", action="store_true")
    export_parser.add_argument("--csv", action="store_true")
    export_parser.add_argument(
        "--scope",
        choices=["active"],
        help="Write a parquet subset containing active document types.",
    )
    return parser


# Print a standard summary and return the correct success or partial exit code.
def _report_run(result: RunResult) -> int:
    print(f"New documents: {result.new_documents:,}")
    print(f"Recovered details: {result.recovered_details:,}")
    empty_count = result.permanently_empty_details
    if empty_count:
        empty_noun = "document has" if empty_count == 1 else "documents have"
        print(f"{empty_count:,} {empty_noun} no detail record server-side.")
    print(f"Completed windows: {result.completed_windows:,}")
    if result.limit_reached:
        print("Stopped at --limit; remaining windows are queued for the next run.")
    if result.partial:
        print(
            "WARNING: RUN PARTIAL — "
            f"{result.pending_windows:,} window(s) and {result.pending_refs:,} "
            "detail request(s) remain pending. Re-run the same command."
        )
        return 2
    return 0


# Download a verified release and let the library reporter print its release summary.
def _command_download(args: argparse.Namespace) -> int:
    download_dataset(
        args.data_dir,
        url=args.url,
        expected_sha256=args.sha256,
        force=args.force,
        reporter=print,
    )
    return 0


# Resolve the same explicit, full, or cutoff-overlap range used by update_dataset.
def _update_range(store: Store, args: argparse.Namespace) -> tuple[date, date]:
    end = args.until or date.today()
    if args.since is not None:
        start = args.since
    elif args.full:
        start = date(1975, 1, 1)
    elif store.cutoff:
        start = date.fromisoformat(store.cutoff) - timedelta(days=3)
    else:
        raise ValueError("Dataset has no cutoff; specify --since or use --full")
    if start > end:
        raise ValueError(f"since date {start} is after until date {end}")
    return start, end


# Resolve and validate the immutable scope selected for an update invocation.
def _update_scope(store: Store, args: argparse.Namespace) -> DatasetScope:
    if args.scope is not None and not args.full:
        raise ValueError("--scope is only valid together with --full")
    if not args.full:
        return store.scope
    selected: DatasetScope = args.scope or "active"
    if store.exists and store.scope != selected:
        raise ValueError(
            f"Dataset scope is fixed at creation ({store.scope}); "
            f"a {selected}-scope dataset needs a fresh directory"
        )
    return selected


# Confirm a costly full scrape unless the caller supplied --yes.
def _confirm_full_scrape(args: argparse.Namespace, scope: DatasetScope) -> bool:
    if not args.full or args.yes:
        return True
    answer = input(f"Scrape Press Corner from 1975 with {scope} scope? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


# Run an incremental/full update or its count-only dry-run variant.
def _command_update(args: argparse.Namespace) -> int:
    store = Store(args.data_dir, reporter=print)
    selected_scope = _update_scope(store, args)
    if not store.exists and not args.full:
        raise FileNotFoundError(
            f"Dataset not found at {store.path}. Run 'presscorner download' first, "
            "or use 'presscorner update --full'."
        )
    if not _confirm_full_scrape(args, selected_scope):
        print("Cancelled.")
        return 0

    api = PressCornerAPI(request_delay=args.delay)
    if args.dry_run:
        store.load()
        start, end = _update_range(store, args)
        document_types = (
            ACTIVE_TYPE_CODES if selected_scope == "active" else ()
        )
        counts = dry_run_windows(
            api,
            store,
            month_windows(start, end),
            document_types=document_types,
            progress=True,
        )
        print("window                         api      local      difference")
        for row in counts:
            print(
                f"{row.date_from} to {row.date_to}  "
                f"{row.api_count:>8,}  {row.local_count:>8,}  {row.difference:>10,}"
            )
        print("Dry run only: no details were fetched and no files were changed.")
        return 0

    result = update_dataset(
        api,
        store,
        since_date=args.since,
        until_date=args.until,
        full=args.full,
        scope=selected_scope if args.full else None,
        limit=args.limit,
        reporter=print,
    )
    return _report_run(result)


# Remove only the exact corpus and sidecar/ledger files selected by --fresh.
def _clear_corpus(store: Store) -> None:
    for path in store.reset():
        print(f"Removed {path}.")


# Build or resume one validated YAML-defined corpus.
def _command_build(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(output_path(config), reporter=print)
    if args.fresh:
        _clear_corpus(store)
    delay = config.processing.request_delay if args.delay is None else args.delay
    api = PressCornerAPI(request_delay=delay)
    result = build_corpus(
        config,
        api,
        store,
        limit=args.limit,
        reporter=print,
    )
    print(f"Dataset: {store.path}")
    return _report_run(result)


# Resolve default audit bounds from local dataset dates.
def _audit_range(store: Store, args: argparse.Namespace) -> tuple[date, date]:
    dates = store.frame["date"].dropna().astype(str)
    dates = dates[dates.ne("")]
    if dates.empty and (args.since is None or args.until is None):
        raise ValueError("Cannot infer audit dates from an empty dataset")
    start = args.since or date.fromisoformat(dates.min())
    end = args.until or date.today()
    if start > end:
        raise ValueError(f"since date {start} is after until date {end}")
    return start, end


# Print only mismatched audit rows plus the required surplus warning and summary.
def _print_audit(results: list[AuditResult]) -> None:
    problems = mismatches(results)
    if problems:
        print("window                         api      local      status")
        for row in problems:
            print(
                f"{row.date_from} to {row.date_to}  "
                f"{row.api_count:>8,}  {row.local_count:>8,}  {row.status}"
            )
    deficient = sum(row.status == "deficient" for row in results)
    surplus = sum(row.status == "surplus" for row in results)
    reconciled = sum(row.status == "reconciled" for row in results)
    matched = len(results) - deficient - surplus - reconciled
    summary = (
        f"Audited {len(results):,} window(s): {deficient:,} deficient, "
        f"{surplus:,} surplus"
    )
    if reconciled:
        summary += f", {reconciled:,} reconciled"
    print(f"{summary}, {matched:,} matched.")
    if surplus:
        print(
            "Local surplus can reflect server-side deletions; surplus rows are reported and never deleted."
        )


# Audit the full dataset and optionally repair only deficient windows.
def _command_audit(args: argparse.Namespace) -> int:
    store = Store(args.data_dir, reporter=print)
    if not store.exists:
        raise FileNotFoundError(f"Dataset not found at {store.path}")
    store.load()
    start, end = _audit_range(store, args)
    api = PressCornerAPI(request_delay=args.delay)
    results = audit_store(
        api,
        store,
        start,
        end,
        granularity=args.granularity,
        progress=True,
        reporter=print,
    )
    _print_audit(results)
    if not args.fix:
        return 0

    repaired, run_result = repair_deficiencies(
        api,
        store,
        results,
        reporter=print,
    )
    for item in repaired:
        print(
            f"Rechecked {item.before.date_from} to {item.before.date_to}: "
            f"local {item.before.local_count:,} -> {item.after.local_count:,}; "
            f"API now {item.after.api_count:,} ({item.after.status})."
        )
        if item.after.reconciled:
            print(
                f"Server count inflated by {item.phantom_count:,} — phantom index "
                "entries; local holds all retrievable documents."
            )
    run_exit_code = _report_run(run_result)
    if any(item.after.status == "deficient" for item in repaired):
        print(
            "WARNING: Some deficient windows remain after repair; re-run audit --fix."
        )
        return 2
    return run_exit_code


# Print dataset size, date/type coverage, detail quality, failures, and release version.
def _command_status(args: argparse.Namespace) -> int:
    store = Store(args.data_dir, reporter=print)
    if not store.exists:
        raise FileNotFoundError(f"Dataset not found at {store.path}")
    frame = store.load()
    metadata = store.load_metadata()
    dates = frame["date"].dropna().astype(str)
    dates = dates[dates.ne("")]
    date_min = dates.min() if not dates.empty else "n/a"
    date_max = dates.max() if not dates.empty else "n/a"
    cutoff = store.cutoff or "n/a"
    detail_share = (
        float(frame["detail_ok"].fillna(False).mean() * 100) if len(frame) else 0.0
    )
    type_counts = frame["doc_type"].fillna("(missing)").astype(str).value_counts()
    failed_windows = len(store.load_failed_windows())
    failed_refs = len(store.load_failed_refs())
    dataset_version = (
        metadata.get("dataset_version") or metadata.get("version") or "n/a"
    )

    print(f"Dataset: {store.path}")
    print(f"Total documents: {len(frame):,}")
    print(f"Cutoff: {cutoff}")
    print(f"Scope: {store.scope}")
    print(f"Date range: {date_min} to {date_max}")
    print(f"Detail complete: {detail_share:.1f}%")
    print(f"Pending windows: {failed_windows:,}")
    print(f"Pending references: {failed_refs:,}")
    print(f"Dataset version: {dataset_version}")
    print("Top document types:")
    for code, count in type_counts.head(12).items():
        print(f"  {code}: {count:,}")
    if len(type_counts) > 12:
        print(f"  …: {int(type_counts.iloc[12:].sum()):,}")
    return 0


# Write the packaged example config without overwriting an existing user file.
def _command_init(args: argparse.Namespace) -> int:
    target: Path = args.path
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing config: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"Wrote {target}.")
    return 0


# Write requested by-type parquet subsets and/or a complete CSV export.
def _command_export(args: argparse.Namespace) -> int:
    if not args.by_type and not args.csv and args.scope is None:
        raise ValueError(
            "Choose at least one export format: --by-type, --csv, and/or --scope active"
        )
    store = Store(args.data_dir, reporter=print)
    if not store.exists:
        raise FileNotFoundError(f"Dataset not found at {store.path}")
    store.load()
    if args.by_type:
        for path in store.export_by_type():
            print(f"Wrote {path}.")
    if args.csv:
        print(f"Wrote {store.export_csv()}.")
    if args.scope == "active":
        print(f"Wrote {store.export_active()}.")
    return 0


# Dispatch one parsed command and convert expected failures into exit code 1.
def main(argv: list[str] | None = None) -> int:
    """Run the console interface and return its documented process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    commands = {
        "download": _command_download,
        "update": _command_update,
        "build": _command_build,
        "audit": _command_audit,
        "status": _command_status,
        "init": _command_init,
        "export": _command_export,
    }
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:
        print(
            "Interrupted; completed checkpoints and pending-window ledgers were preserved.",
            file=sys.stderr,
        )
        return 130
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


# Support direct ``python -m presscorner_builder.cli`` execution.
if __name__ == "__main__":
    raise SystemExit(main())
