# PRISM — Project Context (Handoff for a New Chat)

> **How to use this:** Start a new chat, mount/attach the project folder (`Prism Layer 2 Agentic Temp`), and paste this file in as your first message with a note like: *"This is the context for PRISM. I want to build a new version — here's what exists today."* It gives a fresh assistant everything it needs to understand the system without re-reading all the code.

---

## 1. What PRISM is (in one paragraph)

PRISM (**P**roactive **R**ecommendation & **I**nsight **S**ystem for **M**etrics) is the proactive half of the "Jersey Mike's Data Assistant," built by WWT. It's a Python/Streamlit app that watches every store's weekly balanced-scorecard metrics, automatically finds the stores that are genuinely in trouble, uses an LLM to explain the most likely cause, checks every number the AI cites against the real data, links related problems across stores into bigger patterns, and delivers **one card per store** — problem, cause, recommended action, and who owns it. The other half is **Ask PRISM**, a chat assistant for plain-English questions. The whole point: reports tell you *what happened*; PRISM tells you *what to do next*, on its own.

## 2. Core design principles

- **Deterministic core + optional AI.** Detection (deciding a store is in trouble) is done by fixed rules, never by AI. The AI only *explains*. This is the trust story.
- **The guard ("Ground").** Every number the AI writes is validated against the real data. The AI can never downgrade a rule-based severity. If it cites a number that doesn't match, it's corrected or the card falls back.
- **Safe fallback.** If the AI is unavailable or returns junk, PRISM falls back to a deterministic diagnosis (`diagnose.py`). It never invents an anomaly or a number.
- **Forced tool-use.** The AI returns a *structured object* via Anthropic tool-calling (not hand-written JSON), so parsing can't break on bad formatting.
- **One card per store.** Output is grouped by store, not one row per metric — a manager sees a store's whole story at once.
- **Cross-store ("rippling") reasoning.** Problems are correlated across stores/consultants/regions to surface systemic patterns a per-store dashboard can't show.
- **Data boundary isolation.** All data access goes through `data_connector.py` — the single swap point to move from local CSV to Microsoft Fabric.
- **UI / logic separation.** Only `app.py` and `agent_ui.py` import Streamlit; the engine is UI-agnostic so it can feed Power BI, Teams, etc.

## 3. The pipeline (7 stages)

`detect → enrich → correlate → gather (group by store) → reason → ground → route/persist`

1. **Detect** — deterministic rules flag anomalies (see §4).
2. **Enrich** — attach ownership/hierarchy (owner, FBC, Area Director, region).
3. **Correlate** — find cross-store patterns (see §5) on the *full* flagged set.
4. **Gather** — group anomalies by store; build an evidence "packet" per store (metrics, trends, peer context).
5. **Reason** — AI reads the packet and writes root cause, causal chain, and recommended actions.
6. **Ground** — validate every cited number; enforce severity floors; fall back if needed.
7. **Route / persist** — write to the `insights` table + CSV mirror; route to the owner.

## 4. Detection rules (deterministic)

- **Statistical drop vs own trend:** compare the latest value to the store's **trailing average** = simple mean of the prior up-to-8 weeks, *excluding* the current week, per store. Percent change = `(latest − trailing) / abs(trailing) × 100`. A large enough negative move flags.
- **Below target:** value below its target minus a buffer.
- **Food-safety floor (FSA):** event-based hard floor of **80** — any breach flags regardless of trend.
Severities are rule-derived; the AI can raise concern but never lower a hard-rule severity.

## 5. Cross-store correlation (the "patterns")

Computed in `agent.py` `_find_patterns` on the full flagged set (before the display trim). Pattern types and thresholds:
- **FBC pattern** — same Franchise Business Consultant, ≥2 stores affected.
- **Area Director pattern** — ≥3 stores.
- **Region + metric pattern** — same region and metric, ≥3 stores.
- **Multi-metric store** — one store failing ≥2 metrics.
Patterns are scored and capped for display.

## 6. Peer comparison ("vs peers")

