"""agent_ui.py — Streamlit UI for the Proactive Agent (with patterns + pipeline trigger)"""

import streamlit as st
import pandas as pd
import html
from agent import run_analysis, get_latest_week, narrate_alert, narrate_pattern, _route_recipient
import alert_store
import schema_scan

# Escape any data-/LLM-derived value before it goes into unsafe_allow_html,
# so a store name, audit finding or model output containing HTML can't inject.
esc = html.escape

SEV_STYLES = {
    "Critical": {"bg": "#FDECEC", "accent": "#CE1126", "badge": "#CE1126"},
    "High":     {"bg": "#FFF4E5", "accent": "#E67E00", "badge": "#E67E00"},
    "Moderate": {"bg": "#FFF9E5", "accent": "#C9A100", "badge": "#C9A100"},
}

METHOD_LABEL = {
    "statistical": "📉 Sudden change",
    "threshold": "🎯 Below target",
    "critical_floor": "🚨 Below safety floor",
    "generic": "📈 Generic watch",
}


def _method_badge(m, a):
    """Method label plus the computed miss, so the card states exactly what it
    was compared against and by how much (e.g. '🎯 Below target (-4.0 pts)')."""
    label = METHOD_LABEL.get(m, m)
    if m in ("statistical", "generic") and a.get("stat_delta") is not None:
        return f"{label} ({a['stat_delta']:+.2f} pts)"     # vs recent average (points)
    if m in ("statistical", "generic") and a.get("stat_pct") is not None:
        return f"{label} ({a['stat_pct']:+.0f}%)"          # vs recent average (legacy ratio)
    if m == "threshold" and a.get("target_gap") is not None:
        return f"{label} (-{a['target_gap']:.1f} pts)"      # below target
    if m == "critical_floor":
        score, floor = a.get("latest_value"), a.get("critical_floor")
        if score is not None and floor is not None:
            return f"{label} ({score:g} < {floor:g})"       # audit score vs floor
    return label

PATTERN_ICON = {
    "FBC": "👥",
    "Area Director": "🗺️",
    "Region": "📍",
    "Multi-Metric Store": "🔥",
}


def _agent_css():
    st.markdown("""
    <style>
        .agent-intro {
            background: linear-gradient(135deg, #1F2937 0%, #111827 100%);
            border-radius: 16px; padding: 22px 28px; margin-bottom: 18px; color: #fff;
        }
        .agent-intro h2 { margin: 0 0 4px 0; font-size: 1.4rem; font-weight: 800; color:#fff; }
        .agent-intro p { margin: 0; color: #C9CDD3; font-size: 0.92rem; }

        .pipeline-event {
            background: #0F2A1F; border: 1px solid #1E5C3F; border-left: 5px solid #25C685;
            border-radius: 12px; padding: 14px 18px; margin-bottom: 8px;
            color: #C8F4DE; font-size: 0.9rem; font-family: monospace;
        }
        .pipeline-event b { color: #25C685; }

        /* "What triggers this" architecture strip */
        .trigger-strip {
            background: #F7F8FA; border: 1px solid #E4E7EC; border-radius: 12px;
            padding: 14px 18px; margin-bottom: 16px;
        }
        .trig-label {
            font-size: 0.72rem; font-weight: 700; color: #8A94A6;
            letter-spacing: 0.8px; margin-bottom: 10px;
        }
        .trig-chain { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
        .trig-node {
            background: #FFFFFF; border: 1px solid #DDE1E8; border-radius: 8px;
            padding: 6px 12px; font-size: 0.82rem; color: #3A4252; font-weight: 500;
            white-space: nowrap;
        }
        .trig-node.fire {
            background: #CE1126; border-color: #CE1126; color: #FFFFFF; font-weight: 700;
            box-shadow: 0 2px 8px rgba(206,17,38,0.25);
        }
        .trig-arrow { color: #B0B6C0; font-weight: 700; }
        .trig-note { font-size: 0.78rem; color: #8A94A6; margin-top: 10px; line-height: 1.5; }

        .dispatch-bar {
            background: #EAF1FA; border: 1px solid #BBD3EE; border-radius: 12px;
            padding: 12px 18px; margin: 14px 0 22px 0; font-size: 0.9rem; color: #1F3A5F;
        }
        .dispatch-bar b { color: #2E5C9E; }

        .section-title {
            font-size: 1.05rem; font-weight: 800; color: #1A1A1A;
            margin: 22px 0 4px 0;
        }
        .section-note { font-size: 0.85rem; color: #777; margin-bottom: 14px; }

        /* Pattern cards — visually distinct (dark, premium) */
        .pattern-card {
            background: linear-gradient(135deg, #2A1215 0%, #3D1A1F 100%);
            border-radius: 14px; padding: 18px 22px; margin-bottom: 14px;
            border-left: 6px solid #CE1126; box-shadow: 0 3px 12px rgba(0,0,0,0.12);
        }
        .pattern-head { display:flex; align-items:center; gap:10px; margin-bottom: 8px; }
        .pattern-tag {
            background:#CE1126; color:#fff; font-weight:700; font-size:0.68rem;
            letter-spacing:0.5px; padding:3px 10px; border-radius:20px; text-transform:uppercase;
        }
        .pattern-title { font-weight:700; font-size:1.05rem; color:#FFFFFF; }
        .pattern-meta { color:#E5A9B0; font-size:0.82rem; margin-bottom:12px; }
        .pattern-meta .derived { color:#F0C0C5; font-weight:600; }
        .pattern-body { color:#F3DDE0; font-size:0.92rem; line-height:1.55; margin:6px 0; }
        .pattern-body .lbl { font-weight:700; color:#FFFFFF; }
        .pattern-stores { margin-top:10px; }
        .pattern-stores .chip {
            display:inline-block; background:rgba(255,255,255,0.1); color:#fff;
            border-radius:8px; padding:3px 10px; margin:3px 4px 0 0; font-size:0.76rem;
        }

        .alert-card {
            border-radius: 14px; padding: 18px 20px; margin-bottom: 14px;
            border-left: 6px solid var(--accent); background: var(--bg);
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }
        .alert-head { display:flex; align-items:center; gap:10px; margin-bottom: 8px; flex-wrap: wrap;}
        .sev-badge {
            color:#fff; font-weight:700; font-size:0.72rem; letter-spacing:0.5px;
            padding: 3px 10px; border-radius: 20px; text-transform: uppercase;
        }
        .alert-title { font-weight: 700; font-size: 1.02rem; color:#1A1A1A; }
        .alert-sub { color:#666; font-size:0.85rem; margin-bottom: 12px; }
        .metric-pills { display:flex; gap:8px; flex-wrap:wrap; margin-bottom: 12px; }
        .pill {
            background:#fff; border:1px solid #E3E3E3; border-radius:20px;
            padding:4px 12px; font-size:0.78rem; color:#333; font-weight:500;
        }
        .pill b { color:#CE1126; }
        .reason-block { margin: 6px 0; font-size:0.9rem; line-height:1.5; }
        .reason-block .lbl { font-weight:700; color:#1A1A1A; }
        .resp-chain { margin-top:10px; padding-top:10px; border-top:1px dashed #DDD; }
        .resp-chain .who {
            display:inline-block; background:#fff; border:1px solid #E3E3E3;
            border-radius:8px; padding:4px 10px; margin:3px 4px 0 0; font-size:0.78rem;
        }
        .resp-chain .who b { color:#444; }
        .resp-chain .who span { color:#CE1126; font-weight:600; }

        /* store-card extras */
        .source-badge {
            font-size:0.68rem; font-weight:700; padding:3px 10px; border-radius:20px;
            letter-spacing:0.4px; margin-left:auto;
        }
        .source-ai { background:#E8F0FE; color:#1A56DB; border:1px solid #BBD3EE; }
        .source-fallback { background:#F1F1F1; color:#555; border:1px solid #DDD; }
        .role-chip {
            font-size:0.64rem; font-weight:700; padding:1px 7px; border-radius:10px;
            background:#EEE; color:#555; text-transform:uppercase; letter-spacing:0.3px;
        }
        .role-root { background:#FDE7E9; color:#CE1126; }
    </style>
    """, unsafe_allow_html=True)


