"""Scheduling tests use the real durable store and controlled network responses."""

import asyncio
from copy import deepcopy

import pytest

from grafana_collector import engine
from grafana_collector.engine import Collector
from grafana_collector.errors import AuthenticationRequired, QueryError
from grafana_collector.storage import Store


BASE = 1_789_459_200_000
FIVE_MINUTES = 300_000


def make_query(ref="A", panel_id=1, **overrides):
    return {
        "key": f"{panel_id}-{ref}", "panel_id": panel_id, "ref_id": ref,
        "kind": "opentsdb", "route": "/api/datasources/proxy/3337/api/query",
        "query": {"metric": f"metric.{ref}", "aggregator": "sum", "downsample": "1m-avg"},
        "interval_ms": 60_000, "alias": ref, "unit": "reqps", "hidden": False,
        "dependencies": [], "metadata": {"required": True}, "error": None,
        **overrides,
    }


def make_plan(*queries, mode="watch", from_ms=BASE, to_ms=BASE, started_ms=BASE):
    panels = {}
    for query in queries or (make_query(),):
        panels.setdefault(query["panel_id"], {
            "id": query["panel_id"], "title": f"Panel {query['panel_id']}",
            "group": "Test", "queries": [], "transformations": [], "metadata": {},
        })["queries"].append(query)
    return {
        "schema_version": 1, "source_url": "https://grafana.byted.org/d/inyUtsTSk/sdkv2",
        "dashboard": {"uid": "inyUtsTSk", "title": "SDKv2", "version": 319},
        "variables": {}, "panels": list(panels.values()), "from_ms": from_ms,
        "to_ms": to_ms, "mode": mode, "started_ms": started_ms,
        "metadata": {"timezone": "Asia/Shanghai"},
    }


class Clock:
    def __init__(self, now=BASE):
        self.now = now
        self.delays = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += int(seconds * 1000)
        await asyncio.sleep(0)


class Transport:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler
        self.auth_count = 0

    async def execute(self, query, start, end):
        self.calls.append((query["key"], start, end, query["interval_ms"]))
        if self.handler:
            return await self.handler(query, start, end)
        return {"points": [[start + 1, 1.0], [end - 1, 2.0]]}

    async def reauthenticate(self):
        self.auth_count += 1


@pytest.fixture(autouse=True)
def controlled_query_conversion(monkeypatch):
    def parse(query, raw):
        if raw.get("invalid"):
            raise QueryError("Malformed query response")
        if not raw.get("points"):
            return []
        return [{
            "ref_id": query["ref_id"], "metric": query["query"].get("metric", "derived"),
            "labels": {"namespace": "test"}, "name": query["alias"], "unit": query["unit"],
            "points": raw["points"],
        }]

    def render(panel, by_ref):
        return [series for values in by_ref.values() for series in values]

    monkeypatch.setattr(engine, "parse_response", parse)
    monkeypatch.setattr(engine, "render_panel", render)


@pytest.fixture
def open_store(tmp_path):
    stores = []

    def create(plan):
        store = Store(tmp_path / f"run-{len(stores)}")
        store.initialize(plan)
        stores.append(store)
        return store

    yield create
    for store in stores:
        store.close()


async def test_no_history_waits_then_uses_own_cursor_and_survives_restart(open_store):
    plan = make_plan()
    frozen = deepcopy(plan)
    store = open_store(plan)
    clock = Clock()
    transport = Transport()
    collector = Collector(plan, store, transport, clock=clock, sleep=clock.sleep)

    summary = await collector.watch(rounds=3)

    assert summary["rounds"] == 3
    assert transport.calls == [
        ("1-A", BASE, BASE + FIVE_MINUTES, 60_000),
        ("1-A", BASE, BASE + 2 * FIVE_MINUTES, 60_000),
        ("1-A", BASE + FIVE_MINUTES, BASE + 3 * FIVE_MINUTES, 60_000),
    ]
    assert clock.delays == [300, 300, 300]
    assert plan == frozen

    # A restart after more than 30 minutes must query the entire missing gap.
    clock.now = BASE + 3 * 60 * 60 * 1000
    resumed = Collector(store.load_plan(), store, transport, clock=clock, sleep=clock.sleep)
    await resumed.watch(rounds=1)
    assert transport.calls[-1] == ("1-A", BASE + 2 * FIVE_MINUTES, clock.now, 60_000)
    assert len(clock.delays) == 3  # Resume catches up immediately.
    assert store.get_cursor("1-A") == clock.now


