import copy
import math

import pytest

from grafana_collector.errors import QueryError, UnsupportedQuery
from grafana_collector.query import (build_request, calculate_interval, compile_dashboard,
                                     parse_response, render_panel)


def dashboard(targets=None, **panel):
    return {"uid": "test", "templating": {"list": [
        {"name": "prefix", "type": "constant", "query": "test.sdk"},
        {"name": "datasource", "current": {"value": "metrics"}},
        {"name": "filesystem", "current": {"value": "saved_fs"}},
        {"name": "task_id", "includeAll": True, "allValue": "*", "current": {"value": ["$__all"]}},
    ]}, "panels": [{"id": 90, "type": "row", "title": "API", "panels": [
        {"id": 1, "type": "graph", "title": "Test", "datasource": "${datasource}",
         "yaxes": [{"format": "bytes"}], "targets": targets or [target()], **panel}]}]}


def target(ref="A", **kwargs):
    return {"refId": ref, "metric": "${prefix}.qps", "alias": "$filesystem-$tag_method", "aggregator": "sum",
            "downsampleAggregator": "avg", "downsampleFillPolicy": "none",
            "filters": [{"tagk": "filesystem", "filter": "$filesystem", "type": "literal_or", "groupBy": False},
                        {"tagk": "task_id", "filter": "$task_id", "type": "literal_or", "groupBy": True}], **kwargs}


SOURCES = [{"id": 42, "name": "metrics", "uid": "tsdb", "type": "opentsdb", "isDefault": True,
            "jsonData": {"tsdbVersion": 3, "tsdbResolution": 1, "timeInterval": "1s"}}]


def compile(d=None, **kwargs):
    return compile_dashboard(d or dashboard(), SOURCES,
        "https://grafana.example/d/test?var-filesystem=requested_fs&var-task_id=All",
        reference_from_ms=1700000000000, reference_to_ms=1700021600000,
        panel_widths={1: 400}, **kwargs)


def test_variables_query_and_alias_match_grafana_plugin():
    plan = compile()
    assert plan["variables"]["task_id"] == "*"
    panel = plan["panels"][0]
    assert panel["group"] == "API"
    q = panel["queries"][0]
    assert q["kind"] == "opentsdb"
    assert q["interval_ms"] == 60000
    assert q["alias"] == "requested_fs-$tag_method"
    assert q["query"] == {"metric": "test.sdk.qps", "aggregator": "sum", "tenant": "default",
                          "downsample": "1m-avg", "filters": [
        {"tagk": "filesystem", "filter": "requested_fs", "type": "literal_or", "groupBy": False},
        {"tagk": "task_id", "filter": "*", "type": "literal_or", "groupBy": True}]}
    assert q["route"] == "/api/datasources/proxy/42/api/query"
    assert "filesystem" in q["metadata"]["used_variables"]


@pytest.mark.parametrize("raw, expected", [(14, 10), (15, 20), (35, 50), (750, 1000),
                                          (44999, 30000), (45000, 60000), (90000, 120000),
                                          (604800000, 604800000), (3628800000, 31536000000)])
def test_exact_grafana_interval_boundaries(raw, expected):
    assert calculate_interval(0, raw, 1) == expected


def test_minimum_and_explicit_interval_and_native_resolution():
    assert calculate_interval(0, 3600000, 400, "30s") == 30000
    q = compile(dashboard([target(downsampleInterval="30s")]))["panels"][0]["queries"][0]
    assert q["interval_ms"] == 30000
    assert q["metadata"]["auto_interval_ms"] == 60000
    assert q["query"]["downsample"] == "30s-avg"
    q = compile(dashboard([target(disableDownsampling=True, sql={"old": "editor state"})]))["panels"][0]["queries"][0]
    assert "downsample" not in q["query"]
    assert q["metadata"]["downsampling_disabled"]
    assert q["interval_ms"] == 0


