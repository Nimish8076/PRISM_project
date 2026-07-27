"""config.py — all of PRISM's tunable knobs in one place.

Values are read from `prism_config.yaml` (edit that file to tune PRISM without
touching any code). If the YAML file or PyYAML is missing or malformed, PRISM
falls back to the built-in DEFAULTS below and prints a warning — so it always
runs. Top-level keys present in the YAML override the corresponding default.
"""
import os

_YAML_PATH = os.path.join(os.path.dirname(__file__), "prism_config.yaml")

# ── Built-in defaults (identical to the shipped prism_config.yaml) ──────────
DEFAULTS = {
    # which Ref_Targets tier stores are graded against
    "target_persona": "Franchisee",
    # max individual alert cards shown per run
    "max_alerts": 12,
    # LLM used ONLY for the optional per-card "AI" narration
    "narrator_model": "claude-sonnet-4-6",
    # LLM used for the store-level agentic diagnosis (reason.py)
    "reasoning_model": "claude-sonnet-4-6",
    # LLM used for the chat tab (natural-language -> SQL, and answer wording)
    "chat_model": "claude-sonnet-4-6",

    # weekly trend metrics the agent watches (add a metric = add an entry).
    # is_rate=True -> compare in POINTS not a ratio; point/target bands calibrated
    # from real data (see Documents/PRISM_Detection_Calibration.md).
    "metrics": {
        "SSS_Pct": {"label": "Same Store Sales Growth", "direction": "down_is_bad",
                    "trailing_weeks": 8, "target_col": "SSSTarget", "is_rate": True,
                    "point_drop_threshold": 0.73, "high_point_drop": 1.09, "critical_point_drop": 1.81,
                    "min_target_gap": 3.0, "high_target_gap": 6.0, "critical_target_gap": 9.0,
                    "pct_drop_threshold": 25.0, "unit": "%"},
        "OSAT_Pct": {"label": "Customer Satisfaction (OSAT)", "direction": "down_is_bad",
                     "trailing_weeks": 8, "target_col": "OSATTarget", "is_rate": True,
                     "point_drop_threshold": 1.70, "high_point_drop": 2.55, "critical_point_drop": 4.25,
                     "min_target_gap": 5.0, "high_target_gap": 10.0, "critical_target_gap": 15.0,
                     "pct_drop_threshold": 8.0, "unit": "%"},
        "EBITDA_Pct": {"label": "EBITDA Margin", "direction": "down_is_bad",
                       "trailing_weeks": 8, "target_col": "EBITDATarget", "is_rate": True,
                       "point_drop_threshold": 0.73, "high_point_drop": 1.09, "critical_point_drop": 1.81,
                       "min_target_gap": 4.0, "high_target_gap": 8.0, "critical_target_gap": 12.0,
                       "pct_drop_threshold": 20.0, "unit": "%"},
    },

    # food safety is event-based (audits), handled separately. flag_below_target
    # False => only a floor breach (< critical_floor) is a real alert.
    "fsa": {"label": "Food Safety Audit Score", "target_col": "FSATarget",
            "critical_floor": 80.0, "flag_below_target": False},

    # correlation rules: when a pattern fires and how patterns are ranked
    "patterns": {
        "fbc_min_stores": 2,
        "area_director_min_stores": 3,
        "region_min_stores": 3,
        "multi_metric_min_metrics": 2,
        "score_weights": {"fbc_per_store": 10, "area_director_per_store": 8,
                          "region_per_store": 7, "multi_metric_per_metric": 12},
        "max_patterns": 6,
    },

    # deterministic severity bands (percent drop is a positive magnitude here)
    "severity": {"critical_pct_drop": 40, "high_pct_drop": 25, "high_target_gap": 6},

    # cause-aware correlation (v2): when a shared-cause cohort counts as "systemic"
    "correlation": {"min_stores": 3, "min_share": 0.4, "min_lift": 1.8,
                    "fallback": True, "fallback_min_stores": 3,
                    "fleet_min_share": 0.35, "fleet_min_regions": 3, "fleet_min_fbcs": 4},

    # diagnosis tuning
    "diagnosis": {"sss_traffic_ticket_pct": 2.0},  # +/- % move that counts as traffic/ticket driven

    # optional catch-all: statistically trend EVERY unwatched numeric column
    "generic_watch": {"enabled": False, "pct_drop_threshold": 30, "trailing_weeks": 8},

    # store-level agentic diagnosis (reason.py + ground.py). When off, or on any
    # failure, PRISM falls back to the deterministic diagnose.py per metric.
    "reasoning": {"enabled": True, "max_store_cards": 12, "use_tools": False,
                  "max_tool_calls": 8, "cache": True, "max_workers": 6},

    # where PRISM writes the store-level insights for a dashboard to read.
    # sqlite = the memory/recurrence table; csv = the current-state mirror rebuilt
    # each run (what Power BI imports). Paths are relative to this project folder.
    "insights_export": {"sqlite": True, "csv": True,
                        "csv_path": "insights_latest.csv",
                        "history_csv": False,
                        "history_csv_path": "insights_history.csv"},

    # recommended action per diagnosed driver — CURATE WITH OPS
    "playbook": {
        "sss_traffic": ("Traffic-led decline: focus on local marketing, LTO promotion and "
                        "lapsed-loyalty win-back; verify hours and peak-daypart staffing."),
        "sss_ticket": ("Ticket-led decline: review discount/comp usage, promo mix and "
                       "upsell/attachment execution at the line."),
        "sss_both": ("Both traffic and ticket are soft — treat as store health: FBC visit, "
                     "verify operations and staffing, check local competitive pressure."),
        "sss_comp": ("Below-target sales with stable recent inputs — review the prior-year "
                     "comp base and trade-area trends; focus on demand generation (local "
                     "marketing, loyalty) rather than in-store execution."),
        "osat_FoodQuality": ("Food quality is the weakest guest driver — audit prep/hold times, "
                             "portioning and freshness; re-certify line staff on food standards."),
        "osat_Service": ("Service is the weakest guest driver — review peak labor deployment "
                         "and throughput; re-train on service standards."),
        "osat_Cleanliness": ("Cleanliness is the weakest guest driver — deep-clean, reset the "
                             "cleaning-checklist cadence and re-train on sanitation SOP."),
        "osat_Value": ("Value is the weakest guest driver — review price/portion perception "
                       "and loyalty/promo communication for this trade area."),
        "fsa_finding": ("Correct the cited priority finding, re-audit within the follow-up "
                        "window, and re-train staff on the specific standard."),
        "fsa_generic": ("Score is below the safety floor — schedule an immediate corrective "
                        "visit and re-audit; escalate to the Area Director."),
        "ebitda_sales": ("Margin pressure tracks a sales decline — recovering traffic/ticket "
                         "should recover EBITDA; monitor after sales actions take hold."),
        "ebitda_cost": ("Margin is down without a matching sales drop — review food cost, waste "
                        "and labor scheduling against volume."),
        "generic": "Investigate the drivers behind this metric and confirm store operations.",
    },
}


