# PRISM — Current State, Change Log & Hardcoded Inventory

_Reference as of this working session. Covers: (1) how the tool works now, (2) every change we made, (3) a full inventory of what is still hardcoded and whether it matters._

---

## 1. What the tool is

"Jersey Mike's Data Assistant" is a Streamlit app with **two halves**:

- **Ask a Question (chat):** natural-language → SQL → plain-English answer.
- **Proactive Agent (PRISM):** scans every store the moment data lands, detects anomalies, correlates them into systemic patterns, explains cause + recommends action, and routes to the responsible people — without anyone opening a report.

The agent's guiding principle is now fully realized: **the deterministic core decides everything factual** (what's wrong, the numbers, the severity, the recommendation); the LLM is optional and only ever rephrases/elaborates already-decided facts.

---

## 2. How the whole tool works right now

### Data layer (`data_connector.py`) — the single swap point
- `get_connection()` builds **one** in-memory SQLite DB from 8 CSVs and **reuses it** (singleton).
- `execute_query(conn, sql)` runs the query but **only if it's a read-only `SELECT`/`WITH`** (single statement) — otherwise it's refused.
- This one module is the boundary that will later repoint to the live Microsoft Fabric model; nothing else knows where rows come from.

### Chat tab (`app.py` → `sql_generator.py`, `schema_context.py`, `answer_generator.py`)
User question → Claude writes SQLite SQL from the live schema + business rules → SELECT-only guard runs it → Claude turns the result into a 3–5 sentence business answer. Generated SQL and raw data are shown in expanders.

### Proactive agent (`agent.py` → `diagnose.py` → `agent_ui.py`, persisted via `alert_store.py`)
Single entry point `run_analysis(max_alerts=12, reason=True, narrate=False)` runs a pipeline:

1. **Detect** — per store, per weekly metric, two independent tests:
   - *Statistical*: latest week vs the store's own trailing 8-week average; fires on a % drop past the metric's threshold (25% for SSS, 8% for OSAT, 20% for EBITDA).
   - *Threshold*: latest value vs the target from `Ref_Targets` (Franchisee tier), fires only past a per-metric buffer.
   - *Food safety* is event-based: latest audit vs target **or** below the absolute floor of 80.
   - Each anomaly stores which methods fired and the exact deviation.
