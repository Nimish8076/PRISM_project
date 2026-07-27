"""ground.py — the GROUND step: keep the AI honest, or fall back.

Given the evidence packet, the store's anomalies, and the AI diagnosis from
reason.py, this:
  1. Rejects the AI answer if it names a metric that isn't in the evidence.
  2. Checks every number the AI cited against the numbers actually in the packet;
     if too many are unverifiable, it rejects the answer.
  3. Enforces severity hard-rules deterministically: the AI may RAISE severity but
     never lower it below what diagnose.py would assign, and any food-safety
     floor breach is forced to Critical.
  4. Falls back to a deterministic, store-level diagnosis built from diagnose.py
     whenever the AI is missing or rejected.

Net effect: the model can reason and correlate across metrics, but it cannot
invent a metric or a number, and it can never downplay a hard-rule severity.
Every returned diagnosis carries a `source` ("ai" | "fallback") and a
`validation` record so the UI can be transparent about which path was used.
"""
import diagnose

_SEV_RANK = {"Critical": 0, "High": 1, "Moderate": 2}
_ALLOWED_SEV = set(_SEV_RANK)


# ── validation helpers ───────────────────────────────────────────────────────
def _close(a, b):
    """True if two numbers match within a small tolerance. Magnitude-aware, so a
    cited '75' still matches a stored '-75' (models often drop the sign)."""
    try:
        a, b = float(a), float(b)
    except Exception:
        return False
    tol = max(0.1, abs(b) * 0.02)
    return abs(a - b) <= tol or abs(abs(a) - abs(b)) <= tol


def _packet_numbers(packet):
    """Every number that legitimately appears in the evidence packet."""
    nums = []
    for a in packet.get("anomalies", []):
        for k in ("latest_value", "trailing_avg", "target_value", "stat_pct",
                  "target_gap", "floor_gap", "critical_floor", "severity_score"):
            if a.get(k) is not None:
                nums.append(a[k])
    for k, v in (packet.get("weekly_trend") or {}).items():
        if k == "weeks":
            continue
        nums.extend([x for x in v if x is not None])
    for v in (packet.get("osat_subscores") or {}).values():
        if v is not None:
            nums.append(v)
    fsa = packet.get("fsa") or {}
    if fsa.get("score") is not None:
        nums.append(fsa["score"])
    for entry in (packet.get("peer_context") or {}).values():
        for vv in entry.values():
            if isinstance(vv, (int, float)):
                nums.append(vv)
    return nums


def _valid_metrics(packet):
    """Metric keys/labels the AI is allowed to name."""
    keys = set()
    for a in packet.get("anomalies", []):
        if a.get("metric"):
            keys.add(a["metric"])
        if a.get("metric_label"):
            keys.add(a["metric_label"])
    for k in (packet.get("weekly_trend") or {}):
        if k != "weeks":
            keys.add(k)
    return keys


def _worst_det_severity(anomalies):
    """Most severe severity diagnose.py would assign across the store's anomalies."""
    worst = "Moderate"
    for a in anomalies:
        diagnose.diagnose_alert(a)   # sets a['severity'] (and cause/action/driver)
        if _SEV_RANK.get(a.get("severity", "Moderate"), 2) < _SEV_RANK[worst]:
            worst = a["severity"]
    return worst


def _fsa_floor_breach(anomalies):
    return any(a.get("metric") == "FSA_Score"
               and "critical_floor" in (a.get("methods") or [])
               for a in anomalies)


# ── deterministic fallback (store-level, built from diagnose.py) ─────────────
def _fallback(packet, anomalies, reason="AI unavailable"):
    for a in anomalies:
        diagnose.diagnose_alert(a)
    ranked = sorted(anomalies,
                    key=lambda a: (_SEV_RANK.get(a.get("severity", "Moderate"), 2),
                                   -(a.get("severity_score") or 0)))
    root = ranked[0] if ranked else {}
    chain, actions = [], []
    for a in ranked:
        chain.append({"metric": a.get("metric"),
                      "metric_label": a.get("metric_label"),
                      "role": "root" if a is root else "contributing",
                      "note": a.get("cause", "")})
        if a.get("action") and a["action"] not in actions:
            actions.append(a["action"])
    labels = ", ".join(a.get("metric_label", a.get("metric", "")) for a in ranked)
    return {
        "store_id": packet.get("store_id"),
        "severity": root.get("severity", "Moderate") if root else "Moderate",
        "headline": f"Store #{packet.get('store_id')}: {labels} flagged",
        "root_cause": {"metric": root.get("metric"),
                       "explanation": root.get("cause", "")},
        "causal_chain": chain,
        "actions": actions[:4],
        "confidence": None,
        "source": "fallback",
        "validation": {"reason": reason},
    }