def _load():
    cfg = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    try:
        import yaml
        with open(_YAML_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for k in DEFAULTS:
            if k in data and data[k] is not None:
                cfg[k] = data[k]
    except FileNotFoundError:
        pass  # no YAML file -> defaults are fine, run silently
    except Exception as e:  # missing PyYAML, parse error, etc. -> defaults + warning
        print(f"[PRISM config] Warning: could not read prism_config.yaml ({e}); "
              f"using built-in defaults.")
    return cfg


_CFG = _load()

# ── Exposed knobs (imported by agent.py and diagnose.py) ────────────────────
TARGET_PERSONA = _CFG["target_persona"]
MAX_ALERTS = _CFG["max_alerts"]
NARRATOR_MODEL = _CFG["narrator_model"]
REASONING_MODEL = _CFG["reasoning_model"]
CHAT_MODEL = _CFG["chat_model"]
METRICS = _CFG["metrics"]
FSA_CONFIG = _CFG["fsa"]
PATTERN_RULES = _CFG["patterns"]
SEVERITY = _CFG["severity"]
CORRELATION = _CFG["correlation"]
DIAGNOSIS = _CFG["diagnosis"]
PLAYBOOK = _CFG["playbook"]
GENERIC_WATCH = _CFG["generic_watch"]
REASONING = _CFG["reasoning"]
INSIGHTS_EXPORT = _CFG["insights_export"]
