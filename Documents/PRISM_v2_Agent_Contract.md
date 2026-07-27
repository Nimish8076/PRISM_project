# PRISM v2 — Agent Contract (output schema + system prompt)

*This locks the redesigned card's fields into what the agent must produce. Two parts: the **output schema** (the structured object the agent returns, via forced tool-use) and the **system prompt** (persona + rules + loop + stop rule). Build the agent to satisfy this, and it feeds the card in `PRISM_v2_Card_Mockup.html` directly.*

---

## 1. Output schema (the `store_diagnosis` tool)

The agent returns its result by calling one tool. Every field maps to a card element, and the whole object is short by design — the card is a glance, the depth lives in `evidence`.

```json
{
  "name": "store_diagnosis",
  "description": "Return the structured diagnosis for one store (or cohort) after investigating with tools.",
  "input_schema": {
    "type": "object",
    "properties": {
      "severity":   { "type": "string", "enum": ["Critical", "High", "Moderate"] },
      "confidence": { "type": "string", "enum": ["High", "Medium", "Low"] },
      "scope":      { "type": "string", "enum": ["store_specific", "cohort_wide", "market_wide"] },

      "headline":   { "type": "string", "description": "One plain sentence, <= 16 words, including the key number." },

      "primary_metric": {
        "type": "object",
        "properties": {
          "key":    { "type": "string" },
          "label":  { "type": "string" },
          "value":  { "type": "number" },
          "unit":   { "type": "string" },
          "target": { "type": ["number", "null"] }
        },
        "required": ["key", "label", "value", "unit"]
      },

      "trend": { "type": "array", "items": { "type": "number" },
                 "description": "Recent weekly values, oldest to newest, for the sparkline. Optional." },

      "peer": {
        "type": ["object", "null"],
        "properties": {
          "this_value":   { "type": "number" },
          "cohort_avg":   { "type": "number" },
          "cohort_label": { "type": "string" },
          "note":         { "type": "string", "description": "<= 20 words." }
        }
      },

      "root_cause": { "type": "string", "description": "1-2 sentences. ONLY what the tools showed. No untested claims." },

      "causal_chain": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "metric": { "type": "string" },
            "role":   { "type": "string", "enum": ["root", "contributing", "symptom"] },
            "note":   { "type": "string" }
          },
          "required": ["metric", "role", "note"]
        }
      },

      "recommended_action": { "type": "string", "description": "One imperative sentence." },
      "action_context":     { "type": "string", "description": "<= 25 words: why / urgency. Optional." },

      "also_check": {
        "type": "array", "items": { "type": "string" },
        "description": "External factors you could NOT test, each phrased as a possibility ending '(not confirmed in data)'. May be empty."
      },

      "suggested_route": {
        "type": "object",
        "properties": {
          "tier":   { "type": "string", "enum": ["FBC", "Area Director", "RVP", "Functional"] },
          "reason": { "type": "string" }
        },
        "required": ["tier", "reason"]
      },

      "evidence": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "tool":   { "type": "string" },
            "input":  { "type": "string" },
            "result": { "type": "string" }
          },
          "required": ["tool", "result"]
        },
        "description": "The investigation trail — one entry per tool call. Shown under 'View full breakdown'."
      },

      "cited_values": {
        "type": "array",
        "items": { "type": "object",
          "properties": { "label": { "type": "string" }, "value": { "type": "number" } } },
        "description": "EVERY number mentioned anywhere above, so the guard can verify each against tool results."
      },

      "unresolved": { "type": "array", "items": { "type": "string" },
        "description": "Things you wanted to check but couldn't (no data). Feeds the new-tool / new-data list." },

      "secondary_issues": {
        "type": "array",
        "items": { "type": "object", "properties": {
          "severity": { "type": "string", "enum": ["Critical","High","Moderate"] },
          "headline": { "type": "string" },
          "cause": { "type": "string" },
          "recommended_action": { "type": "string" },
          "suggested_route": { "type": "object" } },
          "required": ["headline","cause","recommended_action"] },
        "description": "Additional INDEPENDENT problems in the same store (NOT causally linked to the primary root_cause). Empty for a single-root store. Food safety almost always belongs here as its own issue, never blended into the primary." }
    },
    "required": ["severity", "confidence", "scope", "headline", "primary_metric",
                 "root_cause", "recommended_action", "suggested_route", "evidence", "cited_values"]
  }
}
```

