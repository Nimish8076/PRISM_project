"""agent_loop.py — the PRISM v2 investigation loop.

Turns the single narration call into an AGENT: the model decides what to check,
calls a tool (tools.py), reads the measurement, decides the next question, and
repeats until it can explain the store — then returns a structured store_diagnosis.

Boundaries (deliberate, same spirit as reason.py):
  • Detection still decides WHAT is flagged; the agent only explains WHY + what to do.
  • The agent may only use numbers returned by tools (read-only).
  • On any failure it returns (None, error, trace) and the caller falls back to
    the deterministic diagnose.py.

The Anthropic client is created lazily and can be INJECTED (client=...) so the
loop can be unit-tested with a mock model — no API key needed.
"""
import os
import json
import tools as T
from config import REASONING_MODEL, REASONING

# ── system prompt (persona + rules + loop + stop rule) ───────────────────────
SYSTEM = (
    "You are PRISM, a seasoned field operations analyst for Jersey Mike's. You investigate "
    "one store (or cohort) at a time. Detection has already decided this store is off — your "
    "job is to work out WHY and WHAT TO DO, by calling tools, not by guessing.\n\n"
    "How you work:\n"
    "- One question at a time. Form a hypothesis, call the single most relevant tool, read the "
    "result, then choose the next question from what you just learned.\n"
    "- Prefer an analysis tool (it returns a measurement). Use a raw-data tool (get_store_weeks, "
    "list_columns) only when no analysis tool fits.\n"
    "- Use ONLY numbers a tool returned. Never state a number no tool gave you. Report rates and "
    "percentages as POINT changes ('0.8 points below its average'), never as a ratio of a percentage.\n"
    "- Never assert a cause, or rule out an alternative, that you did not test with a tool. If you "
    "suspect a factor you cannot test (new competitor, weather, a regional price change), put it in "
    "also_check ending '(not confirmed in data)' — never in root_cause.\n"
    "- Decide scope from peers: peers fine -> store_specific; the whole cohort moved -> cohort_wide/market_wide.\n"
    "- Set driver: one tag for the primary driver (traffic, ticket_value, guest_experience, food_safety, "
    "cost_margin, operations, mixed) — this is used to cluster stores that share the same cause.\n"
    "- Recommend exactly ONE primary action. Be honest about confidence; list what you couldn't test in unresolved.\n"
    "- Multiple problems: if the store has more than one INDEPENDENT problem (not one root with downstream "
    "effects), make the most urgent the primary diagnosis and put the rest in secondary_issues, each with its "
    "own cause and action. Do NOT blend unrelated problems into one causal_chain. Food safety is almost always "
    "its own issue — never fold an FSA floor breach into a sales story. If two causes are equally likely and "
    "you cannot separate them, present them as candidates (with confidence) rather than forcing one.\n\n"
    "When to stop: return the diagnosis once you have (a) a root cause backed by evidence, (b) scope "
    "settled via peers, (c) the obvious alternatives for this metric ruled out, and (d) one action. Also "
    "stop if another tool call wouldn't change the conclusion. 'Can't rule this out - no data' is a valid stop.\n\n"
    "Voice: write for a store operator - plain, short, calm. headline <= 16 words; root_cause <= 2 sentences; "
    "recommended_action = one imperative sentence. Sentence case, no drama (a 0.8-point dip is '0.8 points', not '89%').\n\n"
    "Return your result by calling the store_diagnosis tool. Every number in it must also appear in cited_values."
)

