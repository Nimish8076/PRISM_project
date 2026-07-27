# PRISM & JM Data Assistant — Critical Review & Client-Readiness Assessment

**Reviewer role:** software testing / QA
**Scope reviewed:** `agent.py`, `agent_ui.py`, `app.py`, `data_connector.py`, `schema_context.py`, `sql_generator.py`, `answer_generator.py`, the `data/` CSVs, and `requirements.txt`.
**Method:** static read of all source + **the code was actually executed** against the sample data to confirm the two highest-impact findings.
**Framing:** This is *project-specific* software for Jersey Mike's. The goal below is not "make it generic" — it is to fix what is broken, remove what will silently rot when data changes, and close the gap between a demo and something a client will actually rely on.

---

## 1. Executive summary

The architecture is sound and the "deterministic detection + LLM narration + cross-store correlation" idea is genuinely differentiated. But the current build is a **demo, not a product**, and two confirmed defects mean it is not even detecting what it claims to detect:

1. **Half the detection engine is dead.** The target-persona string is wrong, so every target lookup returns empty and the entire "below target" (threshold) method never fires. Verified: **0 threshold detections**; 2 of the 4 headline metrics (OSAT, EBITDA) currently produce **zero alerts**.
2. **The database is fully rebuilt dozens of times per run.** There is no singleton connection (despite the design doc claiming one). A single analysis run reloads all 8 CSVs **~56 times**.

Neither is visible to a user — both fail silently — which is exactly the kind of thing that erodes client trust once real data is flowing. Below, findings are severity-ranked, then a hardcoding inventory, security notes, a testing strategy, and a prioritized roadmap to make it adoptable.

---

## 2. Confirmed defects (ranked)

### 🔴 P0-1 — Target persona mismatch silently disables threshold detection *(verified by running the code)*
`agent.py` sets `TARGET_PERSONA = "FranchiseOwner"`. The `Ref_Targets.csv` personas are `Corporate, Regional, FBC, Franchisee` — there is **no "FranchiseOwner" row**. So `_get_targets()` returns `{}`, every `target_val` is `None`, and **Method 2 (threshold vs target) never triggers for any weekly metric**, and FSA loses its target comparison (only the hard `<80` floor survives).

Measured on the sample data: 25 total anomalies, **all `statistical`-only, 0 `threshold`**; only `SSS_Pct` (18) and `FSA_Score` (7) fired — **`OSAT_Pct` and `EBITDA_Pct` produced nothing at all**. The design doc's core selling point ("catches chronic underperformance a trend check misses") is currently non-functional.
*Fix:* change to `"Franchisee"` (and reconcile `schema_context.py`, which also wrongly documents the persona as "FranchiseOwner"). Add a startup assertion that the configured persona exists in `Ref_Targets`.

### 🔴 P0-2 — No connection reuse; full DB rebuilt on nearly every query *(verified)*
`data_connector.get_connection()` calls `sqlite3.connect(":memory:")` and reloads all 8 CSVs **every time it's called**, and `agent._run_sql()` calls `get_connection()` on every query. A full `run_analysis()` does roughly `6 + 2 × (#anomalies)` reloads — **~56 full reloads** on the sample set (2.4 MB weekly + 1 MB OSAT parsed each time). `st.cache_resource` in `app.py` does **not** help, because `agent.py` bypasses it. This is the difference between sub-second and many-seconds latency, and it will scale linearly worse against live Fabric.
*Fix:* memoize the connection (module-level singleton or `functools.lru_cache`), and pass one connection through the pipeline instead of re-opening.

### 🟠 P1-3 — Enrichment re-introduces the exact date-sort bug the design avoids elsewhere
`_enrich_with_owners()` picks the FBC/RVP with `ORDER BY FiscalWeekKey DESC LIMIT 1`. `FiscalWeekKey` is a `DD-MM-YYYY` **string**, so this is a text sort, not a date sort — "31-03-2025" and "08-01-2024" don't order correctly by day. Detection carefully parses the date in pandas to avoid this; enrichment does not. Impact is usually small (owners rarely change week to week) but it's a latent correctness bug and an inconsistency with the stated design.

### 🟠 P1-4 — FSA audit dates parsed with the wrong day/month intent
`_detect_fsa()` parses `AuditDate` with `dayfirst=True`, but the data is `YYYY-MM-DD` (`2024-03-18`). This emits a pandas warning and risks mis-parsing any ambiguous date. Parse with an explicit `format="%Y-%m-%d"`.

### 🟠 P1-5 — Cross-metric severity is not comparable, yet drives selection and ordering
`severity_score` means different things per method: statistical = percent change (0–100), threshold = absolute point gap, FSA = points below floor. These incomparable numbers are sorted together in `run_analysis` to pick the "top" alerts, and the final display order is decided by the **LLM's** Critical/High/Moderate label — a non-deterministic step deciding the ordering of a system whose whole pitch is a "deterministic core." Normalize severity to a common 0–1 scale computed deterministically; use the LLM label for narration only.