async def test_explicit_since_catches_up_without_initial_wait(open_store):
    plan = make_plan(from_ms=BASE - 3_600_000)
    store = open_store(plan)
    clock = Clock()
    transport = Transport()
    await Collector(plan, store, transport, clock=clock, sleep=clock.sleep).watch(rounds=1)
    assert transport.calls[0][1:3] == (BASE - 3_600_000, BASE)
    assert not clock.delays


async def test_history_uses_one_semantic_window_and_frozen_step(open_store):
    query = make_query()
    query["query"]["topK"] = "top-10-max"
    end = BASE + 6 * 3_600_000
    plan = make_plan(query, mode="fetch", to_ms=end)
    store = open_store(plan)
    transport = Transport()
    collector = Collector(plan, store, transport)
    summary = await collector.collect_until(end + FIVE_MINUTES)
    assert transport.calls == [("1-A", BASE, end, 60_000)]
    assert summary["queries"] == {"success": 1}
    assert store.get_cursor("1-A") == end


async def test_failures_keep_independent_cursors_and_block_derived_output(open_store):
    plan = make_plan(make_query("A"), make_query("B"))
    store = open_store(plan)
    fail_b = True

    async def handler(query, start, end):
        if query["ref_id"] == "B" and fail_b:
            raise QueryError("Rejected query")
        return {"points": [[start + 1, 2]]}

    transport = Transport(handler)
    collector = Collector(plan, store, transport)
    first_end = BASE + 3_600_000
    summary = await collector.collect_until(first_end)
    assert summary["queries"] == {"success": 1, "failed": 1}
    assert summary["panels"] == {"failed": 1}
    assert store.get_cursor("1-A") == first_end
    assert store.get_cursor("1-B") is None
    assert store.get_display_series(1) == []

    fail_b = False
    second_end = first_end + FIVE_MINUTES
    summary = await collector.collect_until(second_end)
    assert transport.calls[-2][1:3] == (first_end - FIVE_MINUTES, second_end)
    assert transport.calls[-1][1:3] == (BASE, second_end)
    assert summary["panels"] == {"success": 1}
    assert store.get_cursor("1-B") == second_end


async def test_legitimate_empty_advances_but_invalid_response_does_not(open_store):
    plan = make_plan(make_query("A", 1), make_query("B", 2))
    store = open_store(plan)

    async def handler(query, start, end):
        return {} if query["ref_id"] == "A" else {"invalid": True}

    transport = Transport(handler)
    summary = await Collector(plan, store, transport).collect_until(BASE + FIVE_MINUTES)
    assert summary["queries"] == {"empty": 1, "failed": 1}
    assert summary["panels"] == {"empty": 1, "failed": 1}
    assert store.get_cursor("1-A") == BASE + FIVE_MINUTES
    assert store.get_cursor("2-B") is None
    assert len(transport.calls) == 2  # Parsing errors are not transient.


async def test_three_transient_retries_and_no_retry_for_permanent_error(open_store):
    plan = make_plan(make_query("A", 1), make_query("B", 2))
    store = open_store(plan)
    clock = Clock()
    counts = {"A": 0, "B": 0}

    async def handler(query, start, end):
        ref = query["ref_id"]
        counts[ref] += 1
        raise QueryError("Network unavailable", retryable=ref == "A")

    collector = Collector(plan, store, Transport(handler), clock=clock, sleep=clock.sleep)
    summary = await collector.collect_until(BASE + FIVE_MINUTES)
    assert counts == {"A": 4, "B": 1}
    assert summary["attempts"] == 5
    assert clock.delays == [1, 2, 4]
    assert store.get_cursor("1-A") is None