def _steps_from_action(text):
    """Split the AI's action ('do X; do Y; do Z') into a list of concrete steps.
    Returns None unless there are at least two, so a single sentence isn't bulleted."""
    parts = [p.strip(" .") for p in str(text or "").split(";") if p.strip(" .")]
    return parts if len(parts) >= 2 else None


def _render_pattern(p, ai_steps=None):
    """Systemic-pattern card, styled like the store card. Colour signals the kind:
    Fleet-wide (company) = purple, Shared Cause (cohort) = blue, co-flag = amber."""
    ptype = p.get("type", "")
    styles = {"Fleet-wide": ("#6D28D9", "#F3EBFD", "FLEET-WIDE"),
              "Shared Cause": ("#1C0087", "#E9F2FC", "SHARED CAUSE")}
    accent, bg, label = styles.get(ptype, ("#B45309", "#FBF1E4", (str(ptype).upper() + " PATTERN").strip()))
    conf = p.get("confidence")
    route = p.get("route_to")
    n_items = len(p.get("items", []))
    chips = "".join(
        f'<span style="display:inline-block;font-size:11px;background:#EEF0F6;color:#333;'
        f'border-radius:6px;padding:2px 8px;margin:3px 4px 0 0;">#{esc(str(s))}</span>'
        for s in (p.get("stores") or [])[:12])
    steps = ""
    if ai_steps:
        lis = "".join(f'<li>{esc(str(s))}</li>' for s in ai_steps)
        steps = (f'<details style="margin-top:8px;"><summary style="cursor:pointer;color:#0F4C81;font-size:0.82rem;">'
                 f'✨ Concrete coordinated steps</summary>'
                 f'<ul style="margin:6px 0 0 0;padding-left:18px;font-size:0.82rem;color:#5A5A66;line-height:1.5;">{lis}</ul></details>')
    meta = f'{p.get("store_count","?")} stores'
    if conf:
        meta += f' · {esc(str(conf))} confidence'
    meta += f' · built from {n_items} signals'
    card = (
        f'<div style="border-left:5px solid {accent};background:#fff;border:.5px solid #E3E1DA;'
        f'border-radius:12px;padding:14px 16px;margin:8px 0;">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap;">'
        f'<div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap;">'
        f'<span style="background:{accent};color:#fff;font-size:10px;font-weight:700;letter-spacing:.05em;'
        f'padding:3px 9px;border-radius:20px;">{esc(label)}</span>'
        f'<span style="font-size:15px;font-weight:600;color:#1A1A22;">{esc(str(p.get("title","")))}</span></div>'
        f'<span style="font-size:11px;color:#8A8A94;white-space:nowrap;">{meta}</span></div>'
        f'<div style="font-size:13px;color:#2A2A33;line-height:1.6;margin:8px 0;">{esc(str(p.get("insight","")))}</div>'
        f'<div style="background:{bg};border-radius:8px;padding:10px 12px;margin:8px 0;">'
        f'<div style="font-size:10px;font-weight:700;letter-spacing:.05em;color:{accent};">RECOMMENDED ACTION</div>'
        f'<div style="font-weight:600;color:#1A1A22;margin-top:2px;">{esc(str(p.get("action","")))}</div></div>'
        + (f'<div style="font-size:12px;color:#5A5A66;margin:4px 0;"><b style="font-weight:600;">Routed to:</b> {esc(str(route))}</div>' if route else "")
        + f'<div style="margin-top:6px;">{chips}</div>'
        + steps
        + '</div>'
    )
    st.markdown(card, unsafe_allow_html=True)