def test_rate_delta_topk_and_fill_options():
    t = target(shouldComputeRate=True, isCounter=True, rateDownsampleType="before_downsample",
               shouldComputeTopK=True, topKType="top", topKValue="10", topKOption="max",
               downsampleFillPolicy="null")
    q = compile(dashboard([t]))["panels"][0]["queries"][0]
    assert q["query"]["rateOptions"] == {"counter": True, "diff": False, "order": "before_downsample", "dropResets": True}
    assert q["query"]["topK"] == "top-10-max"
    assert q["query"]["downsample"] == "1m-avg-null"
    before = copy.deepcopy(q)
    first = build_request(q, 1700000000000, 1700000300000)
    second = build_request(q, 1700000250000, 1700000600000)
    assert first[2]["queries"] == second[2]["queries"]
    assert first[2]["msResolution"] is False
    assert first[2]["showQuery"] is True
    assert q == before


def test_missing_variables_and_width_are_explicit_unsupported():
    q = compile(dashboard([target(metric="$unknown.qps")]))["panels"][0]["queries"][0]
    assert q["kind"] == "unsupported" and "unknown" in q["error"]
    plan = compile_dashboard(dashboard(), SOURCES, "https://grafana.example/", reference_from_ms=0,
                             reference_to_ms=100000, panel_widths={})
    assert "width" in plan["panels"][0]["queries"][0]["error"]


def test_all_without_all_value_must_have_resolved_options():
    d = dashboard()
    d["templating"]["list"][-1]["allValue"] = None
    q = compile(d)["panels"][0]["queries"][0]
    assert q["kind"] == "unsupported"
    assert "task_id" in q["error"]


def test_panel_selection_and_expanded_row_grouping():
    d = dashboard()
    d["panels"].append({"id": 91, "type": "row", "title": "EIC"})
    d["panels"].append({"id": 2, "type": "graph", "datasource": "metrics", "targets": [target()]})
    p = compile_dashboard(d, SOURCES, "https://grafana.example/", reference_from_ms=0,
                          reference_to_ms=100000, panel_widths={2: 300}, panel_ids=[2])["panels"][0]
    assert p["id"] == 2 and p["group"] == "EIC"
    with pytest.raises(UnsupportedQuery, match="absent"):
        compile(panel_ids=[99])


def test_semantic_identity_changes_with_interval_and_filters():
    first = compile()["panels"][0]["queries"][0]
    second = compile(variable_overrides={"filesystem": "other"})["panels"][0]["queries"][0]
    third = compile(dashboard([target(downsampleInterval="10s")]))["panels"][0]["queries"][0]
    assert len({first["key"], second["key"], third["key"]}) == 3


def test_variable_in_legend_does_not_claim_to_filter_query():
    q = compile(dashboard([target(filters=[], tags={})]))["panels"][0]["queries"][0]
    assert "filesystem" not in q["metadata"]["used_variables"]
    assert "filesystem" in q["metadata"]["legend_variables"]
    assert "filesystem" in q["metadata"]["unused_filter_variables"]


def test_response_preserves_all_labels_null_zero_and_precision():
    q = compile()["panels"][0]["queries"][0]
    raw = [{"metric": "test.sdk.qps", "tags": {"method": "read", "extra": "not-in-legend"},
            "aggregateTags": ["host"], "dps": {"1700000000": 0, "1700000060": 1.23456789123,
                                               "1700000120": None, "1700000180": float("nan")}}]
    s = parse_response(q, raw)[0]
    assert s["name"] == "requested_fs-read"
    assert s["labels"]["extra"] == "not-in-legend"
    assert s["points"] == [[1700000000000, 0], [1700000060000, 1.23456789123],
                            [1700000120000, None], [1700000180000, None]]
    assert s["quality"]["nonfinite_values"] == 1
    assert s["aggregate_tags"] == ["host"]


def test_response_ms_resolution_is_explicit_not_timestamp_heuristic():
    q = compile()["panels"][0]["queries"][0]
    q["metadata"]["tsdb_resolution"] = 2
    assert parse_response(q, [{"dps": {"1234": 2}}])[0]["points"] == [[1234, 2]]


def test_secondary_yaxis_unit_follows_legend_override():
    q = compile(dashboard([target(alias="microsec.latency")],
                          yaxes=[{"format": "short"}, {"format": "µs"}],
                          seriesOverrides=[{"alias": "/microsec.*/", "yaxis": 2}]))["panels"][0]["queries"][0]
    assert parse_response(q, [{"dps": {"1234": 2}}])[0]["unit"] == "µs"