async def test_timeout_is_retried_and_then_saved(open_store):
    plan = make_plan()
    store = open_store(plan)
    clock = Clock()
    attempts = 0

    async def handler(query, start, end):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await asyncio.Event().wait()
        return {"points": [[start + 1, 1]]}

    summary = await Collector(plan, store, Transport(handler), timeout=0.01, clock=clock, sleep=clock.sleep).collect_until(BASE + FIVE_MINUTES)
    assert summary["queries"] == {"success": 1}
    assert summary["attempts"] == 2
    assert store.get_cursor("1-A") == BASE + FIVE_MINUTES


async def test_concurrent_auth_expiry_prompts_once_and_retries_both(open_store):
    plan = make_plan(make_query("A"), make_query("B"))
    store = open_store(plan)
    both_started = asyncio.Event()

    class ExpiredTransport(Transport):
        def __init__(self):
            super().__init__()
            self.expired_calls = 0
            self.authenticated = False

        async def execute(self, query, start, end):
            if not self.authenticated:
                self.expired_calls += 1
                if self.expired_calls == 2:
                    both_started.set()
                await both_started.wait()
                raise AuthenticationRequired("Please log in")
            return {"points": [[start + 1, 1]]}

        async def reauthenticate(self):
            self.auth_count += 1
            await asyncio.sleep(0)
            self.authenticated = True

    transport = ExpiredTransport()
    summary = await Collector(plan, store, transport).collect_until(BASE + FIVE_MINUTES)
    assert transport.auth_count == 1
    assert summary["queries"] == {"success": 2}
    assert summary["attempts"] == 4


async def test_auth_rejection_does_not_loop_forever(open_store):
    plan = make_plan()
    store = open_store(plan)

    async def handler(query, start, end):
        raise AuthenticationRequired("Login still expired")

    transport = Transport(handler)
    collector = Collector(plan, store, transport)
    with pytest.raises(AuthenticationRequired, match="Login still expired"):
        await collector.collect_until(BASE + FIVE_MINUTES)
    assert transport.auth_count == 1
    assert len(transport.calls) == 2
    assert collector.last_summary["queries"] == {"failed": 1}
    assert collector.last_summary["fatal_auth"] is True
    assert store.get_cursor("1-A") is None


async def test_concurrency_limit_applies_to_network_calls(open_store):
    plan = make_plan(*(make_query(str(i), i) for i in range(7)))
    store = open_store(plan)
    active = 0
    maximum = 0

    async def handler(query, start, end):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        return {}

    result = await Collector(plan, store, Transport(handler), concurrency=2).collect_until(BASE + FIVE_MINUTES)
    assert maximum == 2
    assert result["queries"] == {"empty": 7}


async def test_hidden_queries_and_math_are_not_sent_but_dependencies_are(open_store):
    plan = make_plan(
        make_query("A", hidden=True, metadata={"required": True}),
        make_query("B", hidden=True, metadata={"required": False}),
        make_query("C", kind="math", dependencies=["A"]),
    )
    store = open_store(plan)
    transport = Transport()
    await Collector(plan, store, transport).collect_until(BASE + FIVE_MINUTES)
    assert [call[0] for call in transport.calls] == ["1-A"]


async def test_renderer_failure_is_not_exported_as_healthy_empty(open_store, monkeypatch):
    plan = make_plan()
    store = open_store(plan)

    def broken_render(panel, by_ref):
        raise QueryError("Missing expression dependency B")

    monkeypatch.setattr(engine, "render_panel", broken_render)
    summary = await Collector(plan, store, Transport()).collect_until(BASE + FIVE_MINUTES)
    assert summary["queries"] == {"success": 1}
    assert summary["panels"] == {"failed": 1}
    assert store.get_cursor("1-A") == BASE + FIVE_MINUTES
    assert store.get_display_series(1) == []