def _render_card(a, ai_steps=None):
    sev = a.get("severity", "Moderate")
    style = SEV_STYLES.get(sev, SEV_STYLES["Moderate"])
    methods = " · ".join(_method_badge(m, a) for m in a.get("methods", []))

    unit = esc(str(a.get("unit", "")))
    pills = [f'<span class="pill">Now: <b>{esc(str(a["latest_value"]))}{unit}</b></span>']
    if a.get("trailing_avg") is not None:
        pills.append(f'<span class="pill">Recent avg: {esc(str(a["trailing_avg"]))}{unit}</span>')
    if a.get("target_value") is not None:
        pills.append(f'<span class="pill">Target: {esc(str(a["target_value"]))}{unit}</span>')
    pills_html = "".join(pills)

    who_html = ""
    for role, name in a.get("responsible", {}).items():
        if name:
            who_html += f'<span class="who"><b>{esc(str(role))}:</b> <span>{esc(str(name))}</span></span>'

    loc = esc(f'{a.get("city","")}, {a.get("region","")} region'.strip(", "))
    finding = (f'<div class="alert-sub">Audit finding: {esc(str(a["finding"]))}</div>'
               if a.get("finding") else "")

    steps_html = ""
    if ai_steps:
        items = "".join(f'<li>{esc(str(s))}</li>' for s in ai_steps)
        steps_html = (
            '<div class="reason-block" style="margin-top:6px;">'
            '<span class="lbl">✨ Concrete steps:</span>'
            f'<ul style="margin:4px 0 0 0; padding-left:20px; line-height:1.5;">{items}</ul>'
            '</div>'
        )

    card_html = (
        f'<div class="alert-card" style="--bg:{style["bg"]}; --accent:{style["accent"]};">'
        f'<div class="alert-head">'
        f'<span class="sev-badge" style="background:{style["badge"]};">{esc(str(sev))}</span>'
        f'<span class="alert-title">Store #{esc(str(a["store_id"]).lstrip("#"))} — {esc(str(a["metric_label"]))}</span>'
        f'</div>'
        f'<div class="alert-sub">{loc} &nbsp;·&nbsp; {methods}</div>'
        f'{finding}'
        f'<div class="metric-pills">{pills_html}</div>'
        f'<div class="reason-block"><span class="lbl">Likely cause:</span> {esc(str(a.get("cause","")))}</div>'
        f'<div class="reason-block"><span class="lbl">Recommended action:</span> {esc(str(a.get("action","")))}</div>'
        f'{steps_html}'
        f'<div class="resp-chain">{who_html}</div>'
        f'</div>'
    )
    st.markdown(card_html, unsafe_allow_html=True)


def _collect_recipients(stores):
    """Who actually gets notified (dispatch bar) — resolved from each store's
    suggested route tier, plus any secondary issue's own tier."""
    people = set()
    for s in stores:
        ctx = s.get("context") or {}
        tier = (s.get("suggested_route") or {}).get("tier") or "FBC"
        people.add(_route_recipient(tier, ctx))
        for si in s.get("secondary_issues") or []:
            people.add(_route_recipient((si.get("suggested_route") or {}).get("tier") or "FBC", ctx))
    return sorted(p for p in people if p and p != "Store Owner")


def _store_message(diag):
    """The email/Teams notification a store's insight WOULD send (composed, not sent).
    Routed by the agent's suggested_route tier (deterministic name lookup), with the
    owner CC'd and any secondary issue routed separately to its own tier."""
    ctx = diag.get("context") or {}
    tier = (diag.get("suggested_route") or {}).get("tier") or "FBC"
    to = _route_recipient(tier, ctx)
    owner = ctx.get("franchise_owner")
    if owner and owner not in to:
        to += f", {owner} (Owner)"
    metrics = ", ".join(diag.get("metric_labels") or [])
    subject = f"[{diag.get('severity','Alert')}] Store #{diag.get('store_id')} — {metrics} need attention"
    rc = diag.get("root_cause")
    rc_txt = rc if isinstance(rc, str) else (rc or {}).get("explanation", "")
    actions = diag.get("actions") or []
    if actions:
        act_txt = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(actions))
    else:
        act_txt = "  " + (diag.get("recommended_action") or "(see card)")
    msg = (
        f"To: {to}\n"
        f"Subject: {subject}\n\n"
        f"{diag.get('headline','')}\n\n"
        f"Root cause: {rc_txt}\n\n"
        f"Recommended action:\n{act_txt}\n"
    )
    sec = diag.get("secondary_issues") or []
    if sec:
        msg += "\nAlso flagged at this store (routed separately):\n"
        for si in sec:
            rec = _route_recipient((si.get("suggested_route") or {}).get("tier") or "FBC", ctx)
            msg += f"  • {si.get('headline','')}  →  {rec}\n"
    return msg + "\n— Jersey Mike's Proactive Insight Agent"


