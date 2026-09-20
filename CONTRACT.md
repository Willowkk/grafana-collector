# Internal module contract

Internal collection modules use JSON-serializable dictionaries. Timestamps are integer UTC milliseconds. Points are `[timestamp_ms, number_or_null]`. Export converts timestamps to Arrow UTC millisecond timestamps or Excel dates. Nonfinite collection values become null with quality metadata. No credentials are part of any dictionary below.

## Series
`{ref_id, metric, labels: dict[str,str], name, unit, points: list[[timestamp_ms,value]], ...optional metadata}`. A series identity includes query key and ALL sorted labels, metric, and ref_id, not just its legend. Export display series may include `series_id` supplied by storage.

## Query
`{key, panel_id: int, ref_id: str, kind: 'opentsdb'|'bosun'|'math'|'unsupported', route: str, query: dict, interval_ms: int, alias: str, unit: str, hidden: bool, dependencies: list[str], metadata: dict, error: str|null}`. `query` is a compiled OpenTSDB query, Bosun request template, or math expression dictionary. `key` is stable semantic hash, includes interval, excludes request time range.

## Panel
`{id: int, title: str, group: str, queries: list[Query], transformations: list[dict], metadata: dict}`.

## Plan
`{schema_version: 1, source_url: str, dashboard: dict, variables: dict, panels: list[Panel], from_ms: int, to_ms: int, mode: 'fetch'|'watch', started_ms: int, metadata: dict}`. from_ms is the dataset start, not the automatic-interval reference start; to_ms is the initial end. `metadata` holds timezone, display range, measured panel widths and sanitized datasources. Freeze plan at creation; resume uses saved plan.

## query.py (query compiler owner)
`compile_dashboard(dashboard, datasources, source_url, *, reference_from_ms, reference_to_ms, panel_widths, panel_ids=None, variable_overrides=None) -> dict` containing `panels`, `variables`, and optional `metadata`. Datasources is Grafana list or map; widths dict[int,float] actual measured pixels. Strict unresolved values -> unsupported query, never silently invented filtering.
`build_request(query, start_ms, end_ms) -> tuple[str,str,object]` (HTTP method, relative/absolute URL, JSON body or request object).
`parse_response(query, raw) -> list[Series]`.
`render_panel(panel, series_by_ref: dict[str,list[Series]]) -> list[Series]` computes expressions and transformations, hidden handling, no interpolation or zero fill. Raises UnsupportedQuery/QueryError for unsupported/broken dependencies.

## storage.py and exporter.py (storage owner)
`Store(run_dir)` creates/opens store; `close()`; `initialize(plan)`; `load_plan()`; `get_cursor(query_key) -> int|None`.
`record_result(query, start_ms, end_ms, series, raw, *, status='success', error=None, observed_ms=None)` writes compressed raw response and transactionally upserts points + attempt + watermark; statuses success/empty advance watermark, failed do not. Clamp points to dataset start and requested end; query overlap may update existing points. Never advance watermark across an unqueried gap. Raw responses must not contain credentials.
`get_query_series(query_key, start_ms=None, end_ms=None) -> list[Series]`.
`record_display(panel_id, series, start_ms, end_ms)` stores/refreshes derived/display points for the collected range without losing unrelated historical data.
`query_statuses() -> list[dict]`; `panel_statuses() -> list[dict]`; `get_display_series(panel_id,start_ms=None,end_ms=None)`.
`export_run(store, out_dir, *, from_ms=None, to_ms=None, panel_ids=None) -> pathlib.Path` returns manifest path, includes all selected panel statuses and raw/query provenance, XLSX only for valid data/legitimate empty status; failures are not healthy empty files. Workbooks: one sheet, Time + legends, timezone Asia/Shanghai, collision-safe names and row/column splits.

## exporting.py and parquet_exporter.py
`exporting.export_run(store, out_dir, *, format='parquet', from_ms=None, to_ms=None, panel_ids=None) -> pathlib.Path` dispatches to Parquet by default or the existing Excel exporter for `format='xlsx'`.
`parquet_exporter.export_run(store, out_dir, *, from_ms=None, to_ms=None, panel_ids=None) -> pathlib.Path` writes an immutable Parquet generation and atomically publishes its manifest. All values, metadata and coverage are read from one SQLite snapshot. The generation is self-contained; each manifest's paths are relative to that manifest's own directory.
Parquet schema name `sdkv2-grafana-parquet`, version 1. Points: `time: timestamp[ms,UTC]`, `panel_id: int64`, `series_id: string`, `value: float64 nullable`. Original display-series numeric values are not unit-scaled or rounded. Integers must round-trip through float64 exactly or export fails. Source null creates a row; absent points do not. The composite key `(panel_id,series_id,time)` is unique within one snapshot.
Series metadata includes full string labels, panel identity/group, legend/metric/ref, Grafana unit, source-value unit and display unit/scale. Percent values remain ratios; the machine display scale is 100 for `%` (different from Excel's cell scale of 1). Manifest retains every selected panel including empty/failed/pending, query definitions, sampling and coverage; file creation alone does not assert complete collection. Provenance is query/batch-level; there is no promised per-point attempt ID. Snapshots replace earlier snapshots, not append-only change streams.

## transport.py (root)
`async transport.execute(query,start_ms,end_ms) -> object` uses query.build_request. Raises AuthenticationRequired, QueryError. `async transport.reauthenticate()` handles manual login wait; no parallel auth prompts. BrowserSession async context manager owns login profile, API request context, dashboard/datasource discovery and width measurement.

## engine.py (engine owner)
`Collector(plan, store, transport, *, concurrency=4, timeout=30, retries=3, lookback_ms=300000, progress=None)`.
`async collect_until(end_ms) -> dict` returns summary counts. Fetch uses plan.from_ms..plan.to_ms as ONE semantic window (do not break TopK); watch each query uses max(dataset_start, cursor-lookback) or dataset_start on first collection. Fully support no-history first start, per-query progress, retry, empty-vs-failed, auth pause and resume, frozen interval, partial result and derived dependency failures.
`async watch(*, poll_interval_ms=300000, rounds=None, duration_seconds=None, stop_event=None) -> dict`: sleep until first due time if from=start; never overlap rounds; graceful cancellation saves state for CLI export. Tests use fake transport/clock where appropriate.

Root owns cli.py, transport.py, timeutil.py, package/build files, docs and integration. Owners may add their own tests; do not modify another owner's files without coordinating.
