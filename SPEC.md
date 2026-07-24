# presscorner-builder — Implementation Specification

Build a pip-installable Python package that creates and maintains research-ready datasets
from the European Commission Press Corner (https://ec.europa.eu/commission/presscorner).
It replaces an earlier single-file scraper. Target users are social scientists, including
non-technical ones: the default path must work with zero configuration.

## Package layout

```
presscorner-builder/
  pyproject.toml            # setuptools, src layout, python >=3.11
  src/presscorner_builder/
    __init__.py             # __version__
    cli.py                  # argparse CLI, console script "presscorner"
    config.py               # Pydantic models + YAML loading (strict, extra="forbid")
    api.py                  # PressCornerAPI: HTTP client for the EC API
    windows.py              # month-window generation helpers
    records.py              # detail JSON -> flat record dict; SCHEMA definition
    store.py                # Store: parquet load/save, dedup, legacy migration, sidecar metadata
    pipeline.py             # orchestration: update / build flows
    audit.py                # per-window count reconciliation + repair
    download.py             # fetch published dataset via manifest URL
  tests/                    # pytest, mocked HTTP (use responses or monkeypatch), NO live API calls
  configs/example-config.yaml
```

Dependencies: `requests`, `pandas`, `pyarrow`, `beautifulsoup4`, `pydantic>=2`, `pyyaml`, `tqdm`.
Dev extra: `pytest`, `responses`.
Console entry point: `presscorner = presscorner_builder.cli:main`.

## The EC Press Corner API (verified 2026-07)

Base: `https://ec.europa.eu/commission/presscorner`

### GET /api/search
Params: `language` (e.g. `en`), `pagesize` (max 100), `pagenumber` (1-based),
`datefrom`, `dateto` — **format `ddmmyyyy`**, e.g. `01012020` (verified working, filters on eventDate),
`documentTypeCodes` (comma-separated codes, accepts legacy codes not shown in the UI),
`global` (keyword search), `text`, `title`, `commissioner`, `policyarea`, `country`, `institution`.

Response: `{"totalNumber": int, "pageSize": int, "pageNumber": int,
"docuLanguageListResources": [ {summary}, ... ]}`.
Summary fields: `refCode` (e.g. `IP/26/301`), `eventDate` (YYYY-MM-DD), `title`, `leadText`,
`docutype: {code, description}`, `languageCode`.
Results are ordered by eventDate descending. Deep pagination works (tested to page 1307).

### GET /api/documents
Params: `reference` (e.g. `SPEECH/26/1671`), `language`.
Response (verified structure):
- `docuLanguageResource`: `title`, `subtitle`, `htmlContent` (full HTML body), `language`,
  `attachmentResources` [], `linkResources` [], `original` (bool)
- `refCd`, `eventDate` (YYYY-MM-DD), `publishDate` (ISO timestamp)
- `docutypeResource: {code, description}`
- `contactsResource`: list of press contacts: `firstName`, `lastName`, `title` (these are
  SPOKESPERSONS, not authors)
- `commissionerResource`: list of `{code, shortDescription}` — the actual commissioner(s)
- `placeResource: {description}` (e.g. "Brussels")
- `policiesResource`: list of `{code, description}`
- `countriesResource`: list
- `originalLanguage`
May return empty body / non-JSON for some references — treat as detail-failure, not crash.

### GET /api/docutypes
Params: `language`. Lists only the ~9 currently active codes. The archive contains 30+ codes
(legacy RAPID types: BIO, PRES, CJE, PESC, CES, COR, ECA, EO, BEI, OLAF, EDPS, EPSO, STAT,
P, DOC, AGENDA, CLDR, WM, DN, ...). Any code can be passed to /api/search. Do NOT validate
type codes against /api/docutypes; accept any code, warn (not error) on unknown ones.

### Client behavior (api.py)
- `requests.Session`, honest User-Agent:
  `"presscorner-builder/{version} (https://github.com/tseidl/presscorner-builder; academic research)"`.
- Rate limit: min delay between requests, default 1.0s (constant, overridable via config/CLI).
- Retries: up to 4 attempts per request, exponential backoff (2s, 4s, 8s), on connection
  errors, timeouts, 5xx, and non-JSON responses where JSON is expected.
- **CRITICAL FAILURE SEMANTICS**: if a search page request ultimately fails, raise
  `WindowFetchError` — NEVER treat a failed page as "no more results". (The predecessor
  scraper did this and silently lost ~3,700 documents.)
- Add cache-busting `ts` param (epoch millis) to every request.
- Search pagination within a window: keep fetching pages until collected >= totalNumber or
  an empty page. If totalNumber changes mid-pagination (new doc published), that's fine —
  stop at empty page.

## Windowed fetching (windows.py, pipeline.py)

All bulk fetching is done in **calendar-month windows** via datefrom/dateto, newest window
first. Rationale: a network interruption loses at most one window; windows are the unit of
audit and repair.

`month_windows(since_date, until_date) -> list[(date_from, date_to)]` — full calendar months
clipped to the given bounds, descending order.

Per window: search all pages -> for each summary whose `document_id` is not already in the
store: fetch detail -> build record -> add. After each completed window, checkpoint-save.
If a window raises WindowFetchError: record it in a ledger file (`data/failed-windows.json`),
continue with the next window, report loudly at the end, and retry ledger windows at the
start of the next run. Same pattern for individual failed detail fetches
(`data/failed-refs.json`): save a partial record (summary fields only, `detail_ok=False`),
retry on next run and replace the partial record when the detail succeeds.

Progress: tqdm progress bars (per window and per document), plus plain-print summaries.

## Record schema (records.py)

One row per document. `document_id` = refCode lowercased, `/` -> `_` (e.g. `ip_26_301`).

| column | type | source |
|---|---|---|
| document_id | str | derived from refCode |
| reference | str | refCode |
| doc_type | str | docutype code |
| doc_type_name | str | docutype description |
| title | str | detail, fallback summary |
| subtitle | str/null | detail |
| summary | str/null | leadText |
| date | str YYYY-MM-DD | eventDate |
| publish_datetime | str ISO/null | publishDate |
| place | str/null | placeResource.description |
| language | str | languageCode |
| original_language | str/null | originalLanguage |
| commissioners | str/null | "; "-joined commissionerResource shortDescriptions |
| spokespersons | str/null | "; "-joined "First Last (title)" from contactsResource |
| policy_areas | str/null | "; "-joined policiesResource descriptions |
| policy_codes | str/null | "; "-joined policiesResource codes |
| full_text | str | htmlContent -> BeautifulSoup get_text(separator="\n", strip=True) |
| html | str/null | raw htmlContent, only when keep_html=True, else null column present |
| url | str | {base}/detail/{lang}/{document_id} |
| pdf_url | str | {base}/api/files/document/print/{lang}/{document_id}/{REF}_{LANG}.pdf |
| detail_ok | bool | whether detail fetch succeeded |
| scraped_at | str ISO | now |

Use "; " as the join separator everywhere (values can contain commas).
No `keywords` field (empty in 100% of the archive). Nullable = pandas NA.

## Store (store.py)

- Single source of truth: one parquet file (default `data/press-corner.parquet`).
- State derives from the parquet itself: `existing_ids` (set of document_id),
  `cutoff` = max(date). No separate state DB.
- **Legacy migration**: transparently accept parquet files from the predecessor scraper
  (columns: document_id, reference_number, document_type, document_type_name, title,
  subtitle, publication_date, language, summary, url, scraped_at, full_text, authors,
  policy_areas, keywords, pdf_url). Mapping: reference_number->reference,
  document_type->doc_type, document_type_name->doc_type_name, publication_date->date,
  authors->spokespersons (they were spokespersons), drop keywords; add missing new columns
  as NA; detail_ok = full_text != "". Migration happens on load; saved back in new schema.
  Print a one-line notice when migrating.
- Save: concat new records, drop_duplicates on document_id keep="last", sort by date
  descending, write parquet (snappy). Atomic write: write to temp file in same dir then
  os.replace.
- Sidecar `data/press-corner.meta.json`: package_version, last_run ISO, total_documents,
  cutoff date, per-type counts, config hash if built from YAML, list of currently-failed
  windows/refs counts.
- Optional per-type subset export is NOT automatic; `presscorner export --by-type` writes
  per-type parquet files using the predecessor's kebab-case stems (press-releases.parquet
  for IP, speeches.parquet for SPEECH, etc. — keep a code->stem map for the 10 modern types,
  `other.parquet` for the rest).