In `evidence.py` (`_peer_context`, `_latest_per_store`): a store is compared to the **average of its cohort** (its FBC group or region). This is what separates a *store-specific* problem (peers are fine, this store isn't — e.g., #8019 at 0.1% vs peer avg 4.9%) from a *market-wide* one (the whole cohort is down). It only appears when there's a valid cohort to compare against.

## 7. Trust model (why leadership can believe it)

The **data** decides what's wrong (rules). The **AI** only explains it. **Every number is checked.** It **falls back safely** and **never invents** an anomaly. That four-part line is the pitch and it's literally how the code is structured.

## 8. The output card (one per store)

Example (real demo card, Slide 6 of the pitch deck):
> **CRITICAL — Store #8019, Hartford, Northeast**
> **AI diagnosis:** Same-store sales collapsed to 0.1% against a 5% target — a declining average ticket is compressing both sales and margin.
> **How it connects:** Same-Store Sales (root) → EBITDA Margin (symptom)
> **Recommended:** review discount/promo mix and line-level attachment; verify peak-daypart staffing; FBC visit this week.
> **Routed to:** East Holdings (owner) · FBC Sam Lee · RVP Morgan Diaz — vs FBC peers: 0.1% vs 4.9% avg

## 9. Data model & persistence

SQLite file `prism_history.db`, plus a CSV mirror for Power BI.
- **`alerts`** — raw detected anomalies per run.
- **`runs`** — run metadata.
- **`insights`** — machine-owned diagnosed cards. Upserted (keyed on store + data signature). **Auto-resolves** when a store recovers.
- **`insight_actions`** — **human write-back** (👍/👎 feedback, Acknowledge, status). Kept in a *separate* table so re-runs never clobber human input; joined back in via `get_insights` (LEFT JOIN).
- **`insights_latest.csv`** — CSV mirror. Written via `_safe_to_csv`, which writes a `_new.csv` fallback if the file is locked (e.g., open in Excel) and never raises.

## 10. Ask PRISM (the chat assistant)

`chat_assistant.py`. Router `is_prism_question` decides between:
- **DATA mode** — `generate_sql` (with conversation history) → `execute_query` (read-only guard) → `generate_answer`.
- **PRISM mode** — `get_insights` → LLM answer about current insights.
Supports chart-able answers (`chartable`), follow-up memory (`_history_pairs`), and `SUGGESTED_PROMPTS` chips. In the UI it's a floating "Ask PRISM" popover (bottom-right) with an "Explain this store" deep-link from each card.

## 11. File-by-file map

| File | Role |
|---|---|
| `app.py` | Streamlit entry: branded header, floating Ask PRISM popover, renders cards + insights table. Imports Streamlit. |
| `agent_ui.py` | Streamlit render helpers: `_render_store_card`, `_render_insights_table`, feedback buttons (👍/👎, Acknowledge, 💬 Explain), source badge, fallback-reason line. Imports Streamlit. |
| `agent.py` | Orchestrator. `run_analysis → {patterns, stores, alerts, flagged_store_ids}`; `_find_patterns` (correlation), `_detect_*`, `_enrich_with_owners`, `_diagnose_stores` (rank+cap, parallel), `_store_signature`, `_DIAG_CACHE`. |
| `evidence.py` | Builds per-store evidence packets. `build_store_packet`, `group_by_store`, `_peer_context`, `_latest_per_store`, `reset_caches`, `_lead_lag`. Column-aware, per-run caching. |
| `reason.py` | AI diagnosis via forced tool-use. `_TOOL` schema (`store_diagnosis`), `reason_store_ex` (thread-safe), lazy client, model from config. |
| `ground.py` | The guard. `ground()`, `_normalize_ai` (coerces bad shapes), `_close` (magnitude-tolerant number match), `_worst_det_severity`, `_fsa_floor_breach`, `_fallback`. |
| `diagnose.py` | The old deterministic diagnosis — now the safe fallback path. |
| `data_connector.py` | Single data door (the Fabric swap point). `write_insights` + `_safe_to_csv`; read-only guard on `execute_query`; `DATA_DIR="data"`. |
| `alert_store.py` | SQLite persistence: the 4 tables above; `save_insights` (upsert + auto-resolve), `get_insights`, `record_insight_feedback`/`set_insight_action`, `insight_feedback_stats`, `export_insights`. |
| `chat_assistant.py` | Ask PRISM (see §10). |
| `config.py` / `prism_config.yaml` | Config incl. `reasoning_model`, `reasoning` block (`enabled`, `max_store_cards=12`, `use_tools`, `cache`, `max_workers=6`), `insights_export` block. |
| `prism_icon.png` | The PRISM app icon (blue squircle + white "P"). |

## 12. Config knobs (`prism_config.yaml`)

- `reasoning.enabled` — turn the AI layer on/off (off = pure deterministic).
- `reasoning.max_store_cards` (12) — how many stores get an AI diagnosis per run (rank + cap before calling the model).
- `reasoning.use_tools` — forced tool-use on/off.
- `reasoning.cache` — cache diagnoses keyed on a data signature.
- `reasoning.max_workers` (6) — parallel model calls.
- `reasoning_model` — which model to use.
- `insights_export` — CSV/table export settings.

## 13. Performance design

- **Cap before diagnose:** rank flagged stores and only diagnose the top `max_store_cards` (e.g., 111 flagged → 12 diagnosed).
- **Parallel model calls** via `ThreadPoolExecutor` (`reason_store_ex`); DB work stays single-threaded.
- **Compute-once peer baselines** (`_latest_per_store` cached per run).
- **Diagnosis cache** keyed on data signature so unchanged stores aren't re-diagnosed.

## 14. Productionization path (where it's going)

- **Repoint `data_connector.py` at Microsoft Fabric** (Lakehouse/Warehouse) instead of local CSV — the isolation makes this a small change.
- **Azure OpenAI in-tenant** for the reasoning model (compliance).
- **Power BI reads the `insights` table** (+ Row-Level Security by owner/region so each person sees only their stores).
- **Alerts to Teams / email** for CRITICAL cards.
- **Auto-resolve + human feedback loop** already modeled (`insights` + `insight_actions`), so a Power App / Teams flow can write back without re-runs clobbering it.
- **Eval harness** — regression tests on known cases to keep the AI honest as it changes.

## 15. Deliverables already produced (in `Documents/`)

- `PRISM_Redesign_Plan.md` / `.docx` — the redesign plan.
- `PRISM_Technical_Reference.docx` — comprehensive branded reference (per-file walkthrough + diagrams).
- `PRISM_Consulting_Deck.pptx` — long (~16-slide) story deck.
- `PRISM_Executive_Deck.pptx` / `_v2.pptx` — short executive deck.
- `PRISM_Pitch_Deck.pptx` — **the current 7-slide client pitch** (see §16).
- `PRISM_Pitch_Script_5min.md` — 5-minute speaker script matching the pitch deck.

## 16. Current pitch deck (7 slides)

1. **PRISM** — "A proactive insight engine + assistant for the Jersey Mike's balanced scorecard."
2. **Executive Summary** — situation / what PRISM is / how it works / why trust it / what's next.
3. **The Dashboard Blind Spot** — the problem: dashboards report, humans must notice/dig/chase; cross-store patterns invisible.
4. **Meet PRISM: The Proactive Agent** — reads every store, explains, routes; correlates across stores; plus Ask PRISM.
5. **Five steps, no guesswork** — Detect · Gather · Reason · Ground · Route.
6. **One card, the whole story** — the Store #8019 card.
7. **Thank you** — "PRISM — automatically turning store data into action."

## 17. Brand (WWT)

- Colors: Dark Blue `#1C0087`, Light Blue `#0086EA`. **Cherry `#A11B33`** replaced the old red `#EE282A` for accents.
- Skills available for on-brand output: `wwt-brand`, `wwt-presentation`, plus `docx` / `pptx` / `pdf` / `xlsx`.
- WWT presentation assets live in the `wwt-presentation` skill folder (navy graphic-device background, gradient line, white WWT logo, color monogram).

## 18. Environment & paths

- App: Python + Streamlit. Persistence: SQLite `prism_history.db`. Data dir: `data/`.
- Reasoning currently uses an Anthropic model (sandbox); production target is Azure OpenAI.
- Project root is the mounted folder **`Prism Layer 2 Agentic Temp`** (docs live in its `Documents/` subfolder).

## 19. Known gaps & ideas for the *next version*

- **External / general factors (liked, was on hold).** Today the AI only explains what's *in the data*. Add a curated, per-driver checklist of *general* causes it can't see — new nearby store opening, seasonal/market trend, weather, competitor move, price change — surfaced under a heading like **"Also consider"** and *steered by the peer signal*: if peers are fine it's store-specific; if the whole cohort is down, point at market/external factors. Keep it clearly labeled as "possible external factors," not invented data.
- **Eval harness** for the reasoning layer (regression cases).
- **Live Fabric + Power BI wiring** (currently local CSV/SQLite).
- **Teams/Power App write-back UI** for feedback/acknowledge (schema already supports it).

## 20. Glossary (Jersey Mike's terms)

- **Balanced scorecard** — the set of weekly store metrics (sales, satisfaction, margin, food safety, etc.).
- **Same-store sales** — sales vs the store's own trend/target; the headline metric.
- **EBITDA margin** — profitability; often a *symptom* downstream of a sales drop.
- **Average ticket** — average spend per order; a common root cause.
- **FSA** — Food Safety Audit; hard floor of 80.
- **FBC** — Franchise Business Consultant (owns a set of stores; e.g., "Sam Lee").
- **Area Director / RVP** — Regional VP (e.g., "Morgan Diaz").
- **Owner** — the franchisee / holding company (e.g., "East Holdings").
- **Hierarchy:** Store → Owner → FBC → Area Director → RVP → Region.

---

*This document is a snapshot for starting fresh. If anything here is out of date versus the code, trust the code — `data_connector.py`, `agent.py`, `reason.py`, and `ground.py` are the four files that define how PRISM behaves.*
