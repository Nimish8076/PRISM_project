"""tools.py — the PRISM agent's tools (v2).

Each tool is a small, VERIFIED, read-only function over the data. Two kinds:
  • analysis doors  → return a MEASUREMENT (numbers + a factual note), never a verdict.
  • raw-data doors  → hand back the actual rows so the agent can look for itself.

Every tool goes through data_connector.execute_query (read-only guard), so the
agent can never mutate data. Tools are registered to the model in agent_loop.py.

Design rule: tools state facts ("transactions +6% vs trend"), not conclusions
("it's a traffic problem"). The agent does the interpreting.
"""
import pandas as pd
import warnings
from data_connector import get_connection, execute_query
from config import TARGET_PERSONA

_WEEKLY = "Fact_StoreWeekly"

# Columns the agent may reference by name (whitelist — prevents bad/unsafe columns).
ALLOWED_METRICS = {
    "SSS_Pct": "Same-store sales growth %", "OSAT_Pct": "Customer satisfaction %",
    "EBITDA_Pct": "EBITDA margin %", "AvgTicket": "Average ticket $",
    "WeeklyTransactions": "Weekly transactions (traffic)", "SST_Pct": "Same-store transactions %",
    "OLO_Pct": "Online-order mix %", "ThreePD_Pct": "Third-party delivery mix %",
    "Loyalty_Pct": "Loyalty mix %", "OpEx_Score": "Operations score", "Accuracy_Pct": "Order accuracy %",
    "WeeklyAUV": "Weekly average unit volume $",
}
_TARGET_COL = {"SSS_Pct": "SSSTarget", "OSAT_Pct": "OSATTarget", "EBITDA_Pct": "EBITDATarget",
               "AvgTicket": None, "WeeklyTransactions": None}


def _sid(store):
    return str(store).replace("#", "").strip()


def _esc(v):
    return str(v).replace("'", "''")


def _q(sql):
    df, err = execute_query(get_connection(), sql)
    if err:
        return None, err
    return df, None


