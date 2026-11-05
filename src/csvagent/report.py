"""Write the agent's result as Markdown, a self-contained HTML page and a step log."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict
from datetime import datetime
from html import escape
from pathlib import Path

from .agent import AgentResult

IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

TRUNCATED_NOTE = (
    "**Incomplete report:** the model hit its output token limit (max_tokens) and the "
    "report below is cut off. Run again with a higher max_tokens (CLI: --max-tokens)."
)


def _steps_table(result: AgentResult) -> str:
    lines = ["| # | Tool | Input | Result |", "|---|---|---|---|"]
    for s in result.steps:
        args = ", ".join(f"{k}={v}" for k, v in s.input.items())
        first = s.output.splitlines()[0] if s.output else ""
        mark = "error: " if s.is_error else ""
        lines.append(f"| {s.index} | `{s.tool}` | {args} | {mark}{first[:90]} |")
    return "\n".join(lines)


def write_report(result: AgentResult, out_dir: str | Path, dataset_name: str) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta = (
        f"_Dataset: {dataset_name} · model: {result.model} · {len(result.steps)} tool calls in "
        f"{result.turns} turns · tokens in/out: {result.input_tokens}/{result.output_tokens} · "
        f"{datetime.now():%Y-%m-%d %H:%M}_"
    )
    if result.stop_reason == "step_limit":
        meta += "\n\n> The agent hit its step limit; the report uses the results gathered so far."

    report = result.report
    if result.truncated:
        # Put the warning first: the cut is at the end, where a reader may never look.
        report = f"> {TRUNCATED_NOTE}\n\n{report}"
    md = f"{report}\n\n---\n{meta}\n\n## Analysis steps\n\n{_steps_table(result)}\n"
    md_path = out / "report.md"
    md_path.write_text(md, encoding="utf-8")

    html_path = out / "report.html"
    html_path.write_text(to_html(md, out, title=f"Analysis of {dataset_name}"), encoding="utf-8")

    log_path = out / "steps.jsonl"
    with open(log_path, "w", encoding="utf-8") as f:
        for s in result.steps:
            f.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
    return {"markdown": md_path, "html": html_path, "log": log_path}


def to_html(md: str, base: Path, title: str) -> str:
    """Markdown -> one HTML file with charts embedded, so it can be shared as is."""
    try:
        import markdown

        body = markdown.markdown(md, extensions=["tables"])
    except ImportError:  # still produce something readable
        body = f"<pre>{escape(md)}</pre>"

    def embed(m: re.Match) -> str:
        src = m.group(1)
        p = base / src
        if p.exists():
            data = base64.b64encode(p.read_bytes()).decode()
            return f'src="data:image/png;base64,{data}"'
        return m.group(0)

    body = re.sub(r'src="([^"]+\.png)"', embed, body)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;
line-height:1.55;color:#1d1d1f}}img{{max-width:100%;border:1px solid #ddd;border-radius:6px}}
table{{border-collapse:collapse;font-size:.85rem}}td,th{{border:1px solid #ddd;padding:4px 8px}}
code{{background:#f3f3f3;padding:1px 4px;border-radius:4px}}</style></head>
<body>{body}</body></html>"""


def split_markdown_images(md: str) -> list[tuple[str, str]]:
    """Split Markdown into ('text', ...) and ('image', path) parts (used by the Streamlit app)."""
    parts, last = [], 0
    for m in IMG_RE.finditer(md):
        if m.start() > last:
            parts.append(("text", md[last : m.start()]))
        parts.append(("image", m.group(2)))
        last = m.end()
    if last < len(md):
        parts.append(("text", md[last:]))
    return parts