### Field → card mapping

| Schema field | Card element |
|---|---|
| `severity` | colour spine + badge (Critical / High / Moderate) |
| `confidence` | the "Medium confidence" meta chip |
| `scope` | drives the peer note ("store-specific" vs "cohort-wide") |
| `headline` | the bold one-line headline |
| `primary_metric` | the big number + "vs target" |
| `trend` | the sparkline |
| `peer` | the "This store vs Cohort avg" bar |
| `root_cause` | the one-line cause (with the ▸) |
| `recommended_action` + `action_context` | the "Do this next" box |
| `also_check` | the quiet "Also worth checking" chip |
| `suggested_route` | routing tier (contacts resolved downstream) |
| `evidence` | "View full breakdown" list |
| `causal_chain`, `cited_values`, `unresolved` | breakdown detail + the guard |

### Multiple root causes

If a store has more than one **independent** problem, the most urgent becomes the primary diagnosis and the rest go in `secondary_issues` (each with its own cause, action, and route). The card leads with the primary and stacks a compact "Also flagged at this store" block per secondary; the overall badge is the **worst severity across primary + secondary**. Food safety is almost always its own issue here. *Linked* metrics (one root + downstream) stay in `causal_chain`, not here — `secondary_issues` is only for genuinely unrelated problems.

### Populated by the system, NOT the agent
- `store_id`, and the **owner/FBC/RVP names** (resolved from the org table using `suggested_route.tier` — never invented by the agent).
- **Recurrence** ("17th week", times-seen) — from the insights table.
- **Severity floor** — the guard may raise severity (e.g. an FSA floor breach → Critical); it never lowers a hard-rule severity.

---

## 2. System prompt (drop-in)

> You are PRISM, a seasoned field operations analyst for Jersey Mike's. You investigate one store (or cohort) at a time. Detection has already decided this store is off — your job is to work out **why** and **what to do**, by calling tools, not by guessing.
>
> **How you work**
> - One question at a time. Form a hypothesis, call the single most relevant tool, read the result, then choose the next question from what you just learned.
> - Prefer an **analysis tool** (it returns a measurement). Drop to a **raw-data tool** only when no analysis tool fits the question.
> - Use **only** numbers a tool returned. Never state a number no tool gave you. Report rates and percentages as **point changes** ("0.8 points below its average"), never as a ratio of a percentage.
> - Never assert a cause, or rule out an alternative, that you did not test with a tool. If you suspect a factor you cannot test (a new competitor, weather, a regional price change), put it in `also_check` as a possibility ending "(not confirmed in data)" — never in `root_cause`.
> - Decide `scope` from peers: peers fine → `store_specific`; the whole cohort moved → `cohort_wide` / `market_wide`.
> - Recommend exactly **one** primary action.
> - Be honest about `confidence`, and list anything you couldn't test in `unresolved`.
>
> **When to stop (definition of done).** Return the diagnosis once you have: (a) a root cause backed by evidence, (b) scope settled via peers, (c) the obvious alternatives for this metric ruled out, and (d) one action. Also stop if another tool call wouldn't change the conclusion, or you hit the tool-call budget — then lower `confidence` accordingly. "Can't rule this out — no data" is a valid stop.
>
> **Voice.** Write for a store operator: plain, short, calm. Limits — `headline` ≤ 16 words; `root_cause` ≤ 2 sentences; `recommended_action` = one imperative sentence. Sentence case, no drama (a 0.8-point dip is "0.8 points," not "89%").
>
> Return your result by calling the `store_diagnosis` tool. Every number in it must also appear in `cited_values`.

---

## 3. How this changes the current code

- Replaces `reason.py`'s current `_TOOL` (headline / severity / root_cause / causal_chain / actions / cited_values) with the richer `store_diagnosis` above, and swaps the single narration call for the **tool-use loop** (model ↔ tools until done).
- `ground.py` keeps validating `cited_values` — now every number should trace to an `evidence` entry, which makes grounding stronger.
- `agent_ui.py` / the card renders straight from these fields (see `PRISM_v2_Card_Mockup.html`) instead of a prose paragraph.
- The tools themselves (`decompose_sss`, `compare_to_peers`, `trend`, `osat_breakdown`, `channel_mix`, `margin_decomp`, `ops_check`, `fsa_history`, `get_store_weeks`, `list_columns`) are the next thing to build — each a small, verified function over `data_connector`.
