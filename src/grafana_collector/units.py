"""Grafana 7.5 unit semantics and numeric Excel display conversions.

The definitions follow the *installed* Grafana 7.5.3 application bundle:
``app.05b1e2420ad003a5e4fe.js``, modules GNR5 (value formatters), dZjC
(IEC/SI suffixes), and the graph panel's ``configureAxisMode``. Grafana
formats each tick/tooltip value independently. Excel deliberately chooses one
scale per compatible unit family in the selected panel/time window so that
cells in a column remain suitable for arithmetic. This is a presentation
conversion only: neither the input series nor stored raw values are changed.

``build_unit_plan`` returns JSON-compatible metadata keyed by ``series_id``.
Use ``convert_value`` for the cell value and ``number_format_for_value`` for
its Excel format. Values are never rounded before writing the numeric cell.
The latter may select scientific notation for very small values, retaining
the same unit throughout the panel. Percentages retain their original ratio
as the cell value; Excel's percent format supplies the factor of 100.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real


_IEC_PREFIXES = ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi", "Yi")
_SI_PREFIXES = ("", "k", "M", "G", "T", "P", "E", "Z", "Y")


@dataclass(frozen=True)
class _Unit:
    family: str
    source_factor: int
    steps: tuple[tuple[int, str, str], ...]  # base divisor, display name, suffix
    first: int = 0


def _steps(base, prefixes, ending):
    return tuple((base ** index, prefix + ending, " " + prefix + ending)
                 for index, prefix in enumerate(prefixes))


_BINARY_BYTES = _steps(1024, _IEC_PREFIXES, "B")
_DURATION = (
    (1, "µs", " µs"), (1000, "ms", " ms"), (1_000_000, "s", " s"),
    (60_000_000, "min", " min"), (3_600_000_000, "hour", " hour"),
    (86_400_000_000, "day", " day"), (31_536_000_000_000, "year", " year"),
)
_UNITS = {
    "bytes": _Unit("binary_bytes", 1, _BINARY_BYTES),
    # Grafana's kbytes is kibibytes (binary B with prefix offset 1), not kB.
    "kbytes": _Unit("binary_bytes", 1024, _BINARY_BYTES, 1),
    "decbytes": _Unit("decimal_bytes", 1, _steps(1000, _SI_PREFIXES, "B")),
    "binBps": _Unit("binary_bytes_per_second", 1, _steps(1024, _IEC_PREFIXES, "B/s")),
    "µs": _Unit("duration", 1, _DURATION[:3]),
    "ms": _Unit("duration", 1000, _DURATION, 1),
    "reqps": _Unit("requests_per_second", 1,
                   tuple((1000 ** index, prefix + "req/s", prefix + " req/s")
                         for index, prefix in enumerate(("", "K", "M", "B", "T")))),
    "short": _Unit("short_count", 1,
                   tuple((1000 ** index, prefix, " " + prefix if prefix else "")
                         for index, prefix in enumerate(("", "K", "Mil", "Bil", "Tri", "Quadr", "Quint", "Sext", "Sept")))),
    "percentunit": _Unit("ratio_percent", 1, ((1, "%", "%"),)),
    "none": _Unit("unscaled_number", 1, ((1, "", ""),)),
    "string": _Unit("unscaled_string", 1, ((1, "", ""),)),
}

SUPPORTED_UNITS = frozenset(_UNITS)


def _unit(unit):
    unit = unit or "none"
    if unit not in _UNITS:
        raise ValueError(f"Unsupported Grafana unit: {unit!r}")
    return str(unit), _UNITS[unit]


def _is_numeric(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _literal(suffix):
    # Suffixes are from the fixed registry, never user/remote number formats.
    return '"' + suffix + '"' if suffix else ""


def _default_format(unit, suffix):
    if unit == "percentunit":
        return "0.##%"
    if unit == "string":
        return "General"
    return "0.###" + _literal(suffix)


def _spec(source_unit, definition, step):
    divisor, display_unit, suffix = step
    # Percentages are represented by the unscaled ratio in a numeric cell.
    scale = definition.source_factor / divisor
    if scale == 1:
        scale = 1
    return {
        "source_unit": source_unit,
        "display_unit": display_unit,
        "unit_family": definition.family,
        "scale": scale,
        "source_to_base": definition.source_factor,
        "display_to_base": divisor,
        "suffix": suffix,
        "excel_percent": source_unit == "percentunit",
        "number_format": _default_format(source_unit, suffix),
        "min_column_width": max(18, len(suffix) + 14),
        "numeric_conversion": "cell_value = source_value * scale; source_value = cell_value / scale",
        "display_policy": "panel_unit_family_max_absolute_value",
        "format_basis": "Grafana 7.5.3 GNR5/dZjC; fixed panel-family scale for numeric Excel",
    }


def build_unit_plan(series):
    """Choose common scales using all selected points, before Excel splitting.

    ``series`` is a panel's list of dictionaries containing ``series_id``,
    ``unit``, and ``points`` as ``[timestamp_ms, value]`` pairs. ``id`` is an
    accepted fallback. Bytes and kibibytes are compatible; byte rates and
    decimal bytes intentionally remain separate. µs and ms are compatible.
    Empty/all-zero families keep the smallest *configured* source unit.
    Returns metadata only and does not mutate any series or point.
    """
    families = {}
    items = []
    seen = set()
    for item in series:
        sid = item.get("series_id", item.get("id"))
        if sid is None:
            raise ValueError("Unit planning requires a series_id")
        sid = str(sid)
        if sid in seen:
            raise ValueError("Unit planning requires unique series IDs")
        seen.add(sid)
        unit, definition = _unit(item.get("unit"))
        group = families.setdefault(definition.family, {"maximum": 0, "definitions": []})
        group["definitions"].append(definition)
        for _, value in item.get("points", []):
            if _is_numeric(value):
                base_value = abs(value) * definition.source_factor
                if not math.isfinite(base_value):
                    raise ValueError("Unit conversion exceeds finite numeric range")
                group["maximum"] = max(group["maximum"], base_value)
        items.append((sid, unit, definition))

    selected = {}
    for family, group in families.items():
        # The longest step table supports mixed ms/µs while µs-only panels
        # retain Grafana's µs -> ms -> s (never minutes/hours) convention.
        steps = max((definition.steps for definition in group["definitions"]), key=len)
        first = min(definition.first for definition in group["definitions"])
        step = steps[first]
        for candidate in steps[first + 1:]:
            if group["maximum"] < candidate[0]:
                break
            step = candidate
        selected[family] = step
    return {sid: _spec(unit, definition, selected[definition.family])
            for sid, unit, definition in items}


def choose_scale(unit, maximum):
    """Select one unit's scale; ``maximum`` is in that source unit."""
    return build_unit_plan([{"series_id": "value", "unit": unit,
                             "points": [[0, maximum]]}])["value"]


