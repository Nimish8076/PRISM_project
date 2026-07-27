# PRISM — Detection Calibration Findings

*Data-driven recalibration for the three detection critiques. Measured against the real data (126 stores, 126 weeks, Franchisee targets). Read-only analysis — no code changed yet.*

## Validation (the model is correct)

Replaying the current logic on the real latest-week data reproduces the reported numbers **exactly**, so the fixes below are trustworthy:

- **301 total alerts** → **226 High (75%)**, 17 Critical, 58 Moderate
- FSA: 126 audits, **111 below target (93)**, only **7 below floor (80)**

## The three fixes

**1. FSA — flag only below the 80 floor (drop the below-target rule).**
`_detect_fsa` flags on `score < 93` OR `score < 80`, and `_severity` labels the below-93-but-safe stores "High". Fix: flag only `score < 80` → Critical. **111 → 7 alerts.**

**2. Percentages: use point-change, not ratio.**
`(latest − trailing)/|trailing|×100` explodes when the base is near zero. Measured on the real data: an SSS move of **0.5 → 0.0** reads as a **−7,205,759,403,792,793,600%** drop; the worst-5% SSS decline is just **−0.81 pts** but shows as **−100%**. OSAT (base ~85) is barely affected; SSS and EBITDA (bases near 0) are badly distorted. Fix: for rate metrics compute `point_change = latest − trailing` and flag/score on points.

**3. Recalibrate severity bands per metric, from real volatility.**
Normal weekly move (points), measured across all store-weeks:

| Metric | p1 (worst 1%) | p5 | p10 | median |
|---|---|---|---|---|
| SSS_Pct | −0.91 | −0.73 | −0.61 | 0.00 |
| OSAT_Pct | −2.10 | −1.70 | −1.44 | +0.01 |
| EBITDA_Pct | −0.90 | −0.73 | −0.61 | 0.00 |

Bands set as multiples of each metric's own worst-5% move (`base = |p5|`): **flag = base, High = 1.5×, Critical = 2.5×.** Target gap kept as a secondary severity input, spread so a mild miss is only Moderate.

## Recommended bands (Option 1)

| Metric | flag (pt drop) | High (pt drop) | Critical (pt drop) | target-gap High | target-gap Critical |
|---|---|---|---|---|---|
| SSS_Pct | 0.73 | 1.09 | 1.81 | ≥6 | ≥9 |
| OSAT_Pct | 1.70 | 2.55 | 4.25 | ≥10 | ≥15 |
| EBITDA_Pct | 0.73 | 1.09 | 1.81 | ≥8 | ≥12 |
| FSA_Score | — | — | below 80 | (target gap dropped) | below 80 → Critical |

## Result — implemented & verified in the app

Run through the live detection + severity code on the real data:

| | Total alerts | Critical | High | Moderate |
|---|---|---|---|---|
| **Before** | 301 | 17 (6%) | **226 (75%)** | 58 (19%) |
| **After** | 203 | 38 (19%) | **52 (26%)** | 113 (56%) |

High drops from **75% → 26%**; Moderate is now the majority, so "High" is meaningful again. FSA: 111 → **7** alerts, all Critical (floor breaches only). By metric (after): OSAT 102 · SSS 47 · EBITDA 47 · FSA 7.

**Note on store #8019 (the deck's example):** its SSS now reads **−0.81 pts** vs recent average (not "−89 %"), and lands **High** — flagged by both trend and target — rather than Critical, because it is *chronically* low (recent avg 0.91) rather than *sharply* dropping this week. If you want chronic-but-severe underperformance to read Critical, lower SSS `critical_target_gap` (e.g. 9 → 4.5); that is a one-line tune.

## Bonus finding — the targets are aspirational (beyond the 3 critiques)

Every target sits above where most of the fleet actually runs, so grading "below target" as an anomaly is the same disease as FSA #1, repeating:

- **OSAT (target 85):** median **74.8**, max 89.1 — **120/126 below target**
- **SSS (target 5.0):** median 2.7 — 112/126 below
- **EBITDA (target 20.8):** median 17.6 — 108/126 below

That's why OSAT becomes the top alert source after the fixes: its Criticals are the genuinely-lowest-satisfaction stores (OSAT < 70), which is legitimate — but if "Critical" should be rarer, that's a one-number tweak (raise OSAT's Critical gap), or a strategic call to re-base targets to realistic bars with ops. (Tested a pure floor-only variant — it collapses to 73% Critical / 0% High, so keep the graded band above.)

## When implementing (files to touch)

- **`prism_config.yaml`** — per metric add `point_drop_threshold`, `high_point_drop`, `critical_point_drop`, `high_target_gap`, `critical_target_gap`; mark rate metrics; FSA `flag_below_target: false`.
- **`agent.py _detect_weekly_metric`** (~L138–153) — point-change for rate metrics; flag/score on points; carry a `stat_delta` (points) + unit.
- **`agent.py _detect_fsa`** (~L195–207) — remove the `below_target` branch; flag only `below_floor`.
- **`diagnose.py _severity`** (~L41–54) — per-metric point bands; FSA below-floor → Critical.
- **`config.py SEVERITY`** — replace the single global band with per-metric bands (keep global as fallback).
- **`agent_ui.py` / `evidence.py`** — display "−X.X pts vs avg" instead of a "%".
