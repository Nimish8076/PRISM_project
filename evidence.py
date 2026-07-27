"""evidence.py — the GATHER step for PRISM's agentic diagnosis.

For a store that DETECTION has already flagged, assemble ONE "evidence packet":
a single dict holding every fact the reasoning step (reason.py) needs to explain
the store as one connected story instead of metric-by-metric. It replaces the
hand-collected sub-metric queries scattered through diagnose.py with a single,
data-driven bundle.

Design properties:
  - Deterministic and needs NO API key — it only reads data.
  - All reads go through data_connector.execute_query (the SELECT-only guard).
  - COLUMN-AWARE: it only selects columns that actually exist, so the lean test
    dataset and the full production set both work with no code change. Adding a
    new metric later means adding a name to a candidate list, not a new branch.

Public API:
  group_by_store(anomalies)          -> {store_id: [anomaly, ...]}
  build_store_packet(store_id, anoms)-> evidence packet dict
"""
import warnings
import pandas as pd
from data_connector import get_connection, execute_query

# Weekly columns to include in the trend IF they exist in Fact_StoreWeekly.
_TREND_CANDIDATES = [
    "SSS_Pct", "SST_Pct", "EBITDA_Pct", "OSAT_Pct", "Accuracy_Pct",
    "WeeklyTransactions", "AvgTicket", "WeeklyAUV",
    "OLO_Pct", "ThreePD_Pct", "Loyalty_Pct", "OpEx_Score", "AUVTrend_Pct",
]
_OSAT_SUBS = ["FoodQuality_Score", "Service_Score", "Cleanliness_Score", "ValueScore"]
_TRAILING_WEEKS = 12

# Drivers always worth checking for lead/lag, on top of whatever fired.
_DRIVER_HINTS = ["SSS_Pct", "WeeklyTransactions", "AvgTicket", "EBITDA_Pct", "OSAT_Pct"]


# ── low-level helpers ────────────────────────────────────────────────────────
def _q(sql):
    """Run a read-only query; return a DataFrame or None."""
    df, err = execute_query(get_connection(), sql)
    if err or df is None or df.empty:
        return None
    return df


def _cols(table):
    """Column names present in a table (empty set on any error)."""
    try:
        rows = get_connection().execute(f"PRAGMA table_info([{table}])").fetchall()
        return {r[1] for r in rows}
    except Exception:
        return set()


def _sid_pred(store_id, col="StoreID"):
    """SQL predicate matching a store id regardless of '#' prefix / int-vs-text."""
    return f"REPLACE(CAST({col} AS TEXT), '#', '') = REPLACE('{store_id}', '#', '')"


def _norm(store_id):
    return str(store_id).replace("#", "").strip()


def _num(v):
    try:
        if pd.isna(v):
            return None
        return round(float(v), 2)
    except Exception:
        return None


