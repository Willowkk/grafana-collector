import gzip
import json

import pytest

from grafana_collector.storage import Store


def query(key="q1", panel_id=1, ref_id="A", **changes):
    return {"key": key, "panel_id": panel_id, "ref_id": ref_id, "kind": "opentsdb",
            "route": "/api/query", "query": {"metric": "example"}, "interval_ms": 1000,
            "alias": "same", "unit": "bytes", "hidden": False, "dependencies": [],
            "metadata": {}, "error": None, **changes}


def plan(queries=None):
    queries = queries or [query()]
    return {"schema_version": 1, "source_url": "https://example.test/d/test?var-filesystem=test",
            "dashboard": {"uid": "test", "version": 42}, "variables": {"filesystem": "test"},
            "panels": [{"id": 1, "title": "Throughput", "group": "Overview", "queries": queries,
                        "transformations": [], "metadata": {}}],
            "from_ms": 1000, "to_ms": 10_000, "mode": "fetch", "started_ms": 11_000,
            "metadata": {"timezone": "Asia/Shanghai"}}


def series(points, labels=None, **changes):
    return {"ref_id": "A", "metric": "example", "labels": labels or {"host": "a"},
            "name": "same", "unit": "bytes", "points": points, **changes}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "run") as instance:
        instance.initialize(plan())
        yield instance


def test_success_coverage_does_not_skip_gap_and_resume(store):
    store.record_result(query(), 3000, 5000, [], [], status="empty")
    assert store.get_cursor("q1") is None
    store.record_result(query(), 1000, 2000, [series([[1000, 0]])], [])
    assert store.get_cursor("q1") == 2000
    store.record_result(query(), 2000, 3000, [], [], status="empty")
    assert store.get_cursor("q1") == 5000
    with Store(store.run_dir) as resumed:
        assert resumed.load_plan() == plan()
        assert resumed.get_cursor("q1") == 5000


def test_late_update_identity_all_labels_and_out_of_order_observation(store):
    store.record_result(query(), 1000, 4000, [series([[0, 99], [1000, 1], [2000, 2], [5000, 99]])], [], observed_ms=10)
    store.record_result(query(), 1000, 4000,
                        [series([[1000, 1], [2000, 3]], name="renamed"),
                         series([[2000, 7]], {"host": "a", "rack": "b"})], [], observed_ms=20)
    store.record_result(query(), 1000, 4000, [series([[2000, -1]])], [], observed_ms=15)
    data = store.get_query_series("q1")
    assert len(data) == 2
    by_labels = {tuple(sorted(s["labels"].items())): s for s in data}
    assert by_labels[(("host", "a"),)]["points"] == [[1000, 1], [2000, 3]]
    assert by_labels[(("host", "a"), ("rack", "b"))]["points"] == [[2000, 7]]
    assert store.query_statuses()[0]["attempts"][0]["quality"]["out_of_range_points"] == 2


def test_failed_attempt_never_advances_or_destroys_data(store):
    store.record_result(query(), 1000, 2000, [series([[1000, 7]])], [])
    store.record_result(query(), 2000, 9000, [], {"message": "unavailable"}, status="failed", error="HTTP 503")
    assert store.get_cursor("q1") == 2000
    assert store.get_query_series("q1")[0]["points"] == [[1000, 7]]
    status = store.query_statuses()[0]
    assert status["status"] == "failed"
    assert status["last_error"] == "HTTP 503"


def test_success_replaces_topk_window_and_empty_clears_only_queried_range(store):
    store.record_result(query(), 1000, 3000, [series([[1000, 1], [2000, 2], [3000, 3]], {"host": "a"})], [])
    store.record_result(query(), 2000, 4000, [series([[2000, 9], [3000, 10]], {"host": "b"})], [])
    by_host = {s["labels"]["host"]: s["points"] for s in store.get_query_series("q1")}
    assert by_host == {"a": [[1000, 1]], "b": [[2000, 9], [3000, 10]]}
    store.record_result(query(), 2000, 5000, [], [], status="empty")
    assert store.get_query_series("q1")[0]["points"] == [[1000, 1]]
    assert store.get_query_series("q1", 2000, 5000) == []
    assert store.get_cursor("q1") == 5000


def test_query_points_attempt_cursor_are_transactional(store):
    store.record_result(query(), 1000, 2000, [series([[1000, 1]])], [])
    statuses_before = store.query_statuses()
    series_before = store.get_query_series("q1")
    with pytest.raises(ValueError, match="numbers"):
        store.record_result(query(), 1000, 5000,
                            [series([[1000, 88]]), series([[2000, "broken"]], {"host": "new"})],
                            {"test": "raw"})
    assert not (store.run_dir / "raw").exists()
    assert store.get_cursor("q1") == 2000
    assert store.query_statuses() == statuses_before
    assert store.get_query_series("q1") == series_before
    assert store.connection.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 1


def test_query_result_rolls_back_points_attempt_and_cursor_on_storage_failure(store, monkeypatch):
    store.record_result(query(), 1000, 2000, [series([[1000, 1]])], [])
    statuses_before = store.query_statuses()
    series_before = store.get_query_series("q1")
    advance_cursor = store._advance_cursor

    def fail_after_cursor_update(query_key, dataset_start):
        advance_cursor(query_key, dataset_start)
        raise RuntimeError("Simulated storage failure")

    monkeypatch.setattr(store, "_advance_cursor", fail_after_cursor_update)
    with pytest.raises(RuntimeError, match="storage failure"):
        store.record_result(query(), 1000, 5000, [series([[1000, 88], [2000, 99]])],
                            {"test": "raw"})
    assert store.get_cursor("q1") == 2000
    assert store.query_statuses() == statuses_before
    assert store.get_query_series("q1") == series_before


