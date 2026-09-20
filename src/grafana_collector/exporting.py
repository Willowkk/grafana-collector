"""Select an export format without changing a run's frozen collection plan."""

from __future__ import annotations

from .errors import CollectorError


DEFAULT_FORMAT = "parquet"
EXPORT_FORMATS = ("parquet", "xlsx")


def validate_format(value: object) -> str:
    """Apply the same validation to TOML, CLI, and direct Python callers."""
    if not isinstance(value, str) or value not in EXPORT_FORMATS:
        raise CollectorError("format 必须为 parquet 或 xlsx。")
    return value


def export_run(store, out_dir, *, from_ms=None, to_ms=None, panel_ids=None,
               format=DEFAULT_FORMAT):
    """Export a local snapshot; imports are lazy so login/inspect stay lightweight."""
    selected = validate_format(format)
    if selected == "parquet":
        from .parquet_exporter import export_run as write_export
    else:
        from .exporter import export_run as write_export
    return write_export(store, out_dir, from_ms=from_ms, to_ms=to_ms,
                        panel_ids=panel_ids)
