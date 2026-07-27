"""schema_scan.py — auto-assist for the watched-metric list.

Reuses the live-schema read (the same idea the chat assistant uses) to find
numeric columns in the weekly fact that are NOT in the configured metric list,
so operators can be prompted to add them. It only *suggests* candidates — a
human still supplies the business meaning (direction / threshold / target) in
prism_config.yaml. This is the discovery half of the "config-driven + auto-assist"
design; the config half already lives in config.py / prism_config.yaml.
"""
from data_connector import get_connection
from config import METRICS

# SQLite numeric type affinities (pandas.to_sql -> INTEGER / REAL)
_NUMERIC = ("INT", "REAL", "NUM", "DOUB", "FLOA", "DEC")

# Numeric columns that are IDs / dates / dimensions, never metrics to trend.
_EXCLUDE_EXACT = {"StoreID", "FiscalYear", "FiscalQuarter", "FiscalMonth", "WeekOfYear"}
_EXCLUDE_SUFFIX = ("id", "key", "year", "quarter", "month", "weekofyear", "_py")


def _columns(fact_table):
    """Return [(name, TYPE), ...]. SQLite PRAGMA today; on the Fabric SQL endpoint
    this would query INFORMATION_SCHEMA.COLUMNS — same idea, one backend-specific call."""
    conn = get_connection()
    rows = conn.execute(f"PRAGMA table_info([{fact_table}])").fetchall()
    return [(r[1], (r[2] or "").upper()) for r in rows]


def scan_unwatched_metrics(fact_table="Fact_StoreWeekly"):
    """Numeric columns in fact_table that are NOT in the configured metric list
    and are not obviously IDs/dates/dimensions."""
    try:
        cols = _columns(fact_table)
    except Exception:
        return []
    watched = set(METRICS.keys())
    out = []
    for name, ctype in cols:
        if name in watched or name in _EXCLUDE_EXACT:
            continue
        if not any(t in ctype for t in _NUMERIC):
            continue
        if name.lower().endswith(_EXCLUDE_SUFFIX):
            continue
        out.append(name)
    return sorted(out)


def suggest_yaml_block(col, direction="down_is_bad", pct_drop=20.0, min_gap=3.0, target_col=""):
    """A ready-to-paste prism_config.yaml `metrics:` block for a chosen column.
    The human picks the direction/threshold/target; this just writes the boilerplate."""
    label = col.replace("_Pct", " %").replace("_", " ").strip()
    tgt = target_col.strip() if (target_col and target_col.strip()) else "null"
    return "\n".join([
        f"  {col}:",
        f"    label: {label}",
        f"    direction: {direction}",
        f"    trailing_weeks: 8",
        f"    target_col: {tgt}",
        f"    pct_drop_threshold: {pct_drop}",
        f"    min_target_gap: {min_gap}",
        f'    unit: "%"',
    ])