def convert_value(value, spec):
    """Convert without rounding. Missing/nonfinite values remain blank."""
    if value is None:
        return None
    if isinstance(value, str) and spec["source_unit"] == "string":
        return value
    if not _is_numeric(value):
        return None
    result = value * spec["scale"]
    if not math.isfinite(result) or (value != 0 and result == 0):
        raise ValueError("Unit conversion exceeds finite numeric range")
    return result


def _grafana_decimals(value):
    """Port of GNR5.toFixed's automatic decimal selector (including sign)."""
    if value == 0 or value % 1 == 0:
        return 0
    digits = 1 - math.floor(math.log(abs(value)) / math.log(10))
    # Written as a ratio in the JS source. Log form avoids under/overflow at
    # subnormal floating-point boundaries without changing ordinary inputs.
    power = 10.0 ** -digits if -323 <= -digits <= 308 else None
    if value > 0 and (value / power > 2.25 if power else
                      math.log10(value) + digits > math.log10(2.25)):
        digits += 1
    return max(0, digits)


def number_format_for_value(value, spec):
    """Excel format for an already converted cell, with a fixed unit suffix.

    Automatic decimal precision follows Grafana for ordinary values. Tiny or
    huge magnitudes use scientific notation instead of displaying a nonzero
    input as zero or creating unreadably wide cells. This only changes cell
    presentation; the numeric value retains all available precision.
    """
    if value is None or not _is_numeric(value):
        return spec["number_format"]
    if spec["source_unit"] == "string":
        return "General"
    displayed = value * 100 if spec["excel_percent"] else value
    suffix = "%" if spec["excel_percent"] else _literal(spec["suffix"])
    if displayed and (abs(displayed) < 1e-4 or abs(displayed) >= 1e12):
        return "0.00E+00" + suffix
    decimals = min(8, _grafana_decimals(displayed))
    if spec["excel_percent"]:
        # Keep useful percentage precision without expanding one-ULP noise
        # around 1 into long strings such as 100.00000000000000%.
        decimals = max(2, decimals)
    return "0" + ("." + "0" * decimals if decimals else "") + suffix


