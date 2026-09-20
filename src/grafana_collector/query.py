"""Compile the SDKv2 Grafana dashboard without changing its query semantics.

The OpenTSDB request compiler and rounding thresholds mirror the instance's
Grafana 7.5.3 assets (opentsdbPlugin/app, build 05b1e2420ad003a5e4fe).
Unsupported configuration is reported explicitly, never exported as empty data.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import operator
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .errors import QueryError, UnsupportedQuery

_VARIABLE = re.compile(r"\$\{([\w.]+)(?::([^}]+))?\}|\[\[([\w.]+)(?::([^\]]+))?\]\]|\$([A-Za-z_][\w]*)")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|[Mwdhmsy])?$")
_UNITS = {"ms": 1, "s": 1000, "m": 60000, "h": 3600000, "d": 86400000,
          "w": 604800000, "M": 2592000000, "y": 31536000000}
_ROUND_INTERVALS = (
    (15, 10), (35, 20), (75, 50), (150, 100), (350, 200), (750, 500),
    (1500, 1000), (3500, 2000), (7500, 5000), (12500, 10000), (17500, 15000),
    (25000, 20000), (45000, 30000), (90000, 60000), (210000, 120000),
    (450000, 300000), (750000, 600000), (1050000, 900000), (1500000, 1200000),
    (2700000, 1800000), (5400000, 3600000), (9000000, 7200000),
    (16200000, 10800000), (32400000, 21600000), (86400000, 43200000),
    (604800000, 86400000), (1814400000, 604800000), (3628800000, 2592000000),
)


def duration_ms(value):
    match = _DURATION.fullmatch(str(value).strip().lstrip(">"))
    if not match:
        raise UnsupportedQuery(f"Invalid interval: {value!r}")
    # Grafana's describeInterval uses parseInt even though its regexp permits decimals.
    amount = int(float(match[1])) if match[2] else int(float(match[1]))
    return int(amount * _UNITS[match[2] or "s"])


def format_interval(ms):
    # rangeUtil.secondsToHms returns the largest whole unit (7d, not 1w;
    # 30d, not 1M), including flooring a non-round minimum such as 90s.
    for unit in ("y", "d", "h", "m", "s"):
        if ms >= _UNITS[unit]:
            return f"{ms // _UNITS[unit]}{unit}"
    return f"{ms}ms"


def calculate_interval(from_ms, to_ms, max_data_points, minimum=None):
    """Grafana 7.5.3 rangeUtil.calculateInterval, with its exact breakpoints."""
    if to_ms <= from_ms or max_data_points <= 0:
        raise UnsupportedQuery("The interval reference range and panel width must be positive")
    raw = (to_ms - from_ms) / max_data_points
    rounded = next((value for boundary, value in _ROUND_INTERVALS if raw < boundary), 31536000000)
    return max(rounded, duration_ms(minimum) if minimum else 1)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _replace(text, variables, *, allow=(), default_format="pipe"):
    if text is None:
        return ""
    def replace(match):
        name = match[1] or match[3] or match[5]
        fmt = match[2] or match[4] or default_format
        if name in allow or ("tag_*" in allow and name.startswith("tag_")):
            return match[0]
        if name not in variables or variables[name] is None:
            raise UnsupportedQuery(f"Unresolved Grafana variable: {name}")
        value = variables[name]
        values = value if isinstance(value, list) else [value]
        if fmt in ("pipe", "raw", "distributed"):
            return "|".join(str(v) for v in values)
        if fmt == "csv":
            return ",".join(str(v) for v in values)
        if fmt == "regex":
            escaped = [re.escape(str(v)) for v in values]
            return "(" + "|".join(escaped) + ")" if len(escaped) > 1 else escaped[0]
        raise UnsupportedQuery(f"Unsupported variable format: {fmt}")
    return _VARIABLE.sub(replace, str(text))


def _variable_values(dashboard, source_url, overrides):
    params = parse_qs(urlsplit(source_url).query, keep_blank_values=True)
    values, definitions = {}, {}
    for spec in dashboard.get("templating", {}).get("list", []):
        name = spec["name"]
        definitions[name] = spec
        supplied = params.get("var-" + name)
        if overrides and name in overrides:
            value = overrides[name]
        elif supplied is not None:
            value = supplied if len(supplied) > 1 else supplied[0]
        else:
            value = spec.get("current", {}).get("value")
            if value is None and spec.get("type") == "constant":
                value = spec.get("query")
        all_tokens = ["All", "$__all"]
        if value in all_tokens or isinstance(value, list) and any(v in all_tokens for v in value):
            if spec.get("allValue") is not None:
                value = spec["allValue"]
            else:
                value = [o.get("value") for o in spec.get("options", [])
                         if o.get("value") not in all_tokens]
                if not value:
                    value = None  # resolving dynamic options belongs to live discovery
        values[name] = value
    # Allow explicit variables not declared by older saved dashboard versions.
    values.update({k: v for k, v in (overrides or {}).items() if k not in definitions})
    for name in list(values):
        value = values[name]
        if isinstance(value, str) and _VARIABLE.search(value):
            values[name] = _replace(value, {k: v for k, v in values.items() if k != name})
    return values, definitions


def _datasource_list(datasources):
    if isinstance(datasources, list):
        return datasources
    if isinstance(datasources, dict) and "datasources" in datasources:
        return _datasource_list(datasources["datasources"])
    if isinstance(datasources, dict):
        return [dict(v, name=v.get("name", k)) for k, v in datasources.items() if isinstance(v, dict)]
    raise UnsupportedQuery("Datasource discovery did not return a list or map")


def _resolve_datasource(reference, variables, datasources):
    if isinstance(reference, dict):
        reference = reference.get("uid") or reference.get("name")
    name = _replace(reference, variables) if reference else None
    if name == "__expr__":
        return {"type": "__expr__", "name": name}
    matches = [d for d in datasources if name in (str(d.get("id")), d.get("uid"), d.get("name"))] if name else [d for d in datasources if d.get("isDefault")]
    if len(matches) != 1:
        raise UnsupportedQuery(f"Cannot uniquely resolve datasource {name or '(default)'!r}")
    if matches[0].get("collectorAuthRequired"):
        raise UnsupportedQuery(f"Datasource {name or matches[0].get('name')!r} requires credentials outside the browser session")
    return matches[0]


def _route(datasource, suffix, region="", group_by_region=None):
    # Grafana bootstrap settings contain browser-safe proxy URLs. The standard
    # datasource list endpoint instead exposes backend URLs: those must be proxied.
    route = datasource.get("collectorRoute") or datasource.get("currentproxy")
    if isinstance(route, dict):
        route = route.get("url")
    if not route:
        url = str(datasource.get("url") or "")
        if "api/datasources/proxy/" in url:
            route = url
        elif datasource.get("access") == "direct":
            raise UnsupportedQuery("Direct-browser datasource requires its own JWT transport; use its validated proxy route")
        elif datasource.get("id") is not None:
            route = f"/api/datasources/proxy/{datasource['id']}"
        else:
            raise UnsupportedQuery(f"Missing validated route for datasource {datasource.get('name')}")
    route = str(route)
    parts = urlsplit(route)
    params = parse_qs(parts.query, keep_blank_values=True)
    if region:
        params["_region"] = [region]
    if group_by_region is not None:
        params["_isGroupByRegion"] = [str(group_by_region).lower()]
    path = parts.path.rstrip("/") + suffix
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(params, doseq=True), ""))


def _panel_stream(panels, group="Ungrouped"):
    for panel in panels:
        if panel.get("type") == "row":
            group = panel.get("title") or "Ungrouped"
            yield from _panel_stream(panel.get("panels", []), group)
        elif panel.get("targets") is not None:
            yield panel, group


def _compile_opentsdb(target, variables, interval_ms, datasource):
    if not target.get("metric"):
        raise UnsupportedQuery("OpenTSDB target has no metric")
    # This Grafana plugin dispatches on metric; stale SQL editor fields in the
    # saved EIC panel 564 are ignored by the plugin and have no query semantics.
    result = {"metric": _replace(target["metric"], variables),
              "aggregator": _replace(target.get("aggregator") or "avg", variables),
              "tenant": _replace(target.get("tenant") or "default", variables)}
    if target.get("shouldComputeRate") or target.get("shouldComputeDelta"):
        if target.get("shouldComputeRate") and target.get("shouldComputeDelta"):
            raise UnsupportedQuery("Both rate and delta are enabled without resolved editor state")
        options = {"counter": bool(target.get("isCounter")), "diff": bool(target.get("shouldComputeDelta"))}
        for source, dest in (("counterMax", "counterMax"), ("counterResetValue", "resetValue")):
            if target.get(source) not in (None, ""):
                options[dest] = int(target[source])
        if target.get("rateDownsampleType"):
            options["order"] = target["rateDownsampleType"]
        if float(datasource.get("jsonData", {}).get("tsdbVersion", 1)) >= 2:
            # Preserve the plugin's ResetValue (capital R) check, including its typo.
            options["dropResets"] = not bool(options.get("counterMax"))
        result.update(rate=True, rateOptions=options)
    if target.get("shouldComputeTopK"):
        result["topK"] = "-".join(_replace(target.get(k, ""), variables)
                                  for k in ("topKType", "topKValue", "topKOption"))
        if not re.fullmatch(r"(?:top|bottom)-[1-9]\d*-[A-Za-z_][\w]*", result["topK"]):
            raise UnsupportedQuery(f"Invalid TopK definition: {result['topK']}")
    if not target.get("disableDownsampling"):
        interval = _replace(target.get("downsampleInterval") or format_interval(interval_ms), variables)
        if re.search(r"\.\d+s$", interval):
            interval = f"{int(float(interval[:-1]) * 1000)}ms"
        duration_ms(interval)  # validate rather than silently sending unknown units
        result["downsample"] = interval + "-" + (target.get("downsampleAggregator") or "avg")
        fill = target.get("downsampleFillPolicy")
        if fill and fill != "none":
            result["downsample"] += "-" + fill
    if target.get("filters"):
        result["filters"] = [dict(f, filter=_replace(f.get("filter", ""), variables)) for f in target["filters"]]
    else:
        result["tags"] = {k: _replace(v, variables) for k, v in target.get("tags", {}).items()}
    if target.get("explicitTags"):
        result["explicitTags"] = True
    if target.get("shouldComputeMulti"):
        result["multiFieldExpr"] = ",".join(_replace(f, variables) for f in target.get("fields", []) if f)
    return result


def compile_dashboard(dashboard, datasources, source_url, *, reference_from_ms,
                      reference_to_ms, panel_widths, panel_ids=None, variable_overrides=None):
    dashboard = dashboard.get("dashboard", dashboard)
    variables, definitions = _variable_values(dashboard, source_url, variable_overrides)
    sources = _datasource_list(datasources)
    output = []
    for raw_panel, group in _panel_stream(dashboard.get("panels", [])):
        pid = int(raw_panel["id"])
        if panel_ids is not None and pid not in panel_ids:
            continue
        width = panel_widths.get(pid, panel_widths.get(str(pid)))
        panel = {"id": pid, "title": raw_panel.get("title", str(pid)), "group": group,
                 "queries": [], "transformations": copy.deepcopy(raw_panel.get("transformations") or []),
                 "metadata": {"width_pixels": width, "grafana_type": raw_panel.get("type"),
                              "unit": raw_panel.get("fieldConfig", {}).get("defaults", {}).get("unit") or
                              (raw_panel.get("yaxes") or [{}])[0].get("format", "")}}
        for index, target in enumerate(raw_panel.get("targets", [])):
            ref = str(target.get("refId") or chr(65 + index))
            query = {"panel_id": pid, "ref_id": ref, "kind": "unsupported", "route": "", "query": {},
                     "interval_ms": 0, "alias": "", "unit": panel["metadata"]["unit"],
                     "hidden": bool(target.get("hide")), "dependencies": [], "metadata": {}, "error": None}
            try:
                if raw_panel.get("timeFrom") or raw_panel.get("timeShift"):
                    raise UnsupportedQuery("Panel-specific timeFrom/timeShift needs explicit adapter support")
                datasource = _resolve_datasource(target.get("datasource") or raw_panel.get("datasource"), variables, sources)
                config = datasource.get("jsonData") or {}
                minimum = raw_panel.get("interval") or datasource.get("interval") or config.get("timeInterval") or config.get("minInterval")
                local_vars = dict(variables)
                minimum = _replace(minimum, local_vars) if minimum else None
                max_points = raw_panel.get("maxDataPoints") or width
                if not max_points:
                    raise UnsupportedQuery("Panel width was not measured and maxDataPoints is unspecified")
                interval = calculate_interval(reference_from_ms, reference_to_ms, float(max_points), minimum)
                local_vars.update(__interval=format_interval(interval), __interval_ms=interval,
                                  __range_ms=reference_to_ms - reference_from_ms,
                                  __range_s=(reference_to_ms - reference_from_ms) // 1000)
                query["interval_ms"] = interval
                query["alias"] = _replace(target.get("alias", ""), local_vars, allow=("tag_*",))
                query["metadata"].update(datasource_id=datasource.get("id"), datasource_name=datasource.get("name"),
                                         datasource_type=datasource.get("type"), auto_interval_ms=interval,
                                         max_data_points=max_points, minimum_interval=minimum,
                                         tsdb_resolution=int(config.get("tsdbResolution") or 1),
                                         tsdb_version=float(config.get("tsdbVersion") or 1),
                                         axis_units=[axis.get("format", "") for axis in raw_panel.get("yaxes", [])],
                                         series_overrides=copy.deepcopy(raw_panel.get("seriesOverrides") or []))
                if target.get("type") == "math" or datasource.get("type") == "__expr__":
                    expression = target.get("expression") or ""
                    tree, dependencies = _math_tree(expression)
                    query.update(kind="math", query={"expression": expression}, dependencies=dependencies)
                elif datasource.get("type") == "opentsdb":
                    query["query"] = _compile_opentsdb(target, local_vars, interval, datasource)
                    if query["query"].get("downsample"):
                        query["interval_ms"] = duration_ms(query["query"]["downsample"].split("-", 1)[0])
                    else:
                        query["interval_ms"] = 0
                        query["metadata"]["downsampling_disabled"] = True
                    region = _replace(target.get("bytedVRegion") or variables.get("metricsBytedVRegion__", ""), local_vars)
                    region_group = target.get("bytedIsGroupByRegion", variables.get("metricsBytedIsGroupByRegion__", "false"))
                    if not config.get("isVRegionAble"):
                        region, region_group = "", None
                    if config.get("vRegionDispatchers") and region:
                        raise UnsupportedQuery("Region dispatchers require explicit multi-route discovery")
                    if str(region_group).lower() == "true" and len(region.split("|")) > 1:
                        raise UnsupportedQuery("Grouping multiple regions requires separate region requests")
                    query.update(kind="opentsdb", route=_route(datasource, "/api/query", region, region_group))
                elif "bosun" in str(datasource.get("type", "")).lower():
                    expr = target.get("expr") or target.get("expression")
                    if not expr:
                        raise UnsupportedQuery("Bosun target has no expression")
                    # Bosun local $assignments and the plugin's $start are not dashboard variables.
                    assignments = set(re.findall(r"^\s*\$([A-Za-z_]\w*)\s*=", expr, re.M))
                    expr = _replace(expr, local_vars, allow=assignments | {"start", "ds"})
                    region = _replace(target.get("bytedVRegion") or variables.get("metricsBytedVRegion__", ""), local_vars)
                    region_group = _replace(target.get("bytedIsGroupByRegion") or variables.get("metricsBytedIsGroupByRegion__", "false"), local_vars)
                    # Unlike OpenTSDB's truthiness check, this instance's Bosun
                    # plugin enables region parameters only for the string "open".
                    enabled = config.get("isVRegionAble") == "open"
                    if enabled and config.get("vRegionDispatchers") and region:
                        raise UnsupportedQuery("Bosun region dispatchers require explicit multi-route discovery")
                    if enabled and region_group == "true" and len(region.split("|")) > 1:
                        raise UnsupportedQuery("Grouping multiple Bosun regions requires separate region requests")
                    is_v2 = config.get("bytedCustomer") == "metrics-v2"
                    query.update(kind="bosun", route=_route(datasource, "/api/v2/expr" if is_v2 else "/api/expr",
                                                           region if enabled else "", region_group if enabled else None),
                                 query={"expr": expr, "useTimestamp": bool(target.get("useTimestamp")),
                                        "bytedVRegion": region, "is_v2": is_v2})
                    query["metadata"].update(effective_region=region if enabled else "", bosun_protocol="internal-bosun-app")
                    if not re.search(r"\$ds\b", expr):
                        # q() is not automatically downsampled by this plugin.
                        # No single fixed point interval can be inferred from
                        # arbitrary Bosun expressions without a $ds reference.
                        query["interval_ms"] = 0
                        query["metadata"]["sampling"] = "native_or_expression_defined"
                else:
                    raise UnsupportedQuery(f"Unsupported datasource type: {datasource.get('type')!r}")
                semantic_fields = ("metric", "aggregator", "filters", "tags", "tenant", "downsampleInterval",
                                   "topKType", "topKValue", "topKOption", "expr", "expression", "fields")
                semantic_target = {key: target[key] for key in semantic_fields if key in target}
                if ((query["kind"] == "opentsdb" and config.get("isVRegionAble")) or
                        (query["kind"] == "bosun" and config.get("isVRegionAble") == "open")):
                    semantic_target.update({key: target[key] for key in ("bytedVRegion", "bytedIsGroupByRegion") if key in target})
                refs = set(m[1] or m[3] or m[5] for m in _VARIABLE.finditer(json.dumps(semantic_target)))
                used = sorted(refs.intersection(variables))
                legend_refs = set(m[1] or m[3] or m[5] for m in _VARIABLE.finditer(target.get("alias") or ""))
                query["metadata"].update(used_variables=used,
                                         legend_variables=sorted(legend_refs.intersection(variables)),
                                         unused_filter_variables=sorted(set(variables) - set(used) - {"prefix", "datasource", "bosun_datasource"}))
            except (UnsupportedQuery, ValueError, TypeError, KeyError) as exc:
                query.update(kind="unsupported", error=str(exc))
            query["key"] = _hash({k: query[k] for k in ("panel_id", "ref_id", "kind", "route", "query", "interval_ms")})
            panel["queries"].append(query)
        by_ref = {q["ref_id"]: q for q in panel["queries"]}
        required = {q["ref_id"] for q in panel["queries"] if not q["hidden"]}
        pending = list(required)
        while pending:
            ref = pending.pop()
            for dep in by_ref.get(ref, {}).get("dependencies", []):
                if dep not in required:
                    required.add(dep)
                    pending.append(dep)
        for q in panel["queries"]:
            q["metadata"]["required"] = q["ref_id"] in required
        output.append(panel)
    if panel_ids is not None:
        missing = set(panel_ids) - {p["id"] for p in output}
        if missing:
            raise UnsupportedQuery(f"Panel IDs absent from dashboard query panels: {sorted(missing)}")
    return {"panels": output, "variables": variables,
            "metadata": {"interval_algorithm": "Grafana 7.5.3 rangeUtil.calculateInterval",
                         "reference_from_ms": reference_from_ms, "reference_to_ms": reference_to_ms,
                         "variable_definitions": definitions}}


def build_request(query, start_ms, end_ms):
    if end_ms <= start_ms:
        raise QueryError("Query end must be after its start")
    if query["kind"] == "opentsdb":
        metadata = query.get("metadata", {})
        body = {"start": int(start_ms), "end": int(end_ms), "queries": [copy.deepcopy(query["query"])],
                "msResolution": metadata.get("tsdb_resolution", 1) == 2, "globalAnnotations": True}
        if metadata.get("tsdb_version", 1) == 3:
            body["showQuery"] = True
        return "POST", query["route"], body
    if query["kind"] == "bosun":
        # Verified against this instance's public/plugins/bosun-app/datasource/
        # datasource.js: v1 uses a raw expression, metrics-v2 uses {expr: ...}.
        # It anchors expression "now" to the UTC range end unless useTimestamp.
        # $start is a duration relative to that end, not an epoch timestamp.
        expr = query["query"]["expr"]
        expr = expr.replace("$start", str((int(end_ms) - int(start_ms)) // 1000) + "s")
        expr = expr.replace("$ds", format_interval(query.get("metadata", {}).get("auto_interval_ms") or query["interval_ms"]))
        end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
        parts = urlsplit(query["route"])
        params = parse_qs(parts.query, keep_blank_values=True)
        if not query["query"].get("useTimestamp"):
            params.update(date=[end.strftime("%Y-%m-%d")], time=[end.strftime("%H:%M:%S")])
        body = {"expr": expr} if query["query"].get("is_v2") else expr
        return "POST", urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params, doseq=True), "")), body
    raise UnsupportedQuery(query.get("error") or f"{query['kind']} queries have no network request")


def _number(value, quality):
    if value is None:
        return None
    if isinstance(value, bool):
        raise QueryError("Boolean time-series value returned by datasource")
    try:
        value = float(value) if not isinstance(value, (int, float)) else value
    except (ValueError, TypeError) as exc:
        raise QueryError("Non-numeric time-series value returned by datasource") from exc
    if not math.isfinite(value):
        quality["nonfinite_values"] = quality.get("nonfinite_values", 0) + 1
        return None
    return value


def _series_name(query, metric, labels):
    if query.get("alias"):
        variables = {"tag_" + k: v for k, v in labels.items()}
        variables.setdefault("tag__region", "")
        # Grafana leaves an absent tag token unchanged.
        return _VARIABLE.sub(lambda m: str(variables.get(m[1] or m[3] or m[5], m[0])), query["alias"])
    filters = query.get("query", {}).get("filters") or []
    keys = {f["tagk"] for f in filters} | set(query.get("query", {}).get("tags", {}))
    selected = [f"{k}={v}" for k, v in labels.items() if k in keys]
    return metric + ("{" + ", ".join(selected) + "}" if selected else "")


def _series_unit(query, name):
    unit = query.get("unit", "")
    metadata = query.get("metadata", {})
    axes = metadata.get("axis_units", [])
    for override in metadata.get("series_overrides", []):
        pattern = override.get("alias", "")
        if pattern.startswith("/") and pattern.endswith("/"):
            try:
                matches = re.search(pattern[1:-1], name) is not None
            except re.error as exc:
                raise UnsupportedQuery(f"Invalid series override pattern: {pattern}") from exc
        else:
            matches = name == pattern
        if matches and override.get("yaxis") is not None:
            index = int(override["yaxis"]) - 1
            if not 0 <= index < len(axes):
                raise QueryError(f"Series {name!r} selects absent Y axis {index + 1}")
            unit = axes[index]
    return unit


def parse_response(query, raw):
    if query["kind"] not in ("opentsdb", "bosun"):
        raise UnsupportedQuery(f"Cannot parse response for {query['kind']}")
    if isinstance(raw, dict) and (raw.get("error") or raw.get("__isError__") or raw.get("Error")):
        # Backend messages can echo request headers or token-bearing URLs.
        # Keep persisted exceptions categorical; never include response text.
        raise QueryError("Datasource returned an error response")
    if query["kind"] == "bosun":
        if not isinstance(raw, dict) or "Results" not in raw:
            raise QueryError("Bosun response is missing Results")
        if raw.get("Type") != "series":
            raise UnsupportedQuery("Bosun returned a non-series result, expected a time series")
        rows = [] if raw["Results"] is None else raw["Results"]
    else:
        rows = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
    if not isinstance(rows, list):
        raise QueryError("Datasource response must contain a time-series list")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise QueryError("Invalid time-series response item")
        if row.get("__isError__") or row.get("error") or row.get("Error"):
            raise QueryError("Datasource query failed")
        bosun = query["kind"] == "bosun"
        metric = str(row.get("metric", query.get("query", {}).get("metric", "bosun")))
        labels = {str(k): str(v) for k, v in (row.get("Group", {}) if bosun else row.get("tags", {})).items()}
        values = (row.get("Values", row.get("Value")) if query.get("query", {}).get("is_v2") else row.get("Value")) if bosun else row.get("dps")
        if not isinstance(values, dict):
            raise QueryError("Response series has no timestamp-value map")
        quality, points = {}, []
        for timestamp, value in values.items():
            try:
                stamp = float(timestamp)
                if not bosun and query.get("metadata", {}).get("tsdb_resolution", 1) == 2:
                    ms = int(stamp)
                else:
                    ms = int(stamp * 1000)
            except (ValueError, TypeError, OverflowError) as exc:
                raise QueryError("Datasource returned an invalid timestamp") from exc
            points.append([ms, _number(value, quality)])
        series = {"ref_id": query["ref_id"], "metric": metric, "labels": labels,
                  "name": ("{" + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}")
                  if bosun and not query.get("alias") else _series_name(query, metric, labels),
                  "unit": query.get("unit", ""),
                  "points": sorted(points), "query_key": query["key"]}
        series["unit"] = _series_unit(query, series["name"])
        if row.get("aggregateTags"):
            series["aggregate_tags"] = row["aggregateTags"]
        if quality:
            series["quality"] = quality
        result.append(series)
    return result


_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow}


def _math_tree(expression):
    refs = []
    def variable(match):
        ref = match[1] or match[3] or match[5]
        if ref not in refs:
            refs.append(ref)
        return "ref_" + str(refs.index(ref))
    source = _VARIABLE.sub(variable, expression).replace("^", "**")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise UnsupportedQuery(f"Unsupported Math expression: {expression!r}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load, ast.BinOp, ast.UnaryOp, ast.UAdd, ast.USub)):
            continue
        if type(node) in _OPERATORS:
            continue
        if isinstance(node, ast.Name) and re.fullmatch(r"ref_\d+", node.id):
            continue
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            continue
        raise UnsupportedQuery(f"Unsupported Math syntax: {type(node).__name__}")
    return tree.body, refs


def _compatible_labels(a, b):
    return all(b[k] == v for k, v in a.items() if k in b) and (set(a) <= set(b) or set(b) <= set(a))


def _combine(left, right, operation):
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        try:
            value = operation(left, right)
            return value if math.isfinite(value) else None
        except (ZeroDivisionError, OverflowError, ValueError):
            return None
    if left is None or right is None:
        return []
    left_scalar, right_scalar = isinstance(left, (int, float)), isinstance(right, (int, float))
    pairs = [(None, r) for r in right] if left_scalar else [(l, None) for l in left] if right_scalar else [
        (l, r) for l in left for r in right if _compatible_labels(l.get("labels", {}), r.get("labels", {}))]
    output = []
    identities = set()
    for l, r in pairs:
        labels = dict((l or {}).get("labels", {}), **(r or {}).get("labels", {}))
        identity = tuple(sorted(labels.items()))
        if identity in identities:
            raise QueryError("Ambiguous Math series join: multiple operands have the same compatible labels")
        identities.add(identity)
        lpoints = dict(l["points"]) if l else {}
        rpoints = dict(r["points"]) if r else {}
        points, quality = [], {}
        for ts in sorted(lpoints.keys() | rpoints.keys()):
            lv, rv = (left if left_scalar else lpoints.get(ts)), (right if right_scalar else rpoints.get(ts))
            value = None
            if lv is not None and rv is not None:
                try:
                    value = _number(operation(lv, rv), quality)
                except (ZeroDivisionError, OverflowError, ValueError):
                    quality["invalid_arithmetic"] = quality.get("invalid_arithmetic", 0) + 1
            points.append([ts, value])
        result = {"metric": "expression", "labels": labels, "points": points}
        if quality:
            result["quality"] = quality
        output.append(result)
    return output


def _evaluate(tree, refs, values):
    if isinstance(tree, ast.Constant):
        return tree.value
    if isinstance(tree, ast.Name):
        ref = refs[int(tree.id[4:])]
        if ref not in values:
            raise QueryError(f"Missing Math dependency: {ref}")
        return values[ref]
    if isinstance(tree, ast.UnaryOp):
        result = _evaluate(tree.operand, refs, values)
        return result if isinstance(tree.op, ast.UAdd) else _combine(result, -1, operator.mul)
    return _combine(_evaluate(tree.left, refs, values), _evaluate(tree.right, refs, values), _OPERATORS[type(tree.op)])


def _transform(panel, series):
    for index, transform in enumerate(panel.get("transformations", [])):
        options = transform.get("options", {})
        if transform.get("id") != "calculateField" or options.get("mode") != "binary":
            raise UnsupportedQuery(f"Unsupported panel transformation: {transform.get('id')}/{options.get('mode')}")
        binary = options.get("binary", {})
        operation = {"+": operator.add, "-": operator.sub, "*": operator.mul, "/": operator.truediv}.get(binary.get("operator"))
        if operation is None:
            raise UnsupportedQuery(f"Unsupported calculateField operator: {binary.get('operator')}")
        def operand(name):
            candidates = [s for s in series if s["name"] == name]
            if not candidates:
                try:
                    return float(name)
                except (TypeError, ValueError):
                    if not series:
                        return []
                    raise QueryError(f"calculateField cannot find field {name!r}")
            # Grafana matches a field name, not an implicit reducer across curves.
            if len(candidates) != 1:
                raise QueryError(f"calculateField field {name!r} matches multiple curves")
            return candidates
        result = _combine(operand(binary.get("left")), operand(binary.get("right")), operation)
        if not isinstance(result, list):
            raise UnsupportedQuery("A constant-only calculateField cannot produce a time axis")
        for curve in result:
            curve.update(ref_id=f"transform_{index}", metric=f"calculateField_{index}",
                         name=options.get("alias") or f"{binary.get('left')} {binary.get('operator')} {binary.get('right')}",
                         unit=panel.get("metadata", {}).get("unit", ""),
                         query_key=_hash({"panel_id": panel["id"], "transform": transform}))
        series = result if options.get("replaceFields") else series + result
    return series


def render_panel(panel, series_by_ref):
    values = copy.deepcopy(series_by_ref)
    queries = {q["ref_id"]: q for q in panel["queries"]}
    visiting = set()
    def resolve(ref):
        if ref in visiting:
            raise QueryError(f"Circular expression dependency: {ref}")
        if ref not in queries:
            raise QueryError(f"Missing query dependency: {ref}")
        q = queries[ref]
        if q["kind"] == "unsupported":
            raise UnsupportedQuery(q.get("error") or f"Unsupported query {ref}")
        if q["kind"] != "math":
            if ref not in values:
                raise QueryError(f"Missing query response for {ref}")
            return values[ref]
        if ref in values:
            return values[ref]
        visiting.add(ref)
        tree, dependencies = _math_tree(q["query"]["expression"])
        for dep in dependencies:
            resolve(dep)
        derived = _evaluate(tree, dependencies, values)
        if not isinstance(derived, list):
            raise UnsupportedQuery("Constant-only Math expression has no time axis")
        for curve in derived:
            curve.update(ref_id=ref, metric="math:" + q["query"]["expression"],
                         name=q.get("alias") or ref, unit=q.get("unit", ""), query_key=q["key"])
        visiting.remove(ref)
        values[ref] = derived
        return derived
    result = []
    for ref, query in queries.items():
        if not query.get("hidden"):
            result.extend(resolve(ref))
    return _transform(panel, result)