## Config (config.py)

Pydantic v2, `model_config = ConfigDict(extra="forbid")`. YAML via yaml.safe_load.

```yaml
metadata:                    # optional, stamped into sidecar
  project_name: ""
  author: ""
  description: ""

data:
  mode: descriptive          # "descriptive" | "fixed"
  # descriptive:
  document_types: []         # any codes, empty = all
  start_date: 1975-01-01     # date
  end_date: null             # null = today
  keywords: []               # each sent as `global` search; results unioned
  commissioners: []          # passed to `commissioner` param; unioned
  policy_areas: []           # passed to `policyarea` param; unioned
  language: en
  # fixed:
  references: []             # e.g. ["IP/26/301", "SPEECH/26/1671"]

processing:
  keep_html: false
  request_delay: 1.0

output:
  output_directory: ./output
  dataset_name: press-corner # -> {output_directory}/{dataset_name}.parquet
```

Discriminate on `data.mode`. Validate dates, reject unknown keys with a clear message
naming the bad key. `references` only valid in fixed mode; search filters only in
descriptive mode.

## CLI (cli.py)

`presscorner <verb>`, argparse subparsers. Global options where sensible:
`--data-dir` (default `./data`), `--delay`.

- `presscorner download [--data-dir] [--url URL] [--force]`
  Downloads the published full dataset. Resolution: fetch a small JSON manifest from
  `MANIFEST_URL` (module constant in download.py, pointing to the raw GitHub URL
  `https://raw.githubusercontent.com/tseidl/presscorner-builder/main/dataset-manifest.json`).
  Manifest format: `{"version": "v2026.07", "cutoff": "2026-07-31", "url": "...",
  "sha256": "...", "size_bytes": N}`. Stream-download with tqdm progress to
  data-dir/press-corner.parquet, verify sha256, print version + cutoff + row count.
  Refuse to overwrite an existing parquet unless --force. `--url` bypasses the manifest.
  Also create the sidecar meta json. Include a `dataset-manifest.json` at repo root with
  placeholder values and a comment-free TODO note in README.
