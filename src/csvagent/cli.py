"""Command line entry point.

csvagent analyze heart.csv
csvagent analyze heart.csv -q "Which factors are linked to heart disease?" --out reports/heart
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_QUESTION = (
    "Explore this dataset. Check data quality, find the most important patterns and "
    "relationships (especially with the target, if there is one), support them with charts "
    "and statistical tests, and give concrete recommendations."
)


def main(argv: list[str] | None = None) -> None:
    # Windows consoles default to a legacy code page (e.g. cp1254) that cannot print
    # some characters in model output or column names (e.g. "\u2212"); never crash on output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from .agent import CSVAgent, default_model
    from .data import Dataset
    from .report import write_report
    from .tools import Toolbox

    p = argparse.ArgumentParser(prog="csvagent", description="LLM agent for CSV analysis")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="analyze a CSV file and write a report")
    a.add_argument("csv")
    a.add_argument("--question", "-q", default=DEFAULT_QUESTION)
    a.add_argument("--out", default=None, help="output folder (default: reports/<name>_<time>)")
    a.add_argument("--model", default=default_model())
    a.add_argument(
        "--max-steps", type=int, default=20, help="max model turns (a turn can call several tools)"
    )
    a.add_argument("--max-tokens", type=int, default=8192, help="max output tokens per turn")
    a.add_argument(
        "--language",
        default=None,
        help="report language, e.g. English (default: detected from the question)",
    )
    a.add_argument("--no-images", action="store_true", help="do not send charts back to the model")
    args = p.parse_args(argv)

    data = Dataset.from_csv(args.csv)
    out = Path(args.out or f"reports/{data.name}_{datetime.now():%Y%m%d-%H%M}")
    toolbox = Toolbox(data, out)
    agent = CSVAgent(
        toolbox,
        model=args.model,
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        send_images=not args.no_images,
        language=args.language,
    )

    print(f"Analyzing {args.csv} ({len(data.df)} rows) with {args.model}...\n")

    def show(step):
        status = "ERROR" if step.is_error else "ok"
        args_s = ", ".join(f"{k}={v}" for k, v in step.input.items())
        print(f"  [{step.index:02d}] {step.tool}({args_s}) -> {status}")

    result = agent.run(args.question, on_step=show)
    paths = write_report(result, out, data.name)
    print(f"\nDone: {len(result.steps)} tool calls, {len(result.charts)} charts.")
    if result.truncated:
        print("WARNING: the report was cut off at max_tokens; rerun with a higher --max-tokens.")
    print(f"Tokens in/out: {result.input_tokens}/{result.output_tokens}")
    for kind, path in paths.items():
        print(f"  {kind}: {path}")


if __name__ == "__main__":
    main()