def _normalize_ai(ai):
    """Coerce the model's output into the exact shapes downstream code expects.
    A forced tool schema guides the model but does not guarantee shape — a field
    like root_cause can still come back as a bare string — so we normalize here,
    once, so nothing downstream (validation, UI, persistence) can crash on a type."""
    if not isinstance(ai, dict):
        return None
    out = dict(ai)

    rc = out.get("root_cause")
    if isinstance(rc, str):
        rc = {"metric": None, "explanation": rc}
    elif not isinstance(rc, dict):
        rc = {}
    rc.setdefault("metric", None)
    rc.setdefault("explanation", "")
    out["root_cause"] = rc

    chain = []
    for c in (out.get("causal_chain") or []):
        if isinstance(c, dict):
            chain.append({"metric": c.get("metric"), "role": c.get("role", ""),
                          "note": str(c.get("note", "") or "")})
        elif isinstance(c, str):
            chain.append({"metric": None, "role": "", "note": c})
    out["causal_chain"] = chain

    acts = out.get("actions")
    if isinstance(acts, str):
        acts = [acts]
    elif not isinstance(acts, list):
        acts = []
    out["actions"] = [str(a) for a in acts]

    cited = []
    for c in (out.get("cited_values") or []):
        if isinstance(c, dict) and c.get("value") is not None:
            cited.append(c)
        elif isinstance(c, (int, float)):
            cited.append({"label": "", "value": c})
    out["cited_values"] = cited

    if not isinstance(out.get("headline"), str):
        out["headline"] = str(out.get("headline") or "")
    return out


# ── public entry point ───────────────────────────────────────────────────────
def ground(packet, anomalies, ai):
    """Validate + harden the AI diagnosis, or fall back. Returns the final dict."""
    ai = _normalize_ai(ai)
    if ai is None:
        return _fallback(packet, anomalies, reason="No AI diagnosis")

    # 1. metric existence
    valid = _valid_metrics(packet)
    named = []
    rc = (ai.get("root_cause") or {}).get("metric")
    if rc:
        named.append(rc)
    named += [c.get("metric") for c in (ai.get("causal_chain") or []) if c.get("metric")]
    invented = list(dict.fromkeys(m for m in named if m not in valid))
    if invented:
        return _fallback(packet, anomalies,
                         reason=f"AI named unknown metric(s): {invented}")

    # 2. number validation
    allowed = _packet_numbers(packet)
    cited = [c for c in (ai.get("cited_values") or [])
             if isinstance(c, dict) and c.get("value") is not None]
    unverified = [c for c in cited if not any(_close(c["value"], p) for p in allowed)]
    if cited and len(unverified) / len(cited) >= 0.5:
        fb = _fallback(packet, anomalies,
                       reason="AI cited too many unverifiable numbers")
        fb["validation"]["unverified"] = unverified
        return fb

    # 3. severity hard-rules (AI may raise, never lower; FSA floor => Critical)
    ai_sev = ai.get("severity") if ai.get("severity") in _ALLOWED_SEV else "Moderate"
    worst_det = _worst_det_severity(anomalies)
    final_sev, overridden = ai_sev, False
    if _SEV_RANK[worst_det] < _SEV_RANK[final_sev]:
        final_sev, overridden = worst_det, True
    if _fsa_floor_breach(anomalies) and final_sev != "Critical":
        final_sev, overridden = "Critical", True

    ai["severity"] = final_sev
    ai["source"] = "ai"
    ai.setdefault("store_id", packet.get("store_id"))
    ai["validation"] = {
        "unverified": unverified,
        "severity_overridden": overridden,
        "det_severity_floor": worst_det,
    }
    return ai


