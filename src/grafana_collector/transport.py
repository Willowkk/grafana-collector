"""Authenticated Grafana HTTP transport backed by a dedicated Chrome profile."""
from __future__ import annotations

import asyncio
import os
import re
import time
from copy import deepcopy
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qsl, urlunsplit

from .errors import AuthenticationRequired, CollectorError, QueryError
from .timeutil import dashboard_identity


_SECRET_PARAMETER = re.compile(r"token|secret|password|passwd|auth|credential|signature|session|api[_-]?key|^(?:key|sig|jwt)$", re.I)


def _retry_delay(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(seconds, 0) if math.isfinite(seconds) else None


def _login_redirect(location):
    """A canonical-path redirect is not proof that authentication expired."""
    parts = urlsplit(location or "")
    return bool(re.search(r"login|sign[-_]?in|sso|oauth|saml|passport|authenticate", parts.hostname or "", re.I)
                or re.search(r"(?:^|/)(?:login|sign[-_]?in|sso|oauth\w*|saml\w*|passport|auth\w*)(?:/|$)", parts.path, re.I))


def default_profile() -> Path:
    return Path.home() / ".local" / "share" / "grafana-collector" / "chrome-profile"


def sanitize_datasource(ds: dict) -> dict:
    """Keep query routing metadata, never auth headers/passwords/secureJsonData."""
    fields = ("id", "uid", "name", "type", "access", "url", "isDefault", "interval", "database", "orgId")
    result = {key: ds[key] for key in fields if key in ds and not isinstance(ds[key], (dict, list))}
    if result.get("url"):
        try:
            parsed = urlsplit(str(result["url"]))
            port = parsed.port
        except ValueError as exc:
            raise CollectorError("数据源 URL 格式无效，无法安全解析路由。") from exc
        sensitive = [(key, value) for key, value in parse_qsl(parsed.query)
                     if _SECRET_PARAMETER.search(key)]
        if parsed.username or parsed.password or sensitive:
            result["collectorAuthRequired"] = True
        clean_query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                       if not _SECRET_PARAMETER.search(key)]
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        if port:
            hostname += f":{port}"
        result["url"] = urlunsplit((parsed.scheme, hostname, parsed.path, urlencode(clean_query), ""))
    data = ds.get("jsonData") or {}
    allowed = ("tsdbVersion", "tsdbResolution", "jwtRegion", "isVRegionAble", "timeInterval", "minInterval", "interval", "tenant", "httpMethod")
    result["jsonData"] = {key: data[key] for key in allowed if key in data and not isinstance(data[key], (dict, list))}
    if "vRegionDispatchers" in data:
        # Only the plugin's routing schema is retained, not arbitrary nested
        # datasource records that could contain credential fields.
        routes = data["vRegionDispatchers"]
        result["jsonData"]["vRegionDispatchers"] = [
            {"regions": [region for region in item.get("regions", []) if isinstance(region, str)]
             if isinstance(item.get("regions"), list) else [],
             "datasourceId": item.get("datasourceId") if isinstance(item.get("datasourceId"), (str, int)) else None}
            for item in routes if isinstance(item, dict)
        ] if isinstance(routes, list) else ([{}] if routes else None)
    return result