- `presscorner update [--data-dir] [--since] [--until] [--limit N] [--dry-run]`
  Incremental top-up of the full dataset: load store (migrating legacy schema if needed),
  retry failed windows/refs, determine since = (cutoff minus 3 days overlap) unless --since
  given, fetch windows to today, append, save. --dry-run: report window counts vs local
  without fetching details. --limit N stops after N new documents (canary).
  If no parquet exists: tell the user to run `download` first, or `update --full` to
  scrape everything from 1975 (confirm with y/N prompt unless --yes).
- `presscorner build CONFIG.yaml [--fresh] [--limit N]`
  Build a YAML-defined corpus into its own output dir. Same windowed machinery, but with
  search filters from config; fixed mode fetches the listed references directly (no search).
  Resumable by default (existing output parquet = state); --fresh deletes and rebuilds.
- `presscorner audit [--data-dir] [--fix] [--since] [--until] [--granularity month|year]`
  For each window: API totalNumber (one 1-result search) vs local count from the parquet;
  print table of mismatches only (plus summary line). --fix: for each deficient window,
  run the full window fetch to pick up missing docs (existing ids are skipped
  automatically), then re-check and report before/after. Note in output that a local
  surplus (local > API) can reflect server-side deletions and is reported, never deleted.
- `presscorner status [--data-dir]`
  Print: total docs, cutoff, date range, per-type counts (top 12 + "…"), share with
  detail_ok, pending failed windows/refs, dataset version from sidecar if present.
