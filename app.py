import streamlit as st
import os
import base64
from dotenv import load_dotenv
from data_connector import get_connection
from agent_ui import render_agent_panel
import chat_assistant as chat

load_dotenv()

_ICON_PATH = os.path.join(os.path.dirname(__file__), "prism_icon.png")


def _load_page_icon():
    try:
        from PIL import Image
        return Image.open(_ICON_PATH)
    except Exception:
        return "🔷"


def _icon_data_uri():
    try:
        with open(_ICON_PATH, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except Exception:
        return None


# ── Page config (must be the first Streamlit call) ────────────
st.set_page_config(page_title="PRISM — Jersey Mike's", page_icon=_load_page_icon(), layout="wide")
_ICON_URI = _icon_data_uri()

# ── Branding + chat styling ───────────────────────────────────
st.markdown("""
<style>
    .main { background-color:#f7f8fa; }
    .prism-header { display:flex; align-items:center; gap:14px; padding:4px 0 0 0; }
    .prism-badge { font-size:34px; line-height:1; }
    .prism-title { font-size:30px; font-weight:800; color:#1C0087; letter-spacing:0.5px; margin:0; }
    .prism-sub { color:#6b7280; font-size:13px; margin:2px 0 0 0; }
    .prism-rule { height:4px; border-radius:3px;
                  background:linear-gradient(90deg,#0086EA,#1C0087,#EE282A);
                  margin:8px 0 14px 0; }
    /* Float the "Ask PRISM" trigger as a small round button, bottom-right. If a
       Streamlit update changes the DOM this falls back to the normal page flow. */
    div[data-testid="stPopover"] {
        position:fixed; bottom:22px; right:24px; z-index:9999;
        width:auto !important; min-width:0 !important;
    }
    div[data-testid="stPopover"] > div { width:auto !important; }
    div[data-testid="stPopover"] button {
        width:auto !important; border-radius:26px; padding:8px 16px;
        font-weight:700; font-size:0.9rem; background:#1C0087; color:#ffffff;
        border:none; box-shadow:0 6px 18px rgba(28,0,135,0.35); white-space:nowrap;
    }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def load_data():
    return get_connection()


conn = load_data()

if "messages" not in st.session_state:
    st.session_state.messages = []

# ── Branded header ────────────────────────────────────────────
_badge = (f'<img src="{_ICON_URI}" style="width:46px;height:46px;border-radius:10px;'
          'box-shadow:0 2px 6px rgba(0,0,0,0.18);" />') if _ICON_URI else "🔷"
st.markdown(
    f'<div class="prism-header"><div class="prism-badge">{_badge}</div>'
    '<div><p class="prism-title">PRISM</p>'
    "<p class=\"prism-sub\">Proactive Recommendation &amp; Insight System for Metrics "
    "· Jersey Mike's · WWT</p></div></div>"
    '<div class="prism-rule"></div>', unsafe_allow_html=True)

# ── Main page: the proactive agent ────────────────────────────
render_agent_panel()


# ── Floating "Ask PRISM" chatbot ──────────────────────────────
def _render_assistant_message(m, idx):
    st.write(m.get("content", ""))
    if m.get("sql"):
        with st.expander("🔍 SQL"):
            st.code(m["sql"], language="sql")
    df = m.get("df")
    if df is not None:
        ch = chat.chartable(df)
        if ch:
            try:
                st.bar_chart(df.set_index(ch[0])[ch[1]])
            except Exception:
                pass
        st.dataframe(df, width='stretch', hide_index=True)
        st.download_button("⬇️ Download CSV", df.to_csv(index=False).encode("utf-8"),
                           file_name="prism_answer.csv", mime="text/csv", key=f"dl_{idx}")


with st.popover("💬  Ask PRISM"):
    st.markdown("**Ask PRISM** — data questions *or* questions about the alerts")

    # suggested prompt chips
    ccols = st.columns(2)
    for i, p in enumerate(chat.SUGGESTED_PROMPTS):
        if ccols[i % 2].button(p, key=f"chip_{i}", width='stretch'):
            st.session_state["chat_prefill"] = p

    # conversation so far
    if not st.session_state.messages:
        st.caption("Ask a question below, or tap a suggestion. I remember the last few "
                   "turns, so follow-ups like \"which is second highest?\" work.")
    for idx, m in enumerate(st.session_state.messages):
        with st.chat_message(m["role"]):
            if m["role"] == "assistant":
                _render_assistant_message(m, idx)
            else:
                st.write(m["content"])

    # input
    with st.form("ask_form", clear_on_submit=True):
        q = st.text_input("Your question", value=st.session_state.get("chat_prefill", ""),
                          label_visibility="collapsed",
                          placeholder="e.g. which stores did PRISM flag as Critical?")
        sent = st.form_submit_button("Send", width='stretch')

    if sent and q and q.strip():
        st.session_state.pop("chat_prefill", None)
        st.session_state.messages.append({"role": "user", "content": q.strip()})
        with st.spinner("Thinking…"):
            try:
                res = chat.ask(q.strip(), st.session_state.messages, conn)
            except Exception as e:
                res = {"answer": f"Sorry — something went wrong answering that ({e})."}
        st.session_state.messages.append({
            "role": "assistant", "content": res.get("answer", ""),
            "sql": res.get("sql"), "df": res.get("df"),
        })
        st.rerun()
