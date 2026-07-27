# PRISM v2 — Full Context (paste into a new chat)

> **How to use this:** open a new chat, mount/attach the project folder (`Prism Layer 2 Agentic Temp`), and paste this in as your first message: *"This is the current state of PRISM v2. Here's the full context."* It reflects everything as of the latest session — the agentic redesign, the recalibrated detection, cause-aware + fleet-wide correlation, routing, and the QA approach. Deeper docs live in `Documents/` (Technical Reference, Agent Contract, Process Flow, Detection Calibration).

---

## 1. What PRISM is

PRISM is the proactive half of the Jersey Mike's data assistant (built by WWT). Weekly, it reads every store's balanced-scorecard metrics, finds the stores genuinely off, has an **AI agent investigate the cause with validated tools**, links related stores into systemic patterns, and routes each issue to the right owner. A Streamlit app today; the target is Power BI on Microsoft Fabric.

**v1 → v2 shift:** v1 had the LLM *narrate* a fixed packet of numbers. v2 makes it an **analyst** — it investigates by calling read-only tools and must *prove each claim*. One line: *"the data decides what's wrong; the agent explains why; every number it cites comes from a tool and is validated."* v2 is live behind `reasoning.use_tools: true`.

## 2. The pipeline (deterministic core, agent at one step)

`Detection → Enrichment → Store diagnosis (THE AGENT) → Ground → Cause-aware correlation → Render · Route · Persist`

Everything is deterministic **except** the store diagnosis. All data access goes through one module, `data_connector` — the single swap point from local CSVs to Fabric.

## 3. Detection (deterministic, recalibrated)

Rules decide *which* stores are off (never the AI). Three fixes were calibrated from the real data:
- **Food safety:** flag only below the **80 floor**, not the aspirational 93 target (111 → 7 alerts). `fsa.flag_below_target: false`.
- **Rate metrics use POINT change** (`latest − trailing`), never a ratio — so a 0.8-pt dip reads as 0.8 pts, not "89%".
- **Per-metric severity bands** from each metric's real volatility (flag/high/critical point drops + target-gap cutoffs), replacing one global ratio band. Result: "High" went from **75% → ~26%** of alerts.

## 4. The agent (LLM as analyst)

**Loop (`agent_loop.diagnose_store_agentic`):** decide the next question → call a tool → read the measurement → decide again → conclude with a structured `store_diagnosis`. A tool-call budget (`max_tool_calls: 8`) guarantees it concludes (a forced final call). Runs in parallel across the top `max_store_cards` (12) ranked stores.

**Tools (`tools.py`, all read-only):**
- Analysis doors (return a *measurement*): `decompose_sss` (traffic vs ticket), `compare_to_peers` (store vs FBC cohort), `metric_trend` (sharp vs chronic), `osat_breakdown` (guest sub-drivers), `channel_mix`, `margin_decomp`, `ops_check`, `fsa_history`.
- Raw-data doors (return rows): `get_store_weeks`, `list_columns`.

**Output — `store_diagnosis` (forced tool-use):** `severity, confidence, scope, driver, headline, primary_metric, trend, peer, root_cause, causal_chain, recommended_action, action_context, also_check, secondary_issues, suggested_route, evidence, cited_values, unresolved`. (Full schema in `Documents/PRISM_v2_Agent_Contract.md`.)

**Persona / rules (system prompt):** a seasoned Jersey Mike's field analyst. Tools return **measurements, never verdicts**; the agent **never asserts what it hasn't checked** (unprovable causes go in `also_check`, labelled "not confirmed in data"); one primary action; honest confidence; multi-root → `secondary_issues`; food safety is its own issue.

**Stop rule (definition of done):** stop once it has a root cause backed by evidence, scope settled via peers, the obvious alternatives ruled out, and one action — or the budget is hit (then conclude with lower confidence).

**Grounding:** `ground.ground_agent` enforces the deterministic **severity floor** (worst-anomaly severity + FSA floor → Critical; the AI may raise, never lower) and bumps to the worst of primary + secondary. The older `ground()` path also validates cited numbers against the packet and falls back to the deterministic diagnosis (`diagnose.py`) if the agent is unavailable — so no store is dropped.

## 5. The card (glanceable) + multi-root

One card per store: severity spine, headline, hero number + sparkline, peer bars, one cause, "Do this next," a quiet "Also worth checking," and the evidence behind "View full breakdown." **Multi-root:** genuinely independent problems (e.g., sales + a food-safety breach) show as a primary + compact "Also flagged" secondary blocks, each with its own action/owner.

## 6. Correlation (cause-aware + fleet-wide)

- **`_quick_driver`** tags *every* flagged store with a cheap deterministic driver (traffic / ticket_value / guest_experience / food_safety / cost_margin / operations), so correlation sees the whole fleet; the agent's richer `driver` overrides on the diagnosed cards.
- **`_correlate_by_cause`** (concentrated cohorts) clusters stores sharing a driver under one FBC, but only calls it systemic if it clears **count ≥ 3, share ≥ 40%, lift ≥ 1.8×** (the driver is that much more common here than fleet-wide). Fallback: any **≥ 3** cohort, marked "Possible." Routes to the **Area Director**.
- **`_find_fleet_patterns`** (company-wide) flags a driver that's **broad** — ≥ 35% of flagged stores across ≥ 3 regions and ≥ 4 FBCs — as one **company-level** card routed to **Ops leadership** (with a "real decline vs target too high?" caveat). Ranked first.
- Rendered in the Systemic Patterns section (fleet-wide → cohort → deterministic co-flag), colour-coded (purple / blue / amber), styled like the store card.

