"""alert_store.py — lightweight persistence for PRISM alerts.

Saves each analysis run's alerts to a local SQLite FILE (not the throwaway
in-memory analysis DB) so the app can show, across sessions:
  - history of what was flagged
  - recurrence ("times seen") for the same store+metric
  - open / resolved status
  - thumbs-up/down usefulness feedback (also a value metric to show the client)
"""
import os
import json
import sqlite3
import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "prism_history.db")


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                alert_key           TEXT PRIMARY KEY,
                store_id            TEXT,
                metric              TEXT,
                metric_label        TEXT,
                region              TEXT,
                city                TEXT,
                last_severity       TEXT,
                last_severity_score REAL,
                last_value          REAL,
                target_value        REAL,
                last_fiscal_week    TEXT,
                last_cause          TEXT,
                last_action         TEXT,
                last_methods        TEXT,
                first_seen          TEXT,
                last_seen           TEXT,
                occurrences         INTEGER DEFAULT 1,
                status              TEXT DEFAULT 'open',
                feedback            TEXT,
                feedback_at         TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id     TEXT PRIMARY KEY,
                run_at     TEXT,
                n_alerts   INTEGER,
                n_patterns INTEGER
            )
        """)
        # Store-level insights (ONE row per store) — the new one-card-per-store
        # output. This table is the local system of record; the CSV mirror and,
        # later, the Fabric Gold table are rebuilt from it.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS insights (
                store_id         TEXT PRIMARY KEY,
                run_id           TEXT,
                week             TEXT,
                severity         TEXT,
                source           TEXT,
                headline         TEXT,
                root_metric      TEXT,
                root_explanation TEXT,
                metrics          TEXT,
                causal_chain     TEXT,
                actions          TEXT,
                confidence       REAL,
                city             TEXT,
                region           TEXT,
                franchise_owner  TEXT,
                fbc              TEXT,
                area_director    TEXT,
                regional_vp      TEXT,
                severity_score   REAL,
                first_seen       TEXT,
                last_seen        TEXT,
                occurrences      INTEGER DEFAULT 1,
                status           TEXT DEFAULT 'open',
                feedback         TEXT,
                feedback_at      TEXT
            )
        """)
        # Human write-back overlay — kept SEPARATE from the PRISM-generated
        # `insights` table so re-runs never clobber a person's input, and so a
        # Power App / Teams flow (or Streamlit) can write here while PRISM only
        # ever writes `insights`. The dashboard joins the two on store_id.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS insight_actions (
                store_id     TEXT PRIMARY KEY,
                ack_status   TEXT,
                ack_by       TEXT,
                ack_at       TEXT,
                feedback     TEXT,
                feedback_at  TEXT,
                note         TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _alert_key(a):
    """Natural key for an alert = one issue at one store on one metric."""
    return f"{a.get('store_id')}|{a.get('metric')}"


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def save_run(result):
    """Persist one run_analysis() result.

    New alerts are inserted; a recurring alert (same store+metric already
    tracked) bumps its occurrence count, refreshes its latest values, and is
    re-opened. Returns the run_id.
    """
    init_db()
    now = _now()
    run_id = "run-" + now.replace(":", "").replace("-", "").replace("T", "-")
    alerts = result.get("alerts", []) or []
    patterns = result.get("patterns", []) or []

    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, run_at, n_alerts, n_patterns) "
            "VALUES (?,?,?,?)",
            (run_id, now, len(alerts), len(patterns)),
        )
        for a in alerts:
            key = _alert_key(a)
            methods = ",".join(a.get("methods", []) or [])
            existing = conn.execute(
                "SELECT alert_key FROM alerts WHERE alert_key = ?", (key,)
            ).fetchone()
            if existing:
                conn.execute("""
                    UPDATE alerts SET
                        last_severity=?, last_severity_score=?, last_value=?,
                        target_value=?, last_fiscal_week=?, last_cause=?,
                        last_action=?, last_methods=?, region=?, city=?,
                        last_seen=?, occurrences=occurrences+1, status='open'
                    WHERE alert_key=?
                """, (
                    a.get("severity"), a.get("severity_score"), a.get("latest_value"),
                    a.get("target_value"), a.get("fiscal_week"), a.get("cause"),
                    a.get("action"), methods, a.get("region"), a.get("city"),
                    now, key,
                ))
            else:
                conn.execute("""
                    INSERT INTO alerts (
                        alert_key, store_id, metric, metric_label, region, city,
                        last_severity, last_severity_score, last_value, target_value,
                        last_fiscal_week, last_cause, last_action, last_methods,
                        first_seen, last_seen, occurrences, status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'open')
                """, (
                    key, a.get("store_id"), a.get("metric"), a.get("metric_label"),
                    a.get("region"), a.get("city"), a.get("severity"),
                    a.get("severity_score"), a.get("latest_value"), a.get("target_value"),
                    a.get("fiscal_week"), a.get("cause"), a.get("action"), methods,
                    now, now,
                ))
        conn.commit()
    finally:
        conn.close()
    return run_id