def _render_store_card(diag):
    """ONE card per store: severity + AI/data source, the flagged metrics with
    their numbers, the connected story (root -> downstream), actions, and owners.
    Every data-/LLM-derived value is HTML-escaped before rendering."""
    sev = diag.get("severity", "Moderate")
    style = SEV_STYLES.get(sev, SEV_STYLES["Moderate"])
    sid = esc(str(diag.get("store_id")))
    src = diag.get("source", "fallback")
    src_cls = "source-ai" if src == "ai" else "source-fallback"
    src_lbl = "🤖 AI diagnosis" if src == "ai" else "📊 Data (fallback)"
    metrics_txt = esc(", ".join(diag.get("metric_labels") or []))
    ctx = diag.get("context") or {}
    loc = esc((f"{ctx.get('city','')}, {ctx.get('region','')} region").strip(", "))
    week = esc(str(diag.get("latest_week") or ""))
    label_of = {a.get("metric"): a.get("metric_label") for a in (diag.get("anomalies") or [])}

    # evidence pills — one per flagged metric, with the miss
    pills = []
    for a in diag.get("anomalies") or []:
        unit = esc(str(a.get("unit", "")))
        bit = f'{esc(str(a.get("metric_label")))}: <b>{esc(str(a.get("latest_value")))}{unit}</b>'
        if a.get("target_value") is not None:
            bit += f' (target {esc(str(a.get("target_value")))}{unit})'
        miss = ""
        if a.get("stat_delta") is not None:
            miss = f'{a["stat_delta"]:+.2f} pts vs recent'
        elif a.get("stat_pct") is not None:
            miss = f'{a["stat_pct"]:+.0f}% vs recent'
        if a.get("target_gap") is not None:
            miss = (miss + ", " if miss else "") + f'-{a["target_gap"]:.1f} pts'
        if miss:
            bit += f' <span style="color:#CE1126;">{esc(miss)}</span>'
        pills.append(f'<span class="pill">{bit}</span>')
    pills_html = "".join(pills)

    # peer context for the root metric — store vs FBC cohort
    peer_html = ""
    root_metric = (diag.get("root_cause") or {}).get("metric")
    pe = (diag.get("peer_context") or {}).get(root_metric) if root_metric else None
    if pe and pe.get("fbc_avg") is not None and pe.get("store_value") is not None:
        peer_html = (f'<div class="reason-block"><span class="lbl">Vs peers:</span> '
                     f'this store {esc(str(pe["store_value"]))} vs FBC cohort avg '
                     f'{esc(str(pe["fbc_avg"]))} ({esc(str(pe.get("fbc_store_count","?")))} stores)</div>')

    # the connected story
    chain_items = ""
    for c in diag.get("causal_chain") or []:
        m = c.get("metric_label") or label_of.get(c.get("metric")) or c.get("metric")
        rcls = "role-root" if c.get("role") == "root" else ""
        chain_items += (f'<li><b>{esc(str(m))}</b> '
                        f'<span class="role-chip {rcls}">{esc(str(c.get("role","")))}</span> '
                        f'— {esc(str(c.get("note","")))}</li>')
    chain_html = (f'<div class="reason-block"><span class="lbl">How it connects:</span>'
                  f'<ul style="margin:4px 0 0 0; padding-left:20px; line-height:1.5;">{chain_items}</ul>'
                  f'</div>' if chain_items else "")

    # actions
    acts = diag.get("actions") or []
    act_html = ""
    if acts:
        items = "".join(f"<li>{esc(str(x))}</li>" for x in acts)
        act_html = (f'<div class="reason-block"><span class="lbl">Recommended actions:</span>'
                    f'<ul style="margin:4px 0 0 0; padding-left:20px; line-height:1.5;">{items}</ul>'
                    f'</div>')

    # responsible chain
    who_html = ""
    for role, key in (("Franchise Owner", "franchise_owner"), ("FBC", "fbc"),
                      ("Area Director", "area_director"), ("Regional VP", "regional_vp")):
        if ctx.get(key):
            who_html += f'<span class="who"><b>{esc(role)}:</b> <span>{esc(str(ctx[key]))}</span></span>'

    # When a card falls back, show WHY (the model/parse/validation reason) so it's
    # diagnosable without digging in the terminal.
    fb_note = ""
    if src == "fallback":
        v = diag.get("validation") or {}
        why = esc(str(v.get("ai_error") or v.get("reason") or "AI unavailable"))
        fb_note = (f'<div style="font-size:0.72rem; color:#EE282A; margin:3px 0 2px 0;">'
                   f'⚠ Data fallback — {why}</div>')

    card = (
        f'<div class="alert-card" style="--bg:{style["bg"]}; --accent:{style["accent"]};">'
        f'<div class="alert-head">'
        f'<span class="sev-badge" style="background:{style["badge"]};">{esc(sev)}</span>'
        f'<span class="alert-title">Store #{sid} — {metrics_txt}</span>'
        f'<span class="source-badge {src_cls}">{src_lbl}</span>'
        f'</div>'
        f'<div class="alert-sub">{loc} &nbsp;·&nbsp; week {week}</div>'
        f'{fb_note}'
        f'<div class="reason-block" style="font-weight:600;">{esc(str(diag.get("headline") or ""))}</div>'
        f'<div class="metric-pills">{pills_html}</div>'
        f'{peer_html}'
        f'<div class="reason-block"><span class="lbl">Root cause:</span> '
        f'{esc(str((diag.get("root_cause") or {}).get("explanation") or ""))}</div>'
        f'{chain_html}'
        f'{act_html}'
        f'<div class="resp-chain">{who_html}</div>'
        f'</div>'
    )
    st.markdown(card, unsafe_allow_html=True)