# ── the final 'answer' tool (mirrors Documents/PRISM_v2_Agent_Contract.md) ────
STORE_DIAGNOSIS = {
    "name": "store_diagnosis",
    "description": "Return the finished diagnosis for this store after investigating. Call this when done.",
    "input_schema": {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["Critical", "High", "Moderate"]},
            "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
            "scope": {"type": "string", "enum": ["store_specific", "cohort_wide", "market_wide"]},
            "driver": {"type": "string",
                       "enum": ["traffic", "ticket_value", "guest_experience", "food_safety",
                                "cost_margin", "operations", "mixed"],
                       "description": "ONE structured tag for the primary driver, used to cluster stores that share the same cause."},
            "headline": {"type": "string"},
            "primary_metric": {"type": "object", "properties": {
                "key": {"type": "string"}, "label": {"type": "string"},
                "value": {"type": "number"}, "unit": {"type": "string"},
                "target": {"type": ["number", "null"]}}, "required": ["key", "label", "value", "unit"]},
            "trend": {"type": "array", "items": {"type": "number"}},
            "peer": {"type": ["object", "null"], "properties": {
                "this_value": {"type": "number"}, "cohort_avg": {"type": "number"},
                "cohort_label": {"type": "string"}, "note": {"type": "string"}}},
            "root_cause": {"type": "string"},
            "causal_chain": {"type": "array", "items": {"type": "object", "properties": {
                "metric": {"type": "string"}, "role": {"type": "string", "enum": ["root", "contributing", "symptom"]},
                "note": {"type": "string"}}, "required": ["metric", "role", "note"]}},
            "recommended_action": {"type": "string"},
            "action_context": {"type": "string"},
            "also_check": {"type": "array", "items": {"type": "string"}},
            "suggested_route": {"type": "object", "properties": {
                "tier": {"type": "string", "enum": ["FBC", "Area Director", "RVP", "Functional"]},
                "reason": {"type": "string"}}, "required": ["tier", "reason"]},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "tool": {"type": "string"}, "input": {"type": "string"}, "result": {"type": "string"}},
                "required": ["tool", "result"]}},
            "cited_values": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string"}, "value": {"type": "number"}}}},
            "unresolved": {"type": "array", "items": {"type": "string"}},
            "secondary_issues": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "severity": {"type": "string", "enum": ["Critical", "High", "Moderate"]},
                    "headline": {"type": "string"},
                    "cause": {"type": "string"},
                    "recommended_action": {"type": "string"},
                    "suggested_route": {"type": "object", "properties": {
                        "tier": {"type": "string", "enum": ["FBC", "Area Director", "RVP", "Functional"]},
                        "reason": {"type": "string"}}}},
                    "required": ["headline", "cause", "recommended_action"]},
                "description": ("Additional INDEPENDENT problems in the same store — ones NOT causally "
                                "linked to the primary root_cause. Empty when there is a single root cause. "
                                "Food safety should almost always appear here as its own issue, never blended "
                                "into the primary narrative."),
            },
        },
        "required": ["severity", "confidence", "scope", "driver", "headline", "primary_metric",
                     "root_cause", "recommended_action", "suggested_route", "evidence", "cited_values"],
    },
}

ALL_SCHEMAS = T.TOOL_SCHEMAS + [STORE_DIAGNOSIS]

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


def _dump(blocks):
    """Rebuild model output as API-valid content dicts for the message history."""
    out = []
    for b in blocks:
        bt = getattr(b, "type", None)
        if bt == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
        elif bt == "text":
            out.append({"type": "text", "text": getattr(b, "text", "") or ""})
    return out or [{"type": "text", "text": "(thinking)"}]


def _initial_prompt(store_id, anomalies, ctx):
    lines = [f"Investigate flagged store #{store_id} ({ctx.get('Region', '')}). Detection flagged:"]
    for a in (anomalies or []):
        lines.append(f"- {a.get('metric_label', a.get('metric'))}: latest {a.get('latest_value')} "
                     f"(target {a.get('target_value')})")
    lines.append("Investigate with your tools, then call store_diagnosis. What do you check first?")
    return "\n".join(lines)


def diagnose_store_agentic(store_id, anomalies, client=None, model=None, max_tool_calls=None):
    """Run the investigation loop. Returns (diagnosis_dict | None, error | None, trace).

    The model investigates freely for up to `budget` tool calls, then we make ONE
    guaranteed forced call that requires store_diagnosis — so the loop always
    concludes even if the model keeps calling tools (or batches several per turn).
    """
    client = client or _get_client()
    model = model or REASONING_MODEL
    budget = max_tool_calls or REASONING.get("max_tool_calls", 8)
    ctx = T._ctx(store_id)
    messages = [{"role": "user", "content": _initial_prompt(store_id, anomalies, ctx)}]
    trace, calls = [], 0

    def _call(force):
        return client.messages.create(
            model=model, max_tokens=2000, temperature=0.0, system=SYSTEM, tools=ALL_SCHEMAS,
            tool_choice=({"type": "tool", "name": "store_diagnosis"} if force else {"type": "auto"}),
            messages=messages,
        )

    def _finalize(block):
        d = dict(block.input)
        d["store_id"] = str(store_id)
        d["_trace"] = trace
        d["source"] = "agent"
        return d, None, trace

    def _pick_final(resp):
        for b in (getattr(resp, "content", []) or []):
            if getattr(b, "type", None) == "tool_use" and b.name == "store_diagnosis":
                return b
        return None

    try:
        # ── free investigation (auto tool choice) up to the budget ──
        while calls < budget:
            resp = _call(force=False)
            blocks = getattr(resp, "content", []) or []
            tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
            final = _pick_final(resp)
            if final is not None:
                return _finalize(final)

            messages.append({"role": "assistant", "content": _dump(blocks)})
            if not tool_uses:  # model spoke without acting — nudge it to conclude
                messages.append({"role": "user", "content": "You have enough — call store_diagnosis now."})
                calls += 1
                continue

            results = []
            for tu in tool_uses:
                out = T.run_tool(tu.name, dict(tu.input))
                trace.append({"tool": tu.name, "input": dict(tu.input), "result": out})
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": json.dumps(out, default=str)})
                calls += 1
            messages.append({"role": "user", "content": results})

        # ── budget reached: ONE guaranteed forced conclusion ──
        final = _pick_final(_call(force=True))
        if final is not None:
            return _finalize(final)
        return None, "no diagnosis produced", trace
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", trace