def get_alerts(status=None):
    """Return tracked alerts (optionally filtered by status), most-recurring first."""
    init_db()
    conn = _connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE status=? "
                "ORDER BY occurrences DESC, last_seen DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY occurrences DESC, last_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_feedback(alert_key, value):
    """value: 'useful' or 'not_useful'."""
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE alerts SET feedback=?, feedback_at=? WHERE alert_key=?",
            (value, _now(), alert_key),
        )
        conn.commit()
    finally:
        conn.close()


def set_status(alert_key, status):
    """status: 'open' or 'resolved'."""
    init_db()
    conn = _connect()
    try:
        conn.execute("UPDATE alerts SET status=? WHERE alert_key=?", (status, alert_key))
        conn.commit()
    finally:
        conn.close()


def feedback_stats():
    """Return (total_with_feedback, marked_useful) across all tracked alerts."""
    init_db()
    conn = _connect()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE feedback IS NOT NULL"
        ).fetchone()[0]
        useful = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE feedback='useful'"
        ).fetchone()[0]
        return total, useful
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# STORE-LEVEL INSIGHTS — the one-card-per-store output (upsert + recurrence)
# ══════════════════════════════════════════════════════════════════════════════
_INSIGHT_COLS = (
    "run_id", "week", "severity", "source", "headline", "root_metric",
    "root_explanation", "metrics", "causal_chain", "actions", "confidence",
    "city", "region", "franchise_owner", "fbc", "area_director", "regional_vp",
    "severity_score",
)


def _insight_row(s, run_id):
    """Flatten a store-level diagnosis (from agent.run_analysis) into DB columns."""
    ctx = s.get("context") or {}
    rc = s.get("root_cause") or {}
    return {
        "store_id": str(s.get("store_id")),
        "run_id": run_id,
        "week": s.get("latest_week"),
        "severity": s.get("severity"),
        "source": s.get("source"),
        "headline": s.get("headline"),
        "root_metric": rc.get("metric"),
        "root_explanation": rc.get("explanation"),
        "metrics": ", ".join(s.get("metric_labels") or []),
        "causal_chain": json.dumps(s.get("causal_chain") or [], default=str),
        "actions": json.dumps(s.get("actions") or [], default=str),
        "confidence": s.get("confidence"),
        "city": ctx.get("city"),
        "region": ctx.get("region"),
        "franchise_owner": ctx.get("franchise_owner"),
        "fbc": ctx.get("fbc"),
        "area_director": ctx.get("area_director"),
        "regional_vp": ctx.get("regional_vp"),
        "severity_score": s.get("_score"),
    }


def save_insights(result, run_id=None):
    """Upsert the run's store-level insights (one row per store) and AUTO-RESOLVE
    recovered stores.

    - A new store is inserted; a still-flagged store bumps 'occurrences', refreshes
      its values, and is (re)opened.
    - Any tracked store that is currently 'open' but NO LONGER in this run's full
      flagged set (result["flagged_store_ids"]) is set to 'resolved' — i.e. its
      metrics recovered, so it closes itself with no human action needed.

    `status` here is the MACHINE view (open = still flagged, resolved = recovered).
    Human acknowledgement/feedback lives in the separate insight_actions table.
    Returns the run_id."""
    init_db()
    now = _now()
    run_id = run_id or ("run-" + now.replace(":", "").replace("-", "").replace("T", "-"))
    conn = _connect()
    try:
        for s in result.get("stores", []) or []:
            r = _insight_row(s, run_id)
            sid = r["store_id"]
            exists = conn.execute(
                "SELECT store_id FROM insights WHERE store_id=?", (sid,)).fetchone()
            if exists:
                sets = ", ".join(f"{c}=?" for c in _INSIGHT_COLS)
                conn.execute(
                    f"UPDATE insights SET {sets}, last_seen=?, "
                    f"occurrences=occurrences+1, status='open' WHERE store_id=?",
                    tuple(r[c] for c in _INSIGHT_COLS) + (now, sid),
                )
            else:
                cols = ("store_id",) + _INSIGHT_COLS + ("first_seen", "last_seen")
                ph = ",".join("?" for _ in cols)
                conn.execute(
                    f"INSERT INTO insights ({','.join(cols)}, occurrences, status) "
                    f"VALUES ({ph},1,'open')",
                    (sid,) + tuple(r[c] for c in _INSIGHT_COLS) + (now, now),
                )

        # Auto-resolve stores that recovered (open, but not flagged this run).
        flagged = result.get("flagged_store_ids")
        if flagged is not None:
            flagged_set = {str(x) for x in flagged}
            open_ids = [row[0] for row in conn.execute(
                "SELECT store_id FROM insights WHERE status='open'").fetchall()]
            for sid in open_ids:
                if sid not in flagged_set:
                    conn.execute("UPDATE insights SET status='resolved', last_seen=? "
                                 "WHERE store_id=?", (now, sid))
        conn.commit()
    finally:
        conn.close()
    return run_id


