"""Transport protocol tests use fake Playwright objects; no browser or network."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from grafana_collector import transport
from grafana_collector.errors import AuthenticationRequired, CollectorError, QueryError
from grafana_collector.transport import BrowserSession, sanitize_datasource


URL = "https://grafana.example.test/grafana/d/sdkv2/title?orgId=42&refresh=30s"
ROUTE = "/api/datasources/proxy/3337/api/query"


class Response:
    def __init__(self, status=200, data=None, *, headers=None, json_error=None, disposal_error=None):
        self.status = status
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.data = data
        self.json_error = json_error
        self.disposal_error = disposal_error
        self.disposed = 0

    async def json(self):
        if self.json_error:
            raise self.json_error
        return self.data

    async def dispose(self):
        self.disposed += 1
        if self.disposal_error:
            raise self.disposal_error


class Requests:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class Page:
    def __init__(self, geometry=None):
        self.visited = []
        self.foregrounded = 0
        self.geometry = geometry
        self.first = self

    async def goto(self, url, **kwargs):
        self.visited.append(url)

    async def bring_to_front(self):
        self.foregrounded += 1

    def locator(self, selector):
        return self

    async def wait_for(self, **kwargs):
        pass

    async def evaluate(self, script):
        return self.geometry


def session(*responses, headless=False, geometry=None):
    result = BrowserSession(URL, headless=headless, progress=lambda _: None)
    result.context = SimpleNamespace(request=Requests(responses))
    result.page = Page(geometry)
    return result


@pytest.mark.parametrize("headless", [False, True])
async def test_chrome_launch_keeps_profile_and_sampling_geometry_with_runtime_flags(tmp_path, monkeypatch, headless):
    import playwright.async_api

    page = Page()
    context = SimpleNamespace(pages=[page], close=AsyncMock())
    launch = AsyncMock(return_value=context)
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
    manager = SimpleNamespace(start=AsyncMock(return_value=runtime))
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: manager)
    profile = tmp_path / "chrome-profile"

    async with BrowserSession(URL, profile=profile, headless=headless) as browser:
        assert browser.context is context and browser.page is page
        launch.assert_awaited_once_with(
            str(profile), channel="chrome", headless=headless,
            viewport={"width": 1440, "height": 900}, locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        assert profile.is_dir() and profile.stat().st_mode & 0o777 == 0o700
    context.close.assert_awaited_once()
    runtime.stop.assert_awaited_once()


@pytest.mark.parametrize("route,expected", [
    ("api/query", "https://grafana.example.test/grafana/api/query"),
    ("/api/query?region=a", "https://grafana.example.test/grafana/api/query?region=a"),
    ("/grafana/api/query?region=a", "https://grafana.example.test/grafana/api/query?region=a"),
    ("https://grafana.example.test/grafana/api/query", "https://grafana.example.test/grafana/api/query"),
])
def test_subpath_routes_are_prefixed_exactly_once(route, expected):
    assert session().absolute_url(route) == expected


@pytest.mark.parametrize("route", ["file:///tmp/data", "https://user:secret@example.test/api", "//example.test/api"])
def test_invalid_or_credential_bearing_routes_are_rejected(route):
    with pytest.raises(QueryError):
        session().absolute_url(route)


async def test_request_parameters_share_context_and_dispose_success_response():
    response = Response(data=[{"dps": {}}])
    browser = session(response)
    body = {"start": 10, "end": 20, "queries": []}
    assert await browser.request_json("POST", ROUTE, body) == [{"dps": {}}]
    url, options = browser.context.request.calls[0]
    assert url == "https://grafana.example.test/grafana" + ROUTE
    assert options == {
        "method": "POST", "timeout": 30_000, "max_redirects": 0,
        "headers": {"X-Grafana-Org-Id": "42", "Accept": "application/json"}, "data": body,
    }
    assert response.disposed == 1


async def test_bosun_body_is_sent_as_plain_text():
    browser = session(Response(data={"Results": []}))
    await browser.request_json("POST", "/api/expr", "$q = 1\n$q")
    assert browser.context.request.calls[0][1]["headers"]["Content-Type"] == "text/plain"


@pytest.mark.parametrize("status,headers,error,retryable", [
    (401, {}, AuthenticationRequired, None),
    (302, {"location": "/grafana/login?redirectTo=/"}, AuthenticationRequired, None),
    (302, {"location": "https://sso.example.test/authorize"}, AuthenticationRequired, None),
    (200, {"content-type": "text/html; charset=utf-8"}, AuthenticationRequired, None),
    (403, {"content-type": "text/html"}, QueryError, False),
    (500, {"content-type": "text/html"}, QueryError, True),
    (403, {}, QueryError, False),
    (404, {}, QueryError, False),
    (429, {}, QueryError, True),
    (503, {}, QueryError, True),
    (308, {"location": "/api/query/"}, QueryError, False),
])
async def test_http_error_classification_does_not_confuse_permission_and_login(status, headers, error, retryable):
    response = Response(status, headers=headers, disposal_error=RuntimeError("cleanup failure"))
    browser = session(response)
    with pytest.raises(error) as caught:
        await browser.request_json("POST", ROUTE, {"queries": []})
    if retryable is not None:
        assert caught.value.retryable is retryable
        assert browser._failed_auth_probe is None
    else:
        assert browser._failed_auth_probe == ("POST", ROUTE, {"queries": []})
    assert response.disposed == 1
    assert "cleanup failure" not in str(caught.value)


async def test_json_errors_and_server_error_text_do_not_leak_secrets():
    for response in [Response(data={"error": "Authorization: Bearer private-secret-value"}),
                     Response(json_error=ValueError("Body includes private-secret-value"))]:
        with pytest.raises(QueryError) as caught:
            await session(response).request_json("GET", "/api/query")
        assert "private-secret-value" not in str(caught.value)
        assert response.disposed == 1


async def test_retry_after_accepts_seconds_http_date_and_rejects_nonfinite(monkeypatch):
    monkeypatch.setattr(transport.time, "time", lambda: 0)
    for header, expected in [("1.5", 1.5), ("Thu, 01 Jan 1970 00:01:00 GMT", 60), ("invalid", None), ("inf", None)]:
        with pytest.raises(QueryError) as caught:
            await session(Response(429, headers={"retry-after": header})).request_json("POST", ROUTE)
        assert caught.value.retry_after == expected


async def test_network_failure_does_not_echo_exception_credentials():
    with pytest.raises(QueryError) as caught:
        await session(OSError("token=private-secret-value")).request_json("POST", ROUTE)
    assert caught.value.retryable is True
    assert "private-secret-value" not in str(caught.value)


async def test_disposal_failure_does_not_invalidate_success():
    response = Response(data={"ok": True}, disposal_error=RuntimeError("cleanup failed"))
    assert await session(response).request_json("GET", "/api/query") == {"ok": True}


@pytest.mark.parametrize("stage", ["fetch", "json", "dispose"])
async def test_request_cancellation_is_never_swallowed(stage):
    response = Response(data=[], json_error=asyncio.CancelledError() if stage == "json" else None,
                        disposal_error=asyncio.CancelledError() if stage == "dispose" else None)
    browser = session(asyncio.CancelledError() if stage == "fetch" else response)
    with pytest.raises(asyncio.CancelledError):
        await browser.request_json("POST", ROUTE)
    if stage != "fetch":
        assert response.disposed == 1


async def test_headless_auth_probe_uses_failed_query_not_readable_dashboard():
    browser = session(Response(401), Response(401), headless=True)
    with pytest.raises(AuthenticationRequired):
        await browser.request_json("POST", ROUTE, {"queries": [{"metric": "test"}]})
    with pytest.raises(AuthenticationRequired, match="headless"):
        await browser.reauthenticate()
    assert all(call[0].endswith(ROUTE) and call[1]["method"] == "POST" for call in browser.context.request.calls)
    assert not browser.page.visited


async def test_interactive_login_verifies_original_failed_endpoint(monkeypatch):
    async def immediate_sleep(seconds):
        return None

    monkeypatch.setattr(transport.asyncio, "sleep", immediate_sleep)
    browser = session(Response(401), Response(401), Response(401), Response(data=[]))
    body = {"queries": [{"metric": "test"}]}
    with pytest.raises(AuthenticationRequired):
        await browser.request_json("POST", ROUTE, body)
    await browser.reauthenticate()
    assert browser.page.visited == [URL]
    assert browser.page.foregrounded == 1
    assert browser._failed_auth_probe is None
    assert len(browser.context.request.calls) == 4
    assert all(options["method"] == "POST" and options["data"] == body for _, options in browser.context.request.calls)


async def test_already_refreshed_failed_endpoint_does_not_prompt_again():
    browser = session(Response(401), Response(data=[]))
    with pytest.raises(AuthenticationRequired):
        await browser.request_json("POST", ROUTE, {"queries": []})
    await browser.reauthenticate()
    assert not browser.page.visited
    assert browser._failed_auth_probe is None


def test_datasource_sanitization_keeps_routing_and_discards_nested_credentials():
    source = {
        "id": 3337, "name": "bytetsd", "type": "opentsdb", "basicAuth": "secret-basic",
        "secureJsonData": {"password": "secret-password"},
        "url": "https://user:secret-userinfo@[::1]:8443/api?_region=cn&signature=secret-signature&sessionid=secret-session#secret-fragment",
        "jsonData": {
            "minInterval": "30s", "tsdbResolution": 2, "jwtRegion": "cn", "httpHeaderValue1": "secret-header",
            "vRegionDispatchers": [{"regions": ["cn"], "datasourceId": 4, "password": "secret-nested"}],
        },
    }
    cleaned = sanitize_datasource(source)
    assert cleaned["collectorAuthRequired"] is True
    assert cleaned["url"] == "https://[::1]:8443/api?_region=cn"
    assert cleaned["jsonData"] == {"minInterval": "30s", "tsdbResolution": 2, "jwtRegion": "cn", "vRegionDispatchers": [{"regions": ["cn"], "datasourceId": 4}]}
    assert "secret-" not in json.dumps(cleaned)
    assert "secret-password" in json.dumps(source)  # Sanitization doesn't mutate discovery input.


async def test_viewer_403_datasource_listing_uses_frontend_registry():
    browser = session(
        Response(data={"dashboard": {"uid": "sdkv2", "panels": []}}), Response(403),
        Response(data={"defaultDatasource": "bytetsd", "datasources": {"bytetsd": {"id": 3337, "type": "opentsdb", "url": "/grafana/api/datasources/proxy/3337", "basicAuth": "not-exported"}}}),
    )
    dashboard, sources, widths = await browser.discover(measure=False)
    assert dashboard["uid"] == "sdkv2" and widths == {}
    assert sources[0]["isDefault"] is True
    assert "basicAuth" not in sources[0]
    assert browser.context.request.calls[-1][0].endswith("/grafana/api/frontend/settings")


async def test_runtime_grid_geometry_validates_collapsed_panel_widths():
    geometry = {"width": 1360, "items": [{"panelId": 1, "width": 448, "x": 0}]}
    browser = session(geometry=geometry)
    dashboard = {"panels": [{"type": "row", "id": 100, "panels": [
        {"id": 1, "gridPos": {"w": 8}}, {"id": 2, "gridPos": {"w": 6}},
    ]}]}
    assert await browser.measure_panel_widths(dashboard) == {1: 448, 2: 334}
    assert browser.measurement["directly_measured"] == [1]
    assert "refresh=" not in browser.page.visited[0]
    geometry["items"][0]["width"] = 500
    with pytest.raises(CollectorError, match="实测宽度"):
        await browser.measure_panel_widths(dashboard)


async def test_context_close_failure_still_stops_playwright():
    browser = session()
    stopped = []

    async def close():
        raise RuntimeError("Chrome closed unexpectedly")

    async def stop():
        stopped.append(True)

    browser.context.close = close
    browser._playwright = SimpleNamespace(stop=stop)
    with pytest.raises(RuntimeError):
        await browser.__aexit__(None, None, None)
    assert stopped == [True]