def test_explicit_all_override_is_normalized_and_auth_checks_only_selected_source():
    q = compile(variable_overrides={"task_id": "All"})["panels"][0]["queries"][0]
    assert q["query"]["filters"][1]["filter"] == "*"
    sources = SOURCES + [{"id": 999, "name": "restricted", "collectorAuthRequired": True}]
    result = compile_dashboard(dashboard(), sources, "https://grafana.example/", reference_from_ms=0,
                               reference_to_ms=600000, panel_widths={1: 400})
    assert result["panels"][0]["queries"][0]["kind"] == "opentsdb"


@pytest.mark.parametrize("raw", [{"error": {"message": "failed"}}, [{"__isError__": True, "message": "failed"}],
                                 {"message": "unexpected"}, [{"dps": None}], [{"dps": {"wrong": 3}}]])
def test_failed_or_malformed_response_never_becomes_empty(raw):
    with pytest.raises(QueryError):
        parse_response(compile()["panels"][0]["queries"][0], raw)


@pytest.mark.parametrize("raw", [
    {"error": "Authorization: Bearer credential-example"},
    [{"__isError__": True, "message": "https://example.invalid/?token=credential-example"}],
    [{"error": {"message": "Cookie: session=credential-example"}}],
    [{"dps": {"123": "credential-example"}}],
    [{"dps": {"credential-example": 1}}],
])
def test_response_errors_never_echo_raw_credentials(raw):
    with pytest.raises(QueryError) as failure:
        parse_response(compile()["panels"][0]["queries"][0], raw)
    assert "credential-example" not in str(failure.value)


def curve(name, points, **labels):
    return {"ref_id": name, "name": name, "metric": "metric", "labels": labels, "unit": "",
            "points": points}


def math_panel(expression="$A / $B", hidden_inputs=True):
    return compile(dashboard([target("A", hide=hidden_inputs), target("B", hide=hidden_inputs),
        {"refId": "C", "datasource": {"type": "__expr__", "uid": "__expr__"},
         "type": "math", "expression": expression}]))["panels"][0]


def test_hidden_math_inputs_required_and_divide_zero_null():
    panel = math_panel()
    assert all(q["metadata"]["required"] for q in panel["queries"])
    result = render_panel(panel, {"A": [curve("A", [[1000, 1], [2000, 1], [3000, 1]], host="x")],
                                  "B": [curve("B", [[1000, 2], [2000, 0], [4000, 2]], host="x")]})
    assert len(result) == 1 and result[0]["ref_id"] == "C"
    assert result[0]["points"] == [[1000, .5], [2000, None], [3000, None], [4000, None]]


def test_missing_dependency_different_from_legit_empty():
    panel = math_panel()
    with pytest.raises(QueryError, match="Missing"):
        render_panel(panel, {"A": []})
    assert render_panel(panel, {"A": [], "B": []}) == []


def test_label_join_supports_subset_and_rejects_ambiguity():
    panel = math_panel()
    out = render_panel(panel, {"A": [curve("A", [[1, 4]], host="x")], "B": [curve("B", [[1, 2]])]})
    assert out[0]["labels"] == {"host": "x"}
    with pytest.raises(QueryError, match="Ambiguous"):
        render_panel(panel, {"A": [curve("A", [[1, 4]], host="x"), curve("A2", [[1, 6]], host="x")],
                             "B": [curve("B", [[1, 2]])]})


def test_math_no_eval_calls_and_cycles():
    panel = math_panel('__import__("os").system("touch /tmp/forbidden")')
    assert panel["queries"][-1]["kind"] == "unsupported"
    panel = math_panel("$C + $A")
    with pytest.raises(QueryError, match="Circular"):
        render_panel(panel, {"A": [], "B": []})


def test_calculate_field_replaces_inputs_and_uses_field_names():
    d = dashboard([target("A", alias="success"), target("B", alias="total")], transformations=[{
        "id": "calculateField", "options": {"mode": "binary", "alias": "成功率", "replaceFields": True,
                                               "binary": {"left": "success", "right": "total", "operator": "/"}}}])
    panel = compile(d)["panels"][0]
    result = render_panel(panel, {"A": [curve("success", [[1, 5], [2, 3]])], "B": [curve("total", [[1, 10], [2, 0]])]})
    assert len(result) == 1 and result[0]["name"] == "成功率"
    assert result[0]["points"] == [[1, .5], [2, None]]
    panel["transformations"][0]["id"] = "unimplemented"
    with pytest.raises(UnsupportedQuery, match="transformation"):
        render_panel(panel, {"A": [], "B": []})


