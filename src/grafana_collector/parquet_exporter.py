"""Atomic, typed Parquet snapshots of stored Grafana display curves.

Values remain in their source units. Human display scales are metadata, not
arithmetic applied to the points table. Arrow schemas are also written when
the selected data contains no points or series.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from . import __version__
from .units import SUPPORTED_UNITS, build_unit_plan


SCHEMA_NAME = "sdkv2-grafana-parquet"
SCHEMA_VERSION = 2
POINTS_SCHEMA = pa.schema([
    pa.field("time", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("panel_id", pa.int64(), nullable=False),
    pa.field("series_id", pa.string(), nullable=False),
    pa.field("value", pa.float64(), nullable=True),
])
SERIES_SCHEMA = pa.schema([
    pa.field("panel_id", pa.int64(), nullable=False),
    pa.field("series_id", pa.string(), nullable=False),
    pa.field("name", pa.string(), nullable=False),
    pa.field("metric", pa.string(), nullable=False),
    pa.field("ref_id", pa.string(), nullable=False),
    pa.field("query_key", pa.string(), nullable=True),
    pa.field("labels", pa.map_(pa.string(), pa.string()), nullable=False),
    pa.field("value_unit", pa.string(), nullable=True),
    pa.field("grafana_unit", pa.string(), nullable=True),
    pa.field("display_unit", pa.string(), nullable=True),
    pa.field("display_scale", pa.float64(), nullable=False),
    pa.field("unit_family", pa.string(), nullable=True),
    pa.field("unit_status", pa.string(), nullable=False),
    pa.field("quality_json", pa.string(), nullable=True),
    pa.field("aggregate_tags", pa.list_(pa.string()), nullable=True),
    pa.field("extra_metadata_json", pa.string(), nullable=True),
])

# These describe the numeric *input*, independently of display prefixes.
# Grafana's kbytes is binary kibibytes; percentunit is an unscaled ratio.
# "1" denotes Grafana's unitless numeric formatting, not a physical unit.
_VALUE_UNITS = {
    "bytes": "B", "kbytes": "KiB", "decbytes": "B", "binBps": "B/s",
    "µs": "µs", "ms": "ms", "reqps": "req/s", "short": "1",
    "none": "1", "percentunit": "ratio", "string": None,
}
_BATCH_ROWS = 65_536


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _float64(value, *, panel_id, series_id, timestamp):
    """Reject silent numeric loss rather than publishing a plausible dataset."""
    if value is None:
        return None
    context = f"panel {panel_id}, series {series_id}, time {timestamp}"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Parquet value must be numeric or null ({context})")
    try:
        converted = float(value)
    except OverflowError as error:
        raise ValueError(f"Value cannot be represented as finite float64 ({context})") from error
    if not math.isfinite(converted):
        raise ValueError(f"Non-finite value cannot be exported as float64 ({context})")
    # Python compares int/float without first rounding the integer to float.
    if isinstance(value, int) and converted != value:
        raise ValueError(f"Integer cannot be represented exactly as float64 ({context})")
    return converted


def _schema_description(schema):
    return [{"name": field.name, "type": str(field.type), "nullable": field.nullable}
            for field in schema]


def _unit_metadata(series):
    known = [item for item in series if (item.get("unit") or "none") in SUPPORTED_UNITS]
    planned = build_unit_plan(known)
    result = {}
    for item in series:
        sid = item["series_id"]
        configured = item.get("unit")
        effective = configured or "none"
        spec = planned.get(sid)
        if spec is None:
            result[sid] = {
                "grafana_unit": configured, "value_unit": None,
                "display_unit": None, "display_scale": 1.0,
                "unit_family": None, "unit_status": "unsupported",
            }
        else:
            result[sid] = {
                "grafana_unit": configured, "value_unit": _VALUE_UNITS[effective],
                "display_unit": spec["display_unit"],
                # Excel supplies its own factor of 100; a program does not.
                "display_scale": 100.0 if effective == "percentunit" else float(spec["scale"]),
                "unit_family": spec["unit_family"],
                "unit_status": "configured" if configured else "default_none",
            }
    return result


class _BufferedRows:
    """Bound row buffers across panels, so small panels share row groups."""

    def __init__(self, writer, schema):
        self.writer = writer
        self.schema = schema
        self.rows = []

    def append(self, row):
        self.rows.append(row)
        if len(self.rows) >= _BATCH_ROWS:
            self.flush()

    def flush(self):
        if self.rows:
            self.writer.write_table(pa.Table.from_pylist(self.rows, schema=self.schema),
                                    row_group_size=_BATCH_ROWS)
            self.rows.clear()


def _write_points(buffer, panel_id, series):
    for item in series:
        for timestamp, value in item.get("points", []):
            buffer.append({"time": timestamp, "panel_id": panel_id,
                           "series_id": item["series_id"], "value": value})


def _source_extras(item):
    # Preserve all attributes not represented elsewhere; do not reserialize
    # labels, identifiers, units, or metric/legend text into this extension.
    known = {"points", "series_id", "name", "metric", "ref_id", "query_key",
             "labels", "unit", "quality", "aggregate_tags"}
    extras = {key: value for key, value in item.items() if key not in known}
    aggregate_tags = item.get("aggregate_tags")
    if aggregate_tags is not None and (
        not isinstance(aggregate_tags, list) or
        any(not isinstance(tag, str) for tag in aggregate_tags)
    ):
        raise ValueError("Series aggregate_tags must be a list of strings or null")
    return {
        "quality_json": _json(item["quality"]) if "quality" in item else None,
        "aggregate_tags": aggregate_tags,
        "extra_metadata_json": _json(extras) if extras else None,
    }


def _sampling_summary(metadata):
    keys = ("collector_version", "incremental", "interval_algorithm", "reference_from_ms",
            "reference_to_ms", "poll_interval_ms", "lookback_ms", "timezone",
            "auto_interval_ms", "viewport")
    return {key: metadata[key] for key in keys if key in metadata}


def _provenance(plan, panels, queries, generation):
    # Local Math expressions have no transport status row, but still belong
    # to the frozen query graph and must remain resolvable from panel refs.
    definitions = {query["key"]: {"definition": query}
                   for panel in panels for query in panel["queries"]}
    for query in queries:
        definitions[query["key"]].update({key: value for key, value in query.items()
                                           if key not in {"key", "query_key"}})
    return {
        "schema_name": SCHEMA_NAME, "schema_version": SCHEMA_VERSION,
        "generation": generation, "dashboard": plan["dashboard"],
        "sampling": plan.get("metadata", {}),
        "raw_provenance": None,
        "queries": definitions,
        "panels": {str(panel["id"]): {
            "query_keys": [query["key"] for query in panel["queries"]],
            "transformations": panel.get("transformations", []),
            "metadata": panel.get("metadata", {}),
        } for panel in panels},
    }


def _coverage(store, panel, plan, query_statuses, from_ms, to_ms):
    # Match the Excel export's coverage boundaries, including failed attempts
    # beyond the last successful render. Empty output never implies coverage.
    ends = [a["end_ms"] for q in query_statuses if q["panel_id"] == panel["id"]
            for a in q["attempts"]]
    displayed_end = store.connection.execute(
        "SELECT MAX(end_ms) FROM display_updates WHERE panel_id=?", (panel["id"],)
    ).fetchone()[0]
    start = int(from_ms) if from_ms is not None else int(plan["from_ms"])
    end = int(to_ms) if to_ms is not None else max(
        [int(plan["to_ms"]), displayed_end or int(plan["to_ms"]), *ends])
    if end < start:
        return {"status": "uncollected", "from_ms": from_ms, "to_ms": to_ms,
                "intervals": [], "reason": "Filter does not intersect this panel's dataset range"}
    return store.display_coverage(panel["id"], start, end)


def _file_description(path, schema=None, row_count=None, **extra):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    result = {"path": path.name, "size_bytes": path.stat().st_size,
              "sha256": digest.hexdigest(), **extra}
    if schema is not None:
        result["schema"] = _schema_description(schema)
    if row_count is not None:
        result["row_count"] = row_count
    return result


def _write_json(path, value):
    with path.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _write_provenance(path, value):
    # Stream JSON chunks rather than allocating a second full serialized copy.
    # The gzip header does not depend on the staging path or current time.
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False,
                               separators=(",", ":"))
    with path.open("wb") as output:
        with gzip.GzipFile(fileobj=output, filename="", mode="wb", mtime=0) as compressed:
            for chunk in encoder.iterencode(value):
                compressed.write(chunk.encode("utf-8"))
        output.flush()
        os.fsync(output.fileno())


def _readme():
    return """# Grafana Parquet 数据包（schema_version=2）