def _js_number(value):
    """JavaScript's ordinary Number-to-string form for formatter output."""
    if value == 0:
        return "0"
    if math.isinf(value):
        return "∞" if value > 0 else "-∞"
    if math.isnan(value):
        return "NaN"
    if 1e-6 <= abs(value) < 1e21 and value == int(value):
        return str(int(value))
    rendered = repr(float(value))
    if "e" in rendered:
        mantissa, exponent = rendered.split("e")
        exponent_number = int(exponent)
        if 1e-6 <= abs(value) < 1e21:
            return format(value, "." + str(max(0, -exponent_number + len(mantissa.split(".")[-1]))) + "f").rstrip("0").rstrip(".")
        mantissa = mantissa.removesuffix(".0")
        return mantissa + "e" + ("+" if exponent_number >= 0 else "-") + str(abs(exponent_number))
    return rendered.removesuffix(".0")


def _grafana_fixed(value, decimals=None):
    if value is None:
        return ""
    if not math.isfinite(value):
        return _js_number(value)
    if decimals is None:
        decimals = _grafana_decimals(value)
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 100:
        raise ValueError("Decimals must be an integer from 0 to 100")
    factor = 10 ** decimals
    product = value * factor
    if not math.isfinite(product):
        return _js_number(value)
    # Math.round chooses the integer towards +infinity at exact .5, unlike
    # Python's bankers' round and unlike ROUND_HALF_UP for negative values.
    rounded = math.floor(product)
    if product - rounded >= 0.5:
        rounded += 1
    result = rounded / factor
    text = _js_number(result)
    if "e" in text or value == 0:
        return text
    current = len(text.split(".", 1)[1]) if "." in text else 0
    if current < decimals:
        text += ("" if current else ".") + "0" * (decimals - current)
    return text


def format_value(unit, value, decimals=None):
    """Return Grafana's per-value auto-scaled text for comparison/previews.

    This is separate from Excel's intentional common-scale policy. Null is
    blank. The implementation covers all units present on the SDKv2 board.
    """
    unit, definition = _unit(unit)
    if value is None:
        return ""
    if unit == "string":
        return _js_number(value) if isinstance(value, Real) else str(value)
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ValueError("Grafana numeric format requires a number")
    if not math.isfinite(value):
        return _js_number(value)
    if unit == "percentunit":
        return _grafana_fixed(value * 100, decimals) + "%"
    step = definition.steps[definition.first]
    base_value = abs(value) * definition.source_factor
    for candidate in definition.steps[definition.first + 1:]:
        if base_value < candidate[0]:
            break
        step = candidate
    # scaledUnits returns NA after exhausting its suffix table. Duration
    # formatters are piecewise functions that intentionally keep their last
    # unit indefinitely (e.g. µs never changes seconds into minutes).
    if unit not in {"µs", "ms", "none"}:
        base = 1024 if unit in {"bytes", "kbytes", "binBps"} else 1000
        if base_value >= definition.steps[-1][0] * base:
            return "NA" + (" req/s" if unit == "reqps" else "")
    divisor, _, suffix = step
    scaled = value * definition.source_factor / divisor
    return _grafana_fixed(scaled, decimals) + suffix
