"""Exercise cleanup using the real collector, SQLite and exporters, offline."""
import asyncio
import copy
import json
import signal

import pytest

from grafana_collector import cli, engine, exporting, transport
from grafana_collector.dataset import read_points, read_provenance
from grafana_collector.errors import AuthenticationRequired, CollectorError, QueryError
from grafana_collector.storage import Store
from test_cli import URL, args, saved_plan


@pytest.fixture
def collection(monkeypatch):
    state = {"case": "success", "discoveries": 0, "calls": [], "signal": signal.SIGINT,
             "signals": {}, "close_error": False}
    start = saved_plan()["from_ms"]

    class Clock:
        now = start

        def __call__(self):
            return self.now

        async def sleep(self, seconds):
            self.now += int(seconds * 1000)
            await asyncio.sleep(0)

    clock = Clock()
    state["clock"] = clock

    async def make_plan(options, browser, started):
        state["discoveries"] += 1
        plan = saved_plan(options.command)
        plan["started_ms"] = start
        plan["to_ms"] = start if options.command == "watch" else start + 120_000
        plan["metadata"].update(poll_interval_ms=60_000, lookback_ms=0)
        second = copy.deepcopy(plan["panels"][0])
        second["id"] = 2
        second["queries"][0].update(panel_id=2, key="q2")
        plan["panels"].append(second)
        return plan

    class Session:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            if state["close_error"]:
                raise OSError("browser close failed")

        async def ensure_authenticated(self):
            pass

        async def reauthenticate(self):
            raise AuthenticationRequired("offline authentication failure")

        async def execute(self, query, lower, upper):
            state["calls"].append((query["key"], lower, upper))
            case = state["case"]
            if case == "failed" or (case == "partial" and query["key"] == "q2"):
                raise QueryError("offline query failure")
            if case == "cancelled":
                raise asyncio.CancelledError()
            if case == "auth":
                raise AuthenticationRequired("offline authentication failure")
            if case == "signal":
                state["signals"][state["signal"]]()
            if case == "signal_on_second" and query["key"] == "q2" and len(state["calls"]) >= 4:
                state["signals"][state["signal"]]()
            if case == "empty":
                return []
            return [{"metric": "sample", "tags": {"host": "one"},
                     "dps": {str(upper // 1000): 7}}]

    real_collector = engine.Collector

    def collector(*a, **kw):
        return real_collector(*a, **kw, clock=clock, sleep=clock.sleep)

    monkeypatch.setattr(cli, "make_plan", make_plan)
    monkeypatch.setattr(transport, "BrowserSession", Session)
    monkeypatch.setattr(engine, "Collector", collector)
    return state


def install_signal_capture(monkeypatch, state):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn: state["signals"].__setitem__(sig, fn))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)
    monkeypatch.setattr(signal, "signal", lambda *a: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("command,limits", [("fetch", ()), ("watch", ("--rounds", "2")),
                                            ("watch", ("--duration", "90s"))])
@pytest.mark.parametrize("format", ["parquet", "xlsx"])
async def test_success_exports_before_cleanup_and_rejects_completed_directory(collection, tmp_path,
                                                                              command, limits, format):
    out = tmp_path / "run"
    assert await cli.run(args(command, "--url", URL, "--out", str(out), "--format", format, *limits)) == 0
    assert not list(out.glob("collection.sqlite3*"))
    assert not (out / "raw").exists()
    manifest = json.loads((out / "manifest.json").read_text())
    assert all(p["range_status"] == "covered" for p in manifest["panels"])
    assert all(p["collection_status"] == "success" for p in manifest["panels"])
    if format == "parquet":
        points, curves, _ = read_points(out)
        assert points.num_rows and curves
        provenance = read_provenance(out)
        assert provenance["raw_provenance"] is None
        assert all("raw_path" not in attempt for query in provenance["queries"].values()
                   for attempt in query["attempts"])
    else:
        from openpyxl import load_workbook
        for panel in manifest["panels"]:
            book = load_workbook(out / panel["files"][0]["path"])
            assert book.active["B2"].value == 7
            book.close()
    before = (out / "manifest.json").read_bytes()
    calls = len(collection["calls"])
    with pytest.raises(CollectorError, match="新的 --out"):
        await cli.run(args(command, "--url", URL, "--out", str(out), *limits))
    assert len(collection["calls"]) == calls
    assert (out / "manifest.json").read_bytes() == before


@pytest.mark.asyncio
async def test_confirmed_empty_is_successful_and_cleans_sqlite(collection, tmp_path):
    collection["case"] = "empty"
    assert await cli.run(args("fetch", "--url", URL, "--out", str(tmp_path))) == 0
    points, _, manifest = read_points(tmp_path)
    assert points.num_rows == 0
    assert all(p["collection_status"] == "empty" and p["range_status"] == "covered"
               for p in manifest["panels"])
    assert not (tmp_path / "collection.sqlite3").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [("failed", 2), ("partial", 2), ("cancelled", 130), ("auth", None)])
async def test_failed_or_interrupted_fetch_keeps_state_and_can_resume(collection, tmp_path, case, code):
    collection["case"] = case
    invocation = args("fetch", "--url", URL, "--out", str(tmp_path), "--retries", "0")
    if code is None:
        with pytest.raises(AuthenticationRequired):
            await cli.run(invocation)
    else:
        assert await cli.run(invocation) == code
    with Store(tmp_path) as store:
        assert store.load_plan()["source_url"] == URL
        assert len(store.query_statuses()) == 2
    assert (tmp_path / "manifest.json").is_file()
    collection["case"] = "success"
    assert await cli.run(args("fetch", "--out", str(tmp_path))) == 0
    assert collection["discoveries"] == 1
    assert not (tmp_path / "collection.sqlite3").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_watch_signal_keeps_sqlite_even_when_queries_can_finish(collection, monkeypatch, tmp_path, sig):
    install_signal_capture(monkeypatch, collection)
    collection.update(case="signal", signal=sig)
    assert await cli.run(args("watch", "--url", URL, "--out", str(tmp_path), "--rounds", "1")) == 130
    assert (tmp_path / "collection.sqlite3").is_file()
    assert (tmp_path / "manifest.json").is_file()
    collection["case"] = "success"
    assert await cli.run(args("watch", "--out", str(tmp_path), "--duration", "90s")) == 0
    assert collection["discoveries"] == 1
    assert not (tmp_path / "collection.sqlite3").exists()


@pytest.mark.asyncio
async def test_cancelled_watch_preserves_sqlite(collection, tmp_path):
    collection["case"] = "cancelled"
    assert await cli.run(args("watch", "--url", URL, "--out", str(tmp_path), "--rounds", "1")) == 130
    assert (tmp_path / "collection.sqlite3").is_file()


@pytest.mark.asyncio
async def test_interrupted_later_round_exports_already_saved_points(collection, monkeypatch, tmp_path):
    install_signal_capture(monkeypatch, collection)
    collection["case"] = "signal_on_second"
    assert await cli.run(args("watch", "--url", URL, "--out", str(tmp_path), "--rounds", "3")) == 130
    points, _, _ = read_points(tmp_path, panel_id=1)
    assert max(points.column("time").cast("int64").to_pylist()) == saved_plan()["from_ms"] + 120_000
    assert (tmp_path / "collection.sqlite3").is_file()


@pytest.mark.asyncio
async def test_export_failure_keeps_sqlite_and_retry_can_finish(collection, monkeypatch, tmp_path):
    original = exporting.export_run

    def fail(*a, **kw):
        raise OSError("disk full during export")

    monkeypatch.setattr(exporting, "export_run", fail)
    with pytest.raises(OSError, match="disk full"):
        await cli.run(args("fetch", "--url", URL, "--out", str(tmp_path)))
    assert (tmp_path / "collection.sqlite3").is_file()
    monkeypatch.setattr(exporting, "export_run", original)
    assert await cli.run(args("fetch", "--out", str(tmp_path))) == 0
    assert not (tmp_path / "collection.sqlite3").exists()


@pytest.mark.asyncio
async def test_browser_shutdown_failure_does_not_discard_recovery_state(collection, tmp_path):
    collection["close_error"] = True
    with pytest.raises(OSError, match="browser close"):
        await cli.run(args("fetch", "--url", URL, "--out", str(tmp_path)))
    assert (tmp_path / "collection.sqlite3").is_file()
    assert (tmp_path / "manifest.json").is_file()


@pytest.mark.asyncio
async def test_latest_success_does_not_hide_earlier_display_gap(collection, monkeypatch, tmp_path):
    original = engine.render_panel
    first = True

    def render(panel, data):
        nonlocal first
        if panel["id"] == 1 and first:
            first = False
            raise QueryError("first interval could not render")
        return original(panel, data)

    monkeypatch.setattr(engine, "render_panel", render)
    assert await cli.run(args("watch", "--url", URL, "--out", str(tmp_path), "--rounds", "2")) == 2
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert all(p["collection_status"] == "success" for p in manifest["panels"])
    assert next(p for p in manifest["panels"] if p["panel_id"] == 1)["range_status"] == "partial"
    assert (tmp_path / "collection.sqlite3").is_file()


@pytest.mark.asyncio
async def test_clock_rollback_does_not_trim_saved_points_before_cleanup(collection, tmp_path):
    plan = saved_plan("watch")
    start = plan["from_ms"]
    end = start + 120_000
    plan.update(to_ms=start, started_ms=start)
    curve = {"ref_id": "A", "metric": "sample", "labels": {"host": "one"},
             "name": "sample", "unit": "bytes", "points": [[end, 42]]}
    with Store(tmp_path) as store:
        store.initialize(plan)
        store.record_result(plan["panels"][0]["queries"][0], start, end, [curve], None)
        store.record_display(1, [curve], start, end)
    collection["clock"].now = start + 30_000
    assert await cli.run(args("watch", "--out", str(tmp_path), "--rounds", "1")) == 0
    assert collection["calls"] == []
    points, _, manifest = read_points(tmp_path)
    assert points.column("time").cast("int64").to_pylist() == [end]
    assert points.column("value").to_pylist() == [42.0]
    assert manifest["panels"][0]["display_coverage"]["to_ms"] == end
    assert not (tmp_path / "collection.sqlite3").exists()