def ground_agent(ai, anomalies):
    """Grounding for the v2 AGENTIC store_diagnosis (tools already sourced every
    number, so there is no packet to re-check). We enforce the deterministic
    severity floor: the AI may RAISE severity but never lower a hard rule, and an
    FSA floor breach is forced to Critical."""
    if not isinstance(ai, dict):
        return ai
    try:
        from diagnose import _severity as _det_sev
    except Exception:
        _det_sev = None
    worst = "Moderate"
    for a in (anomalies or []):
        try:
            s = _det_sev(a) if _det_sev else "Moderate"
        except Exception:
            s = "Moderate"
        if _SEV_RANK.get(s, 2) < _SEV_RANK[worst]:
            worst = s
    final = ai.get("severity") if ai.get("severity") in _ALLOWED_SEV else "Moderate"
    overridden = False
    if _SEV_RANK[worst] < _SEV_RANK[final]:
        final, overridden = worst, True
    if _fsa_floor_breach(anomalies) and final != "Critical":
        final, overridden = "Critical", True
    # a genuinely independent secondary issue can be worse than the primary —
    # the overall card severity is the worst of them
    for s in (ai.get("secondary_issues") or []):
        sv = s.get("severity")
        if sv in _ALLOWED_SEV and _SEV_RANK[sv] < _SEV_RANK[final]:
            final, overridden = sv, True
    ai["severity"] = final
    ai.setdefault("source", "agent")
    v = ai.get("validation") if isinstance(ai.get("validation"), dict) else {}
    v.update({"severity_overridden": overridden, "det_severity_floor": worst})
    ai["validation"] = v
    return ai


# ── manual test: `python ground.py` (works with NO API key via stub + fallback)
if __name__ == "__main__":
    import json
    from data_connector import get_connection
    import evidence
    import agent
    import reason
    from config import METRICS, REASONING_MODEL

    get_connection()
    targets = agent._get_targets()
    found = []
    for m, cfg in METRICS.items():
        found.extend(agent._detect_weekly_metric(m, cfg, targets))
    found.extend(agent._detect_fsa(targets))
    grouped = evidence.group_by_store(found)
    pick = next((s for s, i in grouped.items() if len({x['metric'] for x in i}) >= 2),
                None) or next(iter(grouped))
    anomalies = grouped[pick]
    packet = evidence.build_store_packet(pick, anomalies)
    print(f"\n### Store #{pick}: {len({a['metric'] for a in anomalies})} metrics flagged\n")

    print(f"--- Live AI ({REASONING_MODEL}) ---")
    live = reason.reason_store(packet)
    if live:
        print("AI returned JSON; grounded result:")
        print(json.dumps(ground(packet, [dict(a) for a in anomalies], live),
                         indent=2, default=str))
    else:
        print("AI unavailable (no/invalid key or model id) — using a stub below "
              "to exercise validation, then the pure fallback.\n")

    stub = {
        "headline": f"Traffic-led sales drop is dragging margin at #{pick}",
        "severity": "Moderate",  # deliberately too low — hard rule should raise it
        "root_cause": {"metric": "SSS_Pct",
                       "explanation": "transactions fell while ticket held"},
        "causal_chain": [
            {"metric": "SSS_Pct", "role": "root", "note": "sales down sharply vs recent average"},
            {"metric": "EBITDA_Pct", "role": "symptom", "note": "margin follows the sales decline"}],
        "actions": ["Verify peak-daypart staffing", "Launch a local LTO", "Review labor vs volume"],
        "confidence": 0.7,
        "cited_values": [{"label": "SSS latest", "value": 1.5},
                         {"label": "EBITDA latest", "value": 11.0}],
    }
    print("--- Case A: valid AI diagnosis (severity too low -> hardened) ---")
    print(json.dumps(ground(packet, [dict(a) for a in anomalies], dict(stub)),
                     indent=2, default=str))

    print("\n--- Case B: AI invents a metric -> rejected, falls back ---")
    bad = dict(stub)
    bad["root_cause"] = {"metric": "FootTraffic_Index", "explanation": "made up"}
    bad["causal_chain"] = [{"metric": "FootTraffic_Index", "role": "root", "note": "invented"}]
    res_b = ground(packet, [dict(a) for a in anomalies], bad)
    print(f"source={res_b['source']}, severity={res_b['severity']}, "
          f"reason={res_b['validation'].get('reason')}")

    print("\n--- Case C: no AI (pure deterministic fallback) ---")
    print(json.dumps(ground(packet, [dict(a) for a in anomalies], None),
                     indent=2, default=str))
