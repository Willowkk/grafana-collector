"""CLI integration checks stay offline and never launch Chrome."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

from openpyxl import load_workbook
import pytest

from grafana_collector import cli
from grafana_collector.errors import AuthenticationRequired, CollectorError, QueryError
from grafana_collector.storage import Store
from grafana_collector.timeutil import dashboard_identity, display_range, duration_ms, parse_time


URL = "https://example.test/d/dashboard/test?from=1789369200000&to=1789390800000&var-filesystem=test"


def args(*values):
    return cli.settings(cli.parser().parse_args(values))


def saved_plan(mode="watch"):
    query = {"key": "q1", "panel_id": 1, "ref_id": "A", "kind": "opentsdb", "route": "/api/query",
             "query": {"metric": "sample"}, "interval_ms": 60000, "alias": "sample", "unit": "bytes",
             "hidden": False, "dependencies": [], "metadata": {}, "error": None}
    return {"schema_version": 1, "source_url": URL, "dashboard": {"uid": "dashboard", "title": "Test", "version": 1},
            "variables": {"filesystem": "test"}, "panels": [{"id": 1, "title": "Sample", "group": "Overview",
            "queries": [query], "transformations": [], "metadata": {}}], "from_ms": 1789369200000,
            "to_ms": 1789390800000, "mode": mode, "started_ms": 1789390800000,
            "metadata": {"poll_interval_ms": 60000, "lookback_ms": 90000}}


def test_config_section_cli_precedence_paths_panels_and_time_aliases(tmp_path):
    config = tmp_path / "collector.toml"
    config.write_text('url = "https://example.test/d/test/name"\nheadless = true\n'
                      'concurrency = 2\nprofile = "~/collector-profile"\n'
                      '[fetch]\nconcurrency = 3\npanels = [1, 2]\n'
                      'from = "2026-09-14 15:00:00"\nto = "2026-09-14 16:00:00"\n', encoding="utf-8")
    parsed = args("fetch", "--config", str(config), "--concurrency", "4", "--out", str(tmp_path / "run"))
    assert parsed.url == "https://example.test/d/test/name"
    assert parsed.headless is True
    assert parsed.concurrency == 4
    assert parsed.panel_ids == [1, 2]
    assert parsed.from_value == "2026-09-14 15:00:00"
    assert parsed.to_value == "2026-09-14 16:00:00"
    assert parsed.profile == Path.home() / "collector-profile"
    assert parsed.poll_interval == "5m" and parsed.lookback == "5m"


@pytest.mark.parametrize("option,value", [("--timeout", "0"), ("--concurrency", "0"), ("--retries", "-1"), ("--rounds", "0")])
def test_invalid_settings_fail_without_browser(option, value):
    with pytest.raises(CollectorError):
        args("watch", option, value)


def test_invalid_panel_filter_is_user_readable():
    with pytest.raises(CollectorError, match="panels"):
        args("fetch", "--panels", "one,two")


@pytest.mark.parametrize("command", ["fetch", "watch", "export"])
def test_export_format_defaults_to_parquet_and_accepts_xlsx(command):
    assert args(command).format == "parquet"
    assert args(command, "--format", "xlsx").format == "xlsx"
    with pytest.raises(SystemExit) as invalid:
        args(command, "--format", "csv")
    assert invalid.value.code == 2


@pytest.mark.parametrize("command", ["fetch", "watch", "export"])
def test_export_format_config_and_cli_precedence(tmp_path, command):
    config = tmp_path / "collector.toml"
    config.write_text(f'format = "parquet"\n[{command}]\nformat = "xlsx"\n', encoding="utf-8")
    assert args(command, "--config", str(config)).format == "xlsx"
    assert args(command, "--config", str(config), "--format", "parquet").format == "parquet"
    config.write_text('format = "xlsx"\n', encoding="utf-8")
    assert args(command, "--config", str(config)).format == "xlsx"


@pytest.mark.parametrize("value", ['"csv"', '""', "false", "12", '["parquet"]', '{name = "parquet"}'])
def test_invalid_config_format_fails_before_run(tmp_path, monkeypatch, capsys, value):
    config = tmp_path / "collector.toml"
    config.write_text(f'format = {value}\n', encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid configuration reached run or browser")
    monkeypatch.setattr(cli, "run", forbidden)
    with pytest.raises(SystemExit) as result:
        cli.main(["fetch", "--config", str(config)])
    assert result.value.code == 1
    assert "format 必须为 parquet 或 xlsx" in capsys.readouterr().err


@pytest.mark.parametrize("selected", ["parquet", "xlsx"])
def test_export_dispatch_routes_options_without_changing_them(monkeypatch, tmp_path, selected):
    from grafana_collector import exporting
    calls = []
    def fake_export(format_name):
        def write(store, out, **options):
            calls.append((format_name, store, out, options))
            return out / "manifest.json"
        return write
    monkeypatch.setitem(sys.modules, "grafana_collector.parquet_exporter", SimpleNamespace(export_run=fake_export("parquet")))
    monkeypatch.setitem(sys.modules, "grafana_collector.exporter", SimpleNamespace(export_run=fake_export("xlsx")))
    store = object()
    result = exporting.export_run(store, tmp_path, format=selected, from_ms=100, to_ms=200, panel_ids=[1, 7])
    assert result == tmp_path / "manifest.json"
    assert calls == [(selected, store, tmp_path, {"from_ms": 100, "to_ms": 200, "panel_ids": [1, 7]})]
    with pytest.raises(CollectorError, match="format"):
        exporting.export_run(store, tmp_path, format="csv")
    assert len(calls) == 1


def test_time_parsing_units_timezone_rounding_and_absolute_range():
    assert duration_ms("5m") == 300000
    assert duration_ms("0s") == 0
    assert parse_time("2026-09-14 15:00:00") == parse_time("2026-09-14T07:00:00Z")
    assert parse_time("1789369200") == 1789369200000
    assert parse_time("1789369200000") == 1789369200000
    now = parse_time("2026-09-17T16:27:53+08:00")
    assert parse_time("now-1h/h", now_ms=now) == parse_time("2026-09-17T15:00:00+08:00")
    assert parse_time("now/w", now_ms=now) == parse_time("2026-09-14T00:00:00+08:00")
    assert display_range(URL, {}, now_ms=now) == (1789369200000, 1789390800000)
    assert display_range(URL, {}, now_ms=now, from_value="now-5m", to_value="now") == (now - 300000, now)
    assert display_range("https://example.test/d/a/name", {"time": {"from": "now-6h", "to": "now"}}, now_ms=now) == (now - 21600000, now)
    with pytest.raises(CollectorError):
        display_range(URL, {}, now_ms=now, from_value="now", to_value="now-1h")


def test_dashboard_identity_preserves_subpath_and_refuses_url_credentials():
    assert dashboard_identity("https://example.test/grafana/d/my-uid/title?orgId=42") == ("https://example.test/grafana", "my-uid", "42")
    with pytest.raises(CollectorError):
        dashboard_identity("https://user:password@example.test/d/uid/title")
    with pytest.raises(CollectorError):
        dashboard_identity("file:///tmp/d/uid/title")


@pytest.mark.asyncio
async def test_make_watch_plan_uses_launch_start_and_freezes_link_reference(monkeypatch):
    from grafana_collector import query
    captured = {}
    def compile_fake(dashboard, datasources, url, **kwargs):
        captured.update(kwargs)
        return {"panels": saved_plan()["panels"], "variables": {"filesystem": "test"}, "metadata": {}}
    monkeypatch.setattr(query, "compile_dashboard", compile_fake)
    class Session:
        async def discover(self):
            return {"uid": "dashboard"}, [], {1: 500}
    parsed = args("watch", "--url", URL, "--out", "/tmp/unused")
    started = 1789400000000
    plan = await cli.make_plan(parsed, Session(), started)
    assert plan["from_ms"] == started == plan["to_ms"]
    assert captured["reference_from_ms"] == 1789369200000
    assert captured["reference_to_ms"] == 1789390800000
    assert plan["metadata"]["poll_interval_ms"] == 300000
    parsed.since = "1789369200000"
    plan = await cli.make_plan(parsed, Session(), started)
    assert plan["from_ms"] == 1789369200000


def test_offline_export_command_never_opens_browser(tmp_path, monkeypatch, capsys):
    from grafana_collector import transport
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline export tried to start Chrome")
    monkeypatch.setattr(transport, "BrowserSession", forbidden)
    run_dir, out_dir = tmp_path / "run", tmp_path / "export"
    with Store(run_dir) as store:
        store.initialize(saved_plan())
        store.record_display(1, [{"ref_id": "A", "metric": "sample", "labels": {"host": "one"}, "name": "sample",
                                  "unit": "bytes", "points": [[1789369200000, 7], [1789369260000, 8]]}],
                             1789369200000, 1789369260000)
    with pytest.raises(SystemExit) as result:
        cli.main(["export", "--run", str(run_dir), "--out", str(out_dir), "--panels", "1",
                  "--from", "1789369260000", "--to", "1789369260000", "--format", "xlsx"])
    assert result.value.code == 0
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["panels"][0]["selected_point_count"] == 1
    book = load_workbook(out_dir / manifest["panels"][0]["files"][0]["path"])
    assert book.active["B2"].value == 8
    book.close()
    assert "离线导出完成" in capsys.readouterr().out


def test_offline_export_defaults_to_parquet_and_forwards_filters(tmp_path, monkeypatch, capsys):
    from grafana_collector import exporting, transport
    run_dir, out_dir = tmp_path / "run", tmp_path / "export"
    with Store(run_dir) as store:
        store.initialize(saved_plan())
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline Parquet export tried to start Chrome")
    monkeypatch.setattr(transport, "BrowserSession", forbidden)
    calls = []
    def write(store, out, **options):
        calls.append((store.load_plan(), out, options))
        return out / "manifest.json"
    monkeypatch.setattr(exporting, "export_run", write)
    with pytest.raises(SystemExit) as result:
        cli.main(["export", "--run", str(run_dir), "--out", str(out_dir), "--panels", "1",
                  "--from", "1789369260000", "--to", "1789369260000"])
    assert result.value.code == 0
    assert calls == [(saved_plan(), out_dir, {"format": "parquet", "from_ms": 1789369260000,
                                            "to_ms": 1789369260000, "panel_ids": [1]})]
    assert "离线导出完成（parquet）" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["fetch", "watch"])
@pytest.mark.parametrize("format_options,expected", [((), "parquet"), (("--format", "xlsx"), "xlsx")])
async def test_collection_final_export_uses_selected_format(tmp_path, monkeypatch, capsys,
                                                           command, format_options, expected):
    from grafana_collector import engine, exporting, transport
    plan = saved_plan(command)
    events = []
    async def make_plan(options, session, started_ms):
        events.append("discover")
        return plan
    class Session:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def ensure_authenticated(self):
            events.append("authenticate")
    class Collector:
        def __init__(self, current_plan, store, session, **options):
            assert current_plan == plan
        async def collect_until(self, end_ms):
            events.append(("fetch", end_ms))
        async def watch(self, **options):
            events.append(("watch", options["rounds"]))
    def write(store, out, **options):
        assert store.load_plan() == plan
        events.append(("export", options["format"]))
        return out / "manifest.json"
    monkeypatch.setattr(cli, "make_plan", make_plan)
    monkeypatch.setattr(transport, "BrowserSession", Session)
    monkeypatch.setattr(engine, "Collector", Collector)
    monkeypatch.setattr(exporting, "export_run", write)
    run_dir = tmp_path / "run"
    options = ("--rounds", "1") if command == "watch" else ()
    result = await cli.run(args(command, "--url", URL, "--out", str(run_dir), *options, *format_options))
    # A mocked collector deliberately leaves the panel pending, preserving the
    # CLI's non-success exit status while testing that final export still runs.
    assert result == 2
    assert events == ["discover", (command, 1 if command == "watch" else plan["to_ms"]), ("export", expected)]
    assert f"{expected} 数据与结果清单" in capsys.readouterr().out
    # The output format is not part of the frozen query plan. A previous run
    # can be resumed and exported in the other format without rediscovery.
    events.clear()
    resumed_format = "xlsx" if expected == "parquet" else "parquet"
    result = await cli.run(args(command, "--out", str(run_dir), *options, "--format", resumed_format))
    assert result == 2
    assert events == ["authenticate", (command, 1 if command == "watch" else plan["to_ms"]), ("export", resumed_format)]


@pytest.mark.asyncio
async def test_resume_rejects_mode_url_panel_and_range_changes_before_browser(tmp_path, monkeypatch):
    from grafana_collector import transport
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid resume tried to start Chrome")
    monkeypatch.setattr(transport, "BrowserSession", forbidden)
    run_dir = tmp_path / "run"
    with Store(run_dir) as store:
        store.initialize(saved_plan())
    for values, message in [(("fetch",), "模式"), (("watch", "--url", "https://example.test/d/different/title"), "链接"),
                            (("watch", "--panels", "2"), "面板"), (("watch", "--since", "now-1h"), "时间")]:
        with pytest.raises(CollectorError, match=message):
            await cli.run(args(*values, "--out", str(run_dir)))


def test_main_reports_missing_run_without_traceback(capsys, tmp_path):
    with pytest.raises(SystemExit) as result:
        cli.main(["export", "--run", str(tmp_path / "missing")])
    assert result.value.code == 1
    assert "--run" in capsys.readouterr().err


@pytest.fixture
def login_session_stub(monkeypatch):
    """Dashboard discovery can succeed while the real datasource needs login."""
    from grafana_collector import transport

    def setup(*, permission_denied=False, reauthentication_error=False, retry_expired=False):
        events = []
        plan = saved_plan("fetch")

        async def make_plan(options, browser, started):
            events.append("dashboard_readable")
            return plan

        class Session:
            def __init__(self, *args, **kwargs):
                self.attempts = 0

            async def __aenter__(self):
                events.append("open")
                return self

            async def __aexit__(self, *args):
                events.append("close")

            async def execute(self, query, start, end):
                self.attempts += 1
                events.append(("execute", query["key"], start, end))
                if permission_denied:
                    raise QueryError("HTTP 403: datasource permission denied")
                if self.attempts == 1 or retry_expired:
                    raise AuthenticationRequired("Datasource session expired")
                return [{"metric": "sample", "tags": {"host": "one"}, "dps": {"1789369200": 4}}]

            async def reauthenticate(self):
                events.append("reauthenticate")
                if reauthentication_error:
                    raise AuthenticationRequired("Interactive login timed out")

        monkeypatch.setattr(cli, "make_plan", make_plan)
        monkeypatch.setattr(transport, "BrowserSession", Session)
        return events, plan

    return setup


@pytest.mark.asyncio
async def test_login_reauthenticates_failed_datasource_and_validates_real_response(login_session_stub, capsys):
    events, plan = login_session_stub()
    result = await cli.run(args("login", "--url", URL))
    expected_query = ("execute", "q1", plan["from_ms"], plan["to_ms"])
    assert result == 0
    assert events == ["open", "dashboard_readable", expected_query, "reauthenticate", expected_query, "close"]
    assert "返回 1 条曲线" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("case,exception,executions,auth_calls", [
    ({"permission_denied": True}, QueryError, 1, 0),
    ({"reauthentication_error": True}, AuthenticationRequired, 1, 1),
    ({"retry_expired": True}, AuthenticationRequired, 2, 1),
])
async def test_login_does_not_loop_or_report_permission_failure_as_authenticated(login_session_stub, capsys, case, exception, executions, auth_calls):
    events, _ = login_session_stub(**case)
    with pytest.raises(exception):
        await cli.run(args("login", "--url", URL))
    assert sum(isinstance(event, tuple) and event[0] == "execute" for event in events) == executions
    assert events.count("reauthenticate") == auth_calls
    assert events[-1] == "close"
    assert "登录及真实查询已验证" not in capsys.readouterr().out
