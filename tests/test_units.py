"""Golden unit results evaluated from the installed Grafana 7.5.3 bundle."""

from copy import deepcopy
import json
import math

import pytest

from grafana_collector.units import (
    SUPPORTED_UNITS, build_unit_plan, choose_scale, convert_value, format_value,
    number_format_for_value,
)


def series(sid, unit, *values):
    return {"series_id": sid, "unit": unit,
            "points": [[1000 * index, value] for index, value in enumerate(values)]}


@pytest.mark.parametrize(("unit", "raw", "expected"), [
    ("binBps", 506523156.5333, "483 MiB/s"),
    ("binBps", 1651714147.7666, "1.54 GiB/s"),
    ("binBps", 1000, "1000 B/s"),
    ("bytes", 1234.5678, "1.21 KiB"),
    ("kbytes", 1234.5678, "1.21 MiB"),
    ("decbytes", 1234.5678, "1.23 kB"),
    ("µs", 1234.5678, "1.23 ms"),
    ("µs", 1651714147.7666, "1652 s"),
    ("ms", 506523156.5333, "5.86 day"),
    ("ms", 60_000, "1 min"),
    ("ms", 31_536_000_000, "1 year"),
    ("reqps", 1234.5678, "1.23K req/s"),
    ("reqps", 1651714147.7666, "1.65B req/s"),
    ("short", 506523156.5333, "507 Mil"),
    ("short", 1651714147.7666, "1.65 Bil"),
    ("percentunit", 0.01234, "1.23%"),
    ("percentunit", 1.001001001001001, "100%"),
    ("none", 1234.5678, "1235"),
    ("none", -1.2345, "-1.2"),
    ("string", 1234.5678, "1234.5678"),
    ("string", "healthy", "healthy"),
])
def test_grafana_source_golden_auto_format(unit, raw, expected):
    assert format_value(unit, raw) == expected


def test_exact_rounding_and_explicit_decimals_from_javascript():
    assert format_value("none", 1.25, 1) == "1.3"
    assert format_value("none", -1.25, 1) == "-1.2"
    assert format_value("none", 1, 3) == "1.000"
    assert format_value("none", 0, 3) == "0"
    assert format_value("none", 1e-8) == "1e-8"
    assert format_value("none", 1e20) == "100000000000000000000"


def test_actual_throughput_extent_uses_common_gibps_without_losing_small_points():
    items = [series("large", "binBps", 506523156.5333, 1651714147.7666, 2048719434),
             series("small", "binBps", 0.1666, None, 0)]
    original = deepcopy(items)
    plan = build_unit_plan(items)
    assert items == original
    assert {spec["display_unit"] for spec in plan.values()} == {"GiB/s"}
    assert {spec["scale"] for spec in plan.values()} == {1 / 1024 ** 3}
    cell = convert_value(0.1666, plan["small"])
    assert cell == 0.1666 / 1024 ** 3
    assert cell > 0
    assert number_format_for_value(cell, plan["small"]) == '0.00E+00" GiB/s"'
    assert convert_value(1651714147.7666, plan["large"]) == 1651714147.7666 / 1024 ** 3
    assert convert_value(0, plan["small"]) == 0
    assert convert_value(None, plan["small"]) is None
    assert number_format_for_value(None, plan["small"]) == plan["small"]["number_format"]


def test_binary_bytes_and_kibibytes_share_scale_and_other_dimensions_stay_separate():
    plan = build_unit_plan([
        series("bytes", "bytes", 2 * 1024 ** 2),
        series("kibibytes", "kbytes", 1024),
        series("rate", "binBps", 1024 ** 3),
        series("decimal", "decbytes", 1000 ** 3),
    ])
    assert plan["bytes"]["display_unit"] == plan["kibibytes"]["display_unit"] == "MiB"
    assert plan["bytes"]["scale"] == 1 / 1024 ** 2
    assert plan["kibibytes"]["scale"] == 1 / 1024
    assert convert_value(1024, plan["kibibytes"]) == 1
    assert plan["rate"]["display_unit"] == "GiB/s"
    assert plan["decimal"]["display_unit"] == "GB"
    assert plan["rate"]["unit_family"] != plan["decimal"]["unit_family"]
    assert json.loads(json.dumps(plan, allow_nan=False)) == plan