_INSIGHT_SELECT = (
    "store_id", "run_id", "week", "severity", "source", "headline", "root_metric",
    "root_explanation", "metrics", "causal_chain", "actions", "confidence", "city",
    "region", "franchise_owner", "fbc", "area_director", "regional_vp",
    "severity_score", "first_seen", "last_seen", "occurrences", "status",
)


def get_insights(status=None):
    """Return tracked store insights joined with the human write-back overlay
    (ack_status / feedback), most severe first. Optionally filter by machine status.
    This joined shape is exactly what the CSV mirror and the dashboard consume."""
    init_db()
    conn = _connect()
    sel = ", ".join("i." + c for c in _INSIGHT_SELECT)
    q = (f"SELECT {sel}, a.ack_status AS ack_status, a.ack_by AS ack_by, "
         f"a.feedback AS feedback, a.feedback_at AS feedback_at, a.note AS ack_note "
         f"FROM insights i LEFT JOIN insight_actions a ON i.store_id = a.store_id")
    order = (" ORDER BY CASE i.severity WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 "
             "WHEN 'Moderate' THEN 2 ELSE 3 END, i.severity_score DESC, i.last_seen DESC")
    try:
        if status:
            rows = conn.execute(q + " WHERE i.status=?" + order, (status,)).fetchall()
        else:
            rows = conn.execute(q + order).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_insight_status(store_id, status):
    """status: 'open' or 'resolved'."""
    init_db()
    conn = _connect()
    try:
        conn.execute("UPDATE insights SET status=? WHERE store_id=?",
                     (status, str(store_id)))
        conn.commit()
    finally:
        conn.close()


def export_insights(csv_path=None, history_path=None, status="open"):
    """Rebuild the dashboard CSV mirror from the current insights table.
    Imports data_connector lazily to avoid any import-time coupling."""
    import data_connector
    rows = get_insights(status=status)
    return data_connector.write_insights(rows, csv_path=csv_path, history_path=history_path)


def persist_insights(result, run_id=None):
    """One call for the UI/pipeline: upsert store insights into SQLite and refresh
    the CSV mirror, honouring config (insights_export). Returns the run_id."""
    from config import INSIGHTS_EXPORT as EXP
    rid = save_insights(result, run_id=run_id) if EXP.get("sqlite", True) else run_id
    if EXP.get("csv", True):
        export_insights(
            csv_path=EXP.get("csv_path"),
            history_path=EXP.get("history_csv_path") if EXP.get("history_csv") else None,
        )
    return rid


def _upsert_action(conn, store_id, **fields):
    """Insert-or-update one store's row in the human write-back table."""
    sid = str(store_id)
    exists = conn.execute(
        "SELECT store_id FROM insight_actions WHERE store_id=?", (sid,)).fetchone()
    if exists:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE insight_actions SET {sets} WHERE store_id=?",
                     tuple(fields.values()) + (sid,))
    else:
        cols = ["store_id"] + list(fields)
        ph = ",".join("?" for _ in cols)
        conn.execute(f"INSERT INTO insight_actions ({','.join(cols)}) VALUES ({ph})",
                     (sid,) + tuple(fields.values()))


def record_insight_feedback(store_id, value):
    """value: 'useful' or 'not_useful' — usefulness feedback on a store's insight
    card. Written to the human write-back table (insight_actions), NOT to the
    PRISM-owned insights table, so re-runs never overwrite it. This is exactly the
    write a Power App / Teams button would perform in the dashboard world."""
    init_db()
    conn = _connect()
    try:
        _upsert_action(conn, store_id, feedback=value, feedback_at=_now())
        conn.commit()
    finally:
        conn.close()


def set_insight_action(store_id, ack_status=None, ack_by=None, note=None):
    """Record a human acknowledgement on a store (e.g. 'acknowledged' / 'working').
    Written to insight_actions — the same table a Power App / Teams flow targets."""
    init_db()
    conn = _connect()
    try:
        fields = {"ack_at": _now()}
        if ack_status is not None:
            fields["ack_status"] = ack_status
        if ack_by is not None:
            fields["ack_by"] = ack_by
        if note is not None:
            fields["note"] = note
        _upsert_action(conn, store_id, **fields)
        conn.commit()
    finally:
        conn.close()


def insight_feedback_stats():
    """Return (total_with_feedback, marked_useful) from the write-back table."""
    init_db()
    conn = _connect()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM insight_actions WHERE feedback IS NOT NULL").fetchone()[0]
        useful = conn.execute(
            "SELECT COUNT(*) FROM insight_actions WHERE feedback='useful'").fetchone()[0]
        return total, useful
    finally:
        conn.close()