### 🟡 P2-6 — FSA critical-floor breaches are mislabeled as "Sudden change"
In `_detect_fsa()`, a `<80` floor breach is tagged `"statistical"`, which the UI renders as "📉 Sudden change." A failed audit is not a sudden change — it's an absolute-floor breach. Add a dedicated method label (e.g. `critical_floor` → "🚨 Below safety floor").

### 🟡 P2-7 — Dead multi-turn feature
`generate_sql()` accepts `conversation_history` for follow-up questions, but `app.py` calls `generate_sql(question)` with no history — so follow-ups ("and for the Northeast?") won't work. Either wire the history through or remove the parameter.

---

## 3. Hardcoded-values inventory

Not all hardcoding is bad in project-specific software — brand colors and the JM metric set are fine to bake in. The problem is hardcoded values that are **wrong**, that **misrepresent live state**, or that **need tuning per rollout but are buried in code**.

**Will misrepresent reality / rot when data changes (fix these):**
- **Fake "latest week" banner** — `agent_ui.py` hardcodes `"Week 31-03-2025 ingested"`. It will always show that date no matter what data actually landed. Derive it from the max parsed week.
- **`TARGET_PERSONA = "FranchiseOwner"`** — wrong value, see P0-1.
- **Model id `"claude-sonnet-4-6"`** duplicated in 4 places (`agent.py` ×2, `sql_generator.py`, `answer_generator.py`). Centralize in one config/env var; verify the identifier is even valid (if it isn't, every LLM call is silently hitting the fallback path).
- **Simulated pipeline event** text and the whole "Simulate Pipeline Run" button — fine for demo, but there is no real trigger behind it yet.

**Tuning knobs that belong in config, not source (should be externalized):**
- Per-metric `pct_drop_threshold` (25 / 8 / 20), `min_target_gap` (3 / 5 / 4), `trailing_weeks` (8 for all), FSA `critical_floor` (80).
- Pattern firing thresholds (FBC ≥2, AD ≥3, Region ≥3, Multi-metric ≥2) and the scoring weights (`×10 / ×8 / ×7 / ×12`) — these are unexplained magic numbers driving what gets surfaced.
- `max_alerts=12`, `per_metric_cap`, `patterns[:6]` cap, `max_tokens` (300/250/500), `LIMIT 20` in the SQL rules.
- `direction` is `"down_is_bad"` for every metric — no support for a metric where "up is bad" (e.g. complaint rate, cost ratios) if one is ever added.

**Acceptable as-is (project-specific):** brand palette/emoji in the UI, the fixed four-metric watch list, the JM-specific prompt wording.

**Recommendation:** move the tuning knobs into a single `config.yaml`/`config.py` (or a `Ref_` table) so operators can adjust sensitivity without a code deploy — that alone is a major adoption lever.

---

## 4. Architecture & correctness weaknesses

- **String-keyed correlation.** Patterns group by FBC/AD **name**. Two people sharing a name, a typo, or a case/whitespace difference silently splits or merges groups. Group by a stable ID where possible.
- **Per-store "latest week" is not time-aligned.** Each store is compared to its own most recent row. A store that stopped reporting weeks ago is treated as current, and stores at different weeks are correlated as if simultaneous. Consider anchoring to a global "current fiscal week" and flagging stale stores separately.
- **Two sources of truth for ownership.** `FranchiseOwner`/`AreaDirector` come from `Dim_Store`; `FBC_Name`/`RegionalVP` from `Fact_StoreWeekly`. They can disagree. Pick a system of record.
- **Silent global degradation.** If the LLM endpoint is down, every alert falls back to "Moderate / manual review" with no operator-visible banner — the run *looks* successful but every severity is wrong. Surface API health explicitly.
- **Routing/recipient inconsistency.** Cards show the full 4-role chain, but the "Routed to…" dispatch bar only counts Regional VP + Franchise Owner. Decide who is actually notified and make the two consistent.
- **`_run_sql` signature-sniffing via `inspect`.** Guessing whether `execute_query` takes `(sql)` or `(conn, sql)` is fragile; standardize the interface (the design doc's whole premise is that this boundary is stable).
- **No persistence.** Alerts/patterns live only in `st.session_state`. There's no history, no "is this still open next week," no dedupe of the same alert recurring. This is essential for the product to be useful beyond a single glance.

---

## 5. Security & data-safety

- **Arbitrary SQL execution.** `sql_generator` → `execute_query` runs whatever the LLM emits with no allow-list. Against in-memory SQLite the blast radius is small, but the moment this points at live Fabric, a generated `DELETE`/`UPDATE`/`DROP` (or an expensive cross-join) executes. Enforce **read-only / SELECT-only**, wrap in a read-only transaction, and cap rows/timeout.
- **XSS via `unsafe_allow_html`.** Store names, cities, audit findings, and raw LLM text are interpolated straight into HTML in `agent_ui.py`. Live data (or a prompt-injected finding) containing `<script>`/HTML will render. Escape all interpolated values.
- **Prompt-injection surface (future).** Free-text fields (`FirstPriorityFinding`, store names) are placed directly into LLM prompts. With governed live data this is low risk, but sanitize/delimit untrusted text before the Fabric/Azure OpenAI cutover.
- **Secrets handling is OK but brittle.** Key is in `.env` (good, not committed), but the client is constructed at import with `os.environ.get(...)` and no missing-key guard — a missing key yields a confusing runtime error deep in a call rather than a clear startup message.

---

## 6. Testing gaps & recommended strategy

**Current state: zero automated tests, no README, no CI.** For a client-bound analytics product this is the biggest structural risk — every one of the bugs above would have been caught by a basic suite.

Recommended, in priority order:

1. **Golden-path unit tests on the deterministic core** (highest value, no API needed). Feed small fixed CSVs into `_detect_weekly_metric`, `_detect_fsa`, `_find_patterns` and assert exact anomalies/patterns. A single test asserting "threshold method fires when a store is below target" would have caught P0-1 immediately.
2. **Config/contract tests:** assert `TARGET_PERSONA` exists in `Ref_Targets`; assert every `target_col` in `METRICS` exists as a column; assert every metric column exists in `Fact_StoreWeekly`. These guard against schema/data drift.
3. **Date-handling tests:** DD-MM-YYYY weekly keys and YYYY-MM-DD audit dates sort to the correct "latest," including month/day-ambiguous dates.
4. **Determinism test:** with `reason=False`, the same input yields byte-identical anomalies/patterns across runs.
5. **LLM-boundary tests with a mocked client:** malformed/empty responses, missing `SEVERITY:`/`CAUSE:` labels, markdown, and API exceptions all degrade gracefully (the parsers and fallbacks are reasonable but untested).
6. **SQL-safety tests:** generated SQL that isn't a `SELECT` is rejected.
7. **Perf regression test:** assert the number of `get_connection()` calls per run is O(1), not O(anomalies) — locks in the P0-2 fix.

Add a `README`, a `Makefile`/`pytest` entry, and a small CI workflow. Remove the five `debug_*.py` scratch scripts (and the empty nested `jm-data-assistant/` folder) from what ships.

---

## 7. Data & repository hygiene

- `data/` contains **loaded-but-unused** and **present-but-unloaded** files. `data_connector` loads 8 tables; the `*_Showcase*` CSVs, `Periods.csv`, `Personas.csv`, and `Scorecard MVP.xlsx` are never loaded. `Fact_OSAT` (1 MB) and `Dim_Date` **are** loaded but the agent reads OSAT from `Fact_StoreWeekly`, not `Fact_OSAT` — so a 1 MB file is reparsed ~56×/run for nothing. Trim what the agent doesn't use.
- `requirements.txt` uses exact pins (good for reproducibility) but there is no supported-Python constraint and no lockfile/hashes. Confirm the pinned versions install cleanly in a fresh environment (a single bad pin blocks onboarding) and record the target Python version. Note the committed `venv/` is a Windows build — it should be git-ignored, not shipped.
- `print("✅ Loaded …")` in `get_connection()` spams stdout on every rebuild — replace with real logging at DEBUG.

---

## 8. Client-readiness roadmap (to actually increase usage)

The single biggest adoption blocker is that today it's a **click-to-simulate demo**. To make franchise/ops users depend on it:

**P0 — correctness & trust (do first)**
- Fix P0-1 and P0-2; add the config/contract tests. Nothing else matters if detection is wrong or slow.
- Replace the fake "Week 31-03-2025" banner with the real latest week.

**P1 — make it live and actionable**
- **Real trigger:** wire `run_analysis()` to the Gold-layer pipeline completion (or a schedule) so it runs without a human — the story the UI already tells.
- **Real dispatch:** connect the composed message to email/Teams (with a dry-run/preview toggle), so "routed" becomes true, not aspirational.
- **Persistence + history:** store alerts/patterns so users can see trend, "still open," resolved, and recurrence — the reason someone comes back daily.
- **Fabric + Azure OpenAI cutover** behind the existing `data_connector` boundary, keeping data in-tenant (already designed for — execute now).

**P2 — stickiness & tuning**
- **Feedback loop:** thumbs up/down + "was this actionable?" on each alert, captured to tune thresholds and prove value.
- **Config UI / `config.yaml`** for the tuning knobs in §3 so ops adjusts sensitivity without a deploy.
- **Role-scoped views:** an FBC sees their stores, an RVP sees their region — routing already computes this; expose it as filters.
- **Auth + audit trail** for who saw/acted on what (needed before anything leaves a demo).
- **Observability:** log LLM latency/cost/failure and detection counts; alert operators when the LLM path is degraded (see §4).

**Quick wins (hours, high visibility):** persona fix, connection singleton, real latest-week banner, remove debug scripts, escape HTML, SELECT-only guard.
**Bigger bets (weeks):** real trigger + dispatch, persistence/history, Fabric/Azure cutover, feedback loop.

---

## 9. What's genuinely good (keep it)
The deterministic-core / LLM-narration split is the right call and should be defended. The correlation pass is the real differentiator and is cleanly separated. The single `data_connector` boundary is a smart seam for the Fabric swap. The fail-safe fallbacks and "compose but don't transmit / say 'ready to send' not 'sent'" honesty are good instincts — the fixes above are about making the *detection* as trustworthy as the *framing* already is.