2. **Enrich** — attach city, region, and the responsible chain (Franchise Owner, FBC, Area Director, Regional VP).
3. **Correlate** (`_find_patterns`) — group anomalies by FBC, Area Director, Region+metric, and multi-metric-store; score and keep the top 6. Computed on the **full** set before any display trim.
4. **Diagnose — how cause, recommendation & severity are produced.** This step has two layers: a deterministic **data** layer that always runs, and an optional **AI** layer per card.

   **By data (default, `diagnose.py`, no API call):**
   - **Cause** is *computed* by decomposing sub-metrics the warehouse already holds — it is a fact, not a guess:
     - *SSS (same-store sales)* — sales are down, so it looks at the two things that make up sales: **how many customers came in** (transactions) and **how much each spent** (average check), this week versus the store's normal last 8 weeks. Fewer customers but normal spend → a **traffic problem** (not enough people coming in); normal number of customers but lower spend → a **ticket problem** (smaller orders per visit); both lower → **both soft**; neither actually dropped but sales are still under target → not a recent dip but a longer-run **comp / trade-area** issue (a tough year-over-year comparison or the local market).
     - *OSAT* — reads the four guest sub-scores (`FoodQuality`, `Service`, `Cleanliness`, `Value`) for the latest survey; the **lowest** one is named the driver.
     - *FSA (food safety)* — the cause comes straight from the safety audit: whatever the inspector flagged as the top problem (the `FirstPriorityFinding`). If the audit didn't name a specific problem, it simply reports that the store's score dropped below the safety cutoff of 80.
     - *EBITDA* — if the store's same-store sales are also declining → **sales-driven**; otherwise → **cost / labor-driven**.
   - **Recommendation — how it's chosen:** the diagnosis above produces a short **driver code** for the alert (e.g. `osat_Cleanliness`, `sss_traffic`, `fsa_finding`, `ebitda_cost`). That code is used as the key into a **`PLAYBOOK`** — essentially a fix-it reference sheet that pairs each type of problem with a standard response ("if the problem is X, the recommended response is Y"), implemented as a dictionary of ~14 pre-written actions in `diagnose.py`. Whatever action is stored for that driver becomes the card's recommendation. For example: `osat_Cleanliness` → *"deep-clean, reset the cleaning-checklist cadence and re-train on the sanitation SOP"*; `sss_traffic` → *"focus on local marketing, LTO promotion and lapsed-loyalty win-back; verify peak-daypart staffing."* Because it is a fixed lookup, the same driver always yields the same recommendation — nothing is generated, so it stays consistent and auditable, and the `PLAYBOOK` is meant to be written and approved with Jersey Mike's operations team so every action matches their real SOPs. (In **AI** mode the model may expand this into 2–3 concrete steps, but it starts from this playbook text and never replaces the underlying direction.)
   - **Severity** = a rule: FSA floor breach or a ≥40% drop → **Critical**; both methods firing / a ≥25% drop / a ≥6-point target gap → **High**; otherwise **Moderate**.

   **By AI (optional, only when a card's switch is set to AI):** the deterministic cause/action are handed to the model together with the fixed facts (store, metric, severity, the driver's numbers, and the store's other flagged metrics). The model is explicitly told **not to change the meaning or invent any number or metric**; it may only (a) restate the cause in plain language for the responsible role, (b) connect the store's co-occurring issues into one story, and (c) expand the playbook's one-line action into 2–3 concrete first steps — for example, turning *"deep-clean, reset the checklist cadence, retrain on the sanitation SOP"* into *"1) run a full deep-clean before tomorrow's open; 2) put the hourly cleaning sign-off back in place; 3) hold a short sanitation refresher for all line staff this week."* If the AI call fails, or you simply leave the card on **Data**, the original deterministic text is shown instead — the card never errors out or goes blank, it just falls back to the plain data version. So AI never *decides* the cause, recommendation or severity — it only rephrases and elaborates what the data already produced.
5. **Compose** — build the email-style alert message (composed, **not** sent).
6. Return `{"patterns": [...], "alerts": [...]}`.

### The UI (`agent_ui.py`)
- Patterns render first (the differentiator), then individual alert cards.
- **Every card is a bordered container with a Data / AI switch at the top:**
  - **Data** (default) — the deterministic cause/action/severity (or pattern insight/action). No API call.
  - **AI** (opt-in, lazy, cached per card) — `narrate_alert()` / `narrate_pattern()` ask the LLM to explain in plain language for the responsible role, connect other issues at the same store into one story, and expand the fix into concrete steps. **The data still owns the diagnosis** (driver, numbers, severity/grouping are fixed); on any failure it falls back to the Data text.
- Alert cards also carry 👍/👎 feedback and a "message this would send" preview.
- **Persistence** (`alert_store.py`, SQLite `prism_history.db`): each run is saved; a recurring store+metric bumps a "times seen" count; alerts have open/resolved status; a history panel shows everything with a resolve control and an all-time "% marked useful" value metric.

**LLM usage today:** the chat tab (always) and the optional per-card AI narrator (only when a user flips a card). The agent otherwise runs end-to-end with **zero API calls**.

---

## 3. Changes made this session

**Correctness fixes**
- **Target-persona bug** — `TARGET_PERSONA` was `"FranchiseOwner"`, which doesn't exist in `Ref_Targets` (`Franchisee` does). It silently disabled all "below target" detection. Fixed → threshold detections went 0 → 301, and OSAT & EBITDA (previously **zero** alerts) now fire. Also corrected the persona list in `schema_context.py`.
- **Connection singleton** — `get_connection()` was rebuilding the whole 8-CSV DB on every query (~56 rebuilds per run). Now built once and reused.
- **Real latest-week banner** — the pipeline banner hardcoded "Week 31-03-2025"; now computed from the data via `get_latest_week()` (the true latest is 25-05-2026).

