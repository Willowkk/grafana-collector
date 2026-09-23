from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from grafana_collector import __version__, parquet_exporter
from grafana_collector.parquet_exporter import POINTS_SCHEMA, SERIES_SCHEMA, export_run
from grafana_collector.storage import Store


START = 1789369200000
END = START + 6000


def make_plan():
    panels = []
    for panel_id, title in [(1, "Through/put"), (2, "Through/put"), (3, "Failed"), (4, "Empty")]:
        query = {"key": f"q{panel_id}", "panel_id": panel_id, "ref_id": "A", "kind": "opentsdb",
                 "route": "/api/query", "query": {"metric": "test.metric"}, "interval_ms": 1000,
                 "alias": "same", "unit": "binBps", "hidden": False, "dependencies": [],
                 "metadata": {"actual_filters": {"filesystem": "fs"}}, "error": None}
        panels.append({"id": panel_id, "title": title, "group": "Overview", "queries": [query],
                       "transformations": [], "metadata": {"interval_ms": 1000}})
    return {"schema_version": 1, "source_url": "https://example.test/d/test?var-filesystem=fs",
            "dashboard": {"uid": "test", "version": 319}, "variables": {"filesystem": "fs"},
            "panels": panels, "from_ms": START, "to_ms": END, "mode": "fetch", "started_ms": START,
            "metadata": {"viewport": [1440, 900], "auto_interval_ms": 1000}}


def series(points, *, unit="binBps", host="a", **extra):
    return {"name": "same", "metric": "test.metric", "ref_id": "A", "query_key": "q1",
            "labels": {"host": host, "namespace": "9115285645797950347"}, "unit": unit,
            "points": points, **extra}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "run") as instance:
        instance.initialize(make_plan())
        yield instance