- `presscorner init [PATH]`
  Write configs/example-config.yaml content to PATH (default ./config.yaml).
- `presscorner export [--data-dir] [--by-type] [--csv]`
  Subset/format exports as described in Store.

Exit codes: 0 success, 1 error, 2 partial (some windows/refs still failing after a run —
print a prominent warning telling the user to just re-run).

## Audit math

Local counts per window computed from the `date` column. API count via search with
pagesize=1 reading totalNumber, with the same language filter. A window is deficient if
api > local. Ignore (but list) surplus windows.

## Dataset scope: active vs. archive (v0.2 — implemented 2026-07-24)

Specified 2026-07-22. Implement only AFTER the v2026.07 full build has completed and been
certified — no code changes while that run is in progress.

Motivation: the website's search UI exposes only the currently active document types, but
the archive holds 30+ codes (see /api/docutypes section). A third party running a fresh
build usually wants the fast, website-matching corpus; the full archive should be an
explicit choice. The maintained published dataset (v2026.07) is and stays full-archive.

- New module constant `ACTIVE_TYPE_CODES` (api.py), pinned from /api/docutypes as of
  2026-07-22 — do NOT fetch live (reproducibility):
  `("IP", "SPEECH", "STATEMENT", "MEX", "READ", "AC", "QANDA", "FS", "INF")`.
  Note: MEMO is retired and therefore archive scope, like the legacy RAPID series.
- Scope semantics: `active` = every search carries
  `documentTypeCodes=",".join(ACTIVE_TYPE_CODES)`; `all` = no type filter. For scope
  `all`, request behavior MUST remain byte-identical to today so the existing dataset
  audits unchanged.
- **Scope is a dataset property, fixed at creation.** Persist `"scope": "active"|"all"`
  in the meta sidecar. On load, a sidecar without a scope field (or no sidecar) means
  `all` — every pre-feature dataset, including published v2026.07, is full-archive.
  `update` and `audit` derive their search filter from the dataset's recorded scope,
  never from a CLI default.
- CLI: `update --full --scope {active,all}`, default `active` (fresh builds get the
  website-matching corpus unless the archive is asked for). `--scope` is only valid
  together with `--full`; error otherwise. If `--full --scope X` targets an existing
  dataset whose recorded scope differs, error with a message saying scope is fixed at
  creation and a differently-scoped dataset needs a fresh directory.
- Config (`build`): allow `document_types: active` as sugar expanding to
  ACTIVE_TYPE_CODES (the existing field, not a new key — YAML corpora are already
  explicit about types). Reject `active` mixed with literal codes in the same list.
- Audit: apply the dataset's recorded scope to the API count queries (same
  documentTypeCodes param), so an active-scope dataset is compared against
  active-filtered totals. Audit math otherwise unchanged.
- `status`: print a `scope:` line (from sidecar; `all` when absent).
- `download`: after downloading, write the sidecar with `scope` from the manifest if
  present, else `all` (the published dataset is full-archive; dataset-manifest.json
  carries `"scope": "all"` explicitly).
- `export --scope active`: write an active-types-only parquet subset (doc_type in
  ACTIVE_TYPE_CODES) from the local dataset — the answer for "I have the full dataset
  but only want the website-visible corpus" is a one-second filter, never a re-scrape.
  A full-archive dataset is a strict superset of an active-scope build.
- Implementation-time verification (manual, before merging): for 3 sample month windows
  (e.g. 2024-05, 2019-11, 2012-03), check that active-filtered totalNumber plus the
  count of non-active-type rows in the built dataset equals the unfiltered totalNumber,
  confirming the server filter partitions cleanly.
- Tests to add: scope round-trip through the sidecar and missing-field-means-all;
  `update --full` records the chosen scope; scope mismatch on existing dataset errors;
  audit query for an active-scope dataset carries documentTypeCodes; config
  `document_types: active` expands correctly and mixing with literal codes is rejected.

## Permanently-empty details (v0.2 fix — implemented 2026-07-24)

