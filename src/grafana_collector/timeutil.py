"""Time parsing. Public CLI times without an offset use Asia/Shanghai."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from .errors import CollectorError

TZ = ZoneInfo("Asia/Shanghai")
_UNITS = {"ms": 1, "s": 1000, "m": 60000, "h": 3600000, "d": 86400000, "w": 604800000}


def duration_ms(value: str | int | float) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    value = value.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)", value)
    if not match:
        raise CollectorError(f"无效时长 {value!r}，请使用 30s、5m、1h 等形式。")
    return int(float(match[1]) * _UNITS[match[2]])


def parse_time(value: str | int, *, now_ms: int | None = None) -> int:
    text = str(value).strip()
    now_ms = now_ms if now_ms is not None else int(datetime.now().timestamp() * 1000)
    relative = re.fullmatch(r"now(?:([-+])(\d+(?:\.\d+)?(?:ms|s|m|h|d|w)))?(?:/([smhdw]))?", text)
    if relative:
        result = now_ms
        if relative[1]:
            result += duration_ms(relative[2]) * (-1 if relative[1] == "-" else 1)
        if relative[3]:
            dt = datetime.fromtimestamp(result / 1000, TZ)
            unit = relative[3]
            if unit == "w":
                dt = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            elif unit == "d":
                dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            elif unit == "h":
                dt = dt.replace(minute=0, second=0, microsecond=0)
            elif unit == "m":
                dt = dt.replace(second=0, microsecond=0)
            else:
                dt = dt.replace(microsecond=0)
            result = int(dt.timestamp() * 1000)
        return result
    if re.fullmatch(r"\d{10}|\d{13}", text):
        return int(text) * (1000 if len(text) == 10 else 1)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectorError(f"无效时间 {text!r}；可用 ISO 时间、毫秒时间戳或 now-6h。") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp() * 1000)


def dashboard_identity(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise CollectorError("请输入不含登录凭证的 http(s) Grafana 大盘链接。")
    secret_parameters = {"token", "access_token", "refresh_token", "api_key", "apikey", "auth", "authorization",
                         "password", "secret", "session", "sessionid"}
    if any(key.lower() in secret_parameters for key, _ in parse_qsl(parsed.query)):
        raise CollectorError("大盘链接不能包含令牌、密码或会话参数；请使用浏览器登录。")
    match = re.search(r"/(?:d|d-solo)/([^/]+)", parsed.path)
    if not match:
        raise CollectorError("链接应包含 /d/<dashboard UID>/。")
    prefix = parsed.path[:match.start()]
    base = f"{parsed.scheme}://{parsed.netloc}{prefix}"
    org_id = parse_qs(parsed.query).get("orgId", ["1"])[0]
    return base, match[1], org_id


def display_range(url: str, dashboard: dict, *, now_ms: int, from_value=None, to_value=None) -> tuple[int, int]:
    args = parse_qs(urlsplit(url).query)
    default = dashboard.get("time", {})
    start = parse_time(from_value or args.get("from", [default.get("from", "now-30m")])[0], now_ms=now_ms)
    end = parse_time(to_value or args.get("to", [default.get("to", "now")])[0], now_ms=now_ms)
    if start >= end:
        raise CollectorError("开始时间必须早于结束时间。")
    return start, end


def iso_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, TZ).isoformat(timespec="seconds")