def _to_dt(series, **kw):
    """pd.to_datetime with warnings silenced (mixed date formats across datasets)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(series, errors="coerce", **kw)


# ── packet sections ──────────────────────────────────────────────────────────
def _store_context(sid):
    """City, region, and the responsible chain (owner, area director, FBC, RVP)."""
    ctx = {"city": None, "region": None, "franchise_owner": None,
           "area_director": None, "fbc": None, "regional_vp": None}

    ds = _cols("Dim_Store")
    want = [c for c in ("City", "Region", "FranchiseOwner", "AreaDirector") if c in ds]
    if want:
        df = _q(f"SELECT {', '.join(want)} FROM Dim_Store WHERE {_sid_pred(sid)} LIMIT 1")
        if df is not None:
            r = df.iloc[0]
            ctx["city"] = r.get("City")
            ctx["region"] = r.get("Region")
            ctx["franchise_owner"] = r.get("FranchiseOwner")
            ctx["area_director"] = r.get("AreaDirector")

    fw = _cols("Fact_StoreWeekly")
    want2 = [c for c in ("FBC_Name", "RegionalVP") if c in fw]
    if want2 and "FiscalWeekKey" in fw:
        df2 = _q(f"SELECT {', '.join(want2)}, FiscalWeekKey FROM Fact_StoreWeekly "
                 f"WHERE {_sid_pred(sid)} AND FiscalWeekKey IS NOT NULL")
        if df2 is not None:
            df2["_d"] = pd.to_datetime(df2["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
            df2 = df2.dropna(subset=["_d"]).sort_values("_d")
            if not df2.empty:
                last = df2.iloc[-1]
                if "FBC_Name" in df2.columns:
                    ctx["fbc"] = last.get("FBC_Name")
                if "RegionalVP" in df2.columns:
                    ctx["regional_vp"] = last.get("RegionalVP")
    return ctx


def _weekly_trend(sid):
    """Last N weeks of every existing trend column, oldest→newest, as lists."""
    fw = _cols("Fact_StoreWeekly")
    metrics = [c for c in _TREND_CANDIDATES if c in fw]
    if not metrics or "FiscalWeekKey" not in fw:
        return {}
    sel = ", ".join(["FiscalWeekKey"] + metrics)
    df = _q(f"SELECT {sel} FROM Fact_StoreWeekly WHERE {_sid_pred(sid)} "
            f"AND FiscalWeekKey IS NOT NULL")
    if df is None:
        return {}
    df["_d"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
    df = df.dropna(subset=["_d"]).sort_values("_d").tail(_TRAILING_WEEKS)
    trend = {"weeks": [str(w) for w in df["FiscalWeekKey"].tolist()]}
    for m in metrics:
        trend[m] = [_num(v) for v in df[m].tolist()]
    return trend


def _osat_subscores(sid):
    """Latest guest-survey sub-scores (the OSAT driver breakdown)."""
    oc = _cols("Fact_OSAT")
    subs = [c for c in _OSAT_SUBS if c in oc]
    if not subs:
        return None
    has_week = "SurveyWeek" in oc
    sel = ", ".join(subs + (["SurveyWeek"] if has_week else []))
    df = _q(f"SELECT {sel} FROM Fact_OSAT WHERE {_sid_pred(sid)}")
    if df is None:
        return None
    if has_week:
        df["_d"] = pd.to_datetime(df["SurveyWeek"], format="%d-%m-%Y", errors="coerce")
        if df["_d"].notna().any():
            df = df.sort_values("_d")
    row = df.iloc[-1]
    return {s: _num(row.get(s)) for s in subs}


def _fsa_latest(sid):
    """Most recent food-safety audit for the store."""
    fc = _cols("Fact_FSAScore")
    if "FSA_Score" not in fc:
        return None
    want = [c for c in ("FSA_Score", "FirstPriorityFinding", "SecondPriorityFinding",
                        "AuditDate") if c in fc]
    df = _q(f"SELECT {', '.join(want)} FROM Fact_FSAScore WHERE {_sid_pred(sid)}")
    if df is None:
        return None
    if "AuditDate" in df.columns:
        df["_d"] = _to_dt(df["AuditDate"], dayfirst=True)
        if df["_d"].notna().any():
            df = df.sort_values("_d")
    row = df.iloc[-1]
    return {
        "score": _num(row.get("FSA_Score")),
        "first_finding": row.get("FirstPriorityFinding") if "FirstPriorityFinding" in df.columns else None,
        "second_finding": row.get("SecondPriorityFinding") if "SecondPriorityFinding" in df.columns else None,
        "audit_date": str(row.get("AuditDate")) if "AuditDate" in df.columns else None,
    }


# Per-run cache of "latest value per store" for each metric, so the peer
# comparison scans the weekly fact ONCE per metric instead of once per store.
_PEER_CACHE = {}


def reset_caches():
    """Clear per-run caches. Call at the start of a run in case data was reloaded."""
    _PEER_CACHE.clear()


def _latest_per_store(metric):
    """Latest row per store for one metric, with FBC/Region — computed once and
    cached, so N flagged stores don't each re-scan the whole weekly fact."""
    if metric in _PEER_CACHE:
        return _PEER_CACHE[metric]
    fw = _cols("Fact_StoreWeekly")
    result = None
    if metric in fw and "FiscalWeekKey" in fw:
        cols = [metric, "StoreID", "FiscalWeekKey"]
        if "FBC_Name" in fw:
            cols.append("FBC_Name")
        if "Region" in fw:
            cols.append("Region")
        df = _q(f"SELECT {', '.join(cols)} FROM Fact_StoreWeekly "
                f"WHERE {metric} IS NOT NULL AND FiscalWeekKey IS NOT NULL")
        if df is not None:
            df["_d"] = _to_dt(df["FiscalWeekKey"], format="%d-%m-%Y")
            df = df.dropna(subset=["_d"])
            latest = df.sort_values("_d").drop_duplicates(subset="StoreID", keep="last")
            latest["_sid"] = latest["StoreID"].astype(str).str.replace("#", "", regex=False).str.strip()
            result = latest
    _PEER_CACHE[metric] = result
    return result


