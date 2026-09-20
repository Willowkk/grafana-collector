"""Read one exported snapshot without Chrome, Pandas or a collector session.

python examples/read_parquet.py runs/history --panel 166 --limit 5
python examples/read_parquet.py path/to/export/manifest.json --tag method=cfs_pread
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from grafana_collector.dataset import read_points
from grafana_collector.timeutil import parse_time


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
