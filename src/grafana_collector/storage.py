"""Durable, restartable storage for a frozen collection plan.

All timestamps are UTC milliseconds. SQLite stores numbers as JSON rather than
REAL so that the source's integers are not silently rounded before export.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time


_SECRET_KEYS = {"authorization", "cookie", "set-cookie", "password", "passwd",
                "access_token", "refresh_token", "client_secret", "securejsondata",
                "basicauthpassword", "apikey", "api_key"}


def _safe(value):
    """Defence in depth: request credentials never enter the data package."""
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if str(k).lower() in _SECRET_KEYS else _safe(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json(value):
    return json.dumps(_safe(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return time.time_ns() // 1_000_000


class Store:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.run_dir / "collection.sqlite3"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS queries (
                key TEXT PRIMARY KEY, definition TEXT NOT NULL, cursor_ms INTEGER);
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, query_key TEXT NOT NULL REFERENCES queries(key),
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, status TEXT NOT NULL,
                error TEXT, observed_ms INTEGER NOT NULL, raw_path TEXT,
                quality TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS attempt_query ON attempts(query_key, id);
            CREATE TABLE IF NOT EXISTS series (
                id TEXT PRIMARY KEY, owner_kind TEXT NOT NULL, owner_key TEXT NOT NULL,
                description TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS series_owner ON series(owner_kind, owner_key);
            CREATE TABLE IF NOT EXISTS points (
                series_id TEXT NOT NULL REFERENCES series(id), timestamp_ms INTEGER NOT NULL,
                value_json TEXT NOT NULL, observed_ms INTEGER NOT NULL,
                PRIMARY KEY (series_id, timestamp_ms));
            CREATE TABLE IF NOT EXISTS panel_state (
                panel_id INTEGER PRIMARY KEY, status TEXT NOT NULL, error TEXT,
                observed_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS display_updates (
                id INTEGER PRIMARY KEY, panel_id INTEGER NOT NULL,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                observed_ms INTEGER NOT NULL);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.connection.close()

    @contextmanager
    def read_snapshot(self):
        """Keep an export internally consistent while collection continues."""
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN")
        try:
            yield
        finally:
            if owns_transaction:
                self.connection.rollback()

    def initialize(self, plan):
        """Persist once; resuming with a different plan is an explicit error."""
        encoded = _json(plan)
        existing = self.connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()
        if existing:
            if existing[0] != encoded:
                raise ValueError("Run already has a different frozen plan; resume the saved plan or use a new directory")
            return
        if int(plan["to_ms"]) < int(plan["from_ms"]):
            raise ValueError("Plan end precedes dataset start")
        with self.connection:
            self.connection.execute("INSERT INTO metadata VALUES ('plan', ?)", (encoded,))
            for panel in plan["panels"]:
                for query in panel["queries"]:
                    old = self.connection.execute("SELECT definition FROM queries WHERE key=?", (query["key"],)).fetchone()
                    if old and old[0] != _json(query):
                        raise ValueError("Duplicate query key with different definitions")
                    self.connection.execute("INSERT OR IGNORE INTO queries(key,definition) VALUES (?,?)",
                                            (query["key"], _json(query)))

    def load_plan(self):
        row = self.connection.execute("SELECT value FROM metadata WHERE key='plan'").fetchone()
        if row is None:
            raise ValueError("Run has not been initialized")
        return json.loads(row[0])

    def get_cursor(self, query_key):
        row = self.connection.execute("SELECT cursor_ms FROM queries WHERE key=?", (query_key,)).fetchone()
        return row[0] if row else None

    def _upsert_series(self, kind, owner, series, start_ms, end_ms, observed_ms):
        quality = {"nonfinite_points": 0, "out_of_range_points": 0}
        for item in series:
            description = {k: v for k, v in item.items() if k not in {"points", "series_id"}}
            description.setdefault("labels", {})
            description.setdefault("ref_id", "")
            description.setdefault("metric", "")
            # A legend is deliberately absent: renames are not new time series.
            identity = {"kind": kind, "owner": str(owner), "metric": description["metric"],
                        "ref_id": description["ref_id"], "labels": description["labels"],
                        "query_key": description.get("query_key", "")}
            series_id = _digest(identity)
            self.connection.execute(
                "INSERT INTO series VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET description=excluded.description",
                (series_id, kind, str(owner), _json(description)))
            rows = []
            for timestamp, value in item.get("points", []):
                timestamp = int(timestamp)
                if timestamp < start_ms or timestamp > end_ms:
                    quality["out_of_range_points"] += 1
                    continue
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError("Time-series values must be numbers or null")
                if isinstance(value, float) and not math.isfinite(value):
                    quality["nonfinite_points"] += 1
                    value = None
                rows.append((series_id, timestamp, _json(value), observed_ms))
            self.connection.executemany(
                "INSERT INTO points VALUES (?,?,?,?) ON CONFLICT(series_id,timestamp_ms) "
                "DO UPDATE SET value_json=excluded.value_json,observed_ms=excluded.observed_ms "
                "WHERE excluded.observed_ms >= points.observed_ms", rows)
        return quality

    def _advance_cursor(self, query_key, dataset_start):
        boundary = dataset_start
        covered = False
        for row in self.connection.execute(
            "SELECT start_ms,end_ms FROM attempts WHERE query_key=? AND status IN ('success','empty') "
            "ORDER BY start_ms,end_ms", (query_key,)):
            if row[0] > boundary:
                break
            if row[1] >= boundary:
                boundary = row[1]
                covered = True
        self.connection.execute("UPDATE queries SET cursor_ms=? WHERE key=?",
                                (boundary if covered else None, query_key))

    def record_result(self, query, start_ms, end_ms, series, raw, *, status="success", error=None, observed_ms=None):
        """Persist normalized results; raw is accepted for caller compatibility only."""
        status = {"no_data": "empty", "error": "failed"}.get(status, status)
        if status not in {"success", "empty", "failed", "unsupported"}:
            raise ValueError(f"Unknown query status: {status}")
        if end_ms < start_ms:
            raise ValueError("Query end precedes start")
        if status == "empty" and any(item.get("points") for item in series):
            raise ValueError("Empty query result contains data points")
        definition = self.connection.execute("SELECT definition FROM queries WHERE key=?", (query["key"],)).fetchone()
        if not definition:
            raise ValueError("Query is not in this run's frozen plan")
        if definition[0] != _json(query):
            raise ValueError("Query differs from this run's frozen definition")
        dataset_start = int(self.load_plan()["from_ms"])
        observed_ms = _now() if observed_ms is None else int(observed_ms)
        with self.connection:
            quality = {"nonfinite_points": 0, "out_of_range_points": 0}
            if status in {"success", "empty"}:
                # The response is authoritative for this queried window.
                # This removes vanished TopK members and genuinely empty
                # ranges without mixing old curves into current expressions.
                # Failed attempts never enter this branch.
                self.connection.execute(
                    "DELETE FROM points WHERE series_id IN "
                    "(SELECT id FROM series WHERE owner_kind='query' AND owner_key=?) "
                    "AND timestamp_ms>=? AND timestamp_ms<=? AND observed_ms<=?",
                    (query["key"], max(dataset_start, start_ms), end_ms, observed_ms))
                quality = self._upsert_series("query", query["key"], series,
                                              max(dataset_start, start_ms), end_ms, observed_ms)
            # Keep the legacy column so existing SQLite runs can resume without
            # migration, but never retain raw responses or new raw paths.
            self.connection.execute(
                "INSERT INTO attempts(query_key,start_ms,end_ms,status,error,observed_ms,raw_path,quality) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (query["key"], start_ms, end_ms, status, error, observed_ms, None, _json(quality)))
            self._advance_cursor(query["key"], dataset_start)

    def _read_series(self, kind, owner, start_ms=None, end_ms=None):
        result = []
        for row in self.connection.execute(
            "SELECT id,description FROM series WHERE owner_kind=? AND owner_key=? ORDER BY id", (kind, str(owner))):
            sql = "SELECT timestamp_ms,value_json FROM points WHERE series_id=?"
            args = [row["id"]]
            if start_ms is not None:
                sql += " AND timestamp_ms>=?"
                args.append(int(start_ms))
            if end_ms is not None:
                sql += " AND timestamp_ms<=?"
                args.append(int(end_ms))
            sql += " ORDER BY timestamp_ms"
            points = [[p[0], json.loads(p[1])] for p in self.connection.execute(sql, args)]
            # Old descriptions remain for provenance, but are not phantom columns.
            if not points:
                continue
            item = json.loads(row["description"])
            item.update(series_id=row["id"], points=points)
            result.append(item)
        return result

    def get_query_series(self, query_key, start_ms=None, end_ms=None):
        return self._read_series("query", query_key, start_ms, end_ms)

    def record_display(self, panel_id, series, start_ms, end_ms):
        if end_ms < start_ms:
            raise ValueError("Display end precedes start")
        plan = self.load_plan()
        if panel_id not in {p["id"] for p in plan["panels"]}:
            raise ValueError("Panel is not in this run")
        start_ms = max(int(start_ms), int(plan["from_ms"]))
        observed_ms = _now()
        with self.connection:
            self.connection.execute(
                "DELETE FROM points WHERE series_id IN (SELECT id FROM series WHERE owner_kind='display' AND owner_key=?) "
                "AND timestamp_ms>=? AND timestamp_ms<=?", (str(panel_id), start_ms, end_ms))
            self._upsert_series("display", panel_id, series, start_ms, end_ms, observed_ms)
            self.connection.execute("INSERT INTO display_updates(panel_id,start_ms,end_ms,observed_ms) VALUES (?,?,?,?)",
                                    (panel_id, start_ms, end_ms, observed_ms))
            has_points = any(start_ms <= int(p[0]) <= end_ms for s in series for p in s.get("points", []))
            self.connection.execute(
                "INSERT INTO panel_state VALUES (?,?,?,?) ON CONFLICT(panel_id) DO UPDATE SET "
                "status=excluded.status,error=excluded.error,observed_ms=excluded.observed_ms",
                (panel_id, "success" if has_points else "empty", None, observed_ms))

    def record_panel_status(self, panel_id, status, error=None, observed_ms=None):
        if status not in {"success", "empty", "failed", "unsupported", "pending"}:
            raise ValueError(f"Unknown panel status: {status}")
        if panel_id not in {p["id"] for p in self.load_plan()["panels"]}:
            raise ValueError("Panel is not in this run")
        with self.connection:
            self.connection.execute(
                "INSERT INTO panel_state VALUES (?,?,?,?) ON CONFLICT(panel_id) DO UPDATE SET "
                "status=excluded.status,error=excluded.error,observed_ms=excluded.observed_ms",
                (panel_id, status, error, _now() if observed_ms is None else observed_ms))

    def get_display_series(self, panel_id, start_ms=None, end_ms=None):
        return self._read_series("display", panel_id, start_ms, end_ms)

    def display_coverage(self, panel_id, start_ms, end_ms):
        """Successfully rendered intervals, including confirmed no-data ranges."""
        intervals = []
        for row in self.connection.execute(
            "SELECT start_ms,end_ms FROM display_updates WHERE panel_id=? "
            "AND end_ms>=? AND start_ms<=? ORDER BY start_ms,end_ms",
            (panel_id, start_ms, end_ms)):
            lower, upper = max(start_ms, row[0]), min(end_ms, row[1])
            if intervals and lower <= intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], upper)
            else:
                intervals.append([lower, upper])
        if len(intervals) == 1 and intervals[0][0] <= start_ms and intervals[0][1] >= end_ms:
            status = "covered"
        else:
            status = "partial" if intervals else "uncollected"
        return {"status": status, "from_ms": start_ms, "to_ms": end_ms, "intervals": intervals}

    def query_statuses(self):
        result = []
        for row in self.connection.execute("SELECT * FROM queries ORDER BY key"):
            query = json.loads(row["definition"])
            if query.get("kind") == "math":
                continue
            attempts = [dict(a) for a in self.connection.execute(
                "SELECT * FROM attempts WHERE query_key=? ORDER BY id", (row["key"],))]
            for attempt in attempts:
                # Old runs may still contain raw paths. They are legacy local
                # artifacts, not part of the current result/provenance contract.
                attempt.pop("raw_path", None)
                attempt["quality"] = json.loads(attempt["quality"])
            latest = attempts[-1] if attempts else {}
            result.append({"key": row["key"], "query_key": row["key"], "panel_id": query["panel_id"],
                           "ref_id": query["ref_id"], "status": latest.get("status", "unsupported" if query.get("error") else "pending"),
                           "cursor_ms": row["cursor_ms"], "last_error": latest.get("error") or query.get("error"),
                           "last_start_ms": latest.get("start_ms"), "last_end_ms": latest.get("end_ms"),
                           "attempts": attempts, "definition": query})
        return result

    def panel_statuses(self):
        query_status = {q["key"]: q for q in self.query_statuses()}
        result = []
        for panel in self.load_plan()["panels"]:
            required = {q["ref_id"] for q in panel["queries"] if not q.get("hidden")}
            while True:
                expanded = required | {dep for q in panel["queries"] if q["ref_id"] in required for dep in q.get("dependencies", [])}
                if expanded == required:
                    break
                required = expanded
            children = [query_status[q["key"]] for q in panel["queries"] if q["ref_id"] in required and q["key"] in query_status]
            row = self.connection.execute("SELECT * FROM panel_state WHERE panel_id=?", (panel["id"],)).fetchone()
            saved = dict(row) if row else {}
            point_count = self.connection.execute(
                "SELECT COUNT(*) FROM points p JOIN series s ON s.id=p.series_id WHERE s.owner_kind='display' AND s.owner_key=?",
                (str(panel["id"]),)).fetchone()[0]
            errors = [q["last_error"] for q in children if q["status"] in {"failed", "unsupported"} and q["last_error"]]
            if saved.get("error"):
                errors.append(saved["error"])
            bad = saved.get("status") in {"failed", "unsupported"} or any(q["status"] in {"failed", "unsupported"} for q in children)
            if bad:
                status = "partial" if point_count else ("unsupported" if saved.get("status") == "unsupported" or any(q["status"] == "unsupported" for q in children) else "failed")
            elif saved.get("status") in {"success", "empty"}:
                # Overall status describes accumulated data, while the latest
                # batch's no-data state remains available separately below.
                status = "success" if point_count else "empty"
            else:
                status = "pending"
            result.append({"panel_id": panel["id"], "title": panel["title"], "group": panel.get("group", ""),
                           "status": status, "point_count": point_count, "errors": list(dict.fromkeys(errors)),
                           "last_display_status": saved.get("status"), "observed_ms": saved.get("observed_ms"),
                           "queries": [{k: q[k] for k in ("key", "ref_id", "status", "cursor_ms", "last_error")} for q in children]})
        return result
