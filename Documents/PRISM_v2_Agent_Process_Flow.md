# PRISM v2 — Detailed Agent Process Flow (function by function)

*Every function in a full agent run, in call order, with the file it lives in and what it does. Reflects `reasoning.use_tools: true` (the v2 agent path).*

---

## Top-level flow

```
render_agent_panel()                         [agent_ui.py]   ← user clicks "Simulate Pipeline Run"
   └─ run_analysis(reason=True)              [agent.py]      ← the whole pipeline
        1. detect        →  _detect_weekly_metric · _detect_fsa
        2. enrich        →  _enrich_with_owners
        3. co-flag       →  _find_patterns · _summarize_pattern
        4. DIAGNOSE      →  _diagnose_stores → agent_loop.diagnose_store_agentic (the agent)
        5. cause-corr    →  _quick_driver · _correlate_by_cause
        6. alerts        →  _reason_about · _compose_alert_message
        returns {patterns, stores, alerts, flagged_store_ids}
   └─ render patterns + store cards + dispatch + persist
```

---

## Stage 0 — Trigger & data

- **`render_agent_panel()`** *(agent_ui.py)* — the Streamlit page; on the run button it calls `run_analysis`, then renders everything below.
- **`data_connector.get_connection()`** *(data_connector.py)* — loads the 8 CSVs into one in-memory SQLite (built once, reused).
- **`run_analysis(max_alerts, reason, narrate)`** *(agent.py)* — orchestrates the entire pipeline and returns the result dict.
- **`_get_targets()`** *(agent.py)* — reads `Ref_Targets` for the configured persona (Franchisee) → the target for each metric.
- **`_run_sql(sql)`** *(agent.py)* — thin read-only query helper used by detection/enrichment (→ `data_connector.execute_query`).

## Stage 1 — Detection (deterministic, no AI)

- **`_detect_weekly_metric(metric, cfg, targets)`** *(agent.py)* — per store: latest week vs the 8-week trailing average. For **rate metrics** (`is_rate`) it uses the **point change** (`latest − trailing`) and per-metric point thresholds; else the legacy % ratio. Also the target-gap check. Emits anomaly dicts (`stat_delta`, `target_gap`, `severity_score`, `methods`).
- **`_detect_fsa(targets)`** *(agent.py)* — latest audit per store; flags **only below the 80 floor** (`fsa.flag_below_target` is false).
- **`_detect_generic(cols)`** *(agent.py)* — optional catch-all trend on unwatched columns (off by default).

## Stage 2 — Enrichment

- **`_enrich_with_owners(anomaly)`** *(agent.py)* — attaches `city`, `region`, and the `responsible` chain (Franchise Owner, FBC, Area Director, Regional VP) from `Dim_Store` + `Fact_StoreWeekly`. Correlation and routing depend on this.

## Stage 3 — Deterministic correlation (co-flag patterns)

- **`_find_patterns(anomalies)`** *(agent.py)* — groups flagged stores by FBC / Area Director / region+metric / multi-metric-store; scores and caps them. Pure co-occurrence.
- **`_summarize_pattern(pattern, narrate)`** *(agent.py)* → **`diagnose.diagnose_pattern(p)`** *(diagnose.py)* — sets each pattern's deterministic insight + action.

## Stage 4 — Store diagnosis (THE AGENT)

- **`_diagnose_stores(all_found)`** *(agent.py)* — the diagnosis orchestrator:
  - **`evidence.group_by_store(anomalies)`** *(evidence.py)* — groups anomalies into `{store: [items]}`.
  - ranks stores by `severity_score`, caps to `reasoning.max_store_cards` (12).
  - **`_store_signature(items)`** *(agent.py)* — cache key; returns a cached diagnosis if the store's data is unchanged (`_DIAG_CACHE`).
  - **`evidence.build_store_packet(sid, items)`** *(evidence.py)* — assembles the store's evidence (used for context + the deterministic fallback). Internally calls `_store_context`, `_weekly_trend`, `_osat_subscores`, `_fsa_latest`, `_peer_context` (→ `_latest_per_store`), `_lead_lag`, `_compact_anomaly`.
  - runs the agent **in parallel** (`ThreadPoolExecutor`): **`agent_loop.diagnose_store_agentic(sid, items)`** per store.
  - **`_finish_agentic(sid, items, ai, err)`** *(agent.py)* — grounds + enriches the agent's result (or falls back to `_finish` → the deterministic card if the agent returned nothing).

### 4a. The investigation loop — `agent_loop.diagnose_store_agentic(store_id, anomalies)` *(agent_loop.py)*