**Detection & display**
- **Per-method deviation badges** — each card now shows the size of the miss: "📉 Sudden change (-27%)", "🎯 Below target (-4.0 pts)".
- **FSA relabel** — a food-safety floor breach was mislabeled "Sudden change"; it now has its own "🚨 Below safety floor (68 < 80)" badge.

**Hybrid reasoning (the big one)**
- New **`diagnose.py`** computes **cause, action, and severity deterministically** from sub-metrics + a curated `PLAYBOOK`. The LLM no longer decides any of these.
- Deterministic severity also removed the old bug where the model's severity label non-deterministically reordered alerts.
- SSS cause split into honest branches (traffic-led / ticket-led / both / below-target-with-stable-inputs).

**AI as an optional, per-card narrator**
- Replaced the universal AI toggle with a **per-card Data/AI switch** (lazy + cached).
- The AI mode was upgraded from "reword only" to **add bounded value**: audience-tailored explanation, cross-metric story for the same store, and concrete next steps — without touching the diagnosis.
- Added the **same switch to the correlated pattern cards** (`narrate_pattern()`), and moved the switch **inside** each card via bordered containers.

**Persistence, feedback, security**
- New **`alert_store.py`** — history, recurrence, open/resolved status, 👍/👎 feedback (SQLite `prism_history.db`).
- **SELECT-only guard** in `execute_query` — the single function (in `data_connector.py`) that every part of the tool calls to run a SQL query and get the results back; the guard makes it refuse anything that isn't a read-only `SELECT` (blocks `DROP`/`DELETE`/stacked statements — which matters once this points at live Fabric).

**Housekeeping**
- Repaired intermittent **null-byte file corruption** introduced by the sandbox mount (rewrote `data_connector.py`, `alert_store.py`, `diagnose.py`; all verified clean).
- Updated the project memory to reflect the hybrid + per-card design.

**New files:** `diagnose.py`, `alert_store.py` (+ `prism_history.db` created at runtime).

---

## 4. Hardcoded inventory

Every hardcoded item below is tagged with one of three verdicts:

- **Keep** — fine to leave baked into the code. These are things genuinely specific to this project that rarely change (brand colors, the four-metric set, the CSV/join rules). Hardcoding them is the right call for a project-specific tool.
- **→ Config** — the value itself is fine, but it's an operational *tuning knob* someone will want to adjust per rollout (detection thresholds, pattern rules, display caps, the model id). It should live in one config file (or a `Ref_` table) so it can be changed **without editing and redeploying code**.
- **Curate** — this is *content*, not a setting, and it must be reviewed and approved with Jersey Mike's operations team before client use (the recommendation `PLAYBOOK`). The tool runs without this, but the wording needs their sign-off to be trustworthy.

### Detection sensitivity — `agent.py` (`METRICS`, `FSA_CONFIG`)
| Item | Current value | Verdict |
|---|---|---|
| `pct_drop_threshold` | SSS 25%, OSAT 8%, EBITDA 20% | **→ Config** (hand-picked, not calibrated) |
| `min_target_gap` | SSS 3.0, OSAT 5.0, EBITDA 4.0 | **→ Config** |
| `trailing_weeks` | 8 (all metrics) | **→ Config** |
| `critical_floor` (FSA) | 80.0 | **→ Config** |
| `direction` | `"down_is_bad"` for all | Keep (add if an "up is bad" metric appears) |
| `TARGET_PERSONA` — which target tier (which row in `Ref_Targets`) stores are graded against | `"Franchisee"` | Keep (now correct); add a startup check that it exists |

### Correlation rules — `agent.py` (`_find_patterns`)
| Item | Current value | Verdict |
|---|---|---|
| Fire thresholds | FBC ≥2 stores, AD ≥3, Region ≥3, Multi-metric ≥2 | **→ Config** |
| Scoring weights — how each pattern's importance is scored so the most significant ones rank first (and survive the top-6 cap) | FBC `×10+items`, AD `×8+items`, Region `×7`, Multi `×12` | **→ Config** (unexplained magic numbers) |
| Pattern cap | top `6` | **→ Config** |

