"""diagnose.py — deterministic, data-grounded cause + recommended action.

This is the project-specific "brain" that replaces the LLM's role in DECIDING
what caused an anomaly and what to do about it. For each alert it inspects the
supporting sub-metrics already in the warehouse to identify the proximate
DRIVER, then maps that driver to a client-approved recommended action via a
PLAYBOOK. Severity is also computed here, deterministically.

The LLM (when enabled per-card in the UI) only rephrases these already-decided
facts — it never chooses the cause, action or severity. Everything here is
reproducible and needs no API call.

NOTE: the PLAYBOOK text below is a starting point. In production it should be
curated with Jersey Mike's operations team so the recommended actions match
their real SOPs.
"""
import pandas as pd
from data_connector import get_connection, execute_query
from config import PLAYBOOK, SEVERITY, DIAGNOSIS, METRICS


def _q(sql):
    """Run a read-only query, return a DataFrame or None."""
    df, err = execute_query(get_connection(), sql)
    if err or df is None or df.empty:
        return None
    return df


def _sid_pred(store_id):
    """SQL predicate that matches a store id regardless of '#' prefix."""
    return (f"REPLACE(CAST(StoreID AS TEXT), '#', '') = "
            f"REPLACE('{store_id}', '#', '')")


# PLAYBOOK (diagnosed driver -> recommended action) is imported from config.py /
# prism_config.yaml so ops can edit the wording without touching code.


# ── Deterministic severity ──────────────────────────────────────────────────
def _severity(a):
    methods = a.get("methods", [])
    metric = a.get("metric")
    tgap = a.get("target_gap")     # points below target (set only if threshold fired)

    if metric == "FSA_Score":
        # Below-target no longer flags (see agent._detect_fsa); any FSA alert is a
        # floor breach -> Critical. "High" kept only if a below-target alert is
        # explicitly re-enabled via fsa.flag_below_target.
        return "Critical" if "critical_floor" in methods else "High"

    cfg = METRICS.get(metric, {})
    if cfg.get("is_rate"):
        # Rate metrics are graded in POINTS, with per-metric bands (calibrated).
        drop = -a["stat_delta"] if a.get("stat_delta") is not None else None  # positive = decline
        crit_pt, high_pt = cfg.get("critical_point_drop"), cfg.get("high_point_drop")
        crit_gap, high_gap = cfg.get("critical_target_gap"), cfg.get("high_target_gap")
        if (drop is not None and crit_pt is not None and drop >= crit_pt) \
                or (tgap is not None and crit_gap is not None and tgap >= crit_gap):
            return "Critical"
        if (drop is not None and high_pt is not None and drop >= high_pt) \
                or (tgap is not None and high_gap is not None and tgap >= high_gap) \
                or ("statistical" in methods and "threshold" in methods):
            return "High"
        return "Moderate"

    # ── legacy % ratio path (non-rate metrics / generic catch-all watch) ──
    stat = a.get("stat_pct")       # negative % vs recent avg
    if stat is not None and stat <= -SEVERITY["critical_pct_drop"]:
        return "Critical"
    if (("statistical" in methods and "threshold" in methods)
            or (stat is not None and stat <= -SEVERITY["high_pct_drop"])
            or (tgap is not None and tgap >= SEVERITY["high_target_gap"])):
        return "High"
    return "Moderate"