def test_irrelevant_hidden_query_not_required_or_rendered():
    p = compile(dashboard([target(), target("B", hide=True, metric="$missing")]))["panels"][0]
    assert not p["queries"][1]["metadata"]["required"]
    assert render_panel(p, {"A": []}) == []


def test_bosun_compiles_local_assignments_and_anchors_exact_range():
    from urllib.parse import parse_qs, urlsplit
    d = dashboard([{"refId": "A", "expr": '$a = q("sum:$ds-avg:test.qps{fs=$filesystem}", "$start", "")\n$a / 2',
                    "alias": "$tag_method", "useTimestamp": False, "bytedVRegion": "north|west"}])
    d["panels"][0]["panels"][0]["datasource"] = "bosun"
    sources = SOURCES + [{"id": 50, "name": "bosun", "type": "bosun-datasource", "jsonData": {"isVRegionAble": "open"}}]
    q = compile_dashboard(d, sources, "https://grafana.example/", reference_from_ms=1700000000000,
                           reference_to_ms=1700021600000, panel_widths={1: 400})["panels"][0]["queries"][0]
    assert q["kind"] == "bosun"
    method, url, expression = build_request(q, 1700000000000, 1700000300000)
    params = parse_qs(urlsplit(url).query)
    assert method == "POST" and urlsplit(url).path == "/api/datasources/proxy/50/api/expr"
    assert params == {"_region": ["north|west"], "_isGroupByRegion": ["false"], "date": ["2023-11-14"], "time": ["22:18:20"]}
    assert expression == '$a = q("sum:1m-avg:test.qps{fs=saved_fs}", "300s", "")\n$a / 2'
    s = parse_response(q, {"Type": "series", "Results": [{"Group": {"method": "read"},
                                                         "Value": {"1700000000": .125}}]})[0]
    assert s["name"] == "read" and s["points"] == [[1700000000000, .125]]
    with pytest.raises(UnsupportedQuery, match="expected a time series"):
        parse_response(q, {"Type": "number", "Results": []})


@pytest.mark.parametrize("region_setting", [None, True, "close"])
def test_bosun_region_only_enabled_by_exact_open_setting(region_setting):
    d = dashboard([{"refId": "A", "expr": 'q("sum:metric{fs=$filesystem}", "$start", "")',
                    "bytedVRegion": "north|west"}])
    d["panels"][0]["panels"][0]["datasource"] = "bosun"
    sources = [{"id": 50, "name": "bosun", "type": "bosun-datasource", "jsonData": {"isVRegionAble": region_setting}}]
    q = compile_dashboard(d, sources, "https://grafana.example/", reference_from_ms=0,
                           reference_to_ms=600000, panel_widths={1: 400})["panels"][0]["queries"][0]
    assert q["route"] == "/api/datasources/proxy/50/api/expr"
    assert q["interval_ms"] == 0 and q["metadata"]["sampling"] == "native_or_expression_defined"


def test_internal_bosun_v2_timestamp_and_values_response():
    from urllib.parse import parse_qs, urlsplit
    d = dashboard([{"refId": "A", "expr": 'q("sum:$ds-avg:metric", "$start", "")',
                    "useTimestamp": True, "bytedVRegion": "north"}])
    d["panels"][0]["panels"][0]["datasource"] = "bosun"
    sources = [{"id": 50, "name": "bosun", "type": "bosun-datasource", "jsonData": {
        "isVRegionAble": "open", "bytedCustomer": "metrics-v2"}}]
    q = compile_dashboard(d, sources, "https://grafana.example/", reference_from_ms=0,
                           reference_to_ms=600000, panel_widths={1: 400})["panels"][0]["queries"][0]
    method, url, body = build_request(q, 1700000000000, 1700000300000)
    assert urlsplit(url).path == "/api/datasources/proxy/50/api/v2/expr"
    assert parse_qs(urlsplit(url).query) == {"_region": ["north"], "_isGroupByRegion": ["false"]}
    assert body == {"expr": 'q("sum:2s-avg:metric", "300s", "")'}
    assert parse_response(q, {"Type": "series", "Results": [{"Group": {"z": "v"},
        "Values": {"1700000000": 3}}]})[0]["points"] == [[1700000000000, 3]]