Found during the v2026.07 build (2026-07-24): 10 references (6× BIO 1985–92, plus
CJE/11/133, BEI/09/75, COR/08/39, AGENDA/08/a30) return a **permanently empty body**
(HTTP 200, 0 bytes) from /api/documents — verified repeatedly; the search index knows
them but no detail record exists server-side. Current behavior treats every detail
failure as transient: the ref stays in `failed-refs.json` forever, every ref retry
fails, and any run that ends with pending refs exits 2 — so the documented advice
("partial: just re-run") deadlocks, and `&&`-chained stage pipelines never proceed.
(Workaround used for v2026.07: manually cleared `failed-refs.json` after verifying all
10 by hand; their summary-only rows with `detail_ok=False` stay in the dataset.)

Fix: distinguish outcomes in the detail fetch.
- Transient failure (connection error, timeout, 5xx, non-JSON with non-empty body):
  keep current behavior — partial record, ledger entry, retry next run.
- **Empty success** (2xx with empty/whitespace body): permanent. Save the summary-only
  record with `detail_ok=False`, do NOT add to (and remove any existing entry from) the
  retry ledger, count separately in the run summary (e.g. "N documents have no detail
  record server-side"), and do not let it trigger exit code 2.
- Tests: empty-body detail -> record saved, ref absent from ledger, run exits 0;
  transient failure still retries.

## Phantom server counts in audit (v0.2 fix — implemented 2026-07-24)

Found during v2026.07 certification (2026-07-24): for some windows (observed mid-1980s,
diff of 1–3 each) search `totalNumber` EXCEEDS the number of documents the API will
actually return — e.g. 1987-04 claims 94 but enumerating all pages yields 93, all of
which were stored locally. Phantom index entries server-side (deleted docs or entries
with no retrievable variant). Consequence today: such windows stay "deficient" after
repair, `audit --fix` warns and exits 2 forever, and `&&`-chained stages abort.

Fix: when a repaired window remains deficient AND the repair fetch added 0 documents,
enumerate the window's actual search results (all pages) and compare refCodes against
the store. If every returned ref is already stored, reclassify the window as
`reconciled` with an informational note ("server count inflated by N — phantom index
entries; local holds all retrievable documents") — no warning, no exit 2. Only windows
where enumeration surfaces genuinely unstored refs stay deficient.
Tests: mocked window with totalNumber = N+1 but N enumerable docs, all stored ->
reconciled, exit 0; one enumerable doc missing locally -> stays deficient, exit 2.

## UX niceties (v0.2 — implemented 2026-07-24)

- The audit counting phase is currently silent for its whole duration (619 rate-limited
  count queries ≈ 10+ min for a month-granularity full audit) — users think it hung.
  Before the loop, print e.g. `Auditing 619 windows at ~1 request/s — this takes about
  11 minutes.` and wrap the loop in a tqdm bar like the fetch phases.
- Long fetch runs (`update --full`, many-window updates): print a rough ETA up front
  (windows × observed per-window time after the first few windows, or a simple
  docs-per-second heuristic).

## Tests (tests/)

pytest, all HTTP mocked. Cover at least:
- windows.month_windows: bounds, clipping, descending order, single-partial-month
- api: retry-then-succeed, retry-exhausted raises WindowFetchError, non-JSON detail
  returns None-equivalent failure
- records: full detail -> record mapping (use a captured real JSON fixture, abbreviated);
  missing sections -> nulls; spokesperson vs commissioner separation
- store: round-trip, dedup keep-last, legacy-schema migration produces new schema and
  preserves row count, atomic write leaves no temp files
- audit: deficient-window detection from synthetic data
- config: valid YAML parses; unknown key rejected with key name in message; fixed vs
  descriptive field validation
- cli: `--help` runs; `status` on a tiny synthetic parquet

## Conventions

- Python >=3.11, pathlib everywhere, type hints on public functions.
- One-line comment above each function stating what it does.
- No print-noise in library modules; CLI layer owns user-facing output (a thin
  reporter/logging pattern is fine).
- Keep it readable over clever; no premature abstraction; no unused code.
- Do NOT write README.md (the maintainer writes it). Write docstrings.
