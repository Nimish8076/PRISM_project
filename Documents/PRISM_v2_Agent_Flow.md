# PRISM v2 — The Agentic Investigation Flow (LLM as the brain)

*How the diagnosis works when the LLM investigates with tools, instead of narrating a fixed packet. This is the Layer-1 design: LLM + analysis tools + raw-data tools.*

## The flow at a glance

```
1 Detection (rules, no AI)  →  flags a store
        ↓
2 LLM decides what to check  ←──────────────┐
        ↓                                    │
   [ analysis tool for it? ]                 │
     yes ↓            no ↓                    │
3a Analysis tool     3b Raw-data tool         │   "not confident yet —
   (measurement)       (raw rows)             │    investigate again"
        ↓  (reads the store data)  ↓          │
4 LLM reads & interprets  ────────────────────┘
        ↓
   [ enough to explain the store? ]  — no → back to 2
        ↓ yes
5 LLM writes the diagnosis (cause · chain · action)
        ↓
6 Guard — every cited number checked   (fails → deterministic fallback)
        ↓
7 Final card → owner & dashboard
```

## Step by step

**1 · Detection (rules, no AI).** Unchanged from today. Deterministic checks (point-drop vs the store's own trend, target gaps, FSA floor) decide *which* stores are genuinely off. Detection stays rule-based so "what counts as a problem" is never the model's call.

**2 · LLM decides what to check.** The flagged store lands with the LLM. It forms a hypothesis and picks the single next question to ask — e.g. "is this sales drop fewer customers, or smaller orders?" *Choosing the question is the part a narrator cannot do.*

**3 · It calls a tool — two kinds:**

- **3a · Analysis tool (the common path).** Answers a specific question by doing a calculation and returns a **measurement, not a verdict** — `decompose_sss` → "transactions −16%, ticket flat," *not* "it's a traffic problem." Reads the store data under the hood. Written and verified once, trusted after.
- **3b · Raw-data tool (the fallback).** When no analysis tool fits the question, the LLM drops to the raw rows and looks for itself (`get_store_weeks`, `list_columns`). This is the door that stops the agent being boxed into pre-built KPIs — and it's how you spot which new tools are worth building.

**4 · LLM reads & interprets.** The tool returned numbers; the LLM turns them into meaning ("transactions fell, ticket didn't → traffic, not pricing"). Every "therefore" lives here, not in the tool.

**The loop.** After interpreting, the LLM hits **"enough to explain the store?"** If **no**, it returns to step 2 and asks the next question — now informed by what it just learned. A different store yields a different path. If **yes**, it exits the loop.

**5 · LLM writes the diagnosis.** Root cause, causal chain, recommended action — synthesised from the whole trail, including claims no single tool made (e.g. "weak Service is the *leading* driver").

**6 · Guard.** Every number the LLM cites is validated against the tool results. If it doesn't hold up, PRISM falls back to the deterministic diagnosis instead of publishing something shaky.

**7 · Final card.** Cause · evidence · action — routed to the owner and fed to the dashboard.

## Deciding when it's "done" (the stop rule)

The "enough to explain the store?" decision is **not** the model's gut feeling alone. It's the LLM proposing to stop, wrapped in structure and hard limits:

1. **Definition of done (the main lever).** A complete diagnosis must contain: a named root cause, evidence for it, store-specific-vs-market settled, the obvious alternatives ruled out, and a recommended action. The loop continues until those boxes are filled. The **playbook's cause list is exactly the set of alternatives** it must knock down before it may stop.
2. **LLM confidence, justified.** The final answer carries a confidence score and a one-line reason. Low confidence → another round.
3. **Hard caps.** A max number of tool calls (≈6–8), plus "stop if the last call didn't change the hypothesis, or it's repeating." On the cap it concludes with what it has and lowers confidence.
4. **The guard as backstop.** If it stops too early with a thin result, the guard can reject it (weak/failing numbers) and fall back to the deterministic diagnosis.

Two notes: the bar can **scale with severity** (a Critical store earns a deeper dig than a Moderate one), and **"can't rule this out — no data" is a valid stop** (it says so, and that gap feeds the new-tool / new-data list).

## Worked example — a real dry run on store #8019

Real numbers from the data (FBC **Marcus Webb** · Northeast · week of 25-05-2026). Notice the data **overturns the obvious assumption** — which is the whole reason you let the agent investigate instead of narrate.

**Detection (rules):** SSS = 0.1 vs its 8-week trailing 0.91 → a **−0.81-pt** drop (over the 0.73 flag) and far below the 5.0 target → flagged **High** (both methods fire).

1. **Decide:** "soft sales — fewer customers or smaller orders?" → `decompose_sss(8019)`
   - returns: transactions **2431 vs 2294 (+137, UP)**; avg ticket **15.46 vs 15.83 (−0.37, down)**
   - LLM: "traffic is actually *up* — this is **ticket-led, not a traffic problem**." *(assumption overturned)*
2. **Decide:** "ticket-led — just this store, or the area?" → `compare_to_peers(8019, SSS)`
   - returns: 8019 = 0.1; Marcus Webb FBC peer avg = **1.4**; peer range −0.5 … 4.3
   - LLM: "8019 is at the low end, but the whole FBC group is soft → **partly cohort-wide, not purely store-specific**."
3. **Decide:** "new or long-running?" → `trend(8019, SSS, 12)`
   - returns: 1.1, 1.2, 1.6, 1.0, 0.5, 0.9, 0.9, 0.7, 1.1, 0.6, 0.1
   - LLM: "hovered near 1 for months → **chronic softness**, not a sudden collapse."
4. **Decide (hypothesis):** "ticket down — is guest experience slipping, or is it value/pricing?" → `osat_breakdown(8019)`
   - returns: OSAT 69 (stable); Food 3.9, **Service 4.0 (steady)**, Cleanliness 4.0, **Value 3.8 (lowest)**
   - LLM: "guest experience is stable — **not a service problem**. Value is the weakest driver → points to **value / price perception**, consistent with the ticket softness."
5. **Raw-data door:** "confirm the ticket-vs-traffic pattern week by week" → `get_store_weeks(8019, 12)`
   - returns the raw grid → LLM confirms: visits rising, ticket easing, OSAT flat — the story holds.
6. **Enough?** Definition-of-done filled — root cause (ticket-led soft SSS) ✓, evidence ✓, market-vs-store (cohort-wide) ✓, alternatives ruled out (traffic ✓, service ✓) ✓, action ✓, confidence high → **stop.**

**Diagnosis written:** *"Same-store sales are chronically soft and dipped to 0.1% this week — but it's **ticket-led, not traffic** (visits are up; average ticket is down). It's **not a lone-store issue** — the whole Marcus Webb FBC cohort is soft (avg 1.4%), with 8019 at the low end. Guest experience is stable; **Value is the weakest driver**, consistent with price/value perception. Action: handle at the **FBC / area level** — promo mix, attachment/upsell, value messaging across the cohort — not a single-store service fix."*

Every tool returned only **measurements**; the conclusions, the order of questions, and the synthesis (ticket + cohort + value) were the LLM's — and they contradicted the "traffic + service" story you'd have *guessed*. That contradiction is the value.

## Two rules that keep the LLM a brain (not a narrator)

1. **Tools return measurements, never verdicts.** The moment a tool returns "the cause is X," the thinking has moved out of the LLM and back into hardcoded logic — and you're back to an if/else system. Keep tools dumb and factual.
2. **Prefer an analysis tool; drop to raw data only when none fits.** Analysis tools are verified and clean; the raw-data door is the safety valve that guarantees the agent can always go look for itself.

## Where this sits vs. the rest

- **Now (prototype):** build this — LLM + ~5–6 analysis tools + the raw-data door + the loop — over the current CSVs. Keep metric definitions in one place so tools are swappable later.
- **Later (Fabric):** a semantic layer slots in *beneath the tools* — the tools stop hand-writing SQL and ask the Power BI model instead, so PRISM and the dashboards share one definition of truth. The agent loop above does not change.
- **Open SQL** stays in the human-in-the-loop "Ask PRISM" chat, where a person reviews the answer.
