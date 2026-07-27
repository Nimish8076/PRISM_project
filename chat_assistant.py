"""chat_assistant.py — the "Ask PRISM" chatbot logic.

Two answer modes, chosen by a lightweight keyword router:

  • DATA mode  — natural-language question over the warehouse
                 (sql_generator -> execute_query -> answer_generator), with
                 conversation memory so follow-ups like "which is second highest"
                 resolve against the previous turns.
  • PRISM mode — questions about the ALERTS / INSIGHTS themselves ("why is store
                 8019 flagged?", "which stores are Critical?", "what did PRISM
                 recommend for the Northeast?"), answered from the persisted
                 insights table (alert_store.get_insights) with the LLM phrasing it.

Both return a uniform dict: {"mode", "answer", "sql"?, "df"?, "error"?}.
"""
import os
import json
import anthropic
import pandas as pd
from dotenv import load_dotenv
from config import CHAT_MODEL
from sql_generator import generate_sql
from answer_generator import generate_answer
from data_connector import execute_query
import alert_store

load_dotenv()

_client = None


def _client_():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


# Starter questions — a mix of raw-data and PRISM-alert questions.
SUGGESTED_PROMPTS = [
    "Which stores have the highest OSAT score?",
    "Top 10 stores by weekly sales",
    "Which stores did PRISM flag as Critical?",
    "Why is store 8019 flagged?",
    "Bottom 5 stores by food safety score",
    "What did PRISM recommend for the Northeast region?",
]

_PRISM_KEYWORDS = (
    "prism", "flag", "flagged", "alert", "critical", "severity", "recommend",
    "recommendation", "action", "why is store", "why store", "insight", "anomaly",
    "anomalies", "at risk", "needs attention", "resolved", "root cause", "diagnos",
)


def is_prism_question(q: str) -> bool:
    """Route to PRISM mode when the question is about the alerts/insights."""
    ql = (q or "").lower()
    return any(k in ql for k in _PRISM_KEYWORDS)


def chartable(df):
    """Return (label_col, value_col) if the result is a simple one-label-per-row
    by one-number table worth charting; else None."""
    if df is None or getattr(df, "empty", True) or df.shape[1] < 2 or len(df) > 25:
        return None
    label_col = next((c for c in df.columns if df[c].dtype == object), None)
    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if label_col and num_cols and df[label_col].nunique() == len(df):
        return label_col, num_cols[0]
    return None


def _history_pairs(messages, n=3):
    """Turn the chat message list into recent (question, sql) pairs for
    generate_sql, so follow-up questions have context."""
    pairs, last_user = [], None
    for m in messages:
        if m.get("role") == "user":
            last_user = m.get("content")
        elif m.get("role") == "assistant" and last_user is not None:
            pairs.append((last_user, m.get("sql") or m.get("content", "")))
            last_user = None
    return pairs[-n:]


def answer_data(question, messages, conn):
    """DATA mode: NL -> SQL -> answer, with follow-up memory."""
    history = _history_pairs(messages)
    sql, err = generate_sql(question, conversation_history=history)
    if err or not sql:
        return {"mode": "data", "answer": "Sorry — I couldn't turn that into a query. Try rephrasing.", "error": err}
    df, qerr = execute_query(conn, sql)
    if qerr:
        return {"mode": "data", "answer": "I built a query but it didn't run. Try rephrasing.", "sql": sql, "error": qerr}
    if df is None or df.empty:
        return {"mode": "data", "answer": "That ran but matched no records. Try a different angle.", "sql": sql}
    return {"mode": "data", "answer": generate_answer(question, sql, df), "sql": sql, "df": df}


def answer_prism(question, messages):
    """PRISM mode: answer from the persisted insights (alerts), LLM-phrased."""
    try:
        insights = alert_store.get_insights()
    except Exception:
        insights = []
    if not insights:
        return {"mode": "prism",
                "answer": "PRISM hasn't recorded any alerts yet — run the agent "
                          "(Simulate Pipeline Run) first, then ask me again."}

    compact = [{
        "store": r.get("store_id"), "severity": r.get("severity"),
        "status": r.get("status"), "metrics": r.get("metrics"),
        "root_metric": r.get("root_metric"), "why": r.get("root_explanation"),
        "actions": r.get("actions"), "region": r.get("region"),
        "franchise_owner": r.get("franchise_owner"), "fbc": r.get("fbc"),
        "regional_vp": r.get("regional_vp"), "times_seen": r.get("occurrences"),
    } for r in insights]

    convo = "".join(f"{m.get('role')}: {m.get('content', '')}\n" for m in messages[-4:])
    prompt = (
        "You are PRISM's assistant for Jersey Mike's operations. Answer the user's "
        "question using ONLY the PRISM alert data below — these are the stores PRISM "
        "has flagged, with severity, the diagnosed cause, and recommended actions. Be "
        "specific and concise (3-6 sentences or a short list). If the answer isn't in "
        "the data, say so plainly.\n\n"
        f"Recent conversation (for follow-ups):\n{convo}\n"
        f"PRISM ALERTS (JSON):\n{json.dumps(compact, default=str)}\n\n"
        f"Question: {question}"
    )
    try:
        resp = _client_().messages.create(
            model=CHAT_MODEL, max_tokens=600,
            messages=[{"role": "user", "content": prompt}])
        return {"mode": "prism", "answer": resp.content[0].text.strip()}
    except Exception as e:
        return {"mode": "prism", "answer": f"I couldn't reach the model to answer that ({e})."}


def ask(question, messages, conn):
    """Route to PRISM or DATA mode and return a uniform result dict."""
    if is_prism_question(question):
        return answer_prism(question, messages)
    return answer_data(question, messages, conn)