# ── Per-metric driver detection ─────────────────────────────────────────────
def _diagnose_sss(a):
    df = _q(f"""
        SELECT FiscalWeekKey, WeeklyTransactions, AvgTicket
        FROM Fact_StoreWeekly
        WHERE {_sid_pred(a['store_id'])}
          AND WeeklyTransactions IS NOT NULL AND AvgTicket IS NOT NULL
    """)
    if df is None or len(df) < 2:
        return None, "sss_both"
    df["_d"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
    df = df.dropna(subset=["_d"]).sort_values("_d")
    if len(df) < 2:
        return None, "sss_both"
    latest = df.iloc[-1]
    prior = df.iloc[:-1].tail(8)
    tx_base = prior["WeeklyTransactions"].mean()
    tk_base = prior["AvgTicket"].mean()
    tx_pct = (latest["WeeklyTransactions"] - tx_base) / tx_base * 100 if tx_base else 0.0
    tk_pct = (latest["AvgTicket"] - tk_base) / tk_base * 100 if tk_base else 0.0
    cut = DIAGNOSIS["sss_traffic_ticket_pct"]
    tx_down, tk_down = tx_pct <= -cut, tk_pct <= -cut
    if tx_down and not tk_down:
        return (f"Sales decline is traffic-led: weekly transactions are {tx_pct:.0f}% "
                f"vs the recent average while average ticket held ({tk_pct:+.0f}%).",
                "sss_traffic")
    if tk_down and not tx_down:
        return (f"Sales decline is ticket-led: average ticket is {tk_pct:.0f}% vs the "
                f"recent average while transactions held ({tx_pct:+.0f}%).",
                "sss_ticket")
    if tx_down and tk_down:
        return (f"Both traffic and ticket are soft: transactions {tx_pct:+.0f}% and average "
                f"ticket {tk_pct:+.0f}% vs the recent average.", "sss_both")
    return (f"Same-store sales are below target while recent traffic and ticket held steady "
            f"(transactions {tx_pct:+.0f}%, ticket {tk_pct:+.0f}%) — likely a year-over-year "
            f"comp or trade-area issue, not a recent operational drop.", "sss_comp")


def _diagnose_osat(a):
    df = _q(f"""
        SELECT SurveyWeek, FoodQuality_Score, Service_Score, Cleanliness_Score, ValueScore
        FROM Fact_OSAT
        WHERE {_sid_pred(a['store_id'])}
    """)
    if df is None:
        return None, "generic"
    df = df.copy()
    df["_d"] = pd.to_datetime(df["SurveyWeek"], format="%d-%m-%Y", errors="coerce")
    if df["_d"].notna().any():
        df = df.sort_values("_d")
    row = df.iloc[-1]
    subs = {
        "FoodQuality": row.get("FoodQuality_Score"),
        "Service": row.get("Service_Score"),
        "Cleanliness": row.get("Cleanliness_Score"),
        "Value": row.get("ValueScore"),
    }
    subs = {k: float(v) for k, v in subs.items() if pd.notna(v)}
    if not subs:
        return None, "generic"
    driver = min(subs, key=subs.get)
    return (f"Guest satisfaction is being dragged down by {driver} "
            f"(lowest sub-score at {subs[driver]:g}).", f"osat_{driver}")


def _diagnose_fsa(a):
    finding = a.get("finding")
    bad = (finding is not None
           and str(finding).strip().lower() not in ("", "none", "false", "0", "nan"))
    if bad:
        return (f"Food-safety audit flagged a priority finding: {finding}. "
                f"Latest score {a['latest_value']:g} vs target "
                f"{a.get('target_value')}.", "fsa_finding")
    floor = a.get("critical_floor", 80)
    return (f"Food-safety audit score {a['latest_value']:g} is below the safety floor "
            f"of {floor:g}.", "fsa_generic")


def _diagnose_ebitda(a):
    df = _q(f"""
        SELECT FiscalWeekKey, SSS_Pct FROM Fact_StoreWeekly
        WHERE {_sid_pred(a['store_id'])} AND SSS_Pct IS NOT NULL
    """)
    if df is None or len(df) < 2:
        return None, "ebitda_cost"
    df["_d"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
    df = df.dropna(subset=["_d"]).sort_values("_d")
    latest = float(df.iloc[-1]["SSS_Pct"])
    base = df.iloc[:-1].tail(8)["SSS_Pct"].mean()
    if base is not None and latest < base:
        return (f"Margin pressure tracks a sales decline (same-store sales {latest:g}% vs "
                f"recent {base:.1f}%), so it is likely sales-driven rather than pure cost.",
                "ebitda_sales")
    return ("EBITDA is down without a matching sales decline, pointing to a cost or labor "
            "issue rather than demand.", "ebitda_cost")


_DISPATCH = {
    "SSS_Pct": _diagnose_sss,
    "OSAT_Pct": _diagnose_osat,
    "FSA_Score": _diagnose_fsa,
    "EBITDA_Pct": _diagnose_ebitda,
}


def _generic_cause(a):
    how = "below target" if "threshold" in a.get("methods", []) else "down sharply vs its recent average"
    return f"{a.get('metric_label', a.get('metric'))} is {how} at store #{a.get('store_id')}."


def diagnose_alert(a):
    """Set a['cause'], a['action'], a['severity'] and a['driver'] deterministically."""
    fn = _DISPATCH.get(a.get("metric"))
    cause, key = (None, "generic")
    if fn is not None:
        try:
            cause, key = fn(a)
        except Exception:
            cause, key = None, "generic"
    a["driver"] = key
    a["cause"] = cause or _generic_cause(a)
    a["action"] = PLAYBOOK.get(key, PLAYBOOK["generic"])
    a["severity"] = _severity(a)
    return a


def diagnose_pattern(p):
    """Set p['insight'] and p['action'] deterministically from the pattern shape."""
    t = p.get("type")
    key = p.get("key")
    metrics = ", ".join(p.get("metrics", []))
    n = p.get("store_count", len(p.get("stores", [])))
    n_metrics = len(p.get("metrics", []))
    if t == "FBC":
        p["insight"] = (f"{n} different stores under FBC {key} are flagged together on "
                        f"{metrics}. A cluster under one consultant points to a coaching "
                        f"or coverage gap, not {n} unrelated store problems.")
        p["action"] = ("Review this FBC's portfolio and coaching plan; have the Area "
                       "Director ride along and standardise the fix across the stores.")
    elif t == "Area Director":
        p["insight"] = (f"{n} stores across {key}'s territory are down on {metrics} at "
                        f"once — a territory-level cluster rather than isolated stores.")
        p["action"] = ("Escalate to the Area Director for a territory review; look for a "
                       "shared operational, staffing or supply root cause.")
    elif t == "Region":
        p["insight"] = (f"{n} stores in the same region are down on {metrics} together, "
                        f"which suggests a market-level factor rather than coincidence.")
        p["action"] = ("Engage the Regional VP; investigate market conditions (local "
                       "competition, supply, demand) common to these stores.")
    elif t == "Multi-Metric Store":
        p["insight"] = (f"Store #{key} is failing {n_metrics} metrics simultaneously "
                        f"({metrics}) — a whole-store operational issue, not a blip.")
        p["action"] = ("Prioritise an on-site FBC visit for a holistic operational reset "
                       "rather than fixing each metric separately.")
    else:
        p["insight"] = p.get("insight") or "Multiple related anomalies suggest a systemic cause."
        p["action"] = p.get("action") or "Investigate the shared factor across these stores."
    return p