def test_raw_response_is_not_persisted_and_nonfinite_quality_is_preserved(store):
    assert not (store.run_dir / "raw").exists()
    store.record_result(query(), 1000, 3000, [series([[1000, float("nan")], [2000, 0]])],
                        {"headers": {"Authorization": "response-secret", "Cookie": "response-session"},
                         "data": "UNPERSISTED_RESPONSE_PAYLOAD"})
    attempt = store.query_statuses()[0]["attempts"][0]
    assert "raw_path" not in attempt
    assert store.connection.execute("SELECT raw_path FROM attempts").fetchone()[0] is None
    assert not (store.run_dir / "raw").exists()
    assert {path.name for path in store.run_dir.iterdir()} <= {
        "collection.sqlite3", "collection.sqlite3-wal", "collection.sqlite3-shm"}
    for path in store.run_dir.iterdir():
        persisted = path.read_bytes()
        for marker in (b"UNPERSISTED_RESPONSE_PAYLOAD", b"response-secret", b"response-session"):
            assert marker not in persisted
    assert attempt["quality"]["nonfinite_points"] == 1
    assert store.get_query_series("q1")[0]["points"] == [[1000, None], [2000, 0]]


def test_legacy_raw_files_and_database_paths_are_untouched_but_not_advertised(tmp_path):
    run_dir = tmp_path / "legacy-run"
    raw_file = run_dir / "raw" / "legacy.json.gz"
    with Store(run_dir) as store:
        store.initialize(plan())
        store.record_result(query(), 1000, 2000, [series([[1000, 1]])], None)
        raw_file.parent.mkdir()
        with gzip.open(raw_file, "wt", encoding="utf-8") as output:
            json.dump({"legacy_response": True}, output)
        legacy_bytes = raw_file.read_bytes()
        with store.connection:
            store.connection.execute("UPDATE attempts SET raw_path=?", ("raw/legacy.json.gz",))
    with Store(run_dir) as resumed:
        resumed.initialize(plan())
        assert resumed.get_cursor("q1") == 2000
        resumed.record_result(query(), 2000, 3000, [series([[2000, 2]])], {"new_response": True})
        assert resumed.get_cursor("q1") == 3000
        assert resumed.get_query_series("q1")[0]["points"] == [[1000, 1], [2000, 2]]
        assert all("raw_path" not in attempt for attempt in resumed.query_statuses()[0]["attempts"])
        assert [row[0] for row in resumed.connection.execute("SELECT raw_path FROM attempts ORDER BY id")] == [
            "raw/legacy.json.gz", None]
        assert list(raw_file.parent.iterdir()) == [raw_file]
        assert raw_file.read_bytes() == legacy_bytes


def test_display_refresh_replaces_range_and_preserves_other_history(store):
    store.record_display(1, [series([[1000, 1], [2000, 2], [3000, 3], [5000, 5]])], 1000, 5000)
    store.record_display(1, [series([[2000, 20]])], 2000, 4000)
    assert store.get_display_series(1)[0]["points"] == [[1000, 1], [2000, 20], [5000, 5]]
    assert store.get_display_series(1, 1500, 2500)[0]["points"] == [[2000, 20]]
    store.record_display(1, [], 1000, 5000)
    assert store.get_display_series(1) == []
    assert store.panel_statuses()[0]["status"] == "empty"


def test_display_refresh_rolls_back_on_invalid_new_data(store):
    store.record_display(1, [series([[1000, 1]])], 1000, 2000)
    with pytest.raises(ValueError):
        store.record_display(1, [series([[1000, "not numeric"]])], 1000, 2000)
    assert store.get_display_series(1)[0]["points"] == [[1000, 1]]


def test_latest_empty_does_not_mislabel_accumulated_historical_data(store):
    store.record_display(1, [series([[1000, 1]])], 1000, 2000)
    store.record_display(1, [], 2000, 4000)
    status = store.panel_statuses()[0]
    assert status["status"] == "success"
    assert status["last_display_status"] == "empty"
    assert status["point_count"] == 1


def test_derived_failure_is_explicit_and_retains_prior_display(store):
    store.record_result(query(), 1000, 2000, [series([[1000, 1]])], [])
    store.record_panel_status(1, "unsupported", "Unknown transformation")
    assert store.panel_statuses()[0]["status"] == "unsupported"
    store.record_display(1, [series([[1000, 1]])], 1000, 2000)
    store.record_panel_status(1, "failed", "Division dependency unavailable")
    status = store.panel_statuses()[0]
    assert status["status"] == "partial"
    assert status["errors"] == ["Division dependency unavailable"]
    assert status["point_count"] == 1


def test_frozen_plan_rejects_mutations_and_is_copied(store):
    loaded = store.load_plan()
    loaded["variables"]["filesystem"] = "other"
    assert store.load_plan()["variables"]["filesystem"] == "test"
    with pytest.raises(ValueError, match="frozen"):
        store.initialize(loaded)
    with pytest.raises(ValueError, match="frozen"):
        store.record_result(query(interval_ms=2000), 1000, 2000, [], [])


def test_per_query_watermarks_are_independent_and_math_not_pending(tmp_path):
    qs = [query(), query("q2", ref_id="B", hidden=True),
          query("q3", ref_id="C", kind="math", dependencies=["A", "B"])]
    with Store(tmp_path) as store:
        store.initialize(plan(qs))
        store.record_result(qs[0], 1000, 5000, [], [], status="empty")
        store.record_result(qs[1], 1000, 3000, [], [], status="empty")
        assert store.get_cursor("q1") == 5000
        assert store.get_cursor("q2") == 3000
        assert len(store.query_statuses()) == 2
        store.record_display(1, [], 1000, 3000)
        assert store.panel_statuses()[0]["status"] == "empty"
