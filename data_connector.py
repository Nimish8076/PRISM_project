import pandas as pd
import sqlite3
import os
import re
import sys

# Path to your data folder
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Module-level singleton — the in-memory DB is built ONCE and reused across all
# queries. Previously get_connection() rebuilt the whole database (all 8 CSVs)
# on every call, which meant ~56 full reloads per analysis run.
_connection = None


def get_connection():
    """
    Load all CSVs into an in-memory SQLite database once and return the shared
    connection. Subsequent calls reuse the same connection instead of rebuilding.
    """
    global _connection
    if _connection is not None:
        return _connection

    conn = sqlite3.connect(":memory:", check_same_thread=False)

    csv_tables = {
        "Dim_Store": "Dim_Store.csv",
        "Dim_FranchiseOwner": "Dim_FranchiseOwner.csv",
        "Dim_Region": "Dim_Region.csv",
        "Dim_Date": "Dim_Date.csv",
        "Fact_StoreWeekly": "Fact_StoreWeekly.csv",
        "Fact_OSAT": "Fact_OSAT.csv",
        "Fact_FSAScore": "Fact_FSAScore.csv",
        "Ref_Targets": "Ref_Targets.csv",
    }

    for table_name, filename in csv_tables.items():
        filepath = os.path.join(DATA_DIR, filename)
        try:
            df = pd.read_csv(filepath)
            df.to_sql(table_name, conn, if_exists="replace", index=False)
            print(f"Loaded {table_name} ({len(df)} rows)")
        except Exception as e:
            print(f"Failed to load {table_name}: {e}")

    _connection = conn
    return _connection


# ── Read-only guard ────────────────────────────────────────────────────────
# Only single-statement SELECT/WITH queries may execute. This blocks
# INSERT/UPDATE/DELETE/DROP/ALTER/etc. and stacked statements
# ("SELECT ...; DROP ..."). It matters most once execute_query points at live
# Fabric instead of a throwaway in-memory DB, where a bad generated query could
# mutate production data.
def _is_read_only(sql: str) -> bool:
    if not sql:
        return False
    # Remove /* block */ and -- line comments before inspecting.
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    s = re.sub(r"--[^\n]*", " ", s)
    s = s.strip().rstrip(";").strip()
    if not s or ";" in s:            # empty, or more than one statement
        return False
    head = s.lstrip("(").lower()     # allow a leading parenthesis
    return head.startswith("select") or head.startswith("with")


def execute_query(conn, sql: str):
    """
    Execute a read-only SQL query and return (dataframe, error_message).
    Non-SELECT statements are refused.
    """
    if not _is_read_only(sql):
        return None, "Query rejected: only read-only SELECT statements are permitted."
    try:
        df = pd.read_sql_query(sql, conn)
        return df, None
    except Exception as e:
        return None, str(e)


# ── Insights write boundary ──────────────────────────────────────────────────
# The write side of the data boundary. Today it writes a flat CSV the dashboard
# (Power BI) imports directly, rebuilt from the current insights each run. In
# production this is the single place repointed to upsert/MERGE into the Fabric
# Gold "insights" table — nothing downstream changes. This is a separate, explicit
# write path (NOT generated SQL), so the read-only guard above is untouched.
def _resolve(path, default_name):
    if not path:
        path = default_name
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    return path


def _safe_to_csv(df, path):
    """Write a CSV without ever raising. If the file is locked (e.g. open in
    Excel on Windows -> PermissionError), write a '<name>_new.csv' fallback and
    tell the user to close the file. Returns the path actually written, or None."""
    try:
        df.to_csv(path, index=False)
        return path
    except (PermissionError, OSError) as e:
        alt = path[:-4] + "_new.csv" if path.lower().endswith(".csv") else path + "_new.csv"
        try:
            df.to_csv(alt, index=False)
            print(f"[write_insights] '{os.path.basename(path)}' is locked ({e.__class__.__name__}); "
                  f"wrote '{os.path.basename(alt)}' instead. Close the file in Excel to update the main one.",
                  file=sys.stderr)
            return alt
        except Exception as e2:
            print(f"[write_insights] could not write the insights CSV ({e2}). "
                  f"Close '{os.path.basename(path)}' in Excel and re-run.", file=sys.stderr)
            return None


def write_insights(rows, csv_path=None, history_path=None):
    """Write the current store-level insights to CSV. `rows` is a list of dicts
    (typically alert_store.get_insights(status='open')). Overwrites the snapshot
    each run so it always reflects current state. Never raises — a locked file is
    handled gracefully. If history_path is given, also appends to a de-duplicated
    audit trail. Returns the snapshot CSV path (or None if it couldn't be written)."""
    csv_path = _resolve(csv_path, "insights_latest.csv")
    df = pd.DataFrame(rows if rows else [])
    written = _safe_to_csv(df, csv_path)

    if history_path and not df.empty:
        history_path = _resolve(history_path, "insights_history.csv")
        try:
            if os.path.exists(history_path):
                prev = pd.read_csv(history_path)
                combined = pd.concat([prev, df], ignore_index=True)
                subset = [c for c in ("store_id", "week", "run_id") if c in combined.columns]
                if subset:
                    combined = combined.drop_duplicates(subset=subset, keep="last")
                _safe_to_csv(combined, history_path)
            else:
                _safe_to_csv(df, history_path)
        except Exception:
            _safe_to_csv(df, history_path)
    return written