`points.parquet` 是长表：每行一条真实面板曲线数据点，包含 `time`、
`panel_id`、`series_id`、`value`。值来自已完成 Math/面板转换的存储结果，
保留 Grafana 原单位，未乘显示系数。时间类型为 `timestamp[ms, tz=UTC]`。
`series.parquet` 每条曲线一行，以 `(panel_id, series_id)` 连接；完整标签
为 `map<string, string>`，数字形状的标签仍是字符串，名称不用于主键。
面板标题和分组只保存在 `manifest.panels`，按 `panel_id` 关联。
曲线不再保存重复的 `source_metadata_json`；`quality_json` 只保存原始质量
信息，`aggregate_tags` 是可空字符串列表，`extra_metadata_json` 只保存其他
未结构化的扩展属性。JSON 列缺省为空，不重复标签、单位、名称或 ID。

```python
from pathlib import Path
import json
import pyarrow.parquet as pq
from zoneinfo import ZoneInfo

manifest_path = Path("manifest.json")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
base = manifest_path.parent
points = pq.read_table(base / manifest["files"]["points"]["path"])
series = pq.read_table(base / manifest["files"]["series"]["path"])
panels = {row["panel_id"]: row for row in manifest["panels"]}
by_id = {(row["panel_id"], row["series_id"]): row for row in series.to_pylist()}
for point in points.slice(0, 5).to_pylist():
    curve = by_id[(point["panel_id"], point["series_id"])]
    display_value = None if point["value"] is None else point["value"] * curve["display_scale"]
    print(point["time"].astimezone(ZoneInfo("Asia/Shanghai")),
          curve["name"], point["value"], curve["value_unit"],
          display_value, curve["display_unit"], dict(curve["labels"]))
```

