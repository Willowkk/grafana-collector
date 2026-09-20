"""Offline readers for version 2 Grafana Parquet snapshots.

Ordinary point reads only need the manifest and two Parquet files. Detailed
provenance is loaded explicitly. Other schema versions are rejected.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq


SCHEMA_NAME = "sdkv2-grafana-parquet"


def _load_manifest(path):
    path = Path(path)
    manifest_path = path / "manifest.json" if path.is_dir() else path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_name") != SCHEMA_NAME or
            type(manifest.get("schema_version")) is not int or
            manifest["schema_version"] != 2 or manifest.get("format") != "parquet"):
        raise ValueError("Expected sdkv2-grafana-parquet schema version 2")
    return manifest, manifest_path.parent


def _normalize_series(item, panel):
    row = {**item, "labels": dict(item["labels"] or [])}
    row["panel_title"] = panel.get("title", "")
    row["group"] = panel.get("group", "")
    row["quality"] = json.loads(item["quality_json"]) if item.get("quality_json") is not None else None
    row["aggregate_tags"] = item.get("aggregate_tags")
    row["extra_metadata"] = json.loads(item["extra_metadata_json"]) if item.get("extra_metadata_json") is not None else {}
    return row


def read_points(path, *, panel_id=None, tags=None, from_ms=None, to_ms=None):
    """Return ``(Arrow Table, metadata by series_id, manifest)`` for v2.

    Panel/tag filters select curves; time boundaries are inclusive milliseconds
    since the Unix epoch. Metadata includes all selected curves even if the
    requested time range contains no point for one of them. Rows have decoded
    ``labels``, ``quality``, ``aggregate_tags`` and ``extra_metadata``, plus
    ``panel_title``/``group`` resolved from the manifest. Check panel coverage
    and collection status before treating a dataset as complete history.

    Paths resolve against the supplied manifest, so run-level manifests and
    copied, self-contained generation directories both work. This operation
    does not read the potentially larger provenance sidecar.
    """
    manifest, base = _load_manifest(path)
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise ValueError("Start time must not be after end time")
    panels = {panel["panel_id"]: panel for panel in manifest["panels"]}
    series = pq.read_table(base / manifest["files"]["series"]["path"])
    selected = {}
    for item in series.to_pylist():
        if panel_id is not None and item["panel_id"] != panel_id:
            continue
        if item["panel_id"] not in panels:
            raise ValueError(f"Series refers to missing panel {item['panel_id']}")
        row = _normalize_series(item, panels[item["panel_id"]])
        if not all(row["labels"].get(key) == value for key, value in (tags or {}).items()):
            continue
        if item["series_id"] in selected:
            raise ValueError(f"Duplicate series_id: {item['series_id']}")
        selected[item["series_id"]] = row

    condition = ds.field("series_id").isin(pa.array(list(selected), type=pa.string()))
    if panel_id is not None:
        condition = condition & (ds.field("panel_id") == panel_id)
    for boundary, is_start in ((from_ms, True), (to_ms, False)):
        if boundary is not None:
            timestamp = pa.scalar(boundary, type=pa.timestamp("ms", tz="UTC"))
            condition = condition & ((ds.field("time") >= timestamp) if is_start else (ds.field("time") <= timestamp))
    points = ds.dataset(base / manifest["files"]["points"]["path"], format="parquet").to_table(filter=condition)
    return points, selected, manifest


def read_provenance(path):
    """Return full dashboard/sampling and query/panel dictionaries for v2.

    Queries are keyed by query key and contain a full ``definition`` plus any
    saved status/attempts. Panels are keyed by the decimal panel ID string and
    contain ``metadata``, ``transformations``, and ordered ``query_keys``.
    Reads ``files.provenance`` relative to either manifest location, verifies
    its checksum when provided, and rejects a different schema or generation.
    Raw response paths are source references; reading provenance does not
    require the original run or raw response files to remain present.
    """
    manifest, base = _load_manifest(path)
    info = manifest["files"]["provenance"]
    encoded = (base / info["path"]).read_bytes()
    if info.get("sha256") and hashlib.sha256(encoded).hexdigest() != info["sha256"]:
        raise ValueError("Provenance checksum mismatch")
    if info.get("size_bytes") is not None and len(encoded) != info["size_bytes"]:
        raise ValueError("Provenance size mismatch")
    provenance = json.loads(gzip.decompress(encoded))
    if (provenance.get("schema_name") != SCHEMA_NAME or
            type(provenance.get("schema_version")) is not int or provenance["schema_version"] != 2):
        raise ValueError("Unexpected provenance schema version")
    if provenance.get("generation") != manifest.get("generation"):
        raise ValueError("Provenance generation mismatch")
    return {**provenance, "variables": manifest.get("variables", {})}
