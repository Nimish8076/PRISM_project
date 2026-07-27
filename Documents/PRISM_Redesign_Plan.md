# PRISM Redesign Plan — Agentic Diagnosis + Insights Table

**Jersey Mike's Data Platform · World Wide Technology (WWT)**

_This document is the agreed design and build plan for evolving PRISM from a metric-by-metric if/else diagnosis into a store-level, LLM-reasoned diagnosis, and for turning PRISM's output into a table a dashboard can read. It records the goal, the locked decisions, the architecture, the file-by-file changes, the build order, and the guardrails. It is a working handoff document._

> **Build status — ✅ complete (all 5 steps), 8 Jul 2026.** The agentic diagnosis (`evidence.py` → `reason.py` → `ground.py`), the store-grouped pipeline, the insights table + CSV mirror, and the one-card-per-store UI are all built and tested end-to-end against the labelled `prism_test_data/` (7 flagged stores with correct severities, 6 patterns, recurrence, and the CSV mirror all verified). The app runs today via the Streamlit button; the live LLM path activates once a callable `reasoning_model` is set — see §9 (how to run) and §10 (what changed). The optional v2 tools + eval harness remain (§6, step 6).

---

## 1. Why we are changing it

Two problems with PRISM as it stands today, both from the end-user's point of view:

**1. The output is trapped in a separate app.** The only "push" is an email that is composed but never actually sent, and to see anomalies, causes and actions a user has to open a second Streamlit app. A store owner or regional lead who already lives in the Balanced Scorecard dashboard will not do that.

**2. The diagnosis is a growing if/else ladder that reasons one metric at a time.** `diagnose.py` inspects each metric in isolation and reads a pre-written recommendation for it. It cannot express that one metric is *causing* another (sales down → margin down), and every new situation requires a new hardcoded branch. This is brittle and does not scale.

The redesign fixes both without throwing away the property that makes PRISM trustworthy: **detection stays deterministic, and every fact the system emits is checked against the real data.**

---

## 2. Locked decisions

| Decision | Choice | Notes |
|---|---|---|
| Diagnosis engine | **AI-primary, deterministic fallback** | The LLM produces the diagnosis; `diagnose.py` runs only when the AI fails or its numbers don't validate. |
| Detection | **Unchanged, deterministic** | Statistical + threshold + FSA floor stay exactly as they are. The AI never decides *whether* something is an anomaly. |
| Storage | **Both — SQLite is memory, CSV is its mirror** | SQLite `insights` table is the system of record (upsert keyed on store+metric+week, bumps "times seen"). CSV is rebuilt from it each run for the dashboard. |
| Trigger | **Streamlit button stays** | No pipeline access yet. `run_analysis()` remains trigger-agnostic, so a Fabric pipeline step can call it later with no code change. |
| Presentation | **One card per store** | All of a store's flagged metrics are diagnosed together as one connected story. |
| Scope now | AI feature + table feature; keep the app | Real pipeline trigger and Teams/email dispatch come later, once access is available. |

---

## 3. Architecture — gather → reason → ground

The new diagnosis path replaces the *role* of `diagnose.py` (which becomes the fallback) with three explicit steps, grouped by store.

### Gather (`evidence.py`)
For each store that detection flagged, assemble a single **evidence packet** — one dictionary holding everything the model needs:

- The flagged metrics with their numbers (latest, trailing average, target, % move, gap, methods, severity score).
- The supporting sub-metrics currently hardcoded in the ladder — weekly transactions, average ticket, the four OSAT sub-scores, the food-safety finding, EBITDA, and any others that exist — **discovered dynamically** so a leaner or richer dataset both work.
- Recent trajectories (the last ~8–12 weeks of each relevant metric as small number lists) so trends and timing are visible.
- Peer/cohort context — how the store's FBC group and region are performing on the same metric — to separate store-specific from systemic.
- History from `prism_history.db` — has this store+metric recurred, and how many times.
- A deterministic **lead/lag hint** (a few lines of pandas) indicating whether one metric's decline precedes another's. This is a real signal for the model and also works when the AI is off.

Adding a new metric later means adding a field to this packet, not a new code branch.

### Reason (`reason.py`)
Give the model one store's packet and ask for **one diagnosis for the whole store, as structured JSON**: a headline, the root-cause metric, a causal chain (each flagged metric tagged root / contributing / symptom), a store-level severity, and 2–4 concrete actions.

"v1" and "v2" are simply two build phases of this reasoning step — version 1 first, version 2 as a later upgrade. Both live in `reason.py`; v2 is additive, so nothing is thrown away.

