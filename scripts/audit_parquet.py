"""Independently compare a Parquet export against its original SQLite values.

python scripts/audit_parquet.py --run runs/history --manifest runs/history/manifest.json
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import struct

import pyarrow as pa
import pyarrow.parquet as pq


def audit(run, manifest_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    failures = []
    def check(condition, message):
        if not condition:
            failures.append(message)

    check(manifest.get("schema_name") == "sdkv2-grafana-parquet", "unexpected schema name")
    check(manifest.get("schema_version") == 1, "unexpected schema version")
    for name, info in manifest["files"].items():
        path = base / info["path"]
        check(path.is_file(), f"missing file: {name}")
        if path.is_file() and "sha256" in info:
            check(hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"], f"checksum mismatch: {name}")

    points_path = base / manifest["files"]["points"]["path"]
    points = pq.read_table(points_path)
    curves = pq.read_table(base / manifest["files"]["series"]["path"])
    check(points.schema.names == ["time", "panel_id", "series_id", "value"], "points field names")
    check(points["time"].type == pa.timestamp("ms", tz="UTC"), "timestamp type/timezone")
    check(points["value"].type == pa.float64(), "value must be float64")
    check(points.num_rows == manifest["files"]["points"]["row_count"], "manifest point count")
    check(curves.num_rows == manifest["files"]["series"]["row_count"], "manifest series count")
    check(points["value"].null_count == manifest["files"]["points"]["null_count"], "manifest null count")
    parquet = pq.ParquetFile(points_path)
    compression = {parquet.metadata.row_group(i).column(j).compression
                   for i in range(parquet.metadata.num_row_groups)
                   for j in range(parquet.metadata.num_columns)}
    check(compression <= {"ZSTD"}, "unexpected Parquet compression")

    panel_ids = {p["panel_id"] for p in manifest["panels"]}
    check(len(panel_ids) == len(manifest["panels"]), "duplicate panel metadata")
    interval = manifest["requested_range"]
    expected = {}
    descriptions = {}
    source_count = Counter()
    db = Path(run).resolve() / "collection.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        plan = json.loads(connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()[0])
        check(manifest["variables"] == plan["variables"], "frozen variables changed")
        check(manifest["dashboard"] == plan["dashboard"], "frozen dashboard changed")
        check(panel_ids <= {p["id"] for p in plan["panels"]}, "unknown selected panel")
        for sid, owner, description, timestamp, encoded in connection.execute(
            "SELECT s.id,s.owner_key,s.description,p.timestamp_ms,p.value_json "
            "FROM series s JOIN points p ON s.id=p.series_id WHERE s.owner_kind='display'"
        ):
            panel = int(owner)
            if panel not in panel_ids:
                continue
            if interval["from_ms"] is not None and timestamp < interval["from_ms"]:
                continue
            if interval["to_ms"] is not None and timestamp > interval["to_ms"]:
                continue
            expected[(panel, sid, timestamp)] = json.loads(encoded)
            descriptions[sid] = json.loads(description)
            source_count[panel] += 1

    source_rows = len(expected)
    series_rows = curves.to_pylist()
    check(len({row["series_id"] for row in series_rows}) == len(series_rows), "duplicate series metadata")
    check({row["series_id"] for row in series_rows} == set(descriptions), "series metadata does not match actual points")
    units = Counter()
    panels = {p["id"]: p for p in plan["panels"]}
    for row in series_rows:
        description = descriptions.get(row["series_id"])
        if description is None:
            continue
        check(dict(row["labels"]) == description.get("labels", {}), "full labels changed")
        check(row["grafana_unit"] == description.get("unit"), "Grafana unit changed")
        check(row["panel_title"] == panels[row["panel_id"]]["title"], "panel title changed")
        check(row["group"] == panels[row["panel_id"]].get("group", ""), "panel group changed")
        units[row["grafana_unit"]] += 1
        if row["grafana_unit"] == "percentunit":
            check(row["value_unit"] == "ratio" and row["display_unit"] == "%" and row["display_scale"] == 100,
                  "incorrect ratio display conversion")
        if row["grafana_unit"] == "binBps":
            check(row["value_unit"] == "B/s", "incorrect throughput source unit")

    counts = Counter()
    seen = set()
    for timestamp, panel, sid, actual in zip(points["time"].cast(pa.int64()).to_pylist(),
                                           points["panel_id"].to_pylist(),
                                           points["series_id"].to_pylist(), points["value"].to_pylist()):
        key = (panel, sid, timestamp)
        check(key not in seen, "duplicate point key")
        seen.add(key)
        if key not in expected:
            failures.append("export invented a point or timestamp")
            continue
        value = expected.pop(key)
        check((actual is None) == (value is None), "null placement changed")
        if value is not None and actual is not None:
            check(struct.pack("!d", actual) == struct.pack("!d", float(value)), "numeric bits changed")
            if isinstance(value, int):
                check(actual == value, "integer precision lost")
        counts[panel] += 1
    check(not expected, "export omitted source points")
    for panel in manifest["panels"]:
        check(panel["selected_point_count"] == source_count[panel["panel_id"]], "panel point count differs")
    return {
        "pass": not failures,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "generation": manifest["generation"],
        "source_run": str(Path(run).resolve()),
        "manifest": str(manifest_path),
        "panels": len(panel_ids), "series": curves.num_rows,
        "source_points": source_rows, "exported_points": points.num_rows,
        "numeric_points": points.num_rows - points["value"].null_count,
        "explicit_nulls": points["value"].null_count,
        "panel_statuses": dict(Counter(p["status"] for p in manifest["panels"])),
        "units": dict(units), "compression": sorted(compression),
        "failure_count": len(failures), "failures": sorted(set(failures)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = audit(args.run, args.manifest)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    raise SystemExit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