def _render_agent_store_card(diag):
    """v2 AGENT card (store_diagnosis schema): glanceable — headline, one number,
    peer bar, one cause, one action, a quiet 'also worth checking', and the
    evidence tucked into an expander. Used when reasoning.use_tools is on."""
    sev = diag.get("severity", "Moderate")
    style = SEV_STYLES.get(sev, SEV_STYLES["Moderate"])
    sid = esc(str(diag.get("store_id")))
    ctx = diag.get("context") or {}
    loc = esc((f"{ctx.get('city', '')}, {ctx.get('region', '')} region").strip(", "))
    sub = loc
    if diag.get("confidence"):
        sub += f' &nbsp;·&nbsp; {esc(str(diag.get("confidence")))} confidence'
    if diag.get("scope"):
        sub += f' &nbsp;·&nbsp; {esc(str(diag.get("scope")).replace("_", " "))}'

    accent, bg, badge = style["accent"], style["bg"], style["badge"]

    def _fmt(x):
        return f"{x:g}" if isinstance(x, (int, float)) else esc(str(x))

    def _spark(vals):
        vals = [v for v in (vals or []) if isinstance(v, (int, float))]
        if len(vals) < 2:
            return ""
        w, h = 120, 30
        mn, mx = min(vals), max(vals)
        rng = (mx - mn) or 1
        pts = " ".join(f"{i/(len(vals)-1)*w:.1f},{h-(v-mn)/rng*h:.1f}" for i, v in enumerate(vals))
        return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="overflow:visible;">'
                f'<polyline points="{pts}" fill="none" stroke="{accent}" stroke-width="2" stroke-linejoin="round"/></svg>')

    def _bar(v, scale, color):
        try:
            width = max(4, min(100, abs(float(v)) / scale * 100))
        except Exception:
            width = 4
        return (f'<div style="flex:1;height:9px;background:#EAE8E1;border-radius:20px;overflow:hidden;">'
                f'<div style="width:{width:.0f}%;height:100%;background:{color};border-radius:20px;"></div></div>')

    # hero metric + sparkline
    pm = diag.get("primary_metric") or {}
    unit = esc(str(pm.get("unit") or ""))
    metric_html = ""
    if pm.get("value") is not None:
        big = f'<span style="font-size:1.8rem;font-weight:700;color:{accent};">{_fmt(pm.get("value"))}{unit}</span>'
        if pm.get("target") is not None:
            big += f' <span style="color:#6B7280;font-size:0.85rem;">vs {_fmt(pm.get("target"))}{unit} target</span>'
        metric_html = (f'<div style="font-size:0.72rem;color:#5A5A66;margin-top:6px;">{esc(str(pm.get("label","")))}</div>'
                       f'<div style="display:flex;align-items:center;gap:14px;margin-top:2px;">'
                       f'<div>{big}</div><div style="margin-left:auto;">{_spark(diag.get("trend"))}</div></div>')

    # peer bars
    peer_html = ""
    pe = diag.get("peer") or {}
    if pe.get("this_value") is not None and pe.get("cohort_avg") is not None:
        tv, cv = pe.get("this_value"), pe.get("cohort_avg")
        try:
            scale = max(abs(float(tv)), abs(float(cv)), 1) * 1.15
        except Exception:
            scale = 1
        c_this = "#CE1126" if isinstance(tv, (int, float)) and tv < 0 else accent
        note = esc(str(pe.get("note") or ""))
        clabel = esc(str(pe.get("cohort_label", "Cohort")))[:18]
        peer_html = (
            f'<div style="background:#F5F4EF;border-radius:8px;padding:10px 12px;margin:12px 0;">'
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px;">'
            f'<span style="font-size:12px;color:#5A5A66;width:96px;">This store</span>{_bar(tv, scale, c_this)}'
            f'<span style="font-size:13px;font-weight:600;width:44px;text-align:right;">{_fmt(tv)}</span></div>'
            f'<div style="display:flex;align-items:center;gap:10px;">'
            f'<span style="font-size:12px;color:#5A5A66;width:96px;">{clabel}</span>{_bar(cv, scale, "#9AA0AE")}'
            f'<span style="font-size:13px;width:44px;text-align:right;color:#5A5A66;">{_fmt(cv)}</span></div>'
            + (f'<div style="font-size:12px;color:#5A5A66;margin-top:8px;">{note}</div>' if note else "")
            + '</div>')

    rc = diag.get("root_cause")
    rc_txt = esc(str(rc if isinstance(rc, str) else (rc or {}).get("explanation", "")))
    cause_html = f'<div style="font-size:0.9rem;color:#2A2A33;line-height:1.6;margin:8px 0;">{rc_txt}</div>'

    act = esc(str(diag.get("recommended_action") or ""))
    actx = esc(str(diag.get("action_context") or ""))
    next_html = (f'<div style="background:{bg};border-radius:8px;padding:11px 13px;margin:12px 0;">'
                 f'<div style="font-size:0.66rem;font-weight:700;letter-spacing:.05em;color:{accent};">DO THIS NEXT</div>'
                 f'<div style="font-weight:600;color:#1A1A22;margin-top:3px;">{act}</div>'
                 + (f'<div style="font-size:0.8rem;color:#5A5A66;margin-top:3px;">{actx}</div>' if actx else "")
                 + '</div>')

    also = diag.get("also_check") or []
    also_html = (f'<div style="font-size:0.8rem;color:#5A5A66;background:#F5F4EF;border:.5px solid #E3E1DA;'
                 f'border-radius:8px;padding:8px 11px;margin:10px 0;">'
                 f'<b style="font-weight:600;">Also worth checking · </b>{esc("; ".join(str(x) for x in also))}</div>'
                 if also else "")

    # secondary INDEPENDENT issues (multi-root) — compact stacked blocks
    secondary_html = ""
    for si in diag.get("secondary_issues") or []:
        sstl = SEV_STYLES.get(si.get("severity", "Moderate"), SEV_STYLES["Moderate"])
        sact = esc(str(si.get("recommended_action") or ""))
        secondary_html += (
            f'<div style="border-left:3px solid {sstl["accent"]};background:#FAFAF7;border-radius:6px;'
            f'padding:8px 11px;margin:6px 0;">'
            f'<div style="font-size:0.62rem;font-weight:700;letter-spacing:.05em;color:{sstl["accent"]};">'
            f'{esc(str(si.get("severity","Moderate")).upper())}</div>'
            f'<div style="font-weight:600;font-size:0.9rem;color:#1A1A22;">{esc(str(si.get("headline") or ""))}</div>'
            f'<div style="font-size:0.82rem;color:#5A5A66;margin-top:2px;">{esc(str(si.get("cause") or ""))}</div>'
            + (f'<div style="font-size:0.82rem;color:#1A1A22;margin-top:3px;"><b style="font-weight:600;">Do:</b> {sact}</div>' if sact else "")
            + '</div>')
    if secondary_html:
        secondary_html = ('<div style="font-size:0.66rem;font-weight:700;letter-spacing:.05em;color:#5A5A66;'
                          'margin-top:12px;">ALSO FLAGGED AT THIS STORE</div>' + secondary_html)

    who = ""
    for role, key in (("Owner", "franchise_owner"), ("FBC", "fbc"),
                      ("Area Director", "area_director"), ("RVP", "regional_vp")):
        if ctx.get(key):
            who += f'<b>{esc(role)}:</b> {esc(str(ctx[key]))} &nbsp;&nbsp; '
    who_html = (f'<div style="font-size:0.75rem;color:#5A5A66;border-top:.5px solid #E3E1DA;'
                f'padding-top:10px;margin-top:12px;">{who}</div>' if who else "")

    # breakdown: causal chain + evidence + unresolved (off the main face)
    detail = ""
    chain = diag.get("causal_chain") or []
    if chain:
        lis = "".join(f'<li><b>{esc(str(c.get("metric","")))}</b> '
                      f'<span style="color:{accent};font-size:0.68rem;text-transform:uppercase;">{esc(str(c.get("role","")))}</span> — '
                      f'{esc(str(c.get("note","")))}</li>' for c in chain)
        detail += f'<div style="font-size:0.82rem;color:#5A5A66;">How it connects:<ul style="margin:4px 0 8px 0;padding-left:18px;line-height:1.5;">{lis}</ul></div>'
    ev = diag.get("evidence") or []
    if ev:
        lis = "".join(f'<li><code>{esc(str(e.get("tool","")))}</code> — {esc(str(e.get("result","")))}</li>' for e in ev)
        detail += f'<ul style="margin:0;padding-left:18px;font-size:0.82rem;color:#5A5A66;line-height:1.5;">{lis}</ul>'
    unres = diag.get("unresolved") or []
    if unres:
        detail += f'<div style="font-size:0.72rem;color:#8A8A94;margin-top:6px;">Not tested: {esc("; ".join(str(x) for x in unres))}</div>'
    details_html = (f'<details style="margin-top:8px;"><summary style="cursor:pointer;color:#0F4C81;font-size:0.82rem;">'
                    f'View full breakdown — the evidence the agent checked</summary>'
                    f'<div style="margin-top:8px;">{detail}</div></details>' if detail else "")

    header = (f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap;">'
              f'<div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap;">'
              f'<span style="background:{badge};color:#fff;font-size:0.64rem;font-weight:700;letter-spacing:.05em;'
              f'padding:3px 9px;border-radius:20px;">{esc(str(sev).upper())}</span>'
              f'<span style="font-size:1rem;font-weight:700;color:#1A1A22;">Store #{sid}</span>'
              f'<span style="font-size:0.85rem;color:#5A5A66;">{loc}</span></div>'
              f'<div style="font-size:0.72rem;color:#8A8A94;white-space:nowrap;">🤖 Agent{(" · " + esc(str(diag.get("confidence"))) + " confidence") if diag.get("confidence") else ""}</div>'
              f'</div>')
    headline_html = f'<div style="font-size:0.98rem;font-weight:600;color:#1A1A22;line-height:1.5;margin:10px 0 4px;">{esc(str(diag.get("headline") or ""))}</div>'

    card = (f'<div style="border-left:5px solid {accent};padding:2px 2px 2px 14px;">'
            f'{header}{headline_html}{metric_html}{peer_html}{cause_html}{next_html}{also_html}{secondary_html}{who_html}{details_html}'
            f'</div>')
    st.markdown(card, unsafe_allow_html=True)