def test_duration_source_units_convert_into_one_compatible_scale():
    plan = build_unit_plan([series("us", "µs", 122901), series("ms", "ms", 0.001, 123)])
    assert {spec["display_unit"] for spec in plan.values()} == {"ms"}
    assert plan["us"]["scale"] == 0.001
    assert plan["ms"]["scale"] == 1
    assert convert_value(122901, plan["us"]) == 122.901
    assert choose_scale("µs", 120_000_000)["display_unit"] == "s"
    assert choose_scale("ms", 120_000)["display_unit"] == "min"


def test_maximum_uses_absolute_value_and_all_selected_series():
    plan = build_unit_plan([series("a", "reqps", 0.0333), series("b", "reqps", -50095.47625)])
    assert {spec["display_unit"] for spec in plan.values()} == {"Kreq/s"}
    assert convert_value(-50095.47625, plan["b"]) == -50095.47625 * 0.001
    assert number_format_for_value(convert_value(0.0333, plan["a"]), plan["a"]) == '0.00E+00"K req/s"'
    # A caller filtering its selected window first gets an independently
    # appropriate scale, while all file chunks from one plan keep one scale.
    assert build_unit_plan([series("a", "reqps", 0.0333)])["a"]["display_unit"] == "req/s"


def test_percent_ratio_stays_numeric_and_values_above_100_are_not_clamped():
    raw = 1.001001001001001
    spec = choose_scale("percentunit", raw)
    assert spec["scale"] == 1 and spec["excel_percent"] is True
    assert spec["display_unit"] == "%"
    assert convert_value(raw, spec) == raw
    assert number_format_for_value(raw, spec) == "0.00%"
    assert number_format_for_value(1, spec) == "0.00%"
    assert number_format_for_value(math.nextafter(1, 2), spec) == "0.00%"
    assert number_format_for_value(0.005, spec) == "0.000%"
    assert number_format_for_value(1e-12, spec) == "0.00E+00%"


@pytest.mark.parametrize("unit", sorted(SUPPORTED_UNITS))
def test_all_dashboard_units_preserve_missing_zero_and_unrounded_numeric_values(unit):
    raw = 1.23456789012345
    spec = choose_scale(unit, 1234)
    converted = convert_value(raw, spec)
    assert converted == raw * spec["scale"]
    assert converted / spec["scale"] == pytest.approx(raw, rel=1e-15)
    assert convert_value(None, spec) is None
    assert convert_value(float("inf"), spec) is None
    assert convert_value(float("nan"), spec) is None
    assert convert_value(0, spec) == 0
    assert number_format_for_value(None, spec) == spec["number_format"]
    assert format_value(unit, None) == ""


def test_empty_and_zero_families_keep_configured_source_scale():
    assert choose_scale("kbytes", None)["display_unit"] == "KiB"
    assert choose_scale("ms", 0)["display_unit"] == "ms"
    plan = build_unit_plan([series("ms", "ms", 0), series("us", "µs", None)])
    assert {spec["display_unit"] for spec in plan.values()} == {"µs"}
    assert build_unit_plan([]) == {}


def test_string_and_unspecified_units_keep_underlying_values():
    spec = choose_scale("string", 10000)
    assert convert_value("healthy", spec) == "healthy"
    assert convert_value(10000, spec) == 10000
    assert number_format_for_value(10000, spec) == "General"
    assert choose_scale(None, 10000)["source_unit"] == "none"


def test_bad_ids_unsupported_units_and_underflow_fail_explicitly():
    with pytest.raises(ValueError, match="series_id"):
        build_unit_plan([{"unit": "bytes"}])
    with pytest.raises(ValueError, match="unique"):
        build_unit_plan([series("a", "bytes", 0), series("a", "bytes", 0)])
    with pytest.raises(ValueError, match="Unsupported Grafana unit"):
        choose_scale("unrecognized-unit", 1)
    with pytest.raises(ValueError, match="finite numeric range"):
        convert_value(math.ulp(0), choose_scale("binBps", 1024 ** 3))
    assert build_unit_plan([{"id": 7, "unit": "bytes", "points": []}])["7"]["display_unit"] == "B"


def test_scale_retains_maximum_defined_unit_for_large_numeric_excel_values():
    # Grafana's finite prefix table gives NA; Excel can still preserve the
    # measured numeric value and identify its largest supported unit.
    spec = choose_scale("reqps", 10 ** 18)
    assert spec["display_unit"] == "Treq/s"
    assert convert_value(10 ** 18, spec) == 10 ** 6
    assert format_value("reqps", 10 ** 18) == "NA req/s"