async def test_stop_mid_round_keeps_finished_queries_and_cancels_others(open_store):
    plan = make_plan(make_query("A", 1), make_query("B", 2))
    store = open_store(plan)
    clock = Clock()
    blocked = asyncio.Event()
    stop = asyncio.Event()

    async def handler(query, start, end):
        if query["ref_id"] == "B":
            blocked.set()
            await asyncio.Event().wait()
        return {"points": [[start + 1, 1]]}

    collector = Collector(plan, store, Transport(handler), clock=clock, sleep=clock.sleep)
    task = asyncio.create_task(collector.watch(rounds=5, stop_event=stop))
    await blocked.wait()
    while store.get_cursor("1-A") is None:
        await asyncio.sleep(0)
    stop.set()
    summary = await asyncio.wait_for(task, 1)
    assert summary["reason"] == "stopped"
    assert store.get_cursor("1-A") == BASE + FIVE_MINUTES
    assert store.get_cursor("2-B") is None
    assert store.get_display_series(1)
    assert not store.get_display_series(2)


async def test_cancel_watch_returns_for_cli_export(open_store):
    plan = make_plan()
    store = open_store(plan)
    clock = Clock()
    blocked = asyncio.Event()

    async def handler(query, start, end):
        blocked.set()
        await asyncio.Event().wait()

    collector = Collector(plan, store, Transport(handler), clock=clock, sleep=clock.sleep)
    task = asyncio.create_task(collector.watch())
    await blocked.wait()
    task.cancel()
    summary = await task
    assert summary["reason"] == "cancelled"
    assert store.get_cursor("1-A") is None
    assert summary["last"]["cancelled"] is True


async def test_duration_before_first_due_does_not_manufacture_history(open_store):
    plan = make_plan()
    store = open_store(plan)
    clock = Clock()
    transport = Transport()
    result = await Collector(plan, store, transport, clock=clock, sleep=clock.sleep).watch(duration_seconds=60)
    assert result["reason"] == "duration"
    assert result["rounds"] == 0
    assert not transport.calls
    assert clock.delays == [60]


async def test_slow_rounds_do_not_overlap_or_replay_missed_ticks(open_store):
    plan = make_plan()
    store = open_store(plan)
    clock = Clock()

    async def handler(query, start, end):
        clock.now += 700_000  # More than two polling intervals.
        return {}

    transport = Transport(handler)
    result = await Collector(plan, store, transport, clock=clock, sleep=clock.sleep).watch(rounds=2)
    assert result["rounds"] == 2
    assert [call[2] - BASE for call in transport.calls] == [300_000, 1_200_000]
    assert clock.delays == [300, 200]


async def test_real_parser_and_renderer_keep_zero_division_and_missing_values_null(open_store, monkeypatch):
    from grafana_collector import query as compiler

    monkeypatch.setattr(engine, "parse_response", compiler.parse_response)
    monkeypatch.setattr(engine, "render_panel", compiler.render_panel)
    plan = make_plan(
        make_query("A", hidden=True),
        make_query("B", hidden=True),
        make_query("C", kind="math", query={"expression": "$A / $B"}, dependencies=["A", "B"]),
        mode="fetch", to_ms=BASE + FIVE_MINUTES,
    )
    store = open_store(plan)

    async def handler(query, start, end):
        offset = BASE // 1000
        values = {str(offset + 60): 8, str(offset + 120): 4, str(offset + 180): 1}
        if query["ref_id"] == "B":
            values = {str(offset + 60): 10, str(offset + 120): 0}
        return [{"metric": query["query"]["metric"], "tags": {"namespace": "a"}, "dps": values}]

    summary = await Collector(plan, store, Transport(handler)).collect_until(plan["to_ms"])
    assert summary["queries"] == {"success": 2}
    curves = store.get_display_series(1)
    assert len(curves) == 1
    assert curves[0]["ref_id"] == "C"
    assert curves[0]["points"] == [[BASE + 60_000, 0.8], [BASE + 120_000, None], [BASE + 180_000, None]]


async def test_overlap_updates_late_values_without_erasing_earlier_display(open_store):
    plan = make_plan()
    store = open_store(plan)
    first = True

    async def handler(query, start, end):
        nonlocal first
        if first:
            first = False
            return {"points": [[BASE + 60_000, 2], [BASE + 360_000, 1]]}
        return {"points": [[BASE + 360_000, 9], [BASE + 720_000, 3]]}

    collector = Collector(plan, store, Transport(handler))
    await collector.collect_until(BASE + 600_000)
    await collector.collect_until(BASE + 900_000)
    points = store.get_display_series(1)[0]["points"]
    assert points == [[BASE + 60_000, 2], [BASE + 360_000, 9], [BASE + 720_000, 3]]