- **v1 — single call (what we build now).** Hand the model the *entire* evidence packet in one shot; it reads it and returns the diagnosis. Simple, cheap, and easy to test, and it already delivers the one-card-per-store cross-metric story. This is the version for this engagement.
- **v2 — tool-using agent (later).** Instead of pre-loading everything, give the model a set of read-only tools it can *choose* to call — `get_submetric_trend`, `get_peer_comparison`, `check_correlation`, `get_alert_history`. It then investigates rather than guesses: forms a hypothesis ("maybe margin is sales-led"), calls a tool to pull the data that tests it, and confirms or drops it before writing its conclusion. That loop is what turns a one-shot call into a true agent. Every tool goes through `data_connector` and its SELECT-only guard.

Uses the existing `anthropic` client today; the reasoning call moves to Azure OpenAI in production to keep data in-tenant.

### Ground (`ground.py`)
After the model answers, deterministically:

- Validate that every number it cited actually appears in the evidence packet; strip or reject anything invented.
- Enforce non-negotiable rules — a food-safety score below the floor is Critical regardless of what the model says; the model may raise severity but never lower it below the deterministic rule.
- On any validation failure or API outage, discard the AI output and call the existing `diagnose.py` for that store.

This is the step that lets operations trust "do X."

---

## 4. The table feature (data product, landing locally)

PRISM's output becomes a table, not just a screen.

- **`alert_store.py`** gains an `insights` table alongside its existing ones: one row per store per run — store_id, week, severity, root_cause, causal chain (JSON text), actions (JSON text), source (`ai` | `fallback`), confidence, first_seen, last_seen, occurrences, status. Written by **upsert** keyed on store+metric(+week): new rows inserted, repeat rows update last_seen and bump occurrences. This is the system of record and preserves the current history/recurrence/feedback features.
- **`data_connector.py`** gains `write_insights(rows)` — a separate, explicit write path (not generated SQL), so the read-only guard on `execute_query` is untouched. It also exports the current state to `insights_latest.csv` in the workspace folder, **rebuilt from the SQLite table each run** so it always reflects the true current picture (ongoing issues stay, resolved ones drop off, re-runs never duplicate). An optional append-only `insights_history.csv` can hold the full audit trail for trend analysis.
- Power BI imports `insights_latest.csv` directly today — no pipeline access required. Later this same shape is written to a Fabric Gold table via an upsert/MERGE, and the dashboard repoints with no logic change.

---

## 5. File-by-file changes

| File | Change |
|---|---|
| `evidence.py` | **New.** Build the per-store evidence packet (column-aware); deterministic lead/lag helper. |
| `reason.py` | **New.** LLM → structured JSON store diagnosis (v1 no tools; v2 tools later). Fails soft. |
| `ground.py` | **New.** Validate AI numbers against the packet, enforce hard rules, fall back to `diagnose.py`. |
| `agent.py` | Group anomalies by store; per store run gather→reason→ground with fallback; keep `_find_patterns` on the full set before trimming; cap by store cards; add caching keyed on (store, week, fired-metrics); `reasoning.enabled` switch. |
| `diagnose.py` | Unchanged in behavior — demoted to the deterministic fallback. Stops growing. |
| `alert_store.py` | New `insights` table + `save_insights()` / `get_insights()` (upsert). Existing tables/features stay. |
| `data_connector.py` | New `write_insights(rows)` + CSV mirror export. Read-only guard unchanged. Optionally flip `DATA_DIR` back to `data`. |
| `agent_ui.py` | One card per store (grouped metrics, connected story, AI/fallback badge, shows the evidence numbers); new "Insights table" view + CSV download; history/feedback stay. |
| `config.py` / `prism_config.yaml` | New `reasoning_model`, `reasoning` block (`enabled`, `use_tools`, `temperature: 0.0`, `max_store_cards`, `cache`), `insights_export` block. Safe defaults, matching the existing fallback pattern. |
| `requirements.txt` | Add `pyarrow` only if Parquet export is wanted (CSV needs nothing new). |

---

## 6. Build order (each step testable before the next)

1. ✅ **`evidence.py`** — per-store evidence packet (column-aware); deterministic lead/lag. _Done + tested._
2. ✅ **`reason.py` v1 + `ground.py`** — validated JSON diagnosis, severity hard-rules, fallback. _Done + tested._
3. ✅ **Rewire `agent.py`** — group-by-store gather→reason→ground with caching, behind `reasoning.enabled`. _Done + tested._
4. ✅ **Insights table + CSV mirror** (`alert_store` + `data_connector`) + config knobs. _Done + tested (upsert/recurrence verified)._
5. ✅ **`agent_ui.py`** — one card per store + insights table view + CSV download + per-store feedback. _Done + tested._
6. ⬜ **Optional next** — add tools to `reason.py` (v2); add an eval script that runs the reasoner over `prism_test_data/` and checks its root-cause call against the planted labels in that README.