### Display caps — `agent.py` / `agent_ui.py`

*What & why:* a single run can surface hundreds of anomalies, so these caps limit **how many alerts are shown** — keeping the view focused on the most severe, and spreading the shown slots across metrics so one noisy metric doesn't crowd out the rest.

| Item | Current value | Verdict |
|---|---|---|
| `max_alerts` — total alert cards shown per run (most severe first) | 12 (default + UI call) | **→ Config** (12 is arbitrary; some rollouts will want more or fewer) |
| `per_metric_cap` — cap per metric, so the slots stay balanced | `max(2, max_alerts // n_metrics)` | Keep (derived from `max_alerts`, not a standalone magic number) |
| Stores shown in prompt / card | `[:8]` / `[:10]` | Keep |

### Diagnosis thresholds — `diagnose.py`
| Item | Current value | Verdict |
|---|---|---|
| Severity bands | Critical if FSA-floor or stat ≤ −40%; High if both methods / stat ≤ −25% / gap ≥ 6; else Moderate | **→ Config** |
| SSS traffic/ticket cutoff | ±2% vs recent avg | **→ Config** |
| Trailing window (SSS/EBITDA drivers) | 8 weeks | **→ Config** |
| `PLAYBOOK` (all recommended-action text) | ~14 canned strings | **Curate** with ops — the single most important content hardcode |
| Audience pick (`narrate_alert`) | FBC → else Franchise Owner → else "operations team" | Keep (could become a per-card dropdown) |

### Model & LLM — `agent.py`, `sql_generator.py`, `answer_generator.py`
| Item | Current value | Verdict |
|---|---|---|
| Narrator model | `NARRATOR_MODEL = "claude-sonnet-4-6"` (centralized in `agent.py`) | Keep; **valid** id in the installed SDK |
| Chat model | `"claude-sonnet-4-6"` hardcoded **again** in `sql_generator.py` and `answer_generator.py` | **→ Config** (centralize to one place / env var) |
| `max_tokens` | narrator 250–400, SQL 1000, answer 500 | Keep |
| Production endpoint | public Claude API today; Azure OpenAI intended for production | Planned swap |

### Data & storage
| Item | Where | Verdict |
|---|---|---|
| 8-table CSV mapping | `data_connector.py` (`csv_tables`) | Keep (drops when Fabric lands) |
| `DATA_DIR` = `./data` | `data_connector.py` | Keep |
| History DB path = `./prism_history.db` | `alert_store.py` | Keep (make configurable if multi-user) |
| Join rules, `LIMIT 20`, column descriptions | `schema_context.py` | Keep (project-specific by design) |

### UI content — `agent_ui.py` / `app.py`
| Item | Verdict |
|---|---|
| Brand colors, emojis, section copy | Keep (project-specific) |
| Bronze→Silver→Gold "trigger strip" + "Simulate Pipeline Run" | Keep for demo; replace with the real trigger for production |

**Bottom line on hardcoding:** most content-level hardcoding (brand, metric set, join rules, playbook wording) is *appropriate* for a project-specific tool. The items marked **→ Config** are the operational tuning knobs — thresholds, pattern rules, caps, severity bands, the chat model id — which are fine to have chosen by hand but should live in one `config.yaml`/`config.py` (or a `Ref_` table) so they can be tuned per rollout without a code deploy. The **`PLAYBOOK`** is the one hardcode that needs real ops input before client use.

---

## 5. Still open (from the earlier review, not yet done)
- Escape HTML in the cards (`unsafe_allow_html` + interpolated data = XSS risk once live data flows).
- Remove the shipped `debug_*.py` scripts and the committed Windows `venv/`.
- No automated tests / README / CI yet.
- Enrichment picks FBC/RVP by a text sort of `FiscalWeekKey` (should parse to a date, like detection does).
- Bigger bets: real pipeline trigger, real email/Teams dispatch, Fabric + Azure OpenAI cutover.