def _render_insights_table():
    """The store-level insights table the dashboard reads, + a CSV download.
    Reads the persisted store, so it's visible across sessions once a run exists."""
    try:
        rows = alert_store.get_insights()
        total_fb, useful_fb = alert_store.insight_feedback_stats()
    except Exception:
        return
    if not rows:
        return
    with st.expander(f"🗂️ Insights table (dashboard feed) — {len(rows)} stores", expanded=False):
        if total_fb:
            pct = round(useful_fb / total_fb * 100)
            st.markdown(
                f'<div class="dispatch-bar">📈 <b>Value to date:</b> '
                f'{useful_fb}/{total_fb} store insights marked useful ({pct}%)</div>',
                unsafe_allow_html=True)
        df = pd.DataFrame(rows)
        cols = ["store_id", "severity", "source", "metrics", "root_metric",
                "occurrences", "status", "ack_status", "feedback", "last_seen"]
        show = df[[c for c in cols if c in df.columns]].rename(columns={
            "store_id": "Store", "severity": "Severity", "source": "Source",
            "metrics": "Metrics", "root_metric": "Root", "occurrences": "Times seen",
            "status": "Status", "ack_status": "Ack", "feedback": "Feedback",
            "last_seen": "Last seen"})
        st.dataframe(show, width='stretch', hide_index=True)
        st.download_button("⬇️  Download insights CSV",
                           data=df.to_csv(index=False).encode("utf-8"),
                           file_name="insights_latest.csv", mime="text/csv")
        st.caption("This is the exact table a Power BI dashboard would read. In production "
                   "it is written to the Fabric Gold layer instead of a local CSV.")