---

## 7. Guardrails / tradeoffs we are committing to

- **Cost & latency.** Only stores that detected an anomaly go to the AI (most won't); one call per store, not per metric; results cached per (store, data-version). Detection stays free. The AI runs where no user is waiting.
- **Determinism.** Low temperature, structured output, the validation layer, and caching make a given week's result stable and repeatable. The deterministic diagnosis is stored alongside for audit.
- **Trust.** Every emitted number is validated against the warehouse; hard rules can't be overridden; failures fall back to the ladder. Cards show the evidence they rest on.
- **In-tenant (production).** The reasoning call moves to Azure OpenAI so client data stays in the Microsoft tenant.
- **Evaluation.** `prism_test_data/` (already labeled with planted causes) becomes the eval set that scores the model's diagnosis as the system evolves.

---

## 8. How this maps to the eventual Fabric cutover

Nothing in this redesign blocks the documented Fabric plan; it lines up with it:

- `data_connector` stays the single boundary — reads repoint from SQLite to the Fabric SQL endpoint; `write_insights` repoints from local SQLite/CSV to an upsert/MERGE into a Gold `insights` table.
- `run_analysis()` stays trigger-agnostic — the Streamlit button is replaced by a Gold-layer pipeline step or scheduler with no code change.
- The reasoning LLM swaps from Anthropic to Azure OpenAI behind `reason.py`.
- The dashboard reads the Gold `insights` table; Data Activator / Power Automate sends the email/Teams/Slack notification off new rows.

---

## 9. How to run (today)

1. Install deps: `pip install -r requirements.txt` (nothing new beyond the originals; add `pyarrow` only if you later enable Parquet export).
2. Ensure `.env` has `ANTHROPIC_API_KEY`, and set a **callable** `reasoning_model` in `prism_config.yaml` (a current Anthropic model id, or repoint `reason.py` at Azure OpenAI). If the id can't be called, PRISM still runs — every card just shows `source: fallback` (deterministic, and still correct).
3. `streamlit run app.py` → open the **Proactive Agent** tab → **Simulate Pipeline Run**.
4. You get: the systemic **patterns**, then **one card per store** (severity, an AI-vs-Data badge, the flagged metrics with numbers, the store-vs-peers line, the connected root→downstream story, actions, and the responsible chain), plus a **per-store 👍/👎** and a "message this would send" preview.
5. An **Insights table** panel shows the persisted store insights with a **Download CSV** button, and each run rewrites `insights_latest.csv` in the project folder — import that into Power BI to build the dashboard.
6. Tune without touching code in `prism_config.yaml`: `reasoning.enabled` (turn the AI path off to run pure-deterministic), `reasoning.max_store_cards`, `insights_export.*`, the detection thresholds, and the playbook.

---

## 10. What changed in this build

**New files**

- `evidence.py` — *gather*: one evidence packet per flagged store (flagged metrics + numbers, sub-metrics, ~12-week trends, OSAT sub-scores, latest FSA audit, peer/cohort context, recurrence from history, and a deterministic lead/lag ripple hint). Column-aware, so lean and full datasets both work.
- `reason.py` — *reason* (v1): the LLM turns one packet into a single structured, cross-metric JSON diagnosis; lazy client; fails soft to `None`.
- `ground.py` — *ground*: validates every cited number against the packet, rejects invented metrics, enforces severity hard-rules (AI may raise, never lower; FSA floor → Critical), and falls back to a store-level diagnosis built from `diagnose.py`.
- `insights_latest.csv` — sample of the dashboard feed (regenerated each run).

**Changed files**

- `agent.py` — detection and the org-level `_find_patterns` unchanged; new store-grouped `gather → reason → ground` pass with data-signature caching; `run_analysis()` now returns `{patterns, stores, alerts}`.
- `agent_ui.py` — renders **one card per store** (AI/Data badge, evidence, connected story, actions, owners); new insights-table panel + CSV download; per-store feedback; persists insights each run.
- `alert_store.py` — new `insights` table (upsert keyed on store, recurrence "times seen", status, feedback) + `save_insights` / `get_insights` / `persist_insights` / `record_insight_feedback` / `insight_feedback_stats`.
- `data_connector.py` — `write_insights()` CSV mirror (the future Fabric-write point); the read-only query guard is untouched.
- `config.py` + `prism_config.yaml` — added `reasoning_model`, the `reasoning` block, and the `insights_export` block, all with safe fallback defaults.

**Unchanged on purpose** — detection (`_detect_*`), the correlation pass `_find_patterns`, and `diagnose.py`, which is now the deterministic fallback rather than the primary brain (it stops growing).
