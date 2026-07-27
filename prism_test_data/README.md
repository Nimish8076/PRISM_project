# PRISM Test Dataset — labeled anomalies

A small synthetic dataset (**10 stores × 10 weeks**, latest week `10-03-2025`) built to check whether PRISM detects anomalies. Every store has a **known planted condition**, so you can confirm PRISM flags exactly the right ones and leaves the healthy "control" stores alone. Targets used are the production **Franchisee** tier: SSS 5.0, OSAT 85, EBITDA 20.8, FSA 93 (hard floor 80).

## What each store is planted to do

| Store | Region | FBC | Area Director | Planted condition | Expected result |
|---|---|---|---|---|---|
| 9001 | Test-West | Alex Rivera | Dana Cole | SSS 6.0% → **1.4%** (sudden drop + below target) | SSS alert — **Critical** |
| 9002 | Test-West | Alex Rivera | Dana Cole | SSS 6.0% → **1.5%** | SSS alert — **Critical** |
| 9003 | Test-West | Alex Rivera | Dana Cole | SSS 6.0% → **1.3%** | SSS alert — **Critical** |
| 9004 | Test-East | Sam Lee | Chris Bell | SSS 6.0% → **1.5%** *and* EBITDA 22% → **11%** | **Multi-metric** (SSS + EBITDA) — Critical |
| 9005 | Test-East | Sam Lee | Chris Bell | Food-safety audit **62** (below the 80 floor) | FSA alert — **Critical** |
| 9006 | Test-East | Sam Lee | Chris Bell | Food-safety audit **88** (below target 93, above floor) | FSA alert — **High** |
| 9007 | Test-East | Sam Lee | Chris Bell | OSAT 90 → **80** (sudden drop, still above target) | OSAT alert — **Moderate** (statistical only) |
| 9010 | Test-North | Jordan Kim | Lee Park | stable, on target | **no alert** (control) |
| 9011 | Test-North | Jordan Kim | Lee Park | stable, on target | **no alert** (control) |
| 9012 | Test-North | Jordan Kim | Lee Park | stable, on target | **no alert** (control) |

## What PRISM should surface

- **8 individual anomalies** across 7 stores: SSS ×4, EBITDA ×1, FSA ×2, OSAT ×1 — with a spread of Critical / High / Moderate severities.
- **6 correlation patterns:**
  - FBC **Alex Rivera** — 3 stores slipping together (9001–9003)
  - FBC **Sam Lee** — 4 struggling stores (9004–9007)
  - Area Director **Dana Cole** — 3-store cluster
  - Area Director **Chris Bell** — 4-store cluster
  - Region **Test-West** on Same-Store Sales — 3-store geographic cluster
  - **Multi-Metric Store 9004** — failing SSS + EBITDA at once
- **Controls 9010–9012:** nothing flagged (no false positives).

## Verified result (PRISM was actually run on this data)

**PASS** — all 8 planted anomalies detected, all 6 patterns found, all 3 controls clean.

One nuance worth knowing: with `max_alerts = 12` and a per-metric cap of 3, four SSS stores compete for three SSS card slots, so **9004's SSS card is trimmed from the individual-card list**. It is *not* lost — it's still detected and still surfaced through the Multi-Metric, Region, FBC and Area-Director patterns. This is PRISM's documented design: **patterns are computed on the full anomaly set before any display trimming**, so a systemic issue is never hidden just because its store didn't make the top cards.

## How to run it

- **Option A — regenerate:** `python gen_data.py` recreates the 8 CSVs in `./data`.
- **Option B — use in the app:** point the app's data folder at these 8 CSVs (or copy them over `data/`), launch the app, open the **Proactive Agent** tab, and click **Simulate Pipeline Run**.

_Note: because these test stores are healthy on OSAT except 9007, and the numbers are deliberately clean, results are fully deterministic run to run (no AI needed — the diagnosis is computed from the data)._