def _render_history_panel():
    """Persisted alert history + recurrence + all-time feedback stat.

    Always visible (reads from the on-disk store, independent of the current
    session), so the tool has a reason to be checked repeatedly, not once.
    """
    try:
        hist = alert_store.get_alerts()
        total_fb, useful_fb = alert_store.feedback_stats()
    except Exception as e:
        with st.expander("📜 Alert history & recurrence", expanded=False):
            st.caption(f"History unavailable (storage error): {e}")
        return
    with st.expander(f"📜 Alert history & recurrence — {len(hist)} tracked", expanded=False):
        if total_fb:
            pct = round(useful_fb / total_fb * 100)
            st.markdown(
                f'<div class="dispatch-bar">📈 <b>Value to date:</b> '
                f'{useful_fb}/{total_fb} alerts marked useful ({pct}%)</div>',
                unsafe_allow_html=True,
            )
        if not hist:
            st.caption("No history yet — run the agent to start tracking alerts over time.")
            return
        hdf = pd.DataFrame(hist)[
            ["store_id", "metric_label", "last_severity", "occurrences",
             "status", "first_seen", "last_seen", "feedback"]
        ].rename(columns={
            "store_id": "Store", "metric_label": "Metric", "last_severity": "Severity",
            "occurrences": "Times seen", "status": "Status",
            "first_seen": "First seen", "last_seen": "Last seen", "feedback": "Feedback",
        })
        st.dataframe(hdf, width='stretch', hide_index=True)

        open_keys = [h["alert_key"] for h in hist if h["status"] == "open"]
        if open_keys:
            csel, cbtn = st.columns([3, 1])
            sel = csel.selectbox("Mark an alert resolved", ["—"] + open_keys, key="resolve_sel")
            if cbtn.button("Resolve", key="resolve_btn") and sel != "—":
                alert_store.set_status(sel, "resolved")
                st.success(f"Marked {sel} resolved.")
                st.rerun()


def _render_schema_assist():
    """Auto-assist: surface numeric columns that aren't being watched, and help
    the user write a prism_config.yaml block to start watching one."""
    try:
        cands = schema_scan.scan_unwatched_metrics()
    except Exception:
        return
    if not cands:
        return
    with st.expander(f"🔎 {len(cands)} numeric columns not being watched", expanded=False):
        st.caption("PRISM only watches the metrics listed in prism_config.yaml. These numeric "
                   "columns in Fact_StoreWeekly aren't watched yet. Pick one to generate a config "
                   "block, paste it under `metrics:` and restart. (Or enable `generic_watch` in the "
                   "YAML to statistically trend all of them as a shallow catch-all.)")
        st.write("  ".join(f"`{c}`" for c in cands))
        col = st.selectbox("Generate a config block for", ["—"] + cands, key="assist_col")
        if col != "—":
            c1, c2, c3 = st.columns(3)
            direction = c1.selectbox("Direction", ["down_is_bad", "up_is_bad"], key="assist_dir")
            thr = c2.number_input("Sudden-drop %", value=20.0, step=1.0, key="assist_thr")
            gap = c3.number_input("Below-target buffer", value=3.0, step=0.5, key="assist_gap")
            tgt = st.text_input("Target column in Ref_Targets (blank if none)", key="assist_tgt")
            st.code(schema_scan.suggest_yaml_block(col, direction, thr, gap, tgt), language="yaml")


