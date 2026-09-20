"""Read one exported snapshot without Chrome, Pandas or a collector session.

python examples/read_parquet.py runs/history --panel 166 --limit 5
python examples/read_parquet.py path/to/export/manifest.json --tag method=cfs_pread
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from grafana_collector.timeutil import parse_time


def read_points(path, *, panel_id=None, tags=None, from_ms=None, to_ms=None):
    """Return (Arrow Table, metadata by series_id, manifest) for selected points.

    Files are resolved against the manifest, so both a run directory's outer
    manifest and a copied generation's self-contained manifest work unchanged.
    Check manifest panel coverage/status before using data as complete history.
    """
    path = Path(path)
    manifest_path = path / "manifest.json" if path.is_dir() else path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_name"), manifest.get("schema_version"), manifest.get("format")) != (
        "sdkv2-grafana-parquet", 1, "parquet"
    ):
        raise ValueError("Expected sdkv2-grafana-parquet schema version 1")
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise ValueError("Start time must not be after end time")
    base = manifest_path.parent
    series = pq.read_table(base / manifest["files"]["series"]["path"])
    selected = {}
    for item in series.to_pylist():
        if panel_id is not None and item["panel_id"] != panel_id:
            continue
        labels = dict(item["labels"] or [])
        if not all(labels.get(key) == value for key, value in (tags or {}).items()):
            continue
        selected[item["series_id"]] = {**item, "labels": labels}

    condition = ds.field("series_id").isin(pa.array(list(selected), type=pa.string()))
    if panel_id is not None:
        condition = condition & (ds.field("panel_id") == panel_id)
    for boundary, is_start in ((from_ms, True), (to_ms, False)):
        if boundary is None:
            continue
        timestamp = pa.scalar(boundary, type=pa.timestamp("ms", tz="UTC"))
        condition = condition & ((ds.field("time") >= timestamp) if is_start else (ds.field("time") <= timestamp))
    points = ds.dataset(base / manifest["files"]["points"]["path"], format="parquet").to_table(filter=condition)
    return points, selected, manifest


def main():
    parser = argparse.ArgumentParser(description="按面板、完整标签及时间筛选 Parquet；打印少量示例行")
    parser.add_argument("dataset", type=Path, help="包含 manifest.json 的目录，或 manifest.json 文件")
    parser.add_argument("--panel", type=int)
    parser.add_argument("--tag", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--from", dest="from_value", help="开始时间；无时区时按北京时间")
    parser.add_argument("--to", dest="to_value", help="结束时间；无时区时按北京时间")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    tags = {}
    for tag in args.tag:
        key, separator, value = tag.partition("=")
        if not separator or not key:
            parser.error("--tag 必须是 KEY=VALUE")
        tags[key] = value
    if args.limit < 0:
        parser.error("--limit 不能为负数")
    points, series, manifest = read_points(
        args.dataset, panel_id=args.panel, tags=tags,
        from_ms=parse_time(args.from_value) if args.from_value else None,
        to_ms=parse_time(args.to_value) if args.to_value else None,
    )
    statuses = [{key: panel.get(key) for key in ("panel_id", "title", "status", "collection_status", "range_status")}
                for panel in manifest["panels"] if args.panel is None or panel["panel_id"] == args.panel]
    print(json.dumps({"rows": points.num_rows, "null_values": points["value"].null_count,
                      "series": len(series), "panels": statuses}, ensure_ascii=False))
    for point in points.slice(0, args.limit).to_pylist():
        metadata = series[point["series_id"]]
        value, scale = point["value"], metadata["display_scale"]
        print(json.dumps({**point, "time": point["time"].isoformat(),
                          "value_unit": metadata["value_unit"],
                          "display_value": value * scale if value is not None and scale is not None else None,
                          "display_unit": metadata["display_unit"], "labels": metadata["labels"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
