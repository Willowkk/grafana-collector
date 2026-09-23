import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pyarrow as pa
import pytest

from grafana_collector.dataset import read_points, read_provenance
from grafana_collector.parquet_exporter import export_run
from grafana_collector.storage import Store


ROOT = Path(__file__).resolve().parents[1]
START = 1789369200000


@pytest.fixture
def snapshot(tmp_path):
    query = {"key": "q1", "panel_id": 1, "ref_id": "A", "kind": "opentsdb", "route": "/api/query",
             "query": {"metric": "sample"}, "interval_ms": 1000, "hidden": False, "error": None,
             "dependencies": [], "unit": "binBps", "metadata": {"filters": {"filesystem": "test"}}}
    math = {**query, "key": "q2", "ref_id": "B", "kind": "math", "query": "$A * 2", "dependencies": ["A"]}
    plan = {
        "source_url": "https://example.test/d/fixture", "dashboard": {"uid": "fixture", "version": 7,
                                                                     "raw": {"full": "dashboard"}},
        "variables": {"filesystem": "test"}, "metadata": {"viewport": [1440, 900], "full_sampling": {"step": 1000}},
        "from_ms": START, "to_ms": START + 3000, "mode": "fetch",
        "panels": [{"id": 1, "title": "Throughput", "group": "Overview", "queries": [query, math],
                    "metadata": {"width_pixels": 700}, "transformations": [{"id": "fixture"}]},
                   {"id": 2, "title": "Another panel", "group": "Other", "queries": [], "metadata": {}}],
    }
    samples = [
        {"name": "same", "metric": "sample", "ref_id": "A", "query_key": "q1", "unit": "binBps",
         "labels": {"host": "a", "namespace": "9115285645797950347"}, "quality": {"nonfinite": 1},
         "aggregate_tags": ["task_id"], "custom": {"source": "preserve me"},
         "points": [[START, 17.123456789012345], [START + 1000, None], [START + 2000, 0]]},
        {"name": "same", "metric": "sample", "ref_id": "A", "query_key": "q1", "unit": "binBps",
         "labels": {"host": "b"}, "quality": None, "aggregate_tags": [], "points": [[START + 1000, 5]]},
    ]
    run = tmp_path / "run"
    with Store(run) as store:
        store.initialize(plan)
        store.record_result(query, START, START + 3000, samples, {"raw": "response"})
        store.record_display(1, samples, START, START + 3000)
        store.record_display(2, [{"name": "other", "metric": "other", "ref_id": "C", "labels": {},
                                  "unit": "percentunit", "points": [[START, 0.5]]}], START, START + 3000)
        manifest = export_run(store, tmp_path / "export")
    return run, manifest, plan


def test_read_shipped_v2_sample_and_provenance():
    sample = ROOT / "samples/parquet_v2_20260914_1500_2100"
    points, series, manifest = read_points(sample)
    assert manifest["schema_version"] == 2
    assert (points.num_rows, points["value"].null_count, len(series)) == (206492, 8684, 4053)
    panels = {panel["panel_id"]: panel for panel in manifest["panels"]}
    for row in series.values():
        assert "source_metadata_json" not in row
        assert isinstance(row["labels"], dict)
        assert row["quality"] == (json.loads(row["quality_json"]) if row["quality_json"] else None)
        assert row["panel_title"] == panels[row["panel_id"]]["title"]
    provenance = read_provenance(sample)
    expected = {key for panel in manifest["panels"] for key in panel["query_keys"]}
    assert provenance["schema_version"] == 2
    assert set(provenance["queries"]) == expected
    assert provenance["dashboard"]["uid"] == manifest["dashboard"]["uid"]
    assert all(provenance["sampling"][key] == value for key, value in manifest["sampling"].items())
    # Historical v2 packages remain readable with their original raw references.
    assert provenance["raw_provenance"]["paths_in"] == "queries[query_key].attempts[].raw_path"


def test_v2_filters_keep_nulls_exact_labels_and_normalized_metadata(snapshot):
    _, path, _ = snapshot
    points, series, manifest = read_points(path, panel_id=1, tags={"host": "a"},
                                         from_ms=START + 1000, to_ms=START + 2000)
    assert manifest["schema_version"] == 2
    assert points["time"].type == pa.timestamp("ms", tz="UTC")
    assert points["time"].cast(pa.int64()).to_pylist() == [START + 1000, START + 2000]
    assert points["value"].to_pylist() == [None, 0.0]
    assert len(series) == 1
    row = next(iter(series.values()))
    assert row["labels"] == {"host": "a", "namespace": "9115285645797950347"}
    assert row["panel_title"] == "Throughput" and row["group"] == "Overview"
    assert row["quality"] == {"nonfinite": 1} and row["aggregate_tags"] == ["task_id"]
    assert row["extra_metadata"] == {"custom": {"source": "preserve me"}}
    assert (row["value_unit"], row["grafana_unit"]) == ("B/s", "binBps")
    _, second, _ = read_points(path, tags={"host": "b"})
    assert next(iter(second.values()))["aggregate_tags"] == []
    assert next(iter(second.values()))["quality"] is None