- **`_get_client()`** — lazy Anthropic client (injectable for tests).
- **`tools._ctx(store_id)`** *(tools.py)* — store's FBC/region/owner/RVP.
- **`_initial_prompt(store_id, anomalies, ctx)`** — builds the opening user message (the flagged metrics).
- the loop, up to `max_tool_calls` (8):
  - **`_call(force)`** — one `client.messages.create` with the system prompt + all tool schemas (`ALL_SCHEMAS`). `force=True` on the final turn requires `store_diagnosis`.
  - **`_pick_final(resp)`** — did the model call `store_diagnosis`? If yes → done.
  - else **`_dump(blocks)`** appends the model's turn, and each tool call is executed via **`tools.run_tool(name, args)`** and returned as a `tool_result`.
  - when the budget is hit, one **forced** `store_diagnosis` call guarantees a conclusion.
- **`_finalize(block)`** — returns the structured `store_diagnosis` dict (+ the tool `_trace`, `source="agent"`).

### 4b. The tools — `tools.run_tool(name, args)` dispatches to *(tools.py)*

Analysis doors (return a measurement): **`decompose_sss`**, **`compare_to_peers`**, **`metric_trend`**, **`osat_breakdown`**, **`channel_mix`**, **`margin_decomp`**, **`ops_check`**, **`fsa_history`**.
Raw-data doors (return rows): **`get_store_weeks`**, **`list_columns`**.
Shared helpers: **`_weekly`** (per-store series), **`_lt`** (latest vs trailing point change), **`_ctx`**, **`_targets`**, **`_q`** (→ `data_connector.execute_query`), **`_sid`**, **`_esc`**.

### 4c. Grounding — `ground.ground_agent(ai, anomalies)` *(ground.py)*

- enforces the **severity floor**: worst deterministic severity across the store's anomalies (via **`diagnose._severity`**) and **`_fsa_floor_breach`** (FSA → Critical). The AI may raise severity, never lower a hard rule.
- bumps the overall severity to the worst of the **primary + `secondary_issues`**.
- *(the older narration path uses `ground.ground` → `_normalize_ai`, `_valid_metrics`, `_packet_numbers`, `_close`, `_fallback`.)*

## Stage 5 — Cause-aware correlation

- **`_quick_driver(store_id, items)`** *(agent.py)* — a cheap **deterministic** driver bucket for **every** flagged store (rules over the flagged metric + one sub-metric check via `tools.decompose_sss`). Lets correlation see the whole fleet.
- override: the agent's `driver` (from the diagnosed top-12) replaces the cheap tag where present.
- **`_correlate_by_cause(stores)`** *(agent.py)* — clusters stores by shared driver + FBC; keeps a cluster only if it clears **count ≥ 3, share ≥ 40%, lift ≥ 1.8×** (fleet base rate). Falls back to a looser ≥3 rule ("Possible") if nothing clears. Inner **`_pattern(...)`** builds each cluster and resolves **`_route_recipient("Area Director", ctx)`** for it.

## Stage 6 — Individual alerts (legacy, kept for compatibility)

- **`_reason_about(anomaly, narrate)`** *(agent.py)* → **`diagnose.diagnose_alert(a)`** — per-metric deterministic cause + action + severity.
- **`_compose_alert_message(anomaly)`** *(agent.py)* — attaches a composed message to each alert.

## Stage 7 — Render, route & persist *(agent_ui.py)*

- **`_render_pattern(p, ai_steps)`** — the Systemic Patterns cards (co-flag + cause clusters); shows "Routed to" for clusters. **`narrate_pattern(p)`** *(agent.py)* adds AI "concrete steps".
- **`_render_agent_store_card(diag)`** — the redesigned store card (severity spine, hero number + sparkline, peer bars, cause, "Do this next", "Also worth checking", secondary issues, "View full breakdown"). Falls back to **`_render_store_card`** for non-agent diagnoses.
- **`_route_recipient(tier, ctx)`** *(agent.py)* — resolves WHO gets an alert for a tier (escalates if unassigned).
- **`_store_message(diag)`** — composes the email/Teams alert, routed by `suggested_route` (+ secondary issues routed separately, owner CC'd).
- **`_collect_recipients(stores)`** — the dispatch bar's routed recipients.
- feedback buttons → **`alert_store.record_insight_feedback`** / **`set_insight_action`** (write to `insight_actions`).
- persistence → **`alert_store.save_insights`** (upsert + auto-resolve, `insights` table) and **`data_connector.write_insights`** (the CSV mirror Power BI reads).

---

## One-line summary of the split

- **Deterministic (rules):** detection, enrichment, co-flag patterns, `_quick_driver`, the correlation gate, severity floor, routing name-resolution, persistence.
- **The agent (LLM + tools):** only Stage 4a — decide what to check, call tools, and write the `store_diagnosis`. Everything else is code the agent's output flows through.
