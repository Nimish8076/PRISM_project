# Jersey Mike's Data Assistant (PRISM)

A Streamlit app with two halves:

- **Ask a Question** — ask about franchise performance in plain English; the app turns it into SQL, runs it, and answers in business language.
- **Proactive Agent (PRISM)** — scans every store the moment fresh data lands, detects anomalies, correlates them into systemic patterns across the org, and recommends actions. Cause, recommendation and severity are computed deterministically; an optional per-card AI switch only rephrases the wording.

## Setup

1. Python 3.10+.
2. Create and activate a virtual environment (do **not** commit it — it's rebuilt from `requirements.txt`):
   - Windows: `python -m venv venv` then `venv\Scripts\activate`
   - macOS / Linux: `python -m venv venv` then `source venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`
4. Create a `.env` file in this folder with your key:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

## Run

```
streamlit run app.py
```

Open the **Proactive Agent** tab and click **Simulate Pipeline Run**.

## Data

The 8 CSVs are loaded into an in-memory SQLite database. The folder they load from is set by `DATA_DIR` in `data_connector.py`:

- `data` — the realistic sample dataset.
- `prism_test_data` — a small labeled test dataset with planted anomalies (see `prism_test_data/README.md`). **`DATA_DIR` is currently set to this** — switch it back to `data` for the sample set.

`data_connector.py` is the single data-access boundary; in production `execute_query()` is repointed at the live Microsoft Fabric model and nothing else changes.

## Configuration — tune without touching code

All tunable knobs live in **`prism_config.yaml`**: detection thresholds and target buffers, correlation fire-rules / score-weights / cap, `max_alerts`, severity bands, the watched-metric list, target persona, model ids, and the recommendation **PLAYBOOK**. Edit the YAML and restart. If the file is missing or malformed, `config.py` falls back to built-in defaults so the app still runs.

## Project layout

| File | Purpose |
|---|---|
| `app.py` | Streamlit entry point (two tabs) |
| `agent.py` | PRISM pipeline: detect → enrich → correlate → diagnose → compose |
| `diagnose.py` | Deterministic cause / action / severity + PLAYBOOK lookup |
| `config.py` / `prism_config.yaml` | All tunable knobs (with safe fallback) |
| `data_connector.py` | Data-access boundary (SQLite now; Fabric swap point) |
| `alert_store.py` | Alert history / recurrence / feedback (`prism_history.db`) |
| `schema_context.py`, `sql_generator.py`, `answer_generator.py` | The chat tab |
| `agent_ui.py` | The Proactive Agent UI |
| `prism_test_data/` | Labeled test dataset + its own README |

## Notes

- Recommended next steps: an automated pytest suite (using `prism_test_data/`), a real pipeline trigger + email/Teams dispatch, and the Microsoft Fabric + Azure OpenAI cutover.
