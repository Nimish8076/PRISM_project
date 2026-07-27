"""
agent.py — Proactive Anomaly Detection & Alerting Agent
========================================================
The "out of the box" layer of the JM Data Platform.

Instead of waiting for a user to ask a question, this agent inspects the
latest data, detects meaningful change (statistically AND against targets),
asks Claude to reason about the cause and recommend a countermeasure, and
produces alert cards routed to the responsible people.

Designed to be TRIGGER-AGNOSTIC:
  - run_analysis() is the single entry point.
  - Today it's called by a "Run Analysis" button in the Streamlit app.
  - Tomorrow the same function is called as the final step of the Gold-layer
    pipeline, or by a scheduler. Nothing else changes.
"""

import os
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import pandas as pd
from dotenv import load_dotenv
from data_connector import get_connection, execute_query
import diagnose
import evidence
import reason as reason_engine       # aliased — 'reason' is also a run_analysis arg
import ground as ground_engine
import agent_loop                     # v2 tool-using investigation loop (used when reasoning.use_tools)
import tools                          # cheap deterministic tool functions (for _quick_driver)
from config import (METRICS, FSA_CONFIG, TARGET_PERSONA, PATTERN_RULES,
                    MAX_ALERTS, NARRATOR_MODEL, GENERIC_WATCH, REASONING, CORRELATION)
import schema_scan

load_dotenv()
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# NARRATOR_MODEL, METRICS, FSA_CONFIG, TARGET_PERSONA, PATTERN_RULES and
# MAX_ALERTS come from config.py — edit prism_config.yaml to tune them.


def _run_sql(sql):
    """
    Compatibility wrapper around execute_query.
    Works whether your data_connector uses execute_query(sql) OR
    execute_query(conn, sql). Always returns (DataFrame, error).
    """
    import inspect
    try:
        params = inspect.signature(execute_query).parameters
        if len(params) >= 2:
            return execute_query(get_connection(), sql)   # execute_query(conn, sql)
        else:
            return execute_query(sql)                      # execute_query(sql)
    except Exception as e:
        return None, str(e)

# METRICS, FSA_CONFIG and TARGET_PERSONA are imported from config.py (above).
# Edit prism_config.yaml to change the watched metrics, thresholds or persona.


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — DETECTION
# ══════════════════════════════════════════════════════════════════════════════
def _get_targets():
    """Load the target row for the configured persona into a dict."""
    df, err = _run_sql(
        f"SELECT * FROM Ref_Targets WHERE Persona = '{TARGET_PERSONA}'"
    )
    if err or df is None or df.empty:
        return {}
    return df.iloc[0].to_dict()