`value_unit` 描述原值单位；`grafana_unit` 保留 Grafana 配置代码。
例如 `binBps` 的原值单位是 `B/s`，`kbytes` 是 `KiB`，`percentunit`
是比例 `ratio`（1 表示 100%），后者 `display_scale=100`、`display_unit=%`。
`short`/`none` 的 `value_unit=1` 表示无物理单位的数字格式；`string`
不声明数值物理单位。未知单位显式标记 `unit_status=unsupported`，原代码
保留，单位为空且系数为 1；该系数只保留原值，不表示已推断出单位。
显示单位按本次选择的面板/时间窗固定；不同导出窗口可能选择不同前缀。
显示元信息不包含 Excel 数字格式，数值没有提前四舍五入。

`value` 是可空 float64。整数必须能精确转换为 float64，否则整个导出失败；
非有限数值也会明确报错。源存储此前已归一为空值的异常数值仍为空值，
相关批次质量计数见 provenance，曲线质量信息见 quality_json。
实际空值有一行 null；缺少数据点不产生行，
不补时间、不补零、不插值。不能仅凭表中没有行判断正常无数据或失败：
请检查 manifest 的面板 collection_status、range_status 和 display_coverage。

快照主键为 `(panel_id, series_id, time)`，不是采集事件流水；重叠修订只
保留当前存储结果。series_id 不能代替跨运行的 run 身份。
manifest 保留面板状态、范围、精简采样信息及文件索引；完整冻结看板、采样
配置、查询定义和批次状态保存在 `provenance.json.gz`。
`provenance.queries[query_key]` 保存一份 definition 和该查询的采集状态；
`provenance.panels[str(panel_id)]` 保存转换、面板配置及 query_keys 引用。
实际采样间隔见 queries[query_key].definition.interval_ms 及 metadata，
其中 interval_ms=0 代表后端表达式决定采样，不能当作零秒采样。
原始 HTTP 响应不落盘，raw_provenance 为 null，批次记录不包含原始响应路径。
默认结果为面板显示层；网络查询输入未另行导出。