# ── mock-model self-test: `python agent_loop.py` (no API key needed) ──────────
if __name__ == "__main__":
    from data_connector import get_connection
    get_connection()

    class B:  # a fake content block, quacks like the Anthropic SDK's blocks
        def __init__(self, **k): self.__dict__.update(k)

    class Resp:
        def __init__(self, content): self.content = content

    def tu(name, inp, i):
        return B(type="tool_use", name=name, input=inp, id=f"t{i}")

    # A scripted "model" that investigates #8021, then answers — exercises the REAL tools.
    script = [
        Resp([B(type="text", text="Sales are down — traffic or ticket?"), tu("decompose_sss", {"store": "8021"}, 1)]),
        Resp([tu("compare_to_peers", {"store": "8021", "metric": "SSS_Pct"}, 2)]),
        Resp([tu("metric_trend", {"store": "8021", "metric": "SSS_Pct", "weeks": 12}, 3)]),
        Resp([tu("osat_breakdown", {"store": "8021"}, 4)]),
        Resp([tu("store_diagnosis", {
            "severity": "Critical", "confidence": "Medium", "scope": "store_specific",
            "headline": "Same-store sales turned negative (-0.5%) - the weakest store in a soft cohort.",
            "primary_metric": {"key": "SSS_Pct", "label": "Same-store sales growth", "value": -0.5, "unit": "%", "target": 5.0},
            "trend": [0.2, 0.0, -0.4, 0.2, -0.3, -0.5],
            "peer": {"this_value": -0.5, "cohort_avg": 1.4, "cohort_label": "FBC Marcus Webb",
                     "note": "bottom of a soft cohort"},
            "root_cause": "Traffic and ticket are steady, so the softness is demand-side, not execution. Guest satisfaction is low with Value the weakest driver.",
            "recommended_action": "Review local demand and loyalty and the weak Value driver.",
            "action_context": "Visits are steady, so not a throughput fix.",
            "also_check": ["A regional demand or value trend may be in play (not confirmed in data)."],
            "suggested_route": {"tier": "FBC", "reason": "store-specific execution/demand issue"},
            "evidence": [{"tool": "decompose_sss", "result": "transactions flat, ticket flat"},
                         {"tool": "compare_to_peers", "result": "-0.5 vs cohort 1.4"}],
            "cited_values": [{"label": "SSS latest", "value": -0.5}, {"label": "cohort avg", "value": 1.4}],
            "unresolved": ["local competition (no data)"],
        }, 5)]),
    ]

    class FakeClient:
        def __init__(self): self.i = 0

        class _M:
            def __init__(s, outer): s.outer = outer

            def create(s, **kw):
                r = script[s.outer.i]; s.outer.i += 1; return r
        @property
        def messages(self): return FakeClient._M(self)

    anomalies = [{"metric": "SSS_Pct", "metric_label": "Same-store sales growth",
                  "latest_value": -0.5, "target_value": 5.0}]
    diag, err, trace = diagnose_store_agentic("8021", anomalies, client=FakeClient(), max_tool_calls=8)
    print("=== investigation trace (real tools) ===")
    for step in trace:
        r = step["result"]
        print(f"  {step['tool']}({step['input']}) -> {r.get('summary') or r.get('error')}")
    print("\n=== final diagnosis (from store_diagnosis) ===")
    if diag:
        for k in ("severity", "confidence", "scope", "headline", "root_cause", "recommended_action"):
            print(f"  {k}: {diag[k]}")
        print(f"  tools called: {len(trace)}  | error: {err}")
    else:
        print("  none —", err)