def _weekly(store, cols):
    keep = list(dict.fromkeys(["FiscalWeekKey"] + cols))
    sql = (f"SELECT {', '.join(keep)} FROM {_WEEKLY} "
           f"WHERE REPLACE(CAST(StoreID AS TEXT), '#', '') = '{_esc(_sid(store))}'")
    df, err = _q(sql)
    if err or df is None or df.empty:
        return None
    df["_d"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
    return df.dropna(subset=["_d"]).sort_values("_d")


def _lt(df, col, n=8):
    """latest value, trailing-N-week average (excluding latest), and the POINT change."""
    if df is None or col not in df:
        return None
    s = df[col].dropna()
    if len(s) < 2:
        return None
    latest = float(s.iloc[-1])
    trailing = float(s.iloc[:-1].tail(n).mean())
    return {"latest": round(latest, 2), "trailing_avg": round(trailing, 2),
            "point_change": round(latest - trailing, 2)}


def _targets():
    df, err = _q(f"SELECT * FROM Ref_Targets WHERE Persona = '{_esc(TARGET_PERSONA)}'")
    if err or df is None or df.empty:
        return {}
    return df.iloc[0].to_dict()


def _ctx(store):
    df = _weekly(store, ["StoreID", "FBC_Name", "Region", "FranchiseOwner", "RegionalVP"])
    if df is None:
        return {}
    r = df.iloc[-1]
    return {"FBC": r.get("FBC_Name"), "Region": r.get("Region"),
            "Owner": r.get("FranchiseOwner"), "RVP": r.get("RegionalVP")}


# ── ANALYSIS DOORS ───────────────────────────────────────────────────────────
def decompose_sss(store):
    """Is a sales move traffic-led (transactions) or ticket-led (avg ticket)?"""
    df = _weekly(store, ["SSS_Pct", "WeeklyTransactions", "AvgTicket"])
    if df is None:
        return {"error": f"no weekly data for store {store}"}
    tx, tk, sss = _lt(df, "WeeklyTransactions"), _lt(df, "AvgTicket"), _lt(df, "SSS_Pct")
    txpct = round(tx["point_change"] / tx["trailing_avg"] * 100, 1) if tx and tx["trailing_avg"] else None
    return {"sss": sss, "transactions": tx, "transactions_pct_change": txpct, "avg_ticket": tk,
            "summary": (f"transactions {tx['latest']} ({txpct:+}% vs trend); "
                        f"ticket ${tk['latest']} ({tk['point_change']:+} pt vs trend)") if tx and tk else "insufficient data"}


def compare_to_peers(store, metric="SSS_Pct"):
    """Store's latest value vs its FBC-cohort average — store-specific or cohort-wide?"""
    if metric not in ALLOWED_METRICS:
        return {"error": f"unknown metric '{metric}'"}
    ctx = _ctx(store)
    fbc = ctx.get("FBC")
    if not fbc:
        return {"error": f"no FBC found for store {store}"}
    sql = (f"SELECT StoreID, FiscalWeekKey, {metric} FROM {_WEEKLY} "
           f"WHERE FBC_Name = '{_esc(fbc)}' AND {metric} IS NOT NULL")
    df, err = _q(sql)
    if err or df is None or df.empty:
        return {"error": "no cohort data"}
    df["_d"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
    latest = df.dropna(subset=["_d"]).sort_values("_d").groupby("StoreID").tail(1)
    vals = latest[metric].astype(float)
    this_id = _sid(store)
    this_row = latest[latest.StoreID.astype(str).str.replace("#", "") == this_id]
    this_val = round(float(this_row[metric].iloc[0]), 2) if len(this_row) else None
    cohort = round(float(vals.mean()), 2)
    rank = int((vals < (this_val if this_val is not None else 0)).sum()) + 1 if this_val is not None else None
    return {"metric": metric, "this_store": this_val, "cohort_avg": cohort,
            "cohort_label": f"FBC {fbc}", "cohort_size": int(latest.StoreID.nunique()),
            "rank_worst_is_1": rank, "cohort_values": sorted(round(float(v), 2) for v in vals),
            "summary": f"{metric}: this store {this_val} vs {fbc} cohort avg {cohort} (n={latest.StoreID.nunique()})"}


def metric_trend(store, metric="SSS_Pct", weeks=12):
    """Recent weekly values for one metric — sudden drop vs chronically low?"""
    if metric not in ALLOWED_METRICS:
        return {"error": f"unknown metric '{metric}'"}
    df = _weekly(store, [metric])
    if df is None:
        return {"error": f"no data for {store}/{metric}"}
    series = [round(float(v), 2) for v in df[metric].dropna().tail(int(weeks))]
    lt = _lt(df, metric)
    below = sum(1 for v in df[metric].dropna().tail(4) if lt and v < lt["trailing_avg"]) if lt else None
    return {"metric": metric, "weeks": len(series), "series_oldest_to_newest": series,
            "latest_vs_trailing": lt, "weeks_below_trend_of_last_4": below,
            "summary": f"{metric} last {len(series)} wks: {series[-6:]}; latest {lt['latest'] if lt else '?'} "
                       f"vs trend {lt['trailing_avg'] if lt else '?'}"}


def osat_breakdown(store):
    """Guest-satisfaction sub-drivers (food quality, service, cleanliness, value)."""
    sql = ("SELECT SurveyWeek, OSAT_TopBox_Pct, FoodQuality_Score, Service_Score, "
           "Cleanliness_Score, ValueScore FROM Fact_OSAT "
           f"WHERE REPLACE(CAST(StoreID AS TEXT), '#', '') = '{_esc(_sid(store))}'")
    df, err = _q(sql)
    if err or df is None or df.empty:
        return {"error": f"no OSAT data for store {store}"}
    df["_d"] = pd.to_datetime(df["SurveyWeek"], errors="coerce")
    df = df.dropna(subset=["_d"]).sort_values("_d")
    last = df.iloc[-1]
    drivers = {"FoodQuality": round(float(last["FoodQuality_Score"]), 2),
               "Service": round(float(last["Service_Score"]), 2),
               "Cleanliness": round(float(last["Cleanliness_Score"]), 2),
               "Value": round(float(last["ValueScore"]), 2)}
    weakest = min(drivers, key=drivers.get)
    osat = _lt(df.rename(columns={"OSAT_TopBox_Pct": "x"}), "x")
    return {"osat_topbox": osat, "drivers_1to5": drivers, "weakest_driver": weakest,
            "summary": f"OSAT top-box {osat['latest'] if osat else '?'} ({osat['point_change'] if osat else '?':+} pt); "
                       f"weakest driver {weakest} ({drivers[weakest]})"}


def channel_mix(store):
    """Online / third-party-delivery / loyalty mix, latest vs recent trend."""
    df = _weekly(store, ["OLO_Pct", "ThreePD_Pct", "Loyalty_Pct"])
    if df is None:
        return {"error": f"no data for store {store}"}
    out = {c: _lt(df, c) for c in ["OLO_Pct", "ThreePD_Pct", "Loyalty_Pct"]}
    return {**out, "summary": "; ".join(f"{c} {v['latest']} ({v['point_change']:+} pt)"
                                        for c, v in out.items() if v)}


def margin_decomp(store):
    """EBITDA margin move vs the sales move — cost-led or sales-led?"""
    df = _weekly(store, ["EBITDA_Pct", "SSS_Pct"])
    if df is None:
        return {"error": f"no data for store {store}"}
    eb, sss = _lt(df, "EBITDA_Pct"), _lt(df, "SSS_Pct")
    return {"ebitda": eb, "sss": sss,
            "summary": (f"EBITDA {eb['latest']} ({eb['point_change']:+} pt); "
                        f"SSS {sss['latest']} ({sss['point_change']:+} pt) — "
                        f"{'both down' if eb and sss and eb['point_change']<0 and sss['point_change']<0 else 'diverging'}")
            if eb and sss else "insufficient data"}


def ops_check(store):
    """Operations score and order accuracy, latest vs recent trend."""
    df = _weekly(store, ["OpEx_Score", "Accuracy_Pct"])
    if df is None:
        return {"error": f"no data for store {store}"}
    out = {c: _lt(df, c) for c in ["OpEx_Score", "Accuracy_Pct"]}
    return {**out, "summary": "; ".join(f"{c} {v['latest']} ({v['point_change']:+} pt)"
                                        for c, v in out.items() if v)}


def fsa_history(store, n=4):
    """Recent food-safety audits and the latest priority finding."""
    sql = ("SELECT AuditDate, FSA_Score, FirstPriorityFinding FROM Fact_FSAScore "
           f"WHERE REPLACE(CAST(StoreID AS TEXT), '#', '') = '{_esc(_sid(store))}'")
    df, err = _q(sql)
    if err or df is None or df.empty:
        return {"error": f"no FSA data for store {store}"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df["_d"] = pd.to_datetime(df["AuditDate"], errors="coerce", dayfirst=True)
    df = df.dropna(subset=["_d"]).sort_values("_d")
    recent = df.tail(int(n))
    scores = [round(float(v), 1) for v in recent["FSA_Score"]]
    last = recent.iloc[-1]
    floor = 80.0
    return {"recent_scores_oldest_to_newest": scores, "latest": scores[-1] if scores else None,
            "below_floor_80": bool(scores and scores[-1] < floor),
            "latest_finding": (None if pd.isna(last["FirstPriorityFinding"]) else str(last["FirstPriorityFinding"])),
            "summary": f"FSA recent {scores}; latest {scores[-1] if scores else '?'} (floor 80)"}


# ── RAW-DATA DOORS ───────────────────────────────────────────────────────────
def get_store_weeks(store, n=12):
    """Hand back the store's actual weekly rows (key columns) — no calculation."""
    cols = ["SSS_Pct", "WeeklyTransactions", "AvgTicket", "EBITDA_Pct", "OSAT_Pct",
            "OLO_Pct", "ThreePD_Pct", "Loyalty_Pct", "OpEx_Score", "Accuracy_Pct"]
    df = _weekly(store, cols)
    if df is None:
        return {"error": f"no data for store {store}"}
    rows = df.tail(int(n))[["FiscalWeekKey"] + cols].to_dict("records")
    return {"store": _sid(store), "columns": ["FiscalWeekKey"] + cols, "rows": rows,
            "summary": f"{len(rows)} weekly rows for store {_sid(store)}"}


def list_columns():
    """What the agent can look at: the metrics available and what they mean."""
    tgt = _targets()
    return {"weekly_metrics": ALLOWED_METRICS,
            "targets_franchisee": {k: tgt.get(v) for k, v in
                                   {"SSS_Pct": "SSSTarget", "OSAT_Pct": "OSATTarget",
                                    "EBITDA_Pct": "EBITDATarget", "FSA_Score": "FSATarget"}.items()},
            "osat_drivers": ["FoodQuality", "Service", "Cleanliness", "Value"],
            "food_safety": "Fact_FSAScore: audit score (floor 80, target 93) + priority finding",
            "summary": "weekly metrics, OSAT sub-drivers, and food-safety audits are available"}


# ── registry ─────────────────────────────────────────────────────────────────
TOOL_FUNCS = {
    "decompose_sss": decompose_sss, "compare_to_peers": compare_to_peers,
    "metric_trend": metric_trend, "osat_breakdown": osat_breakdown,
    "channel_mix": channel_mix, "margin_decomp": margin_decomp, "ops_check": ops_check,
    "fsa_history": fsa_history, "get_store_weeks": get_store_weeks, "list_columns": list_columns,
}


def _schema(name, desc, props, required):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}}


_STORE = {"store": {"type": "string", "description": "Store id, e.g. '8021'"}}
_METRIC = {"metric": {"type": "string", "enum": list(ALLOWED_METRICS),
                      "description": "Metric column key"}}

TOOL_SCHEMAS = [
    _schema("decompose_sss", "Split a sales move into traffic (transactions) vs ticket (avg ticket).", _STORE, ["store"]),
    _schema("compare_to_peers", "Compare the store's latest value for a metric to its FBC-cohort average (store-specific vs cohort-wide).",
            {**_STORE, **_METRIC}, ["store"]),
    _schema("metric_trend", "Return recent weekly values for a metric (sudden drop vs chronically low).",
            {**_STORE, **_METRIC, "weeks": {"type": "integer", "description": "How many recent weeks (default 12)"}}, ["store"]),
    _schema("osat_breakdown", "Guest-satisfaction sub-drivers: food quality, service, cleanliness, value.", _STORE, ["store"]),
    _schema("channel_mix", "Online, third-party-delivery and loyalty mix, latest vs trend.", _STORE, ["store"]),
    _schema("margin_decomp", "EBITDA margin move vs the sales move (cost-led vs sales-led).", _STORE, ["store"]),
    _schema("ops_check", "Operations score and order accuracy, latest vs trend.", _STORE, ["store"]),
    _schema("fsa_history", "Recent food-safety audit scores and the latest priority finding.", _STORE, ["store"]),
    _schema("get_store_weeks", "RAW rows: the store's actual weekly numbers for the last n weeks (no calculation).",
            {**_STORE, "n": {"type": "integer", "description": "How many recent weeks (default 12)"}}, ["store"]),
    _schema("list_columns", "List the metrics/columns available to investigate and what they mean.", {}, []),
]


def run_tool(name, args):
    """Execute a tool by name with a dict of args; never raises."""
    fn = TOOL_FUNCS.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    try:
        return fn(**(args or {}))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