class BrowserSession:
    def __init__(self, source_url: str, *, profile: Path | None = None, headless=False,
                 login_timeout=600, request_timeout=30, progress=print):
        self.source_url = source_url
        self.base_url, self.uid, self.org_id = dashboard_identity(source_url)
        self.profile = Path(profile) if profile else default_profile()
        self.headless = headless
        self.login_timeout = login_timeout
        self.request_timeout = request_timeout
        self.progress = progress
        self.context = self.page = self._playwright = None
        self._auth_lock = asyncio.Lock()
        self._failed_auth_probe = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.profile, 0o700)
        self._playwright = await async_playwright().start()
        try:
            self.context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=self.headless,
                viewport={"width": 1440, "height": 900}, locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
            )
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        except Exception as exc:
            await self._playwright.stop()
            raise CollectorError("无法启动专用 Chrome。请确认已安装 Google Chrome，且没有其他采集命令占用同一 profile。") from exc
        return self

    async def __aexit__(self, *args):
        try:
            if self.context:
                await self.context.close()
        finally:
            if self._playwright:
                await self._playwright.stop()

    def absolute_url(self, route):
        # Preserve Grafana subpath installations.
        parts = urlsplit(route)
        if parts.username or parts.password:
            raise QueryError("查询路由不得携带 URL 登录凭证。")
        if parts.scheme:
            if parts.scheme not in {"https", "http"}:
                raise QueryError("查询路由必须是 http(s) 地址。")
            return route
        if parts.netloc:
            raise QueryError("不支持省略协议的查询路由。")
        base = urlsplit(self.base_url)
        prefix = base.path.rstrip("/")
        if prefix and (parts.path == prefix or parts.path.startswith(prefix + "/")):
            return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))
        return self.base_url.rstrip("/") + "/" + route.lstrip("/")

    async def request_json(self, method, route, body=None):
        url = self.absolute_url(route)
        response = None
        try:
            kwargs = {"method": method, "timeout": self.request_timeout * 1000, "max_redirects": 0,
                      "headers": {"X-Grafana-Org-Id": self.org_id, "Accept": "application/json"}}
            if body is not None:
                kwargs["data"] = body
                if isinstance(body, str):
                    kwargs["headers"]["Content-Type"] = "text/plain"
            response = await self.context.request.fetch(url, **kwargs)
            status = response.status
            headers = {key.lower(): value for key, value in response.headers.items()}
            content_type = headers.get("content-type", "").lower()
            login_response = (status == 401 or 300 <= status < 400 and _login_redirect(headers.get("location"))
                              or 200 <= status < 300 and ("text/html" in content_type or "application/xhtml+xml" in content_type))
            if login_response:
                # Keep this only in memory. A readable dashboard does not prove
                # that the datasource endpoint that failed has recovered.
                self._failed_auth_probe = (method, route, deepcopy(body))
                raise AuthenticationRequired(f"Grafana 认证检查未通过（HTTP {status}），需要重新登录。")
            if 300 <= status < 400:
                raise QueryError(f"查询接口返回非登录重定向（HTTP {status}），请检查 Grafana 路由。")
            if status >= 400:
                raise QueryError(f"查询接口返回 HTTP {status}：{urlsplit(url).path}",
                                 retryable=status in {408, 429, 500, 502, 503, 504},
                                 retry_after=_retry_delay(headers.get("retry-after")))
            try:
                data = await response.json()
            except Exception as exc:
                raise QueryError("查询接口未返回有效 JSON。") from exc
            # Do not hide a datasource error wrapped in an HTTP 200 envelope.
            if isinstance(data, dict) and data.get("error") and not data.get("results"):
                # Server error strings can echo request credentials; neither
                # logs nor persisted attempts should reproduce that text.
                raise QueryError("数据源在成功 HTTP 响应中报告错误，未将此响应当作有效数据。")
            return data
        except (AuthenticationRequired, QueryError):
            raise
        except Exception as exc:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            if isinstance(exc, PlaywrightTimeoutError):
                raise QueryError("查询请求超时。", retryable=True) from exc
            raise QueryError(f"网络请求失败（{type(exc).__name__}）。", retryable=True) from exc
        finally:
            if response is not None:
                try:
                    await response.dispose()
                except Exception:
                    # Cleanup must not replace a real HTTP/authentication error
                    # or invalidate a successfully parsed response. Cancellation
                    # derives from BaseException and deliberately propagates.
                    pass

    async def ensure_authenticated(self):
        try:
            return await self.request_json("GET", f"/api/dashboards/uid/{self.uid}")
        except AuthenticationRequired:
            await self.reauthenticate()
            return await self.request_json("GET", f"/api/dashboards/uid/{self.uid}")

    async def reauthenticate(self):
        async with self._auth_lock:
            probe = self._failed_auth_probe or ("GET", f"/api/dashboards/uid/{self.uid}", None)
            # A concurrently waiting request may have restored auth. Validate
            # the actual failing request, not just dashboard metadata access.
            try:
                await self.request_json(*probe)
                if self._failed_auth_probe == probe:
                    self._failed_auth_probe = None
                return
            except AuthenticationRequired:
                pass
            if self.headless:
                raise AuthenticationRequired("登录已过期。请运行不带 --headless 的 login 命令，再用同一输出目录恢复。")
            self.progress("请在专用 Chrome 窗口完成 Grafana 登录；程序会自动检测，凭证无需粘贴到终端。")
            await self.page.goto(self.source_url, wait_until="domcontentloaded", timeout=60000)
            await self.page.bring_to_front()
            deadline = time.monotonic() + self.login_timeout
            while time.monotonic() < deadline:
                try:
                    await self.request_json(*probe)
                    if self._failed_auth_probe == probe:
                        self._failed_auth_probe = None
                    self.progress("登录已验证，继续采集。")
                    return
                except AuthenticationRequired:
                    await asyncio.sleep(3)
            raise AuthenticationRequired("等待登录超时，进度已保留。完成登录后重新运行命令即可。")

    async def execute(self, query, start_ms, end_ms):
        from .query import build_request
        method, route, body = build_request(query, start_ms, end_ms)
        return await self.request_json(method, route, body)

    async def discover(self, *, measure=True):
        payload = await self.ensure_authenticated()
        dashboard = payload.get("dashboard", payload)
        # Viewer accounts can query dashboards but may not list administrator
        # datasource records. Frontend settings are the browser's own registry.
        try:
            rows = await self.request_json("GET", "/api/datasources")
        except QueryError as exc:
            if "HTTP 403" not in str(exc):
                raise
            settings = await self.request_json("GET", "/api/frontend/settings")
            registry = settings.get("datasources", {})
            rows = [dict(ds, name=ds.get("name", name), isDefault=name == settings.get("defaultDatasource"))
                    for name, ds in registry.items()]
        if not isinstance(rows, list):
            raise CollectorError("Grafana 数据源目录返回格式不受支持。")
        sources = [sanitize_datasource(ds) for ds in rows]
        widths = await self.measure_panel_widths(dashboard) if measure else {}
        return dashboard, sources, widths

    async def measure_panel_widths(self, dashboard):
        """Measure real grid geometry without changing saved dashboard state.

        Grafana lazy-renders offscreen/collapsed panels. Existing rendered panel
        widths establish each grid-column width; the same measured grid equation
        then determines collapsed panels, avoiding 200 unnecessary graph queries.
        """
        parsed = urlsplit(self.source_url)
        params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                  if k not in {"refresh", "inspect", "inspectTab", "viewPanel", "editPanel"}]
        measure_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))
        await self.page.goto(measure_url, wait_until="domcontentloaded", timeout=60000)
        await self.page.locator(".react-grid-layout").first.wait_for(state="attached", timeout=60000)
        try:
            await self.page.locator(".react-grid-item .panel-container").first.wait_for(state="attached", timeout=10000)
        except Exception:
            # Expanding a row changes only this temporary page's presentation.
            await self.page.locator(".dashboard-row__title").first.click()
            await self.page.locator(".react-grid-item .panel-container").first.wait_for(state="attached", timeout=15000)
        # Geometry reads only. CSS transform scale changes are intentionally ignored;
        # Grafana uses the grid's CSS-pixel width for maxDataPoints.
        geometry = await self.page.evaluate("""() => {
          const grid = document.querySelector('.react-grid-layout');
          const items = [...grid.querySelectorAll(':scope > .react-grid-item')].map(el => ({
            width: el.getBoundingClientRect().width,
            x: el.getBoundingClientRect().x - grid.getBoundingClientRect().x,
            panelId: Number(el.id.replace(/^panel-/, '') || 0),
            label: el.querySelector('[aria-label^="Panel container title"]')?.getAttribute('aria-label') || ''
          }));
          return {width: grid.getBoundingClientRect().width, items};
        }""")
        panels = []
        def walk(values):
            for panel in values:
                if panel.get("type") != "row":
                    panels.append(panel)
                walk(panel.get("panels", []))
        walk(dashboard.get("panels", []))
        actual = {item["panelId"]: item["width"] for item in geometry["items"] if item["panelId"] and item["width"] > 0}
        # Grafana 7 uses 24 columns and 8px horizontal gutters. This is validated
        # against at least one real rendered graph, not guessed from viewport size.
        grid_width = geometry["width"]
        gutter = 8
        if grid_width <= 0:
            raise CollectorError("无法测量 Grafana 面板网格宽度。")
        graph_ids = {int(panel["id"]) for panel in panels}
        measured_graphs = {pid: width for pid, width in actual.items() if pid in graph_ids}
        if not measured_graphs:
            raise CollectorError("未测得真实图表宽度，拒绝猜测自动采样间隔。")
        results = {}
        for panel in panels:
            pid = int(panel["id"])
            span = panel.get("gridPos", {}).get("w")
            if not span:
                raise CollectorError(f"面板 {pid} 缺少 gridPos.w，不能确定自动采样间隔。")
            estimated = int((grid_width + gutter) * span / 24 - gutter + 0.5)
            if pid in measured_graphs and abs(measured_graphs[pid] - estimated) > 1:
                raise CollectorError(f"面板 {pid} 的实测宽度与此 Grafana 版本网格布局不符，需更新布局适配。")
            results[pid] = actual.get(pid, estimated)
        self.measurement = {"viewport": {"width": 1440, "height": 900}, "grid_width": grid_width,
                            "gutter_px": gutter, "directly_measured": sorted(measured_graphs),
                            "method": "Grafana7 react-grid-layout geometry, 24 columns, margin 8px"}
        return results