def _peer_context(sid, ctx, flagged_metrics):
    """For each flagged weekly metric: the store's latest value vs its FBC-cohort
    and region-cohort averages. Answers 'just this store, or the whole area?' —
    the signal correlation needs and a dashboard can't show. Baselines are
    computed once per metric (cached), not once per store."""
    out = {}
    for m in flagged_metrics:
        latest = _latest_per_store(m)
        if latest is None or latest.empty:
            continue
        me = latest[latest["_sid"] == _norm(sid)]
        entry = {"store_value": _num(me.iloc[0][m]) if not me.empty else None}
        if "FBC_Name" in latest.columns and ctx.get("fbc"):
            g = latest[latest["FBC_Name"] == ctx["fbc"]]
            if not g.empty:
                entry["fbc_avg"] = _num(g[m].mean())
                entry["fbc_store_count"] = int(g["_sid"].nunique())
        if "Region" in latest.columns and ctx.get("region"):
            g = latest[latest["Region"] == ctx["region"]]
            if not g.empty:
                entry["region_avg"] = _num(g[m].mean())
                entry["region_store_count"] = int(g["_sid"].nunique())
        out[m] = entry
    return out


def _history(sid):
    """Prior alerts for this store from prism_history.db (recurrence)."""
    try:
        import alert_store
        rows = alert_store.get_alerts()
    except Exception:
        return []
    out = []
    for r in rows:
        if _norm(r.get("store_id", "")) == _norm(sid):
            out.append({
                "metric": r.get("metric_label") or r.get("metric"),
                "occurrences": r.get("occurrences"),
                "status": r.get("status"),
                "last_seen": r.get("last_seen"),
            })
    return out


def _lead_lag(trend, metrics, max_lag=3, min_points=5, corr_threshold=0.6):
    """Deterministic ripple hint: does metric A's movement PRECEDE metric B's?

    For each ordered pair, find the lag k (1..max_lag) maximising the correlation
    between A shifted forward by k weeks and B. A.shift(k).corr(B) measures 'A
    leads B by k'. Reports the strongest few pairs above a correlation threshold.
    Works with whatever series exist; used as evidence for the model AND as a
    non-LLM signal when reasoning is off.
    """
    # Build series, skipping any that are too short or constant (a flat series
    # has zero variance, so correlation is undefined — skip it to avoid noise).
    series = {}
    for m in metrics:
        if m not in trend:
            continue
        s = pd.Series([float("nan") if v is None else v for v in trend[m]])
        std = s.std(skipna=True)
        if s.notna().sum() >= min_points and pd.notna(std) and std > 0:
            series[m] = s

    hints = []
    with warnings.catch_warnings():
        # A shifted window can be locally flat (undefined corr -> NaN); we handle
        # NaN explicitly, so silence the numpy divide-by-zero notice it raises.
        warnings.simplefilter("ignore")
        for a, sa in series.items():
            for b, sb in series.items():
                if a == b:
                    continue
                best_k, best_c = 0, 0.0
                for k in range(1, max_lag + 1):
                    c = sa.shift(k).corr(sb)
                    if pd.notna(c) and abs(c) > abs(best_c):
                        best_k, best_c = k, c
                if best_k and abs(best_c) >= corr_threshold:
                    hints.append({"leader": a, "laggard": b,
                                  "lag_weeks": best_k, "corr": round(float(best_c), 2)})
    hints.sort(key=lambda h: abs(h["corr"]), reverse=True)
    return hints[:5]


