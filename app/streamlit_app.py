"""Streamlit UI: upload a CSV, watch the agent work, read and download the report.

Run with:  streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st

from csvagent.agent import CSVAgent, default_model
from csvagent.cli import DEFAULT_QUESTION
from csvagent.data import Dataset, load_csv
from csvagent.report import TRUNCATED_NOTE, split_markdown_images, write_report
from csvagent.tools import Toolbox

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

st.set_page_config(page_title="CSV Analyst Agent", page_icon="📊", layout="wide")
st.title("📊 CSV Analyst Agent")
st.caption(
    "An LLM agent that explores your CSV with safe analysis tools (no code execution), "
    "draws charts, tests its claims statistically and writes a report."
)

with st.sidebar:
    # default_model() runs after load_dotenv(), so CSVAGENT_MODEL from .env is honoured
    model = st.text_input("Model", default_model())
    max_steps = st.slider("Max model turns (each can call several tools)", 5, 40, 20)
    send_images = st.checkbox("Let the agent see its charts", value=True)

uploaded = st.file_uploader("CSV file", type=["csv"])
question = st.text_area("What do you want to know?", DEFAULT_QUESTION, height=90)


def show_step(step) -> None:
    args = ", ".join(f"{k}={v}" for k, v in step.input.items())
    icon = "⚠️" if step.is_error else "🔧"
    st.write(f"{icon} `{step.tool}({args})`")
    if step.image:
        st.image(step.image, width=420)


if uploaded and st.button("Analyze", type="primary"):
    tmp = Path(tempfile.mkdtemp())
    csv_path = tmp / uploaded.name
    csv_path.write_bytes(uploaded.getvalue())
    data = Dataset(load_csv(csv_path), Path(uploaded.name).stem)
    out = tmp / f"report_{datetime.now():%H%M%S}"
    agent = CSVAgent(Toolbox(data, out), model=model, max_steps=max_steps, send_images=send_images)

    st.write(f"**{data.name}**: {len(data.df)} rows, {data.df.shape[1]} columns")
    with st.status("The agent is analyzing...", expanded=True) as status:
        result = agent.run(question, on_step=show_step)
        status.update(label=f"Done: {len(result.steps)} tool calls", state="complete")

    paths = write_report(result, out, data.name)
    # Streamlit reruns the whole script on every interaction (e.g. the download button),
    # so keep the finished run in session_state; otherwise the report would vanish and
    # getting it back would mean paying for a new analysis.
    st.session_state["last_run"] = {
        "result": result,
        "out": out,
        "html": paths["html"].read_bytes(),
        "name": data.name,
    }

run = st.session_state.get("last_run")
if run:
    result, out = run["result"], run["out"]
    st.divider()
    if result.truncated:
        st.warning(TRUNCATED_NOTE)
    for kind, part in split_markdown_images(result.report):
        if kind == "text":
            st.markdown(part)
        elif (out / part).exists():
            st.image(str(out / part))
    st.caption(
        f"{result.model} · {len(result.steps)} tool calls · tokens in/out "
        f"{result.input_tokens}/{result.output_tokens}"
    )
    st.download_button(
        "Download report (HTML)",
        run["html"],
        file_name=f"{run['name']}_report.html",
        mime="text/html",
    )
