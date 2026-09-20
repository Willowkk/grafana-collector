"""Incremental query collection with durable per-query progress.

The plan is immutable for the life of a run. In particular, this module never
recompiles a query or changes its interval to match a retry's time range.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import Counter
from collections.abc import Awaitable, Callable

from .errors import AuthenticationRequired, QueryError, UnsupportedQuery
from .query import parse_response, render_panel


class Collector:
    def __init__(
        self,
        plan: dict,
        store,
        transport,
        *,
        concurrency: int = 4,
        timeout: float = 30,
        retries: int = 3,
        lookback_ms: int = 300_000,
        progress=None,
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        if concurrency < 1 or timeout <= 0 or retries < 0 or lookback_ms < 0:
            raise ValueError("concurrency/timeout must be positive; retries/lookback nonnegative")
        if plan.get("mode") not in {"fetch", "watch"}:
            raise ValueError("plan.mode must be fetch or watch")
        self.plan = plan
        self.store = store
        self.transport = transport
        self.timeout = timeout
        self.retries = retries
        self.lookback_ms = lookback_ms
        self.progress = progress
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        self.sleep = sleep or asyncio.sleep
        self._semaphore = asyncio.Semaphore(concurrency)
        self._round_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._auth_generation = 0
        self._auth_error: Exception | None = None
        self.last_summary: dict = {}

    @staticmethod
    def _required(query: dict) -> bool:
        return not query.get("hidden", False) or query.get("metadata", {}).get("required", False)

    async def _progress(self, event: dict) -> None:
        if self.progress is not None:
            value = self.progress(event)
            if inspect.isawaitable(value):
                await value

    def _window(self, query: dict, end_ms: int) -> tuple[int, int]:
        start_ms = int(self.plan["from_ms"])
        if self.plan["mode"] == "fetch":
            return start_ms, min(end_ms, int(self.plan["to_ms"]))
        cursor = self.store.get_cursor(query["key"])
        if cursor is not None:
            # A backwards clock must not regress a cursor or rewrite future data.
            if cursor >= end_ms:
                return end_ms, end_ms
            start_ms = max(start_ms, int(cursor) - self.lookback_ms)
        return start_ms, end_ms

    async def _reauthenticate(self, generation: int) -> None:
        async with self._auth_lock:
            if generation != self._auth_generation:
                return  # Another request already refreshed the shared session.
            if self._auth_error is not None:
                raise self._auth_error
            await self._progress({"event": "authentication_required"})
            try:
                await self.transport.reauthenticate()
            except Exception as error:
                self._auth_error = AuthenticationRequired(f"Authentication could not be restored: {error}")
                raise self._auth_error from error
            self._auth_generation += 1
            await self._progress({"event": "authenticated"})

    def _record_failure(self, query: dict, start_ms: int, end_ms: int, error: str) -> None:
        self.store.record_result(
            query, start_ms, end_ms, [], None,
            status="failed", error=error, observed_ms=self.clock(),
        )

    async def _collect_query(self, query: dict, end_ms: int) -> dict:
        start_ms, end_ms = self._window(query, end_ms)
        result = {"key": query["key"], "start_ms": start_ms, "end_ms": end_ms, "attempts": 0}
        if end_ms <= start_ms:
            return {**result, "status": "skipped"}
        if query.get("kind") == "unsupported" or query.get("error"):
            error = query.get("error") or "Unsupported query"
            self._record_failure(query, start_ms, end_ms, error)
            return {**result, "status": "failed", "error": error}
        transient_retries = 0
        auth_retried = False
        try:
            while True:
                generation = self._auth_generation
                error: Exception | None = None
                try:
                    async with self._semaphore:
                        if self._auth_error is not None:
                            raise self._auth_error
                        generation = self._auth_generation
                        result["attempts"] += 1
                        raw = await asyncio.wait_for(
                            self.transport.execute(query, start_ms, end_ms), self.timeout,
                        )
                        series = parse_response(query, raw)
                    status = "success" if any(item.get("points") for item in series) else "empty"
                    self.store.record_result(
                        query, start_ms, end_ms, series, raw,
                        status=status, observed_ms=self.clock(),
                    )
                    result.update(status=status, series_count=len(series))
                    await self._progress({"event": "query_finished", **result})
                    return result
                except AuthenticationRequired as caught:
                    if not auth_retried:
                        auth_retried = True
                        try:
                            await self._reauthenticate(generation)
                        except Exception as auth_error:
                            error = auth_error
                        else:
                            continue
                    else:
                        error = caught
                        self._auth_error = caught
                except (TimeoutError, QueryError, UnsupportedQuery, OSError) as caught:
                    error = caught
                except Exception as caught:
                    # One malformed response must not discard other queries'
                    # successful results. Unknown failures are never retried.
                    error = caught
                assert error is not None
                message = "Query timed out" if isinstance(error, TimeoutError) else str(error)
                self._record_failure(query, start_ms, end_ms, message)
                retryable = isinstance(error, TimeoutError) or (
                    isinstance(error, QueryError) and error.retryable
                )
                if retryable and transient_retries < self.retries:
                    transient_retries += 1
                    retry_after = getattr(error, "retry_after", None)
                    delay = float(retry_after) if retry_after is not None else min(2 ** (transient_retries - 1), 30)
                    await self._progress({
                        "event": "query_retry", **result,
                        "retry": transient_retries, "delay_seconds": max(delay, 0),
                    })
                    await self.sleep(max(delay, 0))
                    continue
                result.update(status="failed", error=message,
                              fatal_auth=isinstance(error, AuthenticationRequired))
                await self._progress({"event": "query_finished", **result})
                return result
        except asyncio.CancelledError:
            if result.get("status") not in {"success", "empty"}:
                self._record_failure(query, start_ms, end_ms, "Collection interrupted before completion")
            raise

    def _render(self, results: dict[str, dict], end_ms: int) -> dict:
        statuses: Counter = Counter()
        for panel in self.plan["panels"]:
            required = [query for query in panel["queries"] if self._required(query)]
            inputs = [query for query in required if query["kind"] != "math"]
            failed = [results[query["key"]] for query in inputs if results[query["key"]]["status"] == "failed"]
            unsupported = [query for query in required if query["kind"] == "unsupported" or query.get("error")]
            if failed or unsupported:
                errors = [item.get("error", "Query failed") for item in failed]
                errors += [item.get("error") or "Unsupported query" for item in unsupported]
                status = "unsupported" if unsupported else "failed"
                self.store.record_panel_status(panel["id"], status, error="; ".join(dict.fromkeys(errors)), observed_ms=self.clock())
                statuses[status] += 1
                continue
            attempted = [results[q["key"]] for q in inputs if results[q["key"]]["status"] != "skipped"]
            if not attempted:
                statuses["skipped"] += 1
                continue
            start_ms = min(item["start_ms"] for item in attempted)
            panel_end = min(end_ms, max(item["end_ms"] for item in attempted))
            by_ref = {
                query["ref_id"]: self.store.get_query_series(query["key"], start_ms, panel_end)
                for query in inputs
            }
            try:
                series = render_panel(panel, by_ref)
            except Exception as error:
                status = "unsupported" if isinstance(error, UnsupportedQuery) else "failed"
                self.store.record_panel_status(panel["id"], status, error=str(error), observed_ms=self.clock())
                statuses[status] += 1
            else:
                self.store.record_display(panel["id"], series, start_ms, panel_end)
                statuses["success" if any(item.get("points") for item in series) else "empty"] += 1
        return dict(statuses)

    async def collect_until(self, end_ms: int) -> dict:
        """Collect one complete semantic window per query; never split TopK."""
        async with self._round_lock:
            self._auth_error = None
            # Equal semantic keys share one fetch, even if several panels use them.
            queries = {
                query["key"]: query
                for panel in self.plan["panels"] for query in panel["queries"]
                if self._required(query) and query["kind"] != "math"
            }
            tasks = {key: asyncio.create_task(self._collect_query(query, int(end_ms))) for key, query in queries.items()}
            try:
                values = await asyncio.gather(*tasks.values())
            except asyncio.CancelledError:
                for task in tasks.values():
                    task.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
                partial = {key: task.result() for key, task in tasks.items() if not task.cancelled() and task.exception() is None}
                for key in tasks.keys() - partial.keys():
                    start, end = self._window(queries[key], int(end_ms))
                    partial[key] = {"key": key, "status": "failed", "error": "Collection interrupted", "start_ms": start, "end_ms": end, "attempts": 0}
                self.last_summary = self._summary(partial, int(end_ms), cancelled=True)
                raise
            except Exception:
                # A local persistence/progress failure must not leave detached
                # writers running after the CLI closes the store.
                for task in tasks.values():
                    task.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
                raise
            results = dict(zip(tasks, values))
            self.last_summary = self._summary(results, int(end_ms))
            await self._progress({"event": "round_finished", **self.last_summary})
            fatal_auth = next((item["error"] for item in results.values() if item.get("fatal_auth")), None)
            if fatal_auth:
                # Headless expiry/manual-login timeout needs user action. Finish
                # persisting this round, then let the CLI's finally export it.
                raise AuthenticationRequired(fatal_auth)
            return self.last_summary

    def _summary(self, results: dict[str, dict], end_ms: int, *, cancelled: bool = False) -> dict:
        return {
            "end_ms": end_ms,
            "queries": dict(Counter(item["status"] for item in results.values())),
            "panels": self._render(results, end_ms),
            "attempts": sum(item["attempts"] for item in results.values()),
            "results": list(results.values()),
            "cancelled": cancelled,
            "fatal_auth": any(item.get("fatal_auth", False) for item in results.values()),
        }

    async def _wait(self, seconds: float, stop_event=None) -> bool:
        """Wait without preventing an explicit stop; injected sleep is in seconds."""
        if stop_event is None:
            await self.sleep(seconds)
            return True
        if stop_event.is_set():
            return False
        sleeper = asyncio.create_task(self.sleep(seconds))
        stopper = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
            return not stop_event.is_set()
        finally:
            for task in (sleeper, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)

    async def _watch_round(self, end_ms: int, stop_event=None) -> dict | None:
        if stop_event is None:
            return await self.collect_until(end_ms)
        if stop_event.is_set():
            return None
        collector = asyncio.create_task(self.collect_until(end_ms))
        stopper = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait({collector, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if collector.done():
                return await collector
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            return None
        finally:
            for task in (collector, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(collector, stopper, return_exceptions=True)

    async def watch(
        self, *, poll_interval_ms: int = 300_000, rounds: int | None = None,
        duration_seconds: float | None = None, stop_event=None,
    ) -> dict:
        """Poll serially, catching cancellation so the CLI can export saved data."""
        if self.plan["mode"] != "watch":
            raise ValueError("watch() requires a watch plan")
        if poll_interval_ms <= 0 or (rounds is not None and rounds < 0) or (duration_seconds is not None and duration_seconds < 0):
            raise ValueError("Invalid polling interval, rounds, or duration")
        started = self.clock()
        deadline = None if duration_seconds is None else started + int(duration_seconds * 1000)
        keys = {query["key"] for panel in self.plan["panels"] for query in panel["queries"] if self._required(query) and query["kind"] != "math"}
        cursors = [self.store.get_cursor(key) for key in keys]
        has_progress = any(cursor is not None for cursor in cursors)
        dataset_start = int(self.plan["from_ms"])
        plan_started = int(self.plan.get("started_ms", dataset_start))
        if has_progress or dataset_start < plan_started:
            next_due = started  # Resume and explicit --since catch up immediately.
        else:
            next_due = max(started, dataset_start + poll_interval_ms)
        completed = 0
        reason = "rounds"
        try:
            while rounds is None or completed < rounds:
                if stop_event is not None and stop_event.is_set():
                    reason = "stopped"
                    break
                now = self.clock()
                if deadline is not None and (now >= deadline or next_due > deadline):
                    if now < deadline and not await self._wait((deadline - now) / 1000, stop_event):
                        reason = "stopped"
                    else:
                        reason = "duration"
                    break
                if next_due > now and not await self._wait((next_due - now) / 1000, stop_event):
                    reason = "stopped"
                    break
                now = self.clock()
                result = await self._watch_round(now, stop_event)
                if result is None:
                    reason = "stopped"
                    break
                completed += 1
                # Never overlap or burst missed rounds; all gaps are in cursors.
                next_due += poll_interval_ms
                now = self.clock()
                if next_due < now:
                    next_due += ((now - next_due) // poll_interval_ms + 1) * poll_interval_ms
        except asyncio.CancelledError:
            reason = "cancelled"
        return {"rounds": completed, "reason": reason, "last": self.last_summary}