def _compact_anomaly(a):
    """Keep only the fields the model needs from a detection anomaly dict."""
    keys = ("metric", "metric_label", "latest_value", "trailing_avg", "target_value",
            "stat_pct", "stat_delta", "target_gap", "floor_gap", "critical_floor", "methods",
            "severity_score", "fiscal_week", "finding", "audit_date", "unit")
    return {k: a.get(k) for k in keys if a.get(k) is not None}


# ── public API ───────────────────────────────────────────────────────────────
def group_by_store(anomalies):
    """Turn a flat anomaly list into {normalised_store_id: [anomaly, ...]}."""
    out = {}
    for a in anomalies:
        out.setdefault(_norm(a.get("store_id")), []).append(a)
    return out


def build_store_packet(store_id, anomalies):
    """Assemble the full evidence packet for one flagged store."""
    sid = _norm(store_id)
    ctx = _store_context(sid)
    trend = _weekly_trend(sid)
    flagged = [a.get("metric") for a in anomalies if a.get("metric")]
    ll_metrics = list(dict.fromkeys(flagged + _DRIVER_HINTS))
    return {
        "store_id": sid,
        "context": ctx,
        "latest_week": trend["weeks"][-1] if trend.get("weeks") else None,
        "anomalies": [_compact_anomaly(a) for a in anomalies],
        "weekly_trend": trend,
        "osat_subscores": _osat_subscores(sid),
        "fsa": _fsa_latest(sid),
        "peer_context": _peer_context(sid, ctx, flagged),
        "history": _history(sid),
        "lead_lag": _lead_lag(trend, ll_metrics),
    }


# ── manual test: `python evidence.py` ────────────────────────────────────────
if __name__ == "__main__":
    import json
    get_connection()

    # Get real anomalies from detection if possible; otherwise fabricate a
    # multi-metric store so the packet builder is still exercised end-to-end.
    found = []
    try:
        import agent
        from config import METRICS
        targets = agent._get_targets()
        for m, cfg in METRICS.items():
            found.extend(agent._detect_weekly_metric(m, cfg, targets))
        found.extend(agent._detect_fsa(targets))
        print(f"Detection produced {len(found)} anomalies.")
    except Exception as e:
        print(f"(Detection unavailable: {e}) — using a fabricated multi-metric store.")
        found = [{"store_id": "9004", "metric": "SSS_Pct", "metric_label": "Same Store Sales Growth"},
                 {"store_id": "9004", "metric": "EBITDA_Pct", "metric_label": "EBITDA Margin"}]

    grouped = group_by_store(found)
    print(f"Stores with anomalies: {sorted(grouped)}")

    # Prefer a multi-metric store to show the cross-metric bundle.
    pick = next((s for s, items in grouped.items()
                 if len({i['metric'] for i in items}) >= 2), None) or next(iter(grouped))
    print(f"\n=== Evidence packet for store #{pick} "
          f"({len(grouped[pick])} anomalies) ===\n")
    print(json.dumps(build_store_packet(pick, grouped[pick]), indent=2, default=str))