def test_no_matches_and_invalid_time_range(snapshot):
    _, path, _ = snapshot
    for filters in ({"panel_id": 999}, {"tags": {"host": "unknown"}}):
        points, series, _ = read_points(path, **filters)
        assert points.num_rows == 0 and series == {}
        assert points["value"].type == pa.float64()
    points, series, _ = read_points(path, from_ms=START + 10000)
    assert points.num_rows == 0 and len(series) == 3
    with pytest.raises(ValueError, match="Start time"):
        read_points(path, from_ms=START + 1, to_ms=START)


def test_copied_generation_keeps_full_provenance_without_original_run(snapshot, tmp_path):
    run, path, plan = snapshot
    original_points, original_series, manifest = read_points(path)
    original_provenance = read_provenance(path)
    assert original_provenance["dashboard"] == plan["dashboard"]
    assert original_provenance["sampling"] == plan["metadata"]
    assert original_provenance["variables"] == plan["variables"]
    assert original_provenance["queries"]["q2"]["definition"] == plan["panels"][0]["queries"][1]
    assert original_provenance["raw_provenance"] is None
    assert all("raw_path" not in attempt
               for query in original_provenance["queries"].values()
               for attempt in query.get("attempts", []))
    copied = tmp_path / "delivered"
    shutil.copytree((path.parent / manifest["generation_manifest"]).parent, copied)
    shutil.rmtree(path.parent)
    shutil.rmtree(run)
    points, series, inner = read_points(copied)
    assert points.equals(original_points) and series == original_series
    assert inner["generation"] == manifest["generation"]
    assert read_provenance(copied) == original_provenance


def test_points_read_does_not_open_provenance_and_provenance_verifies_hash(snapshot):
    _, path, _ = snapshot
    manifest = json.loads(path.read_text())
    sidecar = path.parent / manifest["files"]["provenance"]["path"]
    sidecar.write_bytes(sidecar.read_bytes() + b"tampered")
    assert read_points(path)[0].num_rows == 5
    with pytest.raises(ValueError, match="checksum"):
        read_provenance(path)


@pytest.mark.parametrize("field,value,error", [("generation", "another", "generation"),
                                               ("schema_version", 3, "schema"),
                                               ("schema_name", "other", "schema")])
def test_provenance_rejects_wrong_identity_even_with_correct_hash(snapshot, field, value, error):
    _, path, _ = snapshot
    manifest = json.loads(path.read_text())
    info = manifest["files"]["provenance"]
    sidecar = path.parent / info["path"]
    provenance = json.loads(gzip.decompress(sidecar.read_bytes()))
    provenance[field] = value
    encoded = gzip.compress(json.dumps(provenance).encode())
    sidecar.write_bytes(encoded)
    info.update(sha256=hashlib.sha256(encoded).hexdigest(), size_bytes=len(encoded))
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=error):
        read_provenance(path)


@pytest.mark.parametrize("version", [0, 1, 3, "2", True])
def test_unsupported_manifest_versions_fail_explicitly(tmp_path, version):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema_name": "sdkv2-grafana-parquet", "schema_version": version, "format": "parquet",
    }))
    for read in (read_points, read_provenance):
        with pytest.raises(ValueError, match="schema version 2"):
            read(tmp_path)


@pytest.mark.parametrize("legacy_raw_provenance", [False, True])
def test_sqlite_audit_checks_values_and_full_metadata(snapshot, legacy_raw_provenance):
    run, path, _ = snapshot
    legacy_path = "raw/legacy-response.json.gz"
    with Store(run) as store, store.connection:
        store.connection.execute("UPDATE attempts SET raw_path=?", (legacy_path,))
    if legacy_raw_provenance:
        manifest = json.loads(path.read_text())
        info = manifest["files"]["provenance"]
        sidecar = path.parent / info["path"]
        provenance = json.loads(gzip.decompress(sidecar.read_bytes()))
        provenance["raw_provenance"] = {
            "base_directory": str(run), "format": "gzip JSON",
            "paths_in": "queries[query_key].attempts[].raw_path", "point_to_attempt_mapping": False,
        }
        for query in provenance["queries"].values():
            for attempt in query.get("attempts", []):
                attempt["raw_path"] = legacy_path
        encoded = gzip.compress(json.dumps(provenance).encode())
        sidecar.write_bytes(encoded)
        info.update(sha256=hashlib.sha256(encoded).hexdigest(), size_bytes=len(encoded))
        path.write_text(json.dumps(manifest))
    spec = importlib.util.spec_from_file_location("audit_parquet", ROOT / "scripts/audit_parquet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.audit(run, path)
    assert result["pass"], result["failures"]
    assert result["source_points"] == result["exported_points"] == 5
    assert result["explicit_nulls"] == 1
    assert result["point_row_groups"] == result["series_row_groups"] == 1
    if legacy_raw_provenance:
        with Store(run) as store, store.connection:
            store.connection.execute("UPDATE attempts SET raw_path='raw/changed.json.gz'")
        assert "query attempts changed: q1" in module.audit(run, path)["failures"]
