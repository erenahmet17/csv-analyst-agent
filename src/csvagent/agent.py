"""The agent loop: the model plans, calls tools, looks at results, and writes a report.

Built on the Anthropic Messages API with tool use:

1. send the task + tool definitions
2. if the reply contains ``tool_use`` blocks, run each tool and send the results back
   as ``tool_result`` blocks (charts go back as images, so the model can read them)
3. repeat until the model answers without calling a tool (the final report) or the
   step limit is reached
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .tools import Toolbox

FALLBACK_MODEL = "claude-sonnet-5-5"


def default_model() -> str:
    """The model to use when none is given: $CSVAGENT_MODEL, else FALLBACK_MODEL.

    Read on each call rather than at import time, so a .env loaded after this module
    is imported (as in the Streamlit app) still takes effect."""
    return os.getenv("CSVAGENT_MODEL") or FALLBACK_MODEL


SYSTEM_PROMPT = """You are a careful data analyst. You explore a tabular dataset ONLY through the \
tools provided and then write a report for a non-expert reader.

How to work:
1. Start with describe_dataset. Identify the likely target or outcome column if there is one.
2. Check data quality before drawing conclusions: missing values, duplicates, outliers, and \
impossible values (for example zeros in a column where zero is not physically possible, which \
usually means a missing value).
3. Explore the relationships that matter for the user's question: correlations, group \
comparisons, crosstabs, rates by bins.
4. Before you claim that a difference or association is real, test it with compare_groups and \
report the p-value and effect size. Say "associated with", never "causes".
5. Draw 4-8 charts that support the key findings. Look at each chart you get back and describe \
what it shows. Do not draw charts you will not use.
6. Never invent numbers. Every number in the report must come from a tool result.

When you are done, reply WITHOUT calling a tool, with the final report in Markdown, in the \
language of the user's request. Use these sections:
- Summary (3-4 sentences)
- Data quality (issues found and how they affect the analysis)
- Findings (each with its numbers, test result and chart, embedded as ![title](charts/...))
- Recommendations (concrete next steps: data cleaning, features worth modelling, which model \
family to try and why, what to investigate further)
- Limitations
If the data is medical, add one line saying the analysis is exploratory and not medical advice."""


@dataclass
class Step:
    index: int
    tool: str
    input: dict
    output: str
    is_error: bool = False
    image: str | None = None  # path of a chart, if the tool drew one
    seconds: float = 0.0


@dataclass
class AgentResult:
    report: str
    steps: list[Step]
    charts: list[str]
    stop_reason: str  # "done" | "step_limit"
    truncated: bool = False  # the final report was cut off at max_tokens
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    turns: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _block_to_dict(b) -> dict | None:
    """Convert an SDK content block to the plain dict form used in the message history.

    Other block types (e.g. thinking blocks) must go back to the API unchanged, so they
    are passed through with the SDK's own serialisation."""
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if hasattr(b, "model_dump"):
        return b.model_dump(exclude_none=True)
    return None


def _image_block(path) -> dict:
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


class CSVAgent:
    def __init__(
        self,
        toolbox: Toolbox,
        client=None,
        model: str | None = None,
        max_steps: int = 20,
        max_tokens: int = 8192,
        send_images: bool = True,
    ):
        if client is None:
            from anthropic import Anthropic

            client = Anthropic()  # reads ANTHROPIC_API_KEY
        self.toolbox = toolbox
        self.client = client
        self.model = model or default_model()
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.send_images = send_images

    def _call(self, messages: list[dict], result: AgentResult, **kw):
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            tools=self.toolbox.specs(),
            messages=messages,
            **kw,
        )
        result.turns += 1
        usage = getattr(resp, "usage", None)
        if usage is not None:
            result.input_tokens += getattr(usage, "input_tokens", 0) or 0
            result.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return resp

    def _finish(self, resp, result: AgentResult) -> None:
        """Take the final report from a reply that called no tool."""
        result.report = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        # A reply cut off at max_tokens is an incomplete report. Flag it instead of
        # passing it off as a finished one.
        result.truncated = getattr(resp, "stop_reason", None) == "max_tokens"

    def run(self, question: str, on_step: Callable[[Step], None] | None = None) -> AgentResult:
        data = self.toolbox.data
        task = (
            f"Dataset: '{data.name}' ({len(data.df)} rows, {data.df.shape[1]} columns).\n\n"
            f"Request: {question}"
        )
        messages: list[dict] = [{"role": "user", "content": task}]
        result = AgentResult(report="", steps=[], charts=[], stop_reason="done", model=self.model)

        for _ in range(self.max_steps):
            resp = self._call(messages, result)
            blocks = [d for d in (_block_to_dict(b) for b in resp.content) if d is not None]
            messages.append({"role": "assistant", "content": blocks})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                self._finish(resp, result)
                break

            tool_results = []
            for tu in tool_uses:
                t0 = time.perf_counter()
                step = Step(
                    index=len(result.steps) + 1, tool=tu.name, input=dict(tu.input or {}), output=""
                )
                try:
                    out = self.toolbox.run(tu.name, step.input)
                    step.output = out.text
                    content: list[dict] = [{"type": "text", "text": out.text}]
                    if out.image is not None:
                        step.image = str(out.image)
                        if self.send_images:
                            content.append(_image_block(out.image))
                except Exception as e:  # report the error to the model so it can recover
                    step.is_error = True
                    step.output = f"Error: {type(e).__name__}: {e}"
                    content = [{"type": "text", "text": step.output}]
                step.seconds = time.perf_counter() - t0
                result.steps.append(step)
                if on_step:
                    on_step(step)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": content,
                        "is_error": step.is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            # Step limit reached: ask for the report with what has been gathered so far.
            # The last message holds tool results; the API wants any text AFTER them in the
            # same user message, not as a separate message.
            result.stop_reason = "step_limit"
            messages[-1]["content"].append(
                {
                    "type": "text",
                    "text": "Step limit reached. Write the final report now using only the "
                    "results you already have. Do not call any more tools.",
                }
            )
            resp = self._call(messages, result, tool_choice={"type": "none"})
            self._finish(resp, result)

        result.charts = [str(p) for p in self.toolbox.charts]
        return result