async def test_unrecoverable_auth_terminates_watch_once_and_preserves_success(open_store):
    plan = make_plan(make_query("A", 1), make_query("B", 2), make_query("C", 3))
    store = open_store(plan)
    clock = Clock()
    expired_started = 0
    both_expired = asyncio.Event()

    class HeadlessTransport(Transport):
        async def execute(self, query, start, end):
            nonlocal expired_started
            if query["ref_id"] == "A":
                return {"points": [[start + 1, 2]]}
            expired_started += 1
            if expired_started == 2:
                both_expired.set()
            await both_expired.wait()
            raise AuthenticationRequired("Session expired")

        async def reauthenticate(self):
            self.auth_count += 1
            raise AuthenticationRequired("Headless login needs an interactive restart")

    transport = HeadlessTransport()
    collector = Collector(plan, store, transport, clock=clock, sleep=clock.sleep)
    with pytest.raises(AuthenticationRequired, match="Headless"):
        await collector.watch(rounds=3)
    assert transport.auth_count == 1
    assert clock.delays == [300]  # No second polling round or repeated login wait.
    assert store.get_cursor("1-A") == BASE + FIVE_MINUTES
    assert store.get_cursor("2-B") is None
    assert store.get_cursor("3-C") is None
    assert store.get_display_series(1)
    assert collector.last_summary["queries"] == {"success": 1, "failed": 2}


async def test_permission_denied_query_does_not_trigger_login(open_store):
    plan = make_plan()
    store = open_store(plan)

    async def handler(query, start, end):
        raise QueryError("HTTP 403: insufficient datasource permissions")

    transport = Transport(handler)
    summary = await Collector(plan, store, transport).collect_until(BASE + FIVE_MINUTES)
    assert transport.auth_count == 0
    assert summary["queries"] == {"failed": 1}
    assert summary["fatal_auth"] is False


async def test_topk_membership_changes_and_empty_replace_only_latest_window(open_store, monkeypatch):
    from grafana_collector import query as compiler

    monkeypatch.setattr(engine, "parse_response", compiler.parse_response)
    monkeypatch.setattr(engine, "render_panel", compiler.render_panel)
    query = make_query()
    query["query"]["topK"] = "top-1-max"
    plan = make_plan(query)
    store = open_store(plan)
    snapshot = 0

    async def handler(query, start, end):
        nonlocal snapshot
        snapshot += 1
        if snapshot == 3:
            return []
        points = {60: 9, 360: 8, 540: 7} if snapshot == 1 else {360: 10, 720: 11}
        return [{
            "metric": query["query"]["metric"], "tags": {"task": "old" if snapshot == 1 else "new"},
            "dps": {str(BASE // 1000 + offset): value for offset, value in points.items()},
        }]

    collector = Collector(plan, store, Transport(handler))
    await collector.collect_until(BASE + 600_000)
    await collector.collect_until(BASE + 900_000)
    by_task = {series["labels"]["task"]: series["points"] for series in store.get_display_series(1)}
    assert by_task == {
        "old": [[BASE + 60_000, 9]],
        "new": [[BASE + 360_000, 10], [BASE + 720_000, 11]],
    }
    # The new top1 replaces the overlap; the earlier historical winner remains.
    latest = await collector.collect_until(BASE + 1_200_000)
    by_task = {series["labels"]["task"]: series["points"] for series in store.get_display_series(1)}
    assert by_task == {"old": [[BASE + 60_000, 9]], "new": [[BASE + 360_000, 10]]}
    assert latest["queries"] == {"empty": 1}
    assert latest["panels"] == {"empty": 1}
    assert store.get_cursor(query["key"]) == BASE + 1_200_000
    assert len(list(store.raw_dir.glob("*.json.gz"))) == 3
