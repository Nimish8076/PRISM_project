# PRISM — how to run it

PRISM is the proactive half of Jersey Mike's data assistant: every week it finds the
stores that are genuinely off, has an AI agent diagnose the cause, links related stores
into patterns, and routes each issue to the right owner. It's a Python + Streamlit app.

## Run it locally

**1. Create a virtual environment**

```
python -m venv venv
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# macOS / Linux:
source venv/bin/activate
```

**2. Install dependencies**

```
pip install -r requirements.txt
```

**3. Add your API key**

Copy `.env.example` to `.env` and paste an Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

(Without a key, detection and the deterministic fallback still run; the AI diagnosis
and the "Ask PRISM" chat need the key.)

**4. Run the app**

```
streamlit run app.py
```

If `streamlit` isn't found on your PATH, use `python -m streamlit run app.py`.
The app loads the CSVs in `data/` into an in-memory database and opens in your browser.

## What's inside

| Area | Files |
|---|---|
| UI (Streamlit) | `app.py`, `agent_ui.py` |
| Pipeline orchestration | `agent.py` (`run_analysis`) |
| The one AI step + its tools | `agent_loop.py`, `tools.py` |
| Analysis & validation | `evidence.py`, `diagnose.py`, `ground.py` |
| Data, persistence, config | `data_connector.py`, `alert_store.py`, `config.py`, `prism_config.yaml` |
| "Ask PRISM" chat | `chat_assistant.py`, `sql_generator.py`, `answer_generator.py`, `schema_context.py` |
| Sample data | `data/` (and `prism_test_data/` to regenerate it) |
| Docs | `Documents/` — the deck, the technical documentation, and project context |

Start with **`Documents/PRISM_Technical_Documentation.docx`** for the full architecture,
data flow, and a file-by-file reference.

## Notes

- `venv/`, `.env`, and the runtime databases (`prism_history*.db`) are intentionally not
  shared — recreate the venv and add your own `.env` as above.
- All settings live in `prism_config.yaml`; it's read once at startup, so restart the app
  after changing it.