def read_export(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    tables = {key: pq.read_table(path.parent / manifest["files"][key]["path"])
              for key in ("points", "series")}
    return manifest, tables["points"], tables["series"]


def read_provenance(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    with gzip.open(path.parent / manifest["files"]["provenance"]["path"], "rt", encoding="utf-8") as stream:
        return json.load(stream)


def test_typed_long_table_uses_display_values_preserves_nulls_ids_labels_and_utc(store, tmp_path):
    samples = [series([[START + 1, 17.123456789012345], [START + 2000, None]]),
               series([[START + 3000, 0]], host="b")]
    store.record_result(make_plan()["panels"][0]["queries"][0], START, END,
                        [series([[START + 1, 999]])], {"source": "query input"})
    store.record_display(1, samples, START, END)
    store.record_display(2, [series([[START + 1, 9]])], START, END)
    before = store.get_display_series(1)
    path = export_run(store, tmp_path / "export", panel_ids=[1, 2])
    manifest, points, curves = read_export(path)
    assert points.schema == POINTS_SCHEMA
    assert curves.schema == SERIES_SCHEMA
    assert points.schema.field("time").type == pa.timestamp("ms", tz="UTC")
    assert points.column("time").cast(pa.int64()).to_pylist().count(START + 1) == 2
    timestamp = points.column("time").to_pylist()[0]
    assert timestamp.tzname() == "UTC" and timestamp.utcoffset().total_seconds() == 0
    assert datetime.fromtimestamp(START / 1000, timezone.utc).hour == 7
    rows = points.to_pylist()
    assert len(rows) == 4  # No outer join, generated minute rows, or query-input row.
    assert sorted(row["value"] for row in rows if row["value"] is not None) == [0.0, 9.0, 17.123456789012345]
    assert len({(row["panel_id"], row["series_id"], row["time"]) for row in rows}) == 4
    descriptions = curves.to_pylist()
    assert len(descriptions) == 3 and len({row["series_id"] for row in descriptions}) == 3
    assert {row["name"] for row in descriptions} == {"same"}
    assert {row["title"] for row in manifest["panels"]} == {"Through/put"}
    assert not {"group", "panel_title", "source_metadata_json"} & set(curves.column_names)
    assert all(dict(row["labels"])["namespace"] == "9115285645797950347" for row in descriptions)
    host_a = next(row for row in descriptions if row["panel_id"] == 1 and dict(row["labels"])["host"] == "a")
    a_points = [row for row in rows if row["series_id"] == host_a["series_id"]]
    assert len(a_points) == 2 and sum(row["value"] is None for row in a_points) == 1
    assert manifest["files"]["points"]["null_count"] == 1
    assert manifest["exporter"]["version"] == __version__
    assert manifest["schema_name"] == "sdkv2-grafana-parquet" and manifest["schema_version"] == 2
    provenance = read_provenance(path)
    assert "raw_path" not in provenance["queries"]["q1"]["attempts"][0]
    assert provenance["raw_provenance"] is None
    assert store.get_display_series(1) == before


def test_original_units_and_display_metadata_include_percent_program_factor(store, tmp_path):
    samples = [series([[START, 2 ** 30]], unit="binBps", host="bytes"),
               series([[START, 1.001001001]], unit="percentunit", host="ratio"),
               series([[START, 1000]], unit="µs", host="duration"),
               series([[START, 1024]], unit="bytes", host="byte-count"),
               series([[START, 1]], unit="kbytes", host="kibibyte-count")]
    store.record_display(1, samples, START, END)
    _, points, curves = read_export(export_run(store, tmp_path / "export", panel_ids=[1]))
    descriptions = {dict(row["labels"])["host"]: row for row in curves.to_pylist()}
    values = {row["series_id"]: row["value"] for row in points.to_pylist()}
    rate = descriptions["bytes"]
    assert (rate["grafana_unit"], rate["value_unit"], rate["display_unit"], rate["display_scale"]) == ("binBps", "B/s", "GiB/s", 1 / 1024 ** 3)
    assert values[rate["series_id"]] == 2 ** 30
    ratio = descriptions["ratio"]
    assert (ratio["value_unit"], ratio["display_unit"], ratio["display_scale"]) == ("ratio", "%", 100.0)
    assert values[ratio["series_id"]] == 1.001001001
    duration = descriptions["duration"]
    assert (duration["value_unit"], duration["display_unit"], duration["display_scale"]) == ("µs", "ms", 0.001)
    byte_count, kibibytes = descriptions["byte-count"], descriptions["kibibyte-count"]
    assert byte_count["display_unit"] == kibibytes["display_unit"] == "KiB"
    assert byte_count["display_scale"] == 1 / 1024 and kibibytes["display_scale"] == 1
    assert byte_count["value_unit"] == "B" and kibibytes["value_unit"] == "KiB"
    assert not {"number_format", "excel_percent", "min_column_width"} & set(curves.column_names)
    assert all(row["extra_metadata_json"] is None for row in descriptions.values())


@pytest.mark.parametrize("unit,expected", [("bytes", "B"), ("kbytes", "KiB"), ("decbytes", "B"),
                                           ("binBps", "B/s"), ("µs", "µs"), ("ms", "ms"),
                                           ("reqps", "req/s"), ("short", "1"), ("percentunit", "ratio"),
                                           ("none", "1"), ("string", None)])
def test_all_supported_source_units_are_explicit(store, tmp_path, unit, expected):
    store.record_display(1, [series([[START, 1]], unit=unit)], START, END)
    _, _, curves = read_export(export_run(store, tmp_path / "export", panel_ids=[1]))
    row = curves.to_pylist()[0]
    assert row["grafana_unit"] == unit and row["value_unit"] == expected
    assert row["unit_status"] == "configured"


def test_unknown_units_preserve_values_and_metadata_without_invented_units(store, tmp_path):
    store.record_display(1, [series([[START, 1234.5]], unit="custom:widgets")], START, END)
    manifest, points, curves = read_export(export_run(store, tmp_path / "export", panel_ids=[1]))
    row = curves.to_pylist()[0]
    assert row["grafana_unit"] == "custom:widgets" and row["unit_status"] == "unsupported"
    assert row["value_unit"] is None and row["display_unit"] is None and row["display_scale"] == 1
    assert points.column("value").to_pylist() == [1234.5]
    assert manifest["panels"][0]["unsupported_unit_series_ids"] == [row["series_id"]]


def test_empty_failed_pending_and_uncollected_are_distinct_with_typed_empty_files(store, tmp_path):
    query = make_plan()["panels"][3]["queries"][0]
    store.record_result(query, START, END, [], [], status="empty")
    store.record_display(4, [], START, END)
    store.record_panel_status(3, "failed", "HTTP 503")
    path = export_run(store, tmp_path / "export")
    manifest, points, curves = read_export(path)
    assert points.num_rows == curves.num_rows == 0
    assert points.schema == POINTS_SCHEMA and curves.schema == SERIES_SCHEMA
    panels = {p["panel_id"]: p for p in manifest["panels"]}
    assert len(panels) == 4
    assert panels[3]["status"] == "failed" and panels[3]["export_status"] == "no_valid_display_data"
    assert panels[4]["status"] == "empty" and panels[4]["range_status"] == "covered"
    assert panels[4]["export_status"] == "empty_selected_range"
    assert panels[1]["status"] == "pending" and panels[1]["range_status"] == "uncollected"
    outside, _, _ = read_export(export_run(store, tmp_path / "outside", panel_ids=[4], from_ms=END + 1))
    assert outside["panels"][0]["status"] == "uncollected"
    assert outside["panels"][0]["export_status"] == "no_valid_display_data"


def test_history_remains_exportable_after_failure_without_masking_status(store, tmp_path):
    store.record_display(1, [series([[START, 1]])], START, START + 2000)
    store.record_panel_status(1, "failed", "transform failed")
    manifest, points, _ = read_export(export_run(store, tmp_path / "export", panel_ids=[1]))
    assert points.num_rows == 1
    panel = manifest["panels"][0]
    assert panel["status"] == panel["collection_status"] == "partial"
    assert panel["range_status"] == "partial" and panel["errors"] == ["transform failed"]


def test_filters_are_inclusive_and_scale_uses_selected_window_without_changing_ids(store, tmp_path):
    store.record_display(1, [series([[START, 1024 ** 3], [START + 1000, 1], [START + 2000, None]])], START, END)
    store.record_display(2, [series([[START + 1000, 99]])], START, END)
    out = tmp_path / "export"
    first_path = export_run(store, out)
    first, _, first_series = read_export(first_path)
    first_bytes = (out / first["files"]["points"]["path"]).read_bytes()
    selected, points, curves = read_export(export_run(store, out, panel_ids=[1], from_ms=START + 1000, to_ms=START + 2000))
    assert points.column("time").cast(pa.int64()).to_pylist() == [START + 1000, START + 2000]
    assert points.column("value").to_pylist() == [1.0, None]
    assert selected["selected_panel_ids"] == [1] and len(selected["panels"]) == 1
    assert selected["panels"][0]["query_keys"] == ["q1"]
    assert set(read_provenance(first_path)["queries"]) == {"q1"}
    assert first_series.to_pylist()[0]["series_id"] == curves.to_pylist()[0]["series_id"]
    assert curves.to_pylist()[0]["display_unit"] == "B/s"
    assert first["generation"] != selected["generation"]
    assert (out / first["files"]["points"]["path"]).read_bytes() == first_bytes


def test_both_manifests_resolve_their_own_relative_paths(store, tmp_path, monkeypatch, capsys):
    store.record_display(1, [series([[START, 1]])], START, END)
    outer_path = export_run(store, tmp_path / "export")
    outer, points, curves = read_export(outer_path)
    inner_path = outer_path.parent / outer["generation_manifest"]
    inner, inner_points, inner_curves = read_export(inner_path)
    assert inner_points.equals(points) and inner_curves.equals(curves)
    assert inner["generation_manifest"] == "manifest.json"
    for key in outer["files"]:
        assert (inner_path.parent / inner["files"][key]["path"]).resolve() == (outer_path.parent / outer["files"][key]["path"]).resolve()
    readme = (inner_path.parent / "README.md").read_text()
    example = re.search(r"```python\n(.*?)```", readme, re.S).group(1)
    assert "pandas" not in example
    monkeypatch.chdir(inner_path.parent)
    exec(example, {})
    assert "same" in capsys.readouterr().out


def test_export_failure_after_generation_rename_preserves_previous_manifest(store, tmp_path, monkeypatch):
    store.record_display(1, [series([[START, 1]])], START, END)
    out = tmp_path / "export"
    path = export_run(store, out, panel_ids=[1])
    before = path.read_bytes()
    generations = set((out / "exports").iterdir())
    write_json = parquet_exporter._write_json

    def disk_full(path, value):
        if path.name.startswith(".manifest-"):
            raise OSError("disk full")
        return write_json(path, value)

    monkeypatch.setattr(parquet_exporter, "_write_json", disk_full)
    with pytest.raises(OSError, match="disk full"):
        export_run(store, out, panel_ids=[1])
    assert path.read_bytes() == before
    # The fully written but unpublished generation may remain. Deleting a
    # renamed generation is unsafe when a signal arrives just after publish.
    assert generations < set((out / "exports").iterdir())
    assert not list((out / "exports").glob(".*.tmp"))
    assert not list(out.glob(".manifest-*.tmp"))
    assert read_export(path)[1].num_rows == 1


def test_interrupt_after_manifest_replacement_cannot_remove_published_files(store, tmp_path, monkeypatch):
    store.record_display(1, [series([[START, 1]])], START, END)
    out = tmp_path / "export"
    manifest_path = export_run(store, out, panel_ids=[1])
    previous, _, _ = read_export(manifest_path)
    real_replace = parquet_exporter.os.replace

    def interrupt_after_publish(source, destination):
        real_replace(source, destination)
        if Path(destination) == manifest_path:
            raise KeyboardInterrupt()

    monkeypatch.setattr(parquet_exporter.os, "replace", interrupt_after_publish)
    with pytest.raises(KeyboardInterrupt):
        export_run(store, out, panel_ids=[1])
    current, points, curves = read_export(manifest_path)
    assert current["generation"] != previous["generation"]
    assert points.num_rows == curves.num_rows == 1
    assert (out / previous["files"]["points"]["path"]).exists()


def test_arrow_exception_is_reported_as_value_error_and_leaves_previous_export(store, tmp_path, monkeypatch):
    store.record_display(1, [series([[START, 1]])], START, END)
    out = tmp_path / "export"
    path = export_run(store, out, panel_ids=[1])
    before = path.read_bytes()

    def invalid(*args):
        raise pa.ArrowTypeError("invalid source type")

    monkeypatch.setattr(parquet_exporter, "_write_points", invalid)
    with pytest.raises(ValueError, match="Parquet export failed: invalid source type"):
        export_run(store, out, panel_ids=[1])
    assert path.read_bytes() == before
    assert not list((out / "exports").glob(".*.tmp"))


@pytest.mark.parametrize("bad_json,error", [(str(2 ** 53 + 1), "exactly"), (str(10 ** 400), "finite"),
                                            ("NaN", "Non-finite"), ("Infinity", "Non-finite"),
                                            ("true", "numeric")])
def test_unsafe_numeric_conversion_fails_without_replacing_valid_export(store, tmp_path, bad_json, error):
    store.record_display(1, [series([[START, 1]])], START, END)
    out = tmp_path / "export"
    path = export_run(store, out, panel_ids=[1])
    before = path.read_bytes()
    generations = set((out / "exports").iterdir())
    # Source Store deliberately normalizes nonfinite numbers. Inject corruption
    # here to verify that the exporter still refuses untrustworthy input.
    with store.connection:
        store.connection.execute("UPDATE points SET value_json=?", (bad_json,))
    with pytest.raises(ValueError, match=error):
        export_run(store, out, panel_ids=[1])
    assert path.read_bytes() == before and set((out / "exports").iterdir()) == generations


def test_exact_large_integer_and_all_null_curve_can_be_exported(store, tmp_path):
    store.record_display(1, [series([[START, 2 ** 60]], host="exact"),
                             series([[START, None]], host="null")], START, END)
    _, points, curves = read_export(export_run(store, tmp_path / "export", panel_ids=[1]))
    assert curves.num_rows == 2 and points.column("value").null_count == 1
    assert 2 ** 60 in points.column("value").to_pylist()


def test_snapshot_is_consistent_when_collection_changes_an_unread_panel(store, tmp_path, monkeypatch):
    store.record_display(1, [series([[START, 1]])], START, END)
    store.record_display(2, [series([[START, 2]])], START, END)
    original = parquet_exporter.build_unit_plan
    changed = False

    def collect_during_export(data):
        nonlocal changed
        if not changed:
            changed = True
            with Store(store.run_dir) as other:
                other.record_display(2, [series([[START, 999]])], START, END)
        return original(data)

    monkeypatch.setattr(parquet_exporter, "build_unit_plan", collect_during_export)
    _, points, _ = read_export(export_run(store, tmp_path / "export", panel_ids=[1, 2]))
    assert sorted(points.column("value").to_pylist()) == [1.0, 2.0]
    assert store.get_display_series(2)[0]["points"][0][1] == 999


def test_invalid_panel_or_time_selection_does_not_publish(store, tmp_path):
    out = tmp_path / "export"
    with pytest.raises(ValueError, match="Unknown panel"):
        export_run(store, out, panel_ids=[999])
    with pytest.raises(ValueError, match="end precedes start"):
        export_run(store, out, from_ms=END, to_ms=START)
    assert not (out / "manifest.json").exists()


def test_v2_keeps_unique_source_attributes_and_normalizes_shared_provenance(store, tmp_path):
    plan = make_plan()
    query = plan["panels"][0]["queries"][0]
    sample = series([[START, 5], [START + 1000, None]],
                    quality={"nonfinite_values": 1, "future_quality_flag": "retained"},
                    aggregate_tags=["filesystem", "task_id"],
                    extension={"origin": ["native", {"version": 2}]})
    store.record_result(query, START, END, [sample], {"response": "raw"})
    store.record_display(1, [sample], START, END)
    path = export_run(store, tmp_path / "export", panel_ids=[1], from_ms=START + 1000)
    manifest, points, curves = read_export(path)
    row = curves.to_pylist()[0]
    assert points["value"].to_pylist() == [None]
    assert json.loads(row["quality_json"]) == sample["quality"]
    assert row["aggregate_tags"] == sample["aggregate_tags"]
    assert json.loads(row["extra_metadata_json"]) == {"extension": sample["extension"]}
    assert "query_statuses" not in manifest
    assert "query_definitions" not in manifest["panels"][0]
    assert "series_ids" not in manifest["panels"][0]
    provenance = read_provenance(path)
    assert provenance["generation"] == manifest["generation"]
    assert provenance["dashboard"] == plan["dashboard"]
    assert provenance["sampling"] == plan["metadata"]
    assert provenance["queries"]["q1"]["definition"] == query
    assert len(provenance["queries"]["q1"]["attempts"]) == 1
    assert provenance["panels"]["1"] == {
        "query_keys": ["q1"], "metadata": plan["panels"][0]["metadata"], "transformations": []}
    # Main-manifest references resolve even when exporting only a subrange.
    assert set(manifest["panels"][0]["query_keys"]) <= provenance["queries"].keys()


def test_legacy_raw_paths_are_omitted_without_changing_query_provenance(store, tmp_path):
    plan = make_plan()
    query = plan["panels"][0]["queries"][0]
    samples = [series([[START, 5]])]
    store.record_result(query, START, END, samples, {"response": "discarded"})
    store.record_display(1, samples, START, END)
    legacy_path = "raw/legacy-response.json.gz"
    with store.connection:
        store.connection.execute("UPDATE attempts SET raw_path=?", (legacy_path,))

    path = export_run(store, tmp_path / "export", panel_ids=[1])
    manifest, points, _ = read_export(path)
    provenance = read_provenance(path)
    saved_query = provenance["queries"]["q1"]
    assert provenance["raw_provenance"] is None
    assert saved_query["definition"] == query
    assert saved_query["cursor_ms"] == END
    assert saved_query["attempts"][0]["status"] == "success"
    assert "raw_path" not in saved_query["attempts"][0]
    assert legacy_path not in json.dumps(provenance)
    assert points["value"].to_pylist() == [5.0]
    assert store.connection.execute("SELECT raw_path FROM attempts").fetchone()[0] == legacy_path
    readme = (path.parent / manifest["files"]["readme"]["path"]).read_text(encoding="utf-8")
    assert "原始 HTTP 响应不落盘" in readme
    assert "离线导出" not in readme


def test_small_panels_share_row_groups_and_preserve_all_points(store, tmp_path, monkeypatch):
    monkeypatch.setattr(parquet_exporter, "_BATCH_ROWS", 3)
    store.record_display(1, [series([[START + i, i] for i in range(4)]),
                             series([[START + 4, None]], host="b")], START, END)
    store.record_display(2, [series([[START + i, 10 + i] for i in range(4)])], START, END)
    path = export_run(store, tmp_path / "export", panel_ids=[1, 2])
    manifest, points, curves = read_export(path)
    assert points.num_rows == 9 and curves.num_rows == 3
    metadata = pq.ParquetFile(path.parent / manifest["files"]["points"]["path"]).metadata
    assert [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)] == [3, 3, 3]
    series_metadata = pq.ParquetFile(path.parent / manifest["files"]["series"]["path"]).metadata
    assert series_metadata.num_row_groups == 1
    assert set(points["panel_id"].to_pylist()) == {1, 2}
    assert points["value"].null_count == 1
    assert sorted(x for x in points["value"].to_pylist() if x is not None) == [0, 1, 2, 3, 10, 11, 12, 13]


def test_provenance_failure_cannot_publish_incomplete_package(store, tmp_path, monkeypatch):
    store.record_display(1, [series([[START, 1]])], START, END)
    out = tmp_path / "export"
    path = export_run(store, out, panel_ids=[1])
    previous = path.read_bytes()
    generations = set((out / "exports").iterdir())

    def fail_provenance(path, value):
        path.write_bytes(b"incomplete gzip")
        raise OSError("provenance write failed")

    monkeypatch.setattr(parquet_exporter, "_write_provenance", fail_provenance)
    with pytest.raises(OSError, match="provenance write failed"):
        export_run(store, out, panel_ids=[1])
    assert path.read_bytes() == previous
    assert set((out / "exports").iterdir()) == generations
    assert not list(out.glob(".manifest-*.tmp"))
    assert read_provenance(path)["schema_version"] == 2


def test_invalid_aggregate_tags_fails_instead_of_discarding_source_metadata(store, tmp_path):
    store.record_display(1, [series([[START, 1]], aggregate_tags=[1])], START, END)
    out = tmp_path / "export"
    with pytest.raises(ValueError, match="aggregate_tags"):
        export_run(store, out, panel_ids=[1])
    assert not (out / "manifest.json").exists()


def test_local_math_definition_remains_resolvable_without_network_attempts(tmp_path):
    plan = make_plan()
    formula = {**plan["panels"][0]["queries"][0], "key": "formula", "ref_id": "B",
               "kind": "math", "dependencies": ["A"], "query": {"expression": "$A * 2"}}
    plan["panels"][0]["queries"].append(formula)
    with Store(tmp_path / "math-run") as store:
        store.initialize(plan)
        path = export_run(store, tmp_path / "export", panel_ids=[1])
    manifest = json.loads(path.read_text())
    provenance = read_provenance(path)
    assert manifest["panels"][0]["query_keys"] == ["q1", "formula"]
    assert provenance["queries"]["formula"] == {"definition": formula}