manifest 的所有产物路径都相对它所在目录；外层和 generation 内均可独立
读取。每次导出写入新的不可变目录，全部文件写完后才原子更新外层 manifest。
交付时复制整个 generation 目录，包括 provenance.json.gz。只读取数据点
不需要解压 provenance；需要追溯时：

```python
import gzip

with gzip.open(base / manifest["files"]["provenance"]["path"], "rt", encoding="utf-8") as stream:
    provenance = json.load(stream)
assert provenance["generation"] == manifest["generation"]
```

项目的 grafana_collector.dataset.read_points/read_provenance 只接受协议 v2。
读取数据包无需采集时的 SQLite 或浏览器登录状态。
"""


def export_run(store, out_dir, *, from_ms=None, to_ms=None, panel_ids=None):
    """Publish a consistent, immutable Parquet snapshot and return its manifest."""
    try:
        with store.read_snapshot():
            return _export_run(store, out_dir, from_ms=from_ms, to_ms=to_ms, panel_ids=panel_ids)
    except pa.ArrowException as error:
        raise ValueError(f"Parquet export failed: {error}") from error


def _export_run(store, out_dir, *, from_ms, to_ms, panel_ids):
    if from_ms is not None and to_ms is not None and to_ms < from_ms:
        raise ValueError("Export end precedes start")
    plan = store.load_plan()
    all_ids = {p["id"] for p in plan["panels"]}
    selected_ids = set(panel_ids) if panel_ids is not None else all_ids
    if missing := selected_ids - all_ids:
        raise ValueError(f"Unknown panel IDs: {sorted(missing)}")
    panels = [p for p in plan["panels"] if p["id"] in selected_ids]
    statuses = {p["panel_id"]: p for p in store.panel_statuses()}
    queries = [q for q in store.query_statuses() if q["panel_id"] in selected_ids]
    out_dir = Path(out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    exports = out_dir / "exports"
    exports.mkdir(exist_ok=True)
    generation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    staging = exports / ("." + generation + ".tmp")
    destination = exports / generation
    temporary_manifest = out_dir / (".manifest-" + uuid.uuid4().hex + ".tmp")
    staging.mkdir()
    manifest = {
        "schema_name": SCHEMA_NAME, "schema_version": SCHEMA_VERSION,
        "format": "parquet", "layout": "long-table", "layer": "display",
        "generation": generation, "generation_manifest": "manifest.json",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "exporter": {"name": "sdkv2-grafana-collector", "version": __version__,
                     "pyarrow_version": pa.__version__},
        "source_url": plan["source_url"], "source_run": str(store.run_dir),
        "dashboard": {key: value for key, value in plan["dashboard"].items()
                      if key in {"id", "uid", "title", "version", "schemaVersion"}},
        "variables": plan["variables"],
        "mode": plan["mode"], "timezone": "UTC", "presentation_timezone": "Asia/Shanghai",
        "requested_range": {"from_ms": from_ms, "to_ms": to_ms, "boundaries": "inclusive"},
        "dataset_start_ms": plan["from_ms"], "sampling": _sampling_summary(plan.get("metadata", {})),
        "selected_panel_ids": [p["id"] for p in panels], "dashboard_panel_count": len(all_ids),
        "primary_key": ["panel_id", "series_id", "time"],
        "series_primary_key": ["panel_id", "series_id"],
        "precision": {
            "value": "Unscaled source numeric value as nullable float64; no decimal rounding",
            "integer_conversion": "Fail unless the source integer is exactly representable as float64",
            "nonfinite": "Fail on nonfinite stored values; already normalized source nulls remain null",
            "display": "display_value = value * display_scale; percentunit uses scale 100",
            "missing": "Explicit nulls retain rows; absent points create no rows; no interpolation or zero filling",
        },
        "display_policy": "One prefix per compatible unit family, panel and selected export window",
        "identity": "Snapshot keys identify stored display points; names are not identifiers; IDs are scoped to this run",
        "panels": [],
    }
    point_count = series_count = null_count = 0
    try:
        with pq.ParquetWriter(staging / "points.parquet", POINTS_SCHEMA, compression="zstd") as point_writer, \
                pq.ParquetWriter(staging / "series.parquet", SERIES_SCHEMA, compression="zstd") as series_writer:
            point_buffer = _BufferedRows(point_writer, POINTS_SCHEMA)
            series_buffer = _BufferedRows(series_writer, SERIES_SCHEMA)
            for panel in panels:
                pid = panel["id"]
                status = statuses[pid]
                coverage = _coverage(store, panel, plan, queries, from_ms, to_ms)
                data = store.get_display_series(pid, from_ms, to_ms)
                data.sort(key=lambda item: item["series_id"])
                earliest = latest = None
                count = nulls = 0
                for item in data:
                    for point in item.get("points", []):
                        point[1] = _float64(point[1], panel_id=pid, series_id=item["series_id"], timestamp=point[0])
                        earliest = point[0] if earliest is None else min(earliest, point[0])
                        latest = point[0] if latest is None else max(latest, point[0])
                        count += 1
                        nulls += point[1] is None
                unit_plan = _unit_metadata(data)
                for item in data:
                    labels = item.get("labels") or {}
                    if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                           for k, v in labels.items()):
                        raise ValueError(f"Panel {pid} series labels must be a complete string-to-string map")
                    series_buffer.append({
                        "panel_id": pid, "series_id": item["series_id"],
                        "name": str(item.get("name") or ""), "metric": str(item.get("metric") or ""),
                        "ref_id": str(item.get("ref_id") or ""), "query_key": item.get("query_key"),
                        "labels": sorted(labels.items()), **unit_plan[item["series_id"]],
                        **_source_extras(item),
                    })
                _write_points(point_buffer, pid, data)
                entry = {
                    **{key: value for key, value in status.items() if key != "queries"},
                    "collection_status": status["status"],
                    "range_status": coverage["status"], "display_coverage": coverage,
                    "query_keys": [query["key"] for query in panel["queries"]],
                    "selected_point_count": count,
                    "selected_null_count": nulls, "selected_series_count": len(data),
                    "selected_time_range": {"from_ms": earliest, "to_ms": latest},
                    "unsupported_unit_series_ids": [sid for sid, unit in unit_plan.items()
                                                    if unit["unit_status"] == "unsupported"],
                }
                if status["status"] in {"success", "empty"} and coverage["status"] != "covered":
                    entry["status"] = coverage["status"]
                valid_empty = not count and status["status"] in {"success", "empty"} and coverage["status"] == "covered"
                entry["export_status"] = "data" if count else "empty_selected_range" if valid_empty else "no_valid_display_data"
                manifest["panels"].append(entry)
                point_count += count
                null_count += nulls
                series_count += len(data)
            point_buffer.flush()
            series_buffer.flush()
        _write_provenance(staging / "provenance.json.gz",
                          _provenance(plan, panels, queries, generation))
        (staging / "README.md").write_text(_readme(), encoding="utf-8")
        manifest["summary"] = dict(Counter(p["status"] for p in manifest["panels"]))
        manifest["files"] = {
            "points": _file_description(staging / "points.parquet", POINTS_SCHEMA, point_count,
                                        null_count=null_count, nonnull_count=point_count - null_count),
            "series": _file_description(staging / "series.parquet", SERIES_SCHEMA, series_count),
            "provenance": _file_description(staging / "provenance.json.gz", compression="gzip"),
            "readme": _file_description(staging / "README.md"),
        }
        _write_json(staging / "manifest.json", manifest)
        for filename in ("points.parquet", "series.parquet", "provenance.json.gz", "README.md"):
            with (staging / filename).open("rb") as output:
                os.fsync(output.fileno())
        # Both manifests resolve their paths relative to their own locations.
        published = deepcopy(manifest)
        prefix = Path("exports") / generation
        published["generation_manifest"] = (prefix / "manifest.json").as_posix()
        for description in published["files"].values():
            description["path"] = (prefix / description["path"]).as_posix()
        os.replace(staging, destination)
        _write_json(temporary_manifest, published)
        final_manifest = out_dir / "manifest.json"
        os.replace(temporary_manifest, final_manifest)
        return final_manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        # A signal can arrive after publishing the outer manifest but before
        # os.replace returns to Python. Never remove a renamed generation: it
        # may already be referenced. An unreferenced completed generation is
        # harmless and preserves both old and newly published snapshots.
        temporary_manifest.unlink(missing_ok=True)
        raise