def get_latest_week():
    """Most recent FiscalWeekKey in Fact_StoreWeekly, DD-MM-YYYY, date-aware.

    Used by the UI banner so it reflects the data that actually landed instead
    of a hardcoded week. Returns None if unavailable.
    """
    df, err = _run_sql(
        "SELECT DISTINCT FiscalWeekKey FROM Fact_StoreWeekly WHERE FiscalWeekKey IS NOT NULL"
    )
    if err or df is None or df.empty:
        return None
    df["_d"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
    df = df.dropna(subset=["_d"])
    if df.empty:
        return None
    return str(df.sort_values("_d").iloc[-1]["FiscalWeekKey"])


def _detect_weekly_metric(metric, cfg, targets):
    """
    Detect anomalies for a weekly metric using BOTH methods:
      1. Statistical — latest week deviates sharply from its trailing average
      2. Threshold   — latest week breaches the target from Ref_Targets

    NOTE: This intentionally AVOIDS SQL window functions (ROW_NUMBER/OVER),
    because the bundled SQLite version is older than 3.25 and doesn't support
    them. Instead we pull the raw rows and compute latest-week + trailing
    average in pandas, which works on any SQLite version.
    """
    target_val = targets.get(cfg["target_col"]) if cfg["target_col"] else None

    # Pull every store's weekly values for this metric (simple, portable SQL).
    sql = f"""
    SELECT StoreID, FiscalWeekKey, {metric} AS metric_val
    FROM Fact_StoreWeekly
    WHERE {metric} IS NOT NULL
    """
    df, err = _run_sql(sql)
    if err or df is None or df.empty:
        return []

    # FiscalWeekKey is a DD-MM-YYYY string. Build a real date for correct sorting.
    df["_sortdate"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
    df = df.dropna(subset=["_sortdate"])

    anomalies = []
    trailing_n = cfg["trailing_weeks"]

    # Process each store independently
    for store_id, grp in df.groupby("StoreID"):
        grp = grp.sort_values("_sortdate")          # oldest → newest
        latest_row = grp.iloc[-1]                    # most recent week
        latest = float(latest_row["metric_val"])
        latest_week = latest_row["FiscalWeekKey"]

        # Trailing average = the N weeks BEFORE the latest one
        prior = grp.iloc[:-1].tail(trailing_n)
        trailing = float(prior["metric_val"].mean()) if len(prior) > 0 else None

        flags = []
        severity_score = 0.0
        stat_pct = None      # legacy % change vs recent avg (non-rate metrics only)
        stat_delta = None    # POINT change vs recent avg (rate metrics: latest - trailing)
        target_gap = None    # points below target (set only if threshold fires)

        # ── Method 1: statistical deviation vs the store's own recent average ──
        if trailing is not None:
            if cfg.get("is_rate"):
                # metric is ALREADY a percentage -> compare in POINTS, not a ratio.
                # A ratio of a near-zero % explodes: 0.5% -> 0.1% looks like -80%.
                point_change = latest - trailing
                drop = -point_change                     # positive magnitude of a decline
                if cfg["direction"] == "down_is_bad" and drop >= cfg.get("point_drop_threshold", float("inf")):
                    flags.append("statistical")
                    stat_delta = round(point_change, 2)
                    severity_score = max(severity_score, drop)
            elif trailing != 0:
                pct_change = (latest - trailing) / abs(trailing) * 100.0
                if cfg["direction"] == "down_is_bad" and pct_change <= -cfg.get("pct_drop_threshold", float("inf")):
                    flags.append("statistical")
                    stat_pct = round(pct_change, 1)
                    severity_score = max(severity_score, min(abs(pct_change), 100.0))

        # ── Method 2: threshold vs target (meaningful gap only) ──
        min_gap = cfg.get("min_target_gap", 0)
        if target_val is not None:
            if cfg["direction"] == "down_is_bad" and latest < (target_val - min_gap):
                flags.append("threshold")
                gap = (target_val - latest)
                target_gap = round(gap, 1)
                severity_score = max(severity_score, gap)

        if flags:
            anomalies.append({
                "store_id": str(store_id).replace("#", "").strip(),
                "metric": metric,
                "metric_label": cfg["label"],
                "latest_value": round(latest, 2),
                "trailing_avg": round(trailing, 2) if trailing is not None else None,
                "target_value": round(float(target_val), 2) if target_val is not None else None,
                "fiscal_week": str(latest_week),
                "methods": flags,
                "stat_pct": stat_pct,        # legacy % deviation (non-rate metrics)
                "stat_delta": stat_delta,    # point deviation vs recent average (rate metrics)
                "target_gap": target_gap,    # points below target
                "severity_score": round(float(severity_score), 2),
                "unit": cfg["unit"],
            })
    return anomalies


def _detect_fsa(targets):
    """Detect failed/low food safety audits (event-based, latest audit per store).
    Avoids window functions for older-SQLite compatibility."""
    target_val = targets.get(FSA_CONFIG["target_col"])
    sql = """
    SELECT StoreID, AuditDate, FSA_Score, FirstPriorityFinding
    FROM Fact_FSAScore
    WHERE FSA_Score IS NOT NULL
    """
    df, err = _run_sql(sql)
    if err or df is None or df.empty:
        return []

    # Keep only each store's most recent audit (parse the date for correct sort)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # datasets vary in date format; coerce quietly
        df["_sortdate"] = pd.to_datetime(df["AuditDate"], errors="coerce", dayfirst=True)
    df = df.sort_values("_sortdate").groupby("StoreID", as_index=False).last()

    anomalies = []
    for _, row in df.iterrows():
        score = float(row["FSA_Score"])
        # Only a floor breach is a real food-safety alert. Being merely below the
        # (aspirational) target is NOT flagged unless fsa.flag_below_target is on —
        # otherwise most stores flag as "High" just for sitting under an unmet target.
        flag_below_target = FSA_CONFIG.get("flag_below_target", False)
        below_target = flag_below_target and target_val is not None and score < target_val
        below_floor = score < FSA_CONFIG["critical_floor"]
        if below_target or below_floor:
            methods = []
            target_gap = None
            floor_gap = None
            if below_target:
                methods.append("threshold")
                target_gap = round(target_val - score, 1)
            if below_floor:
                # Distinct method: an absolute safety-floor breach, NOT a trend.
                methods.append("critical_floor")
                floor_gap = round(FSA_CONFIG["critical_floor"] - score, 1)
            anomalies.append({
                "store_id": str(row["StoreID"]).replace("#", "").strip(),
                "metric": "FSA_Score",
                "metric_label": FSA_CONFIG["label"],
                "latest_value": round(score, 2),
                "trailing_avg": None,
                "target_value": round(float(target_val), 2) if target_val is not None else None,
                "fiscal_week": None,
                "audit_date": str(row["AuditDate"]),
                "finding": row.get("FirstPriorityFinding"),
                "methods": methods,
                "target_gap": target_gap,                       # points below target
                "floor_gap": floor_gap,                         # points below safety floor
                "critical_floor": FSA_CONFIG["critical_floor"],  # the floor itself, for display
                "severity_score": round((target_val or FSA_CONFIG["critical_floor"]) - score, 2),
                "unit": "",
            })
    return anomalies


def _detect_generic(cols):
    """Catch-all watch: statistical-only trend on unwatched numeric columns.

    Shallow by design — no target, no tailored cause (diagnosis falls back to the
    generic cause + playbook). Only runs when config.GENERIC_WATCH['enabled'].
    """
    thr = GENERIC_WATCH.get("pct_drop_threshold", 30)
    trailing_n = GENERIC_WATCH.get("trailing_weeks", 8)
    anomalies = []
    for metric in cols:
        df, err = _run_sql(
            f"SELECT StoreID, FiscalWeekKey, {metric} AS metric_val "
            f"FROM Fact_StoreWeekly WHERE {metric} IS NOT NULL"
        )
        if err or df is None or df.empty:
            continue
        df["_sortdate"] = pd.to_datetime(df["FiscalWeekKey"], format="%d-%m-%Y", errors="coerce")
        df = df.dropna(subset=["_sortdate"])
        for store_id, grp in df.groupby("StoreID"):
            grp = grp.sort_values("_sortdate")
            latest = float(grp.iloc[-1]["metric_val"])
            latest_week = grp.iloc[-1]["FiscalWeekKey"]
            prior = grp.iloc[:-1].tail(trailing_n)
            if len(prior) == 0:
                continue
            trailing = float(prior["metric_val"].mean())
            if trailing == 0:
                continue
            pct = (latest - trailing) / abs(trailing) * 100.0
            if pct <= -thr:
                anomalies.append({
                    "store_id": str(store_id).replace("#", "").strip(),
                    "metric": metric,
                    "metric_label": metric,
                    "latest_value": round(latest, 2),
                    "trailing_avg": round(trailing, 2),
                    "target_value": None,
                    "fiscal_week": str(latest_week),
                    "methods": ["generic"],
                    "stat_pct": round(pct, 1),
                    "target_gap": None,
                    "severity_score": round(min(abs(pct), 100.0), 2),
                    "unit": "",
                    "generic": True,
                })
    return anomalies


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — ENRICHMENT (who is responsible + store context)
# ══════════════════════════════════════════════════════════════════════════════
def _enrich_with_owners(anomaly):
    """Attach store name, region, and the full responsible chain to an anomaly."""
    sid = anomaly["store_id"]
    # Both Dim_Store and the normalized anomaly id are compared with '#' stripped
    # from both sides, so it works regardless of prefix format.
    sql = f"""
    SELECT City, Region, FranchiseOwner, AreaDirector
    FROM Dim_Store
    WHERE REPLACE(StoreID, '#', '') = REPLACE('{sid}', '#', '')
    LIMIT 1
    """
    df, err = _run_sql(sql)
    info = {}
    if not err and df is not None and not df.empty:
        info = df.iloc[0].to_dict()

    # FBC and RVP live on the weekly fact rows in this schema
    sql2 = f"""
    SELECT FBC_Name, RegionalVP
    FROM Fact_StoreWeekly
    WHERE REPLACE(CAST(StoreID AS TEXT), '#', '') = REPLACE('{sid}', '#', '')
    ORDER BY FiscalWeekKey DESC LIMIT 1
    """
    df2, err2 = _run_sql(sql2)
    if not err2 and df2 is not None and not df2.empty:
        info.update(df2.iloc[0].to_dict())

    anomaly["city"] = info.get("City")
    anomaly["region"] = info.get("Region")
    anomaly["responsible"] = {
        "Franchise Owner": info.get("FranchiseOwner"),
        "FBC": info.get("FBC_Name"),
        "Area Director": info.get("AreaDirector"),
        "Regional VP": info.get("RegionalVP"),
    }
    return anomaly


# ── ROUTING — who an alert/issue goes to, by tier (deterministic) ───────────
_TIER_KEY = {"FBC": "fbc", "Area Director": "area_director",
             "RVP": "regional_vp", "Regional VP": "regional_vp"}
_ROUTE_ORDER = ["fbc", "area_director", "regional_vp", "franchise_owner"]
_ROUTE_ROLE = {"fbc": "FBC", "area_director": "Area Director",
               "regional_vp": "Regional VP", "franchise_owner": "Owner"}


def _route_recipient(tier, ctx):
    """Resolve WHO an alert/issue goes to: the org person at the suggested tier,
    looked up from the store's context (deterministic — never invented by the model).
    If that tier is unassigned in the data, escalate up the chain to the next
    available person. The AGENT decides the tier; this resolves the name."""
    ctx = ctx or {}
    key = _TIER_KEY.get(tier, "area_director" if tier == "Functional" else "fbc")
    role = "Area Director" if tier == "Functional" else _ROUTE_ROLE.get(key, str(tier))
    if ctx.get(key):
        return f"{ctx[key]} ({role})"
    start = _ROUTE_ORDER.index(key) if key in _ROUTE_ORDER else 0
    for k in _ROUTE_ORDER[start:]:
        if ctx.get(k):
            return f"{ctx[k]} ({_ROUTE_ROLE[k]})"
    return "Store Owner"


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — REASONING (Claude explains cause + recommends countermeasure)
# ══════════════════════════════════════════════════════════════════════════════
def _reason_about(anomaly, narrate=False):
    """Attach severity + cause + action to an alert.

    HYBRID MODEL:
      - Cause, action and severity are decided DETERMINISTICALLY from the store's
        own sub-metrics and a curated playbook (diagnose.py). This is the
        project-specific brain and needs no API call.
      - If narrate=True, Claude is used ONLY to rephrase the already-decided
        cause/action into nicer prose. It never changes the diagnosis, and any
        failure falls back silently to the deterministic text.
    """
    diagnose.diagnose_alert(anomaly)      # sets severity, cause, action, driver
    if narrate:
        _narrate_alert(anomaly)
    return anomaly


def narrate_text(cause, action):
    """Polish a deterministic (cause, action) pair with the narrator LLM and
    return the polished (cause, action). Meaning is preserved; on ANY failure
    the original text is returned unchanged.

    This is the per-card entry point: the UI calls it on demand when a single
    alert is switched from 'Data' to 'AI', so no API call happens unless a user
    explicitly asks for it on that card.
    """
    prompt = (
        "Rewrite the following alert CAUSE and ACTION into two polished, specific "
        "sentences for a Jersey Mike's operations audience. Do NOT change their "
        "meaning and do NOT introduce new facts or numbers. Return EXACTLY:\n"
        "CAUSE: <text>\nACTION: <text>\n\n"
        f"CAUSE: {cause}\nACTION: {action}"
    )
    try:
        resp = client.messages.create(
            model=NARRATOR_MODEL,
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        c, ac = "", ""
        for line in text.splitlines():
            l = line.strip()
            if l.upper().startswith("CAUSE:"):
                c = l.split(":", 1)[1].strip()
            elif l.upper().startswith("ACTION:"):
                ac = l.split(":", 1)[1].strip()
        return (c or cause, ac or action)
    except Exception:
        return (cause, action)


def narrate_alert(alert, siblings=None):
    """Per-card AI narration that ADDS bounded value on top of the data.

    The data still owns the diagnosis — the driver, the numbers and the severity
    are fixed. The model may only: (a) explain the cause in plain language for the
    responsible field role, (b) connect other issues flagged at the SAME store in
    this run into one story, and (c) expand the recommended direction into 2-3
    concrete first steps. It must use only the facts passed in — no invented
    metrics or numbers. Returns (cause, action); falls back to the deterministic
    text on any failure.
    """
    resp = alert.get("responsible") or {}
    audience = ("the Field Business Consultant" if resp.get("FBC")
                else "the franchise owner" if resp.get("Franchise Owner")
                else "the store operations team")
    data_cause = alert.get("cause_data") or alert.get("cause", "")
    data_action = alert.get("action_data") or alert.get("action", "")

    sib_txt = ""
    sibs = [s for s in (siblings or []) if s.get("metric") != alert.get("metric")]
    if sibs:
        lines = "\n".join(
            f"  - {s.get('metric_label')}: {s.get('cause_data') or s.get('cause', '')}"
            for s in sibs)
        sib_txt = ("\nOther issues flagged at THIS SAME store in this run "
                   "(connect them if relevant):\n" + lines)

    prompt = f"""You are writing an internal operations alert for Jersey Mike's Subs.
Use ONLY the facts below. Do NOT invent metrics, numbers, stores or causes that are
not listed, and keep the stated driver and figures exactly as given.

Store: #{alert.get('store_id')} ({alert.get('city', '')}, {alert.get('region', '')})
Metric: {alert.get('metric_label')}
Severity (fixed): {alert.get('severity')}
Data-derived cause: {data_cause}
Recommended direction: {data_action}{sib_txt}
Write for: {audience}.

Return EXACTLY two lines:
CAUSE: 1-2 sentences in plain business language explaining the likely cause for {audience}. If other issues at the same store are listed above, weave them into one connected story.
ACTION: 2-3 concrete first steps as a single line separated by '; ', consistent with the recommended direction and specific enough to act on this week."""
    try:
        resp_msg = client.messages.create(
            model=NARRATOR_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp_msg.content[0].text.strip()
        cause, action = "", ""
        for line in text.splitlines():
            l = line.strip()
            if l.upper().startswith("CAUSE:"):
                cause = l.split(":", 1)[1].strip()
            elif l.upper().startswith("ACTION:"):
                action = l.split(":", 1)[1].strip()
        return (cause or data_cause, action or data_action)
    except Exception:
        return (data_cause, data_action)


def narrate_pattern(pattern):
    """Per-card AI narration for a correlated pattern. The DATA owns the grouping
    (type, stores, metrics); the model only explains why it is systemic for
    leadership and proposes 2-3 coordinated steps. Uses only the facts passed in
    — no invented stores, metrics or numbers. Returns (insight, action); falls
    back to the deterministic text on any failure.
    """
    data_insight = pattern.get("insight_data") or pattern.get("insight", "")
    data_action = pattern.get("action_data") or pattern.get("action", "")
    stores = ", ".join(f"#{s}" for s in pattern.get("stores", [])[:10])
    metrics = ", ".join(pattern.get("metrics", []))
    prompt = f"""You are writing a systemic-issue briefing for Jersey Mike's leadership.
Use ONLY the facts below. Do NOT invent stores, metrics or numbers.

Pattern type: {pattern.get('type')}
Scope: {pattern.get('key')}
Stores involved ({pattern.get('store_count')}): {stores}
Metrics affected: {metrics}
Data-derived insight: {data_insight}
Recommended direction: {data_action}

Return EXACTLY two lines:
INSIGHT: 1-2 sentences on why this cross-store pattern is systemic (not coincidence) and who should own it.
ACTION: 2-3 concrete coordinated steps as a single line separated by '; '."""
    try:
        resp_msg = client.messages.create(
            model=NARRATOR_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        clean = resp_msg.content[0].text.strip().replace("**", "").replace("*", "").replace("#", "")
        insight, action = "", ""
        for line in clean.splitlines():
            l = line.strip()
            if l.upper().startswith("INSIGHT"):
                insight = l.split(":", 1)[1].strip() if ":" in l else l
            elif l.upper().startswith("ACTION"):
                action = l.split(":", 1)[1].strip() if ":" in l else l
        return (insight or data_insight, action or data_action)
    except Exception:
        return (data_insight, data_action)


def _narrate_alert(anomaly):
    """In-place LLM polish (used by the universal narrate=True programmatic path)."""
    anomaly["cause"], anomaly["action"] = narrate_text(
        anomaly.get("cause", ""), anomaly.get("action", ""))
    return anomaly


# ══════════════════════════════════════════════════════════════════════════════
# STORE-LEVEL DIAGNOSIS — gather → reason → ground (ONE card per store)
# ══════════════════════════════════════════════════════════════════════════════
# This is the new hybrid brain. For each store that detection flagged, we build a
# single evidence packet (evidence.py), let the LLM reason across the store's
# metrics into one connected story (reason.py), then validate/harden that against
# the data and fall back to the deterministic diagnose.py when needed (ground.py).
# Detection and the org-level pattern pass are unchanged and still run first.
_DIAG_CACHE = {}


def _store_signature(items):
    """A stable fingerprint of a store's anomalies; the diagnosis is cached on it
    so re-running the same week never re-calls the model, but any change in the
    data (new values / week) invalidates the entry automatically."""
    return frozenset((a.get("metric"), a.get("latest_value"), a.get("fiscal_week"))
                     for a in items)


def _diagnose_store(store_id, items):
    """Sequential single-store path: gather → reason → ground → enrich."""
    packet = evidence.build_store_packet(store_id, items)
    ai, err = (reason_engine.reason_store_ex(packet)
               if REASONING.get("enabled", True) else (None, None))
    return _finish(store_id, items, packet, ai, err)


def _finish(store_id, items, packet, ai, ai_error=None):
    """Ground a (possibly parallel-computed) AI result and attach the display and
    context fields the UI and persistence need."""
    diag = ground_engine.ground(packet, items, ai)

    # Diagnostic: record + surface WHY a store fell back (visible on the card and
    # in the streamlit terminal). ai_error is the exact model/parse failure.
    if diag.get("source") == "fallback":
        _v = diag.setdefault("validation", {})
        if ai_error:
            _v["ai_error"] = ai_error
        why = _v.get("ai_error") or _v.get("reason") or "reasoning disabled / AI returned nothing"
        print(f"[diagnose] store {store_id} -> fallback ({why})", file=sys.stderr)

    diag["context"] = packet.get("context")
    diag["latest_week"] = packet.get("latest_week")
    diag["metric_labels"] = sorted({a.get("metric_label") or a.get("metric") for a in items})
    diag["metrics_keys"] = sorted({a.get("metric") for a in items if a.get("metric")})
    diag["anomalies"] = packet.get("anomalies")        # numbers for the pills / evidence
    diag["peer_context"] = packet.get("peer_context")
    diag["lead_lag"] = packet.get("lead_lag")
    diag["_score"] = max((a.get("severity_score") or 0) for a in items) if items else 0
    return diag


def _finish_agentic(store_id, items, ai, ai_error=None):
    """Finish a v2 AGENTIC diagnosis (store_diagnosis schema). Enforces the
    deterministic severity floor and attaches the display/persistence fields.
    If the agent returned nothing, falls back to the deterministic card so no
    store is ever dropped."""
    packet = evidence.build_store_packet(store_id, items)
    if not ai:
        why = ai_error or "agent returned nothing"
        print(f"[agent] store {store_id} -> fallback ({why})", file=sys.stderr)
        return _finish(store_id, items, packet, None, ai_error)
    diag = ground_engine.ground_agent(ai, items)
    diag["source"] = "agent"
    diag["context"] = packet.get("context")
    diag["latest_week"] = packet.get("latest_week")
    diag["metric_labels"] = sorted({a.get("metric_label") or a.get("metric") for a in items})
    diag["metrics_keys"] = sorted({a.get("metric") for a in items if a.get("metric")})
    diag["_score"] = max((a.get("severity_score") or 0) for a in items) if items else 0
    diag.setdefault("severity_score", diag["_score"])
    return diag


def _diagnose_stores(all_found):
    """Diagnose ONLY the stores we will display (ranked by strongest signal).

    The model calls run in PARALLEL (they are network-bound), while ALL database
    work — building evidence packets and grounding — stays single-threaded, which
    keeps the shared in-memory SQLite connection safe. Results are cached per data
    signature, so re-running an unchanged week costs no model calls at all.
    Diagnosing every flagged store before trimming would pay a call for dozens of
    stores just to discard most — so we cap first.
    """
    grouped = evidence.group_by_store(all_found)
    ranked = sorted(
        grouped.items(),
        key=lambda kv: max((a.get("severity_score") or 0) for a in kv[1]),
        reverse=True,
    )[:REASONING.get("max_store_cards", MAX_ALERTS)]

    use_cache = REASONING.get("cache", True)
    use_ai = REASONING.get("enabled", True)
    use_tools = REASONING.get("use_tools", False)   # v2 agent (agent_loop) vs single narration call
    results = {}      # sid -> diag
    pending = []      # [(sid, items, packet)] needing a model call

    # Phase 1 (sequential, DB): cache hits + build evidence packets
    for sid, items in ranked:
        key = (sid, _store_signature(items), use_ai, use_tools)
        if use_cache and key in _DIAG_CACHE:
            results[sid] = _DIAG_CACHE[key]
            continue
        packet = evidence.build_store_packet(sid, items)
        if use_ai:
            pending.append((sid, items, packet))
        else:
            results[sid] = _finish(sid, items, packet, None)

    # Phase 2 (parallel, network only): the LLM calls
    ai_by_sid = {}
    if pending:
        try:
            (agent_loop._get_client() if use_tools else reason_engine._get_client())  # pre-warm the client
        except Exception:
            pass
        workers = max(1, min(REASONING.get("max_workers", 6), len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            if use_tools:
                futs = {ex.submit(agent_loop.diagnose_store_agentic, sid, it): sid for sid, it, _pk in pending}
            else:
                futs = {ex.submit(reason_engine.reason_store_ex, pk): sid for sid, _it, pk in pending}
            for fut in as_completed(futs):
                sid = futs[fut]
                try:
                    ai_by_sid[sid] = fut.result()          # (diagnosis|None, error|None)
                except Exception as e:
                    ai_by_sid[sid] = (None, f"thread error: {e}")

    # Phase 3 (sequential, DB): ground + enrich + cache
    for sid, items, packet in pending:
        res = ai_by_sid.get(sid)
        if use_tools:
            ai = res[0] if res else None
            err = res[1] if (res and len(res) > 1) else None
            diag = _finish_agentic(sid, items, ai, err)
        else:
            ai, err = res if res else (None, None)
            diag = _finish(sid, items, packet, ai, err)
        results[sid] = diag
        if use_cache:
            _DIAG_CACHE[(sid, _store_signature(items), use_ai, use_tools)] = diag

    out = [results[sid] for sid, _items in ranked if sid in results]
    rank = {"Critical": 0, "High": 1, "Moderate": 2}
    out.sort(key=lambda d: (rank.get(d.get("severity", "Moderate"), 3), -d.get("_score", 0)))
    return out


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT — trigger-agnostic
# ══════════════════════════════════════════════════════════════════════════════
def run_analysis(max_alerts: int = MAX_ALERTS, reason: bool = True, narrate: bool = False):
    """
    The single entry point. Called by the button today, by a pipeline/scheduler
    tomorrow. Returns {"patterns", "stores", "alerts"}: org-level correlation
    patterns, ONE grounded cross-metric diagnosis per flagged store (the primary
    view — gather → reason → ground), and the legacy per-metric alert list kept
    for backward compatibility. All most-severe-first.

    Args:
        max_alerts: cap on number of alerts returned (keeps demo focused)
        reason: if True, attach severity/cause/action to alerts and
                insight/action to patterns (all decided deterministically in
                diagnose.py). Set False for a fast detection-only dry run.
        narrate: if True, additionally use the LLM to polish the wording of the
                 already-decided cause/action/insight. Never changes the
                 diagnosis; falls back to the deterministic text on failure.
    """
    get_connection()  # ensure DB is loaded
    evidence.reset_caches()   # fresh per-run peer baselines
    targets = _get_targets()

    # 1. Detect across all configured metrics (FULL set, before trimming)
    all_found = []
    for metric, cfg in METRICS.items():
        all_found.extend(_detect_weekly_metric(metric, cfg, targets))
    all_found.extend(_detect_fsa(targets))
    if GENERIC_WATCH.get("enabled"):
        all_found.extend(_detect_generic(schema_scan.scan_unwatched_metrics()))

    if not all_found:
        return {"patterns": [], "stores": [], "alerts": [], "flagged_store_ids": []}

    # 2. Enrich EVERY anomaly with owners/region (correlation needs this context)
    all_found = [_enrich_with_owners(a) for a in all_found]

    # 3. CORRELATION PASS — find patterns across the full set (the differentiator)
    patterns = _find_patterns(all_found)
    if reason:
        patterns = [_summarize_pattern(p, narrate=narrate) for p in patterns]

    # 3b. STORE-LEVEL DIAGNOSIS — one grounded, cross-metric card per store
    #     (gather → reason → ground). This is the primary view the UI renders;
    #     the individual alerts below are kept for backward compatibility.
    stores = _diagnose_stores(all_found) if reason else []

    # 3c. CAUSE-AWARE correlation over ALL flagged stores (v2). Every flagged store
    #     gets a cheap deterministic driver so correlation sees the WHOLE fleet (true
    #     cohort sizes + fleet base rates); the agent's richer driver from the top
    #     diagnosed cards overrides where present.
    if reason and REASONING.get("use_tools"):
        corr = []
        for sid, items in evidence.group_by_store(all_found).items():
            resp = (items[0].get("responsible") or {}) if items else {}
            ctx = {"fbc": resp.get("FBC"), "area_director": resp.get("Area Director"),
                   "regional_vp": resp.get("Regional VP"), "franchise_owner": resp.get("Franchise Owner"),
                   "region": (items[0].get("region") if items else None)}
            corr.append({"store_id": str(sid), "driver": _quick_driver(sid, items),
                         "context": ctx, "anomalies": items})
        agent_drv = {str(s.get("store_id")): s.get("driver") for s in stores if s.get("driver")}
        for cs in corr:
            if cs["store_id"] in agent_drv:
                cs["driver"] = agent_drv[cs["store_id"]]
        fleet_patterns = _find_fleet_patterns(corr)      # company-wide (broad) patterns
        cause_patterns = _correlate_by_cause(corr)       # concentrated cohort patterns
        merged = fleet_patterns + cause_patterns + patterns
        if merged:
            patterns = merged[:PATTERN_RULES.get("max_patterns", 6)]

    # 4. Select a balanced subset of INDIVIDUAL alerts for the cards
    from collections import defaultdict
    by_metric = defaultdict(list)
    for a in all_found:
        by_metric[a["metric"]].append(a)

    per_metric_cap = max(2, max_alerts // max(len(by_metric), 1))
    selected = []
    for metric, items in by_metric.items():
        items.sort(key=lambda a: a["severity_score"], reverse=True)
        selected.extend(items[:per_metric_cap])
    selected.sort(key=lambda a: a["severity_score"], reverse=True)
    selected = selected[:max_alerts]

    # 5. Reason about each individual alert (optional)
    if reason:
        selected = [_reason_about(a, narrate=narrate) for a in selected]
        selected = [_compose_alert_message(a) for a in selected]

    sev_rank = {"Critical": 0, "High": 1, "Moderate": 2}
    selected.sort(key=lambda a: (sev_rank.get(a.get("severity", "Moderate"), 3),
                                 -a["severity_score"]))

    # Full flagged set (every store detection flagged, not just the shown cards) —
    # lets persistence auto-resolve stores that have recovered.
    flagged_store_ids = sorted({str(a.get("store_id")) for a in all_found})
    return {"patterns": patterns, "stores": stores, "alerts": selected,
            "flagged_store_ids": flagged_store_ids}


# ══════════════════════════════════════════════════════════════════════════════
# CORRELATION PASS — find patterns no single-metric dashboard can
# ══════════════════════════════════════════════════════════════════════════════
def _find_patterns(anomalies):
    """
    Rule-based grouping of individual anomalies into cross-cutting patterns.
    This is the core differentiator: dashboards flag metrics one at a time,
    per store. This connects anomalies ACROSS stores, metrics, and the org
    hierarchy to surface systemic issues.

    Returns a list of pattern dicts, most significant first.
    """
    from collections import defaultdict
    R = PATTERN_RULES
    W = R["score_weights"]
    patterns = []

    # ── Pattern A: same FBC with multiple struggling stores ──
    by_fbc = defaultdict(list)
    for a in anomalies:
        fbc = (a.get("responsible") or {}).get("FBC")
        if fbc:
            by_fbc[fbc].append(a)
    for fbc, items in by_fbc.items():
        stores = {i["store_id"] for i in items}
        if len(stores) >= R["fbc_min_stores"]:
            patterns.append({
                "type": "FBC",
                "key": fbc,
                "title": f"Multiple stores under FBC {fbc} are underperforming",
                "store_count": len(stores),
                "stores": sorted(stores),
                "metrics": sorted({i["metric_label"] for i in items}),
                "items": items,
                "score": len(stores) * W["fbc_per_store"] + len(items),
            })

    # ── Pattern B: same Area Director with multiple struggling stores ──
    by_ad = defaultdict(list)
    for a in anomalies:
        ad = (a.get("responsible") or {}).get("Area Director")
        if ad:
            by_ad[ad].append(a)
    for ad, items in by_ad.items():
        stores = {i["store_id"] for i in items}
        if len(stores) >= R["area_director_min_stores"]:
            patterns.append({
                "type": "Area Director",
                "key": ad,
                "title": f"Area Director {ad}'s territory shows a cluster of issues",
                "store_count": len(stores),
                "stores": sorted(stores),
                "metrics": sorted({i["metric_label"] for i in items}),
                "items": items,
                "score": len(stores) * W["area_director_per_store"] + len(items),
            })

    # ── Pattern C: same region with a cluster of one metric ──
    by_region_metric = defaultdict(list)
    for a in anomalies:
        region = a.get("region")
        if region:
            by_region_metric[(region, a["metric_label"])].append(a)
    for (region, metric_label), items in by_region_metric.items():
        stores = {i["store_id"] for i in items}
        if len(stores) >= R["region_min_stores"]:
            patterns.append({
                "type": "Region",
                "key": f"{region} — {metric_label}",
                "title": f"{metric_label} is declining across multiple {region} stores",
                "store_count": len(stores),
                "stores": sorted(stores),
                "metrics": [metric_label],
                "items": items,
                "score": len(stores) * W["region_per_store"],
            })

    # ── Pattern D: same store failing MULTIPLE metrics at once ──
    by_store = defaultdict(list)
    for a in anomalies:
        by_store[a["store_id"]].append(a)
    for store_id, items in by_store.items():
        metrics = {i["metric_label"] for i in items}
        if len(metrics) >= R["multi_metric_min_metrics"]:
            sample = items[0]
            patterns.append({
                "type": "Multi-Metric Store",
                "key": store_id,
                "title": f"Store #{store_id} is failing on {len(metrics)} metrics simultaneously",
                "store_count": 1,
                "stores": [store_id],
                "metrics": sorted(metrics),
                "items": items,
                "region": sample.get("region"),
                "responsible": sample.get("responsible"),
                "score": len(metrics) * W["multi_metric_per_metric"],
            })

    # Most significant patterns first; cap from config
    patterns.sort(key=lambda p: p["score"], reverse=True)
    return patterns[:R["max_patterns"]]


# ── CAUSE-AWARE correlation (v2): cluster diagnosed stores by shared driver ──
_DRIVER_LABELS = {
    "traffic": "traffic (fewer customers)",
    "ticket_value": "ticket / value (smaller orders)",
    "guest_experience": "guest experience",
    "food_safety": "food safety",
    "cost_margin": "cost / margin",
    "operations": "operations / accuracy",
    "mixed": "mixed drivers",
}
_DRIVER_ACTIONS = {
    "traffic": "coordinate local-marketing and lapsed-loyalty win-back across the group",
    "ticket_value": "review promo mix, attachment/upsell and value messaging across the group",
    "guest_experience": "run a guest-experience recovery plan across the group (service/cleanliness)",
    "food_safety": "cohort-wide food-safety review and re-audits — escalate immediately",
    "cost_margin": "review food cost, waste and labor scheduling across the group",
    "operations": "re-train on operational execution and order accuracy across the group",
    "mixed": "FBC-led operational review across the group",
}


def _quick_driver(store_id, items):
    """Cheap, DETERMINISTIC driver bucket for a flagged store — fixed rules over the
    flagged metrics + one quick sub-metric check. No model call, so it can tag EVERY
    flagged store (correlation breadth). The agent's richer driver overrides this on
    the diagnosed cards (depth). Its only job is bucketing for cluster-counting."""
    metrics = {a.get("metric") for a in (items or []) if a.get("metric")}
    if "FSA_Score" in metrics:
        return "food_safety"
    if "SSS_Pct" in metrics:
        try:
            d = tools.decompose_sss(store_id)
            txp = d.get("transactions_pct_change")
        except Exception:
            txp = None
        if isinstance(txp, (int, float)) and txp <= -3:   # transactions down >= 3% -> traffic-led
            return "traffic"
        return "ticket_value"
    if "OSAT_Pct" in metrics:
        return "guest_experience"
    if "EBITDA_Pct" in metrics:
        return "cost_margin"
    return "operations"


def _correlate_by_cause(stores):
    """Cluster DIAGNOSED stores that share the same `driver` under one FBC — but
    only call it SYSTEMIC when the cluster is concrete. A cohort qualifies (strict)
    only if it clears all three bars from config.correlation:
      • count >= min_stores               (enough stores, not just 2)
      • share >= min_share                (a big fraction of that FBC's diagnosed stores)
      • lift  >= min_lift                 (the driver is far more common here than fleet-wide)
    If nothing clears the bar and correlation.fallback is on, fall back to the
    looser 'any N stores share a driver' rule, marked 'Possible'. Emits pattern
    dicts in the same shape as _find_patterns so the existing card renders them."""
    from collections import defaultdict, Counter
    cfg = CORRELATION
    driven = [s for s in stores if s.get("driver") and (s.get("context") or {}).get("fbc")]
    if not driven:
        return []
    total = len(driven)
    fleet = Counter(s["driver"] for s in driven)                 # fleet mix (diagnosed set)
    fbc_total = Counter(s["context"]["fbc"] for s in driven)
    groups = defaultdict(list)
    for s in driven:
        groups[(s["context"]["fbc"], s["driver"])].append(s)

    def _pattern(fbc, drv, members, share, lift, confidence, tentative):
        ids = sorted({str(m["store_id"]) for m in members})
        label = _DRIVER_LABELS.get(drv, drv)
        title = f"{len(ids)} stores under FBC {fbc} share one cause: {label}"
        if tentative:
            title = "Possible — " + title
        why = f"{len(ids)} of {fbc}'s diagnosed stores ({share*100:.0f}%) share the SAME driver ({label})"
        if lift is not None and lift != float("inf"):
            why += f" — {lift:.1f}x more common than across the flagged fleet."
        elif lift == float("inf"):
            why += " — a driver seen only in this cohort."
        else:
            why += " — below the strict bar, shown as a tentative signal."
        return {
            "type": "Shared Cause", "key": f"{fbc}|{drv}", "title": title,
            "store_count": len(ids), "stores": ids, "metrics": [label],
            "items": [a for m in members for a in (m.get("anomalies") or [])],
            "insight": why,
            "action": (f"Handle at the FBC / Area-Director level: "
                       f"{_DRIVER_ACTIONS.get(drv, 'coordinated review across the group')}."),
            "route_tier": "Area Director",
            "route_to": _route_recipient("Area Director", members[0].get("context") or {}),
            "confidence": confidence,
            "score": (100 + (lift if lift not in (None, float("inf")) else 5) * 10 + len(ids))
                     if not tentative else (40 + len(ids)),
        }

    strict = []
    for (fbc, drv), members in groups.items():
        n = len({str(m["store_id"]) for m in members})
        share = n / max(fbc_total[fbc], 1)
        fleet_share = (fleet[drv] / total) if total else 0
        lift = (share / fleet_share) if fleet_share else float("inf")
        if (n >= cfg.get("min_stores", 3) and share >= cfg.get("min_share", 0.4)
                and lift >= cfg.get("min_lift", 1.8)):
            strict.append(_pattern(fbc, drv, members, share, lift, "High", False))
    if strict:
        strict.sort(key=lambda p: p["score"], reverse=True)
        return strict

    if cfg.get("fallback", True):
        fmin = cfg.get("fallback_min_stores", 2)
        loose = []
        for (fbc, drv), members in groups.items():
            n = len({str(m["store_id"]) for m in members})
            if n >= fmin:
                share = n / max(fbc_total[fbc], 1)
                loose.append(_pattern(fbc, drv, members, share, None, "Low", True))
        loose.sort(key=lambda p: p["score"], reverse=True)
        return loose
    return []


def _find_fleet_patterns(corr):
    """COMPANY-wide patterns: a driver that is BROAD (high prevalence spread across
    many FBCs and regions) rather than concentrated under one manager. Routed to Ops
    leadership, not an Area Director. Same pattern shape as _find_patterns so the card
    renders it. Complements _correlate_by_cause (which catches the concentrated ones)."""
    from collections import defaultdict
    cfg = CORRELATION
    driven = [s for s in corr if s.get("driver")]
    total = len(driven)
    if total == 0:
        return []
    by_driver = defaultdict(list)
    for s in driven:
        by_driver[s["driver"]].append(s)

    patterns = []
    for drv, members in by_driver.items():
        ids = {str(m["store_id"]) for m in members}
        n = len(ids)
        share = n / total
        regions = {(m.get("context") or {}).get("region") for m in members if (m.get("context") or {}).get("region")}
        fbcs = {(m.get("context") or {}).get("fbc") for m in members if (m.get("context") or {}).get("fbc")}
        if (share >= cfg.get("fleet_min_share", 0.35)
                and len(regions) >= cfg.get("fleet_min_regions", 3)
                and len(fbcs) >= cfg.get("fleet_min_fbcs", 4)):
            label = _DRIVER_LABELS.get(drv, drv)
            cap = label[:1].upper() + label[1:]
            patterns.append({
                "type": "Fleet-wide", "key": f"fleet|{drv}",
                "title": f"{cap} is elevated fleet-wide — {n} stores ({round(share*100)}%) across {len(regions)} regions",
                "store_count": n, "stores": sorted(ids), "metrics": [label],
                "items": [a for m in members for a in (m.get("anomalies") or [])],
                "insight": (f"{label} is broad, not concentrated — {n} stores ({round(share*100)}% of flagged) across "
                            f"{len(regions)} regions and {len(fbcs)} FBCs. A company-level signal, not one manager's fault. "
                            f"Check whether it's a real fleet decline or the target is set too high."),
                "action": (f"Escalate to Ops leadership: {_DRIVER_ACTIONS.get(drv, 'company-wide review')}. "
                           f"Confirm real decline vs target calibration before cascading to the field."),
                "route_tier": "Company", "route_to": "Ops leadership / VP",
                "confidence": "High", "scope": "fleet", "score": 300 + n,   # company issues rank first
            })
    patterns.sort(key=lambda p: p["score"], reverse=True)
    return patterns


def _summarize_pattern(pattern, narrate=False):
    """Attach insight + action to a correlated pattern.

    Deterministic by default (diagnose.diagnose_pattern). If narrate=True, the
    LLM only rephrases the computed insight/action; it never decides them.
    """
    diagnose.diagnose_pattern(pattern)    # sets insight, action
    if narrate:
        _narrate_pattern(pattern)
    return pattern


def _narrate_pattern(pattern):
    """Optional LLM polish of the computed pattern insight/action."""
    prompt = (
        "Rewrite the following pattern INSIGHT and ACTION into two polished, specific "
        "sentences for a Jersey Mike's operations audience. Do NOT change their meaning "
        "and do NOT introduce new facts. Return EXACTLY:\n"
        "INSIGHT: <text>\nACTION: <text>\n\n"
        f"INSIGHT: {pattern.get('insight','')}\nACTION: {pattern.get('action','')}"
    )
    try:
        resp = client.messages.create(
            model=NARRATOR_MODEL,
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        clean = resp.content[0].text.strip().replace("**", "").replace("*", "").replace("#", "")
        insight, action = "", ""
        for line in clean.splitlines():
            l = line.strip()
            if l.upper().startswith("INSIGHT"):
                insight = l.split(":", 1)[1].strip() if ":" in l else l
            elif l.upper().startswith("ACTION"):
                action = l.split(":", 1)[1].strip() if ":" in l else l
        if insight:
            pattern["insight"] = insight
        if action:
            pattern["action"] = action
    except Exception:
        pass    # keep the deterministic wording
    return pattern


# ══════════════════════════════════════════════════════════════════════════════
# FRAMING C — compose the actual alert message that WOULD be sent
# (We display this; we do not transmit it. Honest + shows full output.)
# ══════════════════════════════════════════════════════════════════════════════
def _compose_alert_message(anomaly):
    """Build the email-style notification text for an individual store alert."""
    resp = anomaly.get("responsible") or {}
    owner = resp.get("Franchise Owner") or "Store Owner"
    rvp = resp.get("Regional VP") or ""
    to_line = owner + (f", {rvp}" if rvp else "")

    subject = (f"[{anomaly.get('severity','Alert')}] Store #{anomaly['store_id']} — "
               f"{anomaly['metric_label']} needs attention")

    body = (
        f"To: {to_line}\n"
        f"Subject: {subject}\n\n"
        f"An automated check flagged Store #{anomaly['store_id']} "
        f"({anomaly.get('city','')}, {anomaly.get('region','')}).\n\n"
        f"Metric: {anomaly['metric_label']}\n"
        f"Current value: {anomaly['latest_value']}{anomaly['unit']}"
    )
    if anomaly.get("target_value") is not None:
        body += f"  (target: {anomaly['target_value']}{anomaly['unit']})"
    body += (
        f"\n\nLikely cause: {anomaly.get('cause','')}\n"
        f"Recommended action: {anomaly.get('action','')}\n\n"
        f"— Jersey Mike's Proactive Insight Agent"
    )
    anomaly["alert_message"] = body
    return anomaly


# Quick manual test: `python agent.py`
if __name__ == "__main__":
    result = run_analysis(max_alerts=12, reason=False)  # fast dry run, no API
    patterns = result["patterns"]
    alerts = result["alerts"]

    print(f"\n🧩 PATTERNS DETECTED: {len(patterns)}\n")
    for p in patterns:
        print(f"  [{p['type']}] {p['title']}")
        print(f"      stores: {', '.join('#'+s for s in p['stores'])} | metrics: {', '.join(p['metrics'])}")

    print(f"\n📋 INDIVIDUAL ALERTS: {len(alerts)}\n")
    for a in alerts:
        print(f"  Store #{a['store_id']} | {a['metric_label']} = "
              f"{a['latest_value']}{a['unit']} | methods: {','.join(a['methods'])} "
              f"| severity: {a['severity_score']}")