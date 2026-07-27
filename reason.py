"""reason.py — the REASON step (v1): the LLM turns one store's evidence packet
into a single, structured, cross-metric diagnosis.

v1 = ONE call, whole packet in, structured JSON out. No tools yet (that is v2).

Boundaries (this is deliberate):
  - The model does NOT decide whether something is an anomaly — detection already
    did that. It only explains the flagged metrics as one connected story.
  - Its output is NOT trusted as-is — ground.py validates every number and can
    override severity before anything is shown.
  - On ANY failure (no key, bad model id, network, bad JSON) reason_store returns
    None, and the caller falls back to the deterministic diagnose.py.

The client is created lazily so importing this module never fails when no API
key is present (e.g. offline dry runs).
"""
import os
import sys
import json
import anthropic
from dotenv import load_dotenv
from config import REASONING_MODEL

load_dotenv()

_client = None
LAST_ERROR = None   # populated on failure so a fallback can be diagnosed


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


SYSTEM = (
    "You are a retail operations analyst for Jersey Mike's Subs. You diagnose ONE "
    "store at a time from the evidence provided. Detection has ALREADY decided which "
    "metrics are anomalous; your job is to explain them as ONE connected story: which "
    "flagged metric is the root driver and which are downstream, using the supporting "
    "sub-metrics, the peer comparison, and the lead/lag hints in the evidence.\n\n"
    "Hard rules:\n"
    "- Use ONLY facts present in the evidence. Do NOT invent metrics, numbers or stores.\n"
    "- Keep every number exactly as it appears in the evidence.\n"
    "- Name metrics using the exact metric keys from the evidence (e.g. SSS_Pct).\n"
    "- If the evidence does not support linking two metrics, say so rather than guessing.\n"
    "- Return ONLY valid JSON — no markdown fences, no prose outside the JSON."
)

SCHEMA_HINT = """Return JSON with EXACTLY this shape:
{
  "headline": "one sentence a store owner would understand",
  "severity": "Critical | High | Moderate",
  "root_cause": {"metric": "<metric key from evidence>", "explanation": "why this is the root"},
  "causal_chain": [
    {"metric": "<metric key>", "role": "root | contributing | symptom", "note": "how it connects to the root"}
  ],
  "actions": ["2-4 concrete first steps for the responsible field role"],
  "confidence": 0.0,
  "cited_values": [{"label": "what this number is", "value": 0.0}]
}
In cited_values, list EVERY number you mention anywhere above, so each can be
verified against the source data."""

# Forced tool schema — the model returns its result as a structured object the
# SDK parses for us, so long narrative strings can never break JSON formatting.
TOOL_NAME = "store_diagnosis"
_TOOL = {
    "name": TOOL_NAME,
    "description": "Return the structured cross-metric diagnosis for one store.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "one sentence a store owner understands"},
            "severity": {"type": "string", "enum": ["Critical", "High", "Moderate"]},
            "root_cause": {
                "type": "object",
                "properties": {"metric": {"type": "string"}, "explanation": {"type": "string"}},
                "required": ["metric", "explanation"],
            },
            "causal_chain": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "role": {"type": "string", "enum": ["root", "contributing", "symptom"]},
                        "note": {"type": "string"},
                    },
                    "required": ["metric", "role", "note"],
                },
            },
            "actions": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "cited_values": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"label": {"type": "string"}, "value": {"type": "number"}}},
            },
        },
        "required": ["headline", "severity", "root_cause", "causal_chain", "actions"],
    },
}


def _extract_tool_input(resp):
    """Return the dict a forced tool call produced (already valid), or None."""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    return None


def _first_text(resp):
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return (getattr(block, "text", "") or "").strip()
    return ""


def _reason_once(packet):
    """Call the model for one store. Returns (diagnosis|None, error|None) using no
    shared state, so it is safe to run in parallel threads. Prints the cause of any
    fallback to the console (visible in the `streamlit run` terminal)."""
    raw = ""
    try:
        prompt = ("Diagnose this ONE store from the evidence below, then return the "
                  "result by calling the store_diagnosis tool. Use ONLY facts in the "
                  "evidence, keep every number exactly as given, and name metrics using "
                  "their exact keys.\n\nEVIDENCE:\n" + json.dumps(packet, default=str))
        resp = _get_client().messages.create(
            model=REASONING_MODEL,
            max_tokens=2000,
            temperature=0.0,
            system=SYSTEM,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_tool_input(resp)     # valid structured object from the tool call
        if data is None:                     # no tool block -> tolerate a text/JSON reply
            raw = _first_text(resp)
            data = _parse_json(raw)
        data["store_id"] = packet.get("store_id")
        return data, None
    except Exception as e:
        err = f"{type(e).__name__}: {e}" + (f"  |  raw≈ {raw[:160]}" if raw else "")
        print(f"[reason] store {packet.get('store_id')} -> fallback: {err}", file=sys.stderr)
        return None, err


def reason_store_ex(packet):
    """Thread-safe variant used for parallel runs: returns (diagnosis|None, error|None)."""
    return _reason_once(packet)


def reason_store(packet):
    """Return the structured diagnosis dict for one store, or None on any failure.
    Back-compat wrapper; also records the cause in reason.LAST_ERROR."""
    global LAST_ERROR
    data, err = _reason_once(packet)
    LAST_ERROR = err
    return data


def _strip_fences(t):
    """Remove a leading ```json / ``` fence if the model added one."""
    if t.startswith("```"):
        lines = t.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _parse_json(text):
    """Parse a JSON object from the model output, tolerating code fences or any
    prose the model wrapped around it (extract the outermost {...} block)."""
    t = _strip_fences(text)
    try:
        return json.loads(t)
    except Exception:
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j > i:
            return json.loads(t[i:j + 1])
        raise


# ── manual test: `python reason.py` (needs a valid key + model id) ────────────
if __name__ == "__main__":
    from data_connector import get_connection
    import evidence
    import agent
    from config import METRICS

    get_connection()
    targets = agent._get_targets()
    found = []
    for m, cfg in METRICS.items():
        found.extend(agent._detect_weekly_metric(m, cfg, targets))
    found.extend(agent._detect_fsa(targets))
    grouped = evidence.group_by_store(found)
    pick = next((s for s, i in grouped.items() if len({x['metric'] for x in i}) >= 2),
                None) or next(iter(grouped))
    packet = evidence.build_store_packet(pick, grouped[pick])

    print(f"Calling model '{REASONING_MODEL}' for store #{pick}...\n")
    d = reason_store(packet)
    if d:
        print(json.dumps(d, indent=2, default=str))
    else:
        print("reason_store returned None (no/invalid key or model id). "
              "ground.py will fall back to diagnose.py — run `python ground.py`.")