def render_agent_panel():
    _agent_css()

    st.markdown(
        '<div class="agent-intro">'
        '<h2>🤖 Proactive Insight Agent</h2>'
        '<p>Runs as the final step of the data pipeline. The moment fresh data lands, '
        'it scans every store, correlates anomalies across the org to find systemic '
        'patterns, and routes recommended actions to the people responsible — '
        'before anyone opens a dashboard.</p>'
        '</div>',
        unsafe_allow_html=True
    )

    # ── "What triggers this" architecture strip ──
    # Makes it visually clear the agent is a PIPELINE STEP that fires automatically.
    # The demo button below stands in for the highlighted pipeline event.
    st.markdown(
        '<div class="trigger-strip">'
        '<div class="trig-label">HOW IT RUNS IN PRODUCTION — no human in the loop</div>'
        '<div class="trig-chain">'
        '<span class="trig-node">📥 Source data</span>'
        '<span class="trig-arrow">→</span>'
        '<span class="trig-node">🥉 Bronze</span>'
        '<span class="trig-arrow">→</span>'
        '<span class="trig-node">🥈 Silver</span>'
        '<span class="trig-arrow">→</span>'
        '<span class="trig-node">🥇 Gold layer write</span>'
        '<span class="trig-arrow">→</span>'
        '<span class="trig-node fire">🤖 Agent fires automatically</span>'
        '<span class="trig-arrow">→</span>'
        '<span class="trig-node">📋 Alerts &amp; patterns</span>'
        '</div>'
        '<div class="trig-note">The button below simulates the highlighted step — '
        'in production the completed Gold-layer pipeline run triggers the agent with no manual click.</div>'
        '</div>',
        unsafe_allow_html=True
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        run = st.button("▶  Simulate Pipeline Run", type="primary", width='stretch')
    with col2:
        st.caption("In production the agent fires automatically when the Gold-layer "
                   "pipeline completes. Click to simulate a new week of data landing. "
                   "Each alert below has its own Data / AI switch for the wording.")

    _render_history_panel()
    _render_insights_table()
    _render_schema_assist()

    if run:
        latest_week = get_latest_week() or "latest"
        # Show the pipeline trigger event (makes the 'push' behavior visible).
        # The week shown is the real most-recent week in the data, not hardcoded.
        st.markdown(
            '<div class="pipeline-event">📥 <b>PIPELINE EVENT</b> &nbsp; '
            f'Gold-layer refresh completed · Week {esc(str(latest_week))} ingested · '
            '<b>Agent triggered automatically →</b></div>',
            unsafe_allow_html=True
        )
        with st.spinner("Agent scanning all stores and correlating signals..."):
            result = run_analysis(reason=True)   # store-level AI diagnosis + fallback
        st.session_state["agent_result"] = result
        # Persist this run: per-metric history AND the store-level insights table
        # (which also refreshes the CSV mirror the dashboard reads).
        try:
            alert_store.save_run(result)
            alert_store.persist_insights(result)
        except Exception as e:
            st.warning(f"Could not save run to history: {e}")

    result = st.session_state.get("agent_result")
    if result is None:
        return

    patterns = result.get("patterns", [])
    stores = result.get("stores", [])

    if not patterns and not stores:
        st.success("✅ No anomalies detected. Every store is within normal range and meeting targets.")
        return

    # Routing confirmation — honest: the agent RESOLVES who is accountable.
    # It does not transmit; the message is composed and viewable per store.
    recipients = _collect_recipients(stores)
    if recipients:
        rec_txt = esc(", ".join(recipients[:6]))
        more = f" +{len(recipients)-6} more" if len(recipients) > 6 else ""
        st.markdown(
            f'<div class="dispatch-bar">📧 <b>Routed to responsible owners &amp; regional leads:</b> '
            f'{rec_txt}{more}'
            f'<br><span style="font-size:0.8rem;color:#5A7BA8;">Each alert is composed and ready to send — '
            f'in production this connects to email or Teams.</span></div>',
            unsafe_allow_html=True
        )

    # ── PATTERNS FIRST (the differentiator) ──
    if patterns:
        st.markdown('<div class="section-title">🧩 Systemic Patterns Detected</div>', unsafe_allow_html=True)
        st.markdown('<div class="section-note"><b>What the agent adds</b> — it takes the flagged signals from the '
                    'store cards below and connects them across stores, metrics, and the org hierarchy to surface '
                    'systemic issues. A single-metric dashboard cannot do this.</div>',
                    unsafe_allow_html=True)
        for p in patterns:
            pkey = f'{p.get("type")}|{p.get("key")}'
            p.setdefault("insight_data", p.get("insight", ""))
            p.setdefault("action_data", p.get("action", ""))
            # Insight + action are always the deterministic (data) values.
            p["insight"], p["action"] = p["insight_data"], p["action_data"]
            # AI adds concrete coordinated steps alongside (lazy + cached, no toggle);
            # falls back to the data action (no steps) if the model is off/unavailable.
            pai = st.session_state.get(f"pai_{pkey}")
            if pai is None:
                with st.spinner("Adding concrete steps…"):
                    pai = narrate_pattern(p)
                if pai and pai[1] != p["action_data"]:   # cache only genuine AI output; a fallback retries next run
                    st.session_state[f"pai_{pkey}"] = pai
            pat_steps = _steps_from_action(pai[1]) if (pai and pai[1] != p["action_data"]) else None
            with st.container(border=True):
                _render_pattern(p, ai_steps=pat_steps)

    # ── STORE INSIGHTS (one card per store — the primary view) ──
    if stores:
        crit = sum(1 for s in stores if s.get("severity") == "Critical")
        high = sum(1 for s in stores if s.get("severity") == "High")
        st.markdown('<div class="section-title">🏪 Store Insights (one card per store)</div>',
                    unsafe_allow_html=True)
        st.markdown(f'<div class="section-note">Each store\'s flagged metrics are diagnosed '
                    f'together as ONE connected story — the model reasons across them and the '
                    f'data validates every number (the badge shows AI vs deterministic fallback). '
                    f'{len(stores)} stores · 🔴 {crit} Critical · 🟠 {high} High</div>',
                    unsafe_allow_html=True)
        for s in stores:
            with st.container(border=True):
                if s.get("source") == "agent" and s.get("primary_metric"):
                    _render_agent_store_card(s)   # v2 agent card (store_diagnosis schema)
                else:
                    _render_store_card(s)
                key = f"store_{s.get('store_id')}"
                # 👍/👎 and Acknowledge all write to the insight_actions table — the
                # same write-back a Power App / Teams button performs in the dashboard.
                cup, cdown, cack, cexp, cfb = st.columns([1, 1.2, 1.5, 1.4, 3])
                if cup.button("👍 Useful", key=f"up_{key}"):
                    try:
                        alert_store.record_insight_feedback(s.get("store_id"), "useful")
                        st.session_state[f"fb_{key}"] = "useful"
                    except Exception as e:
                        st.warning(f"Couldn't save feedback: {e}")
                if cdown.button("👎 Not useful", key=f"down_{key}"):
                    try:
                        alert_store.record_insight_feedback(s.get("store_id"), "not_useful")
                        st.session_state[f"fb_{key}"] = "not_useful"
                    except Exception as e:
                        st.warning(f"Couldn't save feedback: {e}")
                if cack.button("✓ Acknowledge", key=f"ack_{key}"):
                    try:
                        alert_store.set_insight_action(s.get("store_id"),
                                                       ack_status="acknowledged", ack_by="streamlit")
                        st.session_state[f"ack_{key}"] = True
                    except Exception as e:
                        st.warning(f"Couldn't save acknowledgement: {e}")
                if cexp.button("💬 Explain", key=f"exp_{key}"):
                    try:
                        import chat_assistant
                        q = (f"Explain why store #{s.get('store_id')} is flagged "
                             f"and what PRISM recommends.")
                        st.session_state.setdefault("messages", [])
                        st.session_state.messages.append({"role": "user", "content": q})
                        res = chat_assistant.answer_prism(q, st.session_state.messages)
                        st.session_state.messages.append(
                            {"role": "assistant", "content": res.get("answer", "")})
                        st.toast("Answered in 💬 Ask PRISM — open it (bottom-right) to read.")
                    except Exception as e:
                        st.warning(f"Couldn't queue explanation: {e}")
                _fb = st.session_state.get(f"fb_{key}")
                _ack = st.session_state.get(f"ack_{key}")
                if _fb or _ack:
                    bits = []
                    if _fb:
                        bits.append("👍 Useful" if _fb == "useful" else "👎 Not useful")
                    if _ack:
                        bits.append("✓ Acknowledged")
                    cfb.caption("  ·  ".join(bits))
                with st.expander("📄  View the alert message this would send"):
                    st.code(_store_message(s), language="text")