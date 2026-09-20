"""Public CLI. All network operations are explicit commands; export is offline."""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
import tomllib
from pathlib import Path

from . import __version__
from .errors import CollectorError, AuthenticationRequired
from .exporting import DEFAULT_FORMAT, EXPORT_FORMATS, validate_format
from .timeutil import duration_ms, parse_time, display_range, iso_time, dashboard_identity


def parser():
    root = argparse.ArgumentParser(description="SDKv2 Grafana 历史下载与增量采集")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    for name, help_text in (("login", "浏览器登录并验证真实查询"), ("inspect", "查看查询口径"),
                            ("fetch", "历史下载并导出 Parquet 或 Excel"), ("watch", "持续增量采集"),
                            ("export", "从本地运行记录离线导出 Parquet 或 Excel")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", type=Path, help="TOML 配置文件；命令行参数优先")
        p.add_argument("--out", type=Path, help="运行/导出目录；inspect 可指定 JSON 文件")
        p.add_argument("--panels", help="面板 ID，逗号分隔，例如 166,70,461")
        if name in {"fetch", "watch", "export"}:
            p.add_argument("--format", choices=EXPORT_FORMATS,
                           help="输出格式，默认 parquet；xlsx 导出带 Grafana 单位的 Excel")
        if name != "export":
            p.add_argument("--url", help="带时间窗和筛选变量的 Grafana 大盘链接")
            p.add_argument("--profile", type=Path, help="专用 Chrome profile 目录")
            p.add_argument("--headless", action="store_true", default=None, help="已有登录状态时后台运行；过期会退出保留进度")
            p.add_argument("--login-timeout", type=float, help="等待手动登录秒数，默认 600")
            p.add_argument("--timeout", type=float, help="单次请求秒数，默认 30")
        if name in {"fetch", "export"}:
            p.add_argument("--from", dest="from_value", help="开始时间，未带时区时按北京时间")
            p.add_argument("--to", dest="to_value", help="结束时间，未带时区时按北京时间")
        if name in {"fetch", "watch"}:
            p.add_argument("--concurrency", type=int, help="并发请求数，默认 4")
            p.add_argument("--retries", type=int, help="临时故障重试次数，默认 3")
        if name == "watch":
            p.add_argument("--since", help="明确指定补采起点；默认从命令启动时刻开始")
            p.add_argument("--poll-interval", help="轮询周期，默认 5m，与数据采样间隔独立")
            p.add_argument("--lookback", help="迟到数据回查范围，默认 5m，可设 0s")
            p.add_argument("--rounds", type=int, help="采集指定轮数后正常停止并导出")
            p.add_argument("--duration", help="运行指定时长后正常停止，例如 1h")
        if name == "export":
            p.add_argument("--run", type=Path, help="已有 collection.sqlite3 的运行目录")
    return root


def settings(args):
    values = vars(args).copy()
    provided = {key for key, value in values.items() if value is not None}
    if args.config:
        with Path(args.config).expanduser().open("rb") as f:
            config = tomllib.load(f)
        merged = {k.replace("-", "_"): v for k, v in config.items()
                  if not isinstance(v, dict) or k == "format"}
        merged.update({k.replace("-", "_"): v for k, v in config.get(args.command, {}).items()})
        aliases = {"from": "from_value", "to": "to_value"}
        for key, value in merged.items():
            key = aliases.get(key, key)
            if key in values and values[key] is None:
                values[key] = value
                provided.add(key)
    defaults = {"headless": False, "login_timeout": 600, "timeout": 30, "concurrency": 4,
                "retries": 3, "poll_interval": "5m", "lookback": "5m"}
    for key, value in defaults.items():
        if values.get(key) is None:
            values[key] = value
    if args.command in {"fetch", "watch", "export"}:
        selected_format = values.get("format")
        values["format"] = validate_format(DEFAULT_FORMAT if selected_format is None else selected_format)
    for key in ("profile", "out", "run"):
        if values.get(key):
            values[key] = Path(values[key]).expanduser()
    panels = values.get("panels")
    if panels:
        try:
            values["panel_ids"] = [int(p) for p in (panels if isinstance(panels, list) else str(panels).split(","))]
        except ValueError as exc:
            raise CollectorError("--panels 必须是逗号分隔的整数 ID。") from exc
    else:
        values["panel_ids"] = None
    if values["timeout"] <= 0 or values["login_timeout"] <= 0 or values["concurrency"] < 1 or values["retries"] < 0:
        raise CollectorError("超时、并发必须为正数，重试次数不能为负数。")
    if values.get("rounds") is not None and values["rounds"] < 1:
        raise CollectorError("--rounds 必须大于 0。")
    values["provided"] = provided
    return argparse.Namespace(**values)


def emit(message):
    if isinstance(message, dict):
        if message.get("event") == "round_finished":
            message = {key: value for key, value in message.items() if key != "results"}
        message = json.dumps(message, ensure_ascii=False)
    print(message, flush=True)


def read_saved_plan(out):
    from .storage import Store
    if not out or not (out / "collection.sqlite3").is_file():
        return None
    with Store(out) as store:
        try:
            return store.load_plan()
        except ValueError:
            return None


async def make_plan(args, session, started_ms):
    from .query import compile_dashboard
    dashboard, datasources, widths = await session.discover()
    ref_start, ref_end = display_range(args.url, dashboard, now_ms=started_ms,
                                       from_value=getattr(args, "from_value", None),
                                       to_value=getattr(args, "to_value", None))
    compiled = compile_dashboard(dashboard, datasources, args.url,
                                 reference_from_ms=ref_start, reference_to_ms=ref_end,
                                 panel_widths=widths, panel_ids=args.panel_ids)
    if not compiled["panels"]:
        raise CollectorError("没有匹配的图表面板，请核对 --panels。")
    mode = "watch" if args.command == "watch" else "fetch"
    start = (parse_time(args.since, now_ms=started_ms) if getattr(args, "since", None) else started_ms) if mode == "watch" else ref_start
    end = started_ms if mode == "watch" else ref_end
    if start > end:
        raise CollectorError("持续采集起点不能晚于当前时间。")
    metadata = {**compiled.get("metadata", {}), "timezone": "Asia/Shanghai",
                "reference_from_ms": ref_start, "reference_to_ms": ref_end,
                "panel_widths": {str(k): v for k, v in widths.items()},
                "measurement": getattr(session, "measurement", {}),
                "poll_interval_ms": duration_ms(args.poll_interval), "lookback_ms": duration_ms(args.lookback),
                "collector_version": __version__, "incremental": mode == "watch"}
    return {"schema_version": 1, "source_url": args.url, "dashboard": dashboard,
            "variables": compiled["variables"], "panels": compiled["panels"],
            "from_ms": start, "to_ms": end, "mode": mode, "started_ms": started_ms,
            "metadata": metadata}


def summarize_plan(plan):
    queries = [q for p in plan["panels"] for q in p["queries"]]
    active = [q for q in queries if not q.get("hidden") or q.get("metadata", {}).get("required")]
    unsupported = [q for q in active if q.get("error") or q["kind"] == "unsupported"]
    emit(f"大盘 {plan['dashboard'].get('title')}，{len(plan['panels'])} 个面板，{len(active)} 条活动查询/表达式。")
    emit(f"数据范围起点：{iso_time(plan['from_ms'])}；自动采样参考跨度已固定。")
    for q in unsupported:
        emit(f"不支持：panel={q['panel_id']} ref={q['ref_id']} {q.get('error')}")
    return unsupported


async def run(args):
    from .storage import Store
    from .exporting import export_run
    started_ms = time.time_ns() // 1_000_000
    if args.command == "export":
        if not args.run or not (args.run / "collection.sqlite3").is_file():
            raise CollectorError("--run 必须指向已有 collection.sqlite3 的运行目录。")
        with Store(args.run) as store:
            manifest = export_run(store, args.out or args.run,
                                  from_ms=parse_time(args.from_value, now_ms=started_ms) if args.from_value else None,
                                  to_ms=parse_time(args.to_value, now_ms=started_ms) if args.to_value else None,
                                  panel_ids=args.panel_ids, format=args.format)
        emit(f"离线导出完成（{args.format}）：{manifest}")
        return 0
    if args.command in {"fetch", "watch"} and not args.out:
        raise CollectorError("请用 --out 指定本次运行的数据目录。")
    saved = read_saved_plan(args.out) if args.command in {"fetch", "watch"} else None
    if saved:
        if saved["mode"] != args.command:
            raise CollectorError("输出目录属于另一种采集模式，请使用新目录。")
        if args.url and args.url != saved["source_url"]:
            raise CollectorError("恢复时不能修改链接或筛选条件，请使用新目录。")
        if args.panel_ids and set(args.panel_ids) != {p["id"] for p in saved["panels"]}:
            raise CollectorError("恢复时不能修改面板范围，请使用新目录或在 export 时筛选。")
        if getattr(args, "since", None) or getattr(args, "from_value", None) or getattr(args, "to_value", None):
            raise CollectorError("恢复沿用原时间起点；更换时间范围请使用新的 --out。")
        if args.command == "watch":
            for option in ("poll_interval", "lookback"):
                if option in args.provided and duration_ms(getattr(args, option)) != saved["metadata"][option + "_ms"]:
                    raise CollectorError(f"恢复沿用已保存的 {option}；更改此参数请使用新目录。")
        args.url = saved["source_url"]
    if not args.url:
        raise CollectorError("请通过 --url 或配置文件提供 Grafana 链接。")
    dashboard_identity(args.url)
    if duration_ms(args.poll_interval) <= 0:
        raise CollectorError("轮询周期必须大于 0。")
    from .transport import BrowserSession
    async with BrowserSession(args.url, profile=args.profile, headless=args.headless,
                              login_timeout=args.login_timeout, request_timeout=args.timeout, progress=emit) as session:
        if saved:
            await session.ensure_authenticated()
            plan = saved
            emit("恢复已保存的配置、采样口径与逐查询进度。")
        else:
            plan = await make_plan(args, session, started_ms)
        unsupported = summarize_plan(plan)
        if args.command == "inspect":
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
                emit(f"查询计划：{args.out.resolve()}")
            else:
                emit({"variables": plan["variables"], "sampling": plan["metadata"],
                      "dashboard_version": plan["dashboard"].get("version")})
                for p in plan["panels"]:
                    emit(f"{p['id']}  {p['group']} / {p['title']}")
                    for q in p["queries"]:
                        emit({key: q.get(key) for key in ("ref_id", "kind", "hidden", "route",
                              "interval_ms", "unit", "query", "metadata", "error")})
            return 2 if unsupported else 0
        if args.command == "login":
            from .query import parse_response
            candidate = next((q for p in plan["panels"] for q in p["queries"]
                              if q["kind"] == "opentsdb" and not q.get("hidden") and not q.get("error")), None)
            if not candidate:
                raise CollectorError("没有可用于验证的查询。")
            try:
                raw = await session.execute(candidate, plan["from_ms"], plan["to_ms"])
            except AuthenticationRequired:
                await session.reauthenticate()
                raw = await session.execute(candidate, plan["from_ms"], plan["to_ms"])
            series = parse_response(candidate, raw)
            emit(f"登录及真实查询已验证：panel={candidate['panel_id']}，返回 {len(series)} 条曲线。")
            return 0
        from .engine import Collector
        with Store(args.out) as store:
            store.initialize(plan)
            collector = Collector(plan, store, session, concurrency=args.concurrency, timeout=args.timeout,
                                  retries=args.retries, lookback_ms=plan["metadata"]["lookback_ms"], progress=emit)
            interrupted = False
            try:
                if args.command == "fetch":
                    await collector.collect_until(plan["to_ms"])
                else:
                    stop = asyncio.Event()
                    loop = asyncio.get_running_loop()
                    previous = {}
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        previous[sig] = signal.getsignal(sig)
                        loop.add_signal_handler(sig, stop.set)
                    emit("持续采集已启动。按 Ctrl+C 正常停止并导出。")
                    try:
                        await collector.watch(poll_interval_ms=plan["metadata"]["poll_interval_ms"],
                                              rounds=args.rounds,
                                              duration_seconds=duration_ms(args.duration) / 1000 if args.duration else None,
                                              stop_event=stop)
                    finally:
                        for sig, handler in previous.items():
                            loop.remove_signal_handler(sig)
                            signal.signal(sig, handler)
            except asyncio.CancelledError:
                interrupted = True
                emit("采集已中断，正在导出已保存的数据。")
            finally:
                manifest = export_run(store, args.out, format=args.format)
                emit(f"{args.format} 数据与结果清单：{manifest}")
            failures = [p for p in store.panel_statuses() if p.get("status") not in {"success", "empty", "no_data"}]
            if failures:
                emit(f"{len(failures)} 个面板存在失败、部分结果或未完成状态，详见 manifest。")
            return 130 if interrupted else (2 if failures else 0)


def main(argv=None):
    try:
        args = settings(parser().parse_args(argv))
        return_code = asyncio.run(run(args))
    except (CollectorError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return_code = 1
    except KeyboardInterrupt:
        print("已停止。已写入的数据可通过 export 命令导出。", file=sys.stderr)
        return_code = 130
    raise SystemExit(return_code)


if __name__ == "__main__":
    main()