## 7. Routing & alerts

The **agent picks the tier** (`suggested_route`); `_route_recipient` resolves the real person from the org table and **escalates up the chain if a tier is unassigned**. Store-specific → **FBC**; systemic/food-safety → up (**Area Director → RVP**); company-wide → **Ops leadership**. Secondary issues route separately. Alerts are **composed, not sent** (the dispatch bar shows recipients); the model never invents a contact.

**Hierarchy (important):** two axes — the **owner** (`FranchiseOwner` = a *holding company* like "Adelphi Holdings"; an individual person franchisee only exists in the 5-store `Dim_Store_Showcase` subset), and the **support/escalation chain FBC → Area Director → Regional VP** (all populated; an FBC covers 1–3 franchises by territory). Data: 126 stores, 26 owners, 18 FBCs, 16 Area Directors, 5 RVPs.

**Emails:** only the **franchise-owner entities** have emails (`Dim_FranchiseOwner.Email`, e.g. `adelphi@jmfranchise.com`). The people (FBC/AD/RVP) have **no email** in the data — a contacts directory would be needed to actually send.

## 8. Persistence & data boundary

`insights` table (SQLite; machine-owned, upserted, auto-resolves on recovery) + a CSV mirror Power BI reads. Human feedback (👍/👎/acknowledge) writes to a separate `insight_actions` table (never clobbered by re-runs). All reads/writes go through `data_connector` — the one place repointed at Fabric in production.

## 9. Config knobs (`prism_config.yaml`)

- `reasoning.use_tools` (true = v2 agent), `max_tool_calls` (8), `max_store_cards` (12).
- `metrics.*` — per-metric `is_rate`, point + target-gap bands.
- `fsa.flag_below_target` (false — floor-only).
- `correlation.min_stores/min_share/min_lift` (3 / 0.4 / 1.8), `fallback_min_stores` (3), `fleet_min_share/fleet_min_regions/fleet_min_fbcs` (0.35 / 3 / 4).
- `insights_export.*` (SQLite + CSV mirror).

## 10. File map

`agent.py` (orchestrator: detect, enrich, correlation, routing helper, run_analysis) · `agent_loop.py` (the investigation loop + schema + system prompt) · `tools.py` (the 10 tools) · `ground.py` (validation + severity floor) · `diagnose.py` (deterministic per-metric diagnosis / fallback) · `evidence.py` (per-store evidence packet) · `data_connector.py` (the data door) · `alert_store.py` (insights + insight_actions) · `config.py` / `prism_config.yaml` · `app.py` / `agent_ui.py` (Streamlit UI — the only files that import Streamlit).

## 11. Guardrails (the trust model)

Detection is deterministic; tools return measurements not verdicts; the agent only uses tool numbers; the guard validates + floors severity; the agent proposes the tier while the org table resolves the person; **freedom scales with oversight** (verified tools on the unattended path, open SQL only in the human-reviewed "Ask PRISM" chat); safe fallback to the deterministic diagnosis.

## 12. Keeping the agent from repeating mistakes (QA loop)

You can't make an LLM *never* err — you drive the rate down and prevent regressions:
**reproduce** the wrong case (save input + the tool trace + the correct answer) → **diagnose** why (trace shows skipped/misread/ambiguous) → **fix the class** (firmer prompt rule / better tool / a deterministic checker in `ground_agent` / move it to code) → **lock it with a regression test** (a labelled set it must always pass) → **score on an eval set** (a change must hold the score) → **feed from field feedback** (👎 cards become new cases). The strongest lever is a **deterministic checker** (e.g. "if driver = traffic but decompose shows ticket → reject"), because it catches the error without trusting the model to be careful.

## 13. Open items / next ideas

- **Eval harness** (labelled store set + scoring run) and **consistency checks in `ground_agent`** (driver-vs-`decompose_sss`; verify every `cited_value` appears in the trace) — the top two to build.
- **Food-safety correlation exemption** (surface a territory's food-safety breaches even below the 40% share bar) — offered, not yet built.
- **Alert delivery:** a contacts table (name → email) to actually send to FBC/AD/RVP.
- **Fabric / production:** repoint `data_connector` at Fabric, Azure OpenAI in-tenant, Power BI + row-level security, a semantic layer beneath the tools (shared truth with dashboards).
- **External factors:** partly handled via `also_check` (labelled hypotheses steered by the store-vs-cohort signal).

## 14. Environment & data facts

Python + Streamlit; SQLite `prism_history.db`; data in `data/` (8 CSVs). Targets (Franchisee persona): SSS 5.0, OSAT 85, EBITDA 20.8, FSA 93; FSA floor 80. Project root is the mounted folder `Prism Layer 2 Agentic Temp` (docs in `Documents/`). Every edit this session was backed up under `backups/`.

*If anything here conflicts with the code, trust the code — `agent.py`, `agent_loop.py`, `tools.py`, and `ground.py` define how PRISM behaves.*
