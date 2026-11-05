"""Analysis tools the agent can call.

The model never runs arbitrary code. It can only call these functions, each
with a JSON schema, so every step is safe, typed, logged and reproducible. A
tool that receives bad input raises an error, and the agent loop sends that
error back to the model so it can correct itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # no display needed
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

from .data import Dataset  # noqa: E402

MAX_ROWS = 25  # cap on table rows returned to the model (keeps context small)


@dataclass
class ToolOutput:
    text: str
    image: Path | None = None


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[..., ToolOutput]

    def spec(self) -> dict:
        """Tool definition in the format the Anthropic Messages API expects."""
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


@dataclass
class Toolbox:
    data: Dataset
    out_dir: Path
    charts: list[Path] = field(default_factory=list)
    tools: dict[str, Tool] = field(default_factory=dict)

    def __post_init__(self):
        self.out_dir = Path(self.out_dir)
        (self.out_dir / "charts").mkdir(parents=True, exist_ok=True)
        for name, (desc, schema, fn) in _REGISTRY.items():
            self.tools[name] = Tool(name, desc, schema, lambda _fn=fn, **kw: _fn(self, **kw))

    def specs(self) -> list[dict]:
        return [t.spec() for t in self.tools.values()]

    def run(self, name: str, args: dict[str, Any]) -> ToolOutput:
        if name not in self.tools:
            raise KeyError(f"unknown tool {name!r}")
        return self.tools[name].fn(**(args or {}))

    @property
    def df(self) -> pd.DataFrame:
        return self.data.df


_REGISTRY: dict[str, tuple[str, dict, Callable]] = {}


def tool(name: str, description: str, properties: dict | None = None, required=()):
    schema = {"type": "object", "properties": properties or {}, "required": list(required)}

    def deco(fn):
        _REGISTRY[name] = (description, schema, fn)
        return fn

    return deco


def _table(obj: pd.DataFrame | pd.Series, rows: int = MAX_ROWS) -> str:
    extra = ""
    if len(obj) > rows:
        extra = f"\n... ({len(obj) - rows} more rows not shown)"
        obj = obj.head(rows)
    return obj.to_string(float_format=lambda v: f"{v:.4g}") + extra


def _col(name: str, desc: str = "Column name, exactly as in describe_dataset") -> dict:
    return {name: {"type": "string", "description": desc}}


# --------------------------------------------------------------------------- overview


@tool(
    "describe_dataset",
    "Overview of the dataset: size, every column with its type, missing values, number of "
    "distinct values and examples, duplicate rows, and a few sample rows. Call this first.",
)
def describe_dataset(tb: Toolbox) -> ToolOutput:
    df = tb.df
    rows = []
    for c in tb.data.columns:
        s = df[c]
        examples = ", ".join(map(str, s.dropna().unique()[:4]))
        rows.append(
            {
                "column": c,
                "dtype": str(s.dtype),
                "kind": tb.data.kind(c),
                "missing_%": round(100 * s.isna().mean(), 1),
                "distinct": s.nunique(dropna=True),
                "examples": examples[:60],
            }
        )
    info = pd.DataFrame(rows).set_index("column")
    text = (
        f"Dataset '{tb.data.name}': {len(df)} rows x {df.shape[1]} columns, "
        f"{int(df.duplicated().sum())} duplicate rows.\n\n{info.to_string()}\n\n"
        f"Sample rows:\n{df.head(5).to_string()}"
    )
    return ToolOutput(text)


@tool(
    "column_summary",
    "Detailed statistics for one column. Numeric: count, mean, std, quartiles, skew, number of "
    "exact zeros (zeros can be disguised missing values). Categorical: value counts and shares.",
    _col("column"),
    required=["column"],
)
def column_summary(tb: Toolbox, column: str) -> ToolOutput:
    tb.data.require(column)
    s = tb.df[column]
    kind = tb.data.kind(column)
    head = f"{column} ({kind}, dtype {s.dtype}), missing: {int(s.isna().sum())}\n"
    if kind == "numeric":
        d = s.describe()
        d["skew"] = s.skew()
        d["zeros"] = int((s == 0).sum())
        return ToolOutput(head + _table(d))
    counts = s.value_counts(dropna=False)
    table = pd.DataFrame({"count": counts, "share_%": (100 * counts / len(s)).round(1)})
    return ToolOutput(head + _table(table))


# --------------------------------------------------------------------------- relationships


def _corr_pvalues(r: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Two-sided p-values for H0: correlation = 0, from r and the number of pairs n.

    Uses t = r * sqrt((n - 2) / (1 - r^2)) with n - 2 degrees of freedom: exact for
    Pearson and the same approximation scipy.stats.spearmanr uses for Spearman."""
    r = np.asarray(r, dtype=float)
    n = np.asarray(n, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = r * np.sqrt((n - 2) / (1 - r**2))
        p = 2 * stats.t.sf(np.abs(t), n - 2)
    p = np.where(np.abs(r) >= 1, 0.0, p)
    return np.where(n > 2, p, np.nan)


def _with_tests(r: pd.Series, n: pd.Series) -> pd.DataFrame:
    """r, p, FDR q (Benjamini-Hochberg over all rows) and n, strongest |r| first."""
    n = n.reindex(r.index)  # stack() drops NaN r (constant columns) but not their n
    table = pd.DataFrame({"r": r, "p": _corr_pvalues(r.to_numpy(), n.to_numpy()), "n": n})
    table = table.dropna(subset=["r", "p"])  # constant columns have no correlation
    table["q_fdr"] = stats.false_discovery_control(table["p"]) if len(table) else []
    table["n"] = table["n"].astype(int)
    table = table[["r", "p", "q_fdr", "n"]]
    return table.reindex(table["r"].abs().sort_values(ascending=False).index)


@tool(
    "correlations",
    "Correlations between numeric columns, each with a significance test: p (two-sided test "
    "of r = 0), q_fdr (p adjusted for multiple comparisons, Benjamini-Hochberg, over every "
    "correlation in the result) and n (rows where both columns are present). With `target`: "
    "every numeric column's correlation with the target, sorted by strength. Without: the "
    "strongest column pairs.",
    {
        **_col("target", "Optional numeric target column"),
        "method": {"type": "string", "enum": ["pearson", "spearman"], "default": "spearman"},
    },
)
def correlations(tb: Toolbox, target: str | None = None, method: str = "spearman") -> ToolOutput:
    num = tb.df[tb.data.numeric_columns()]
    if num.shape[1] < 2:
        raise ValueError("need at least two numeric columns")
    corr = num.corr(method=method)  # pairwise-complete, like n below
    present = num.notna().astype(int)
    n_pairs = present.T @ present  # rows where both columns are present
    note = "p: test of r = 0; q_fdr: p adjusted for the {k} tests in this result"
    if target:
        tb.data.require(target)
        if target not in corr:
            raise ValueError(f"{target!r} is not numeric")
        table = _with_tests(corr[target].drop(target), n_pairs[target].drop(target))
        head = f"{method} correlation with {target} ({note.format(k=len(table))}):"
        return ToolOutput(f"{head}\n{_table(table)}")
    upper = np.triu(np.ones(corr.shape, dtype=bool), k=1)
    table = _with_tests(corr.where(upper).stack(), n_pairs.where(upper).stack())
    head = f"Strongest {method} correlations ({note.format(k=len(table))}):"
    return ToolOutput(f"{head}\n{_table(table, 15)}")


@tool(
    "group_stats",
    "Statistics of a numeric column within each group of a categorical column, e.g. mean "
    "cholesterol per chest pain type.",
    {**_col("group_by"), **_col("column", "Numeric column to summarise")},
    required=["group_by", "column"],
)
def group_stats(tb: Toolbox, group_by: str, column: str) -> ToolOutput:
    tb.data.require(group_by, column)
    g = tb.df.groupby(group_by, dropna=False)[column]
    table = g.agg(["count", "mean", "median", "std", "min", "max"])
    return ToolOutput(f"{column} by {group_by}:\n{_table(table)}")


@tool(
    "crosstab",
    "Cross-tabulation of two categorical columns. normalize='index' shows row percentages, "
    "e.g. the share of patients with heart disease within each sex.",
    {
        **_col("row"),
        **_col("column"),
        "normalize": {"type": "string", "enum": ["none", "index"], "default": "index"},
    },
    required=["row", "column"],
)
def crosstab(tb: Toolbox, row: str, column: str, normalize: str = "index") -> ToolOutput:
    tb.data.require(row, column)
    counts = pd.crosstab(tb.df[row], tb.df[column], margins=True)
    text = f"Counts:\n{_table(counts)}"
    if normalize == "index":
        pct = (pd.crosstab(tb.df[row], tb.df[column], normalize="index") * 100).round(1)
        text += f"\n\nRow %:\n{_table(pct)}"
    return ToolOutput(text)


@tool(
    "binned_rate",
    "Split a numeric column into quantile bins and show the mean of a target in each bin, "
    "e.g. heart disease rate by age group. Good for spotting non-linear effects.",
    {
        **_col("column", "Numeric column to bin"),
        **_col("target", "Numeric or 0/1 target column"),
        "bins": {"type": "integer", "minimum": 2, "maximum": 10, "default": 5},
    },
    required=["column", "target"],
)
def binned_rate(tb: Toolbox, column: str, target: str, bins: int = 5) -> ToolOutput:
    tb.data.require(column, target)
    if not pd.api.types.is_numeric_dtype(tb.df[target]):
        raise ValueError(f"target {target!r} must be numeric (e.g. 0/1)")
    binned = pd.qcut(tb.df[column], q=bins, duplicates="drop")
    table = tb.df.groupby(binned, observed=True)[target].agg(["count", "mean"])
    table.index = table.index.astype(str)
    return ToolOutput(f"Mean of {target} by {column} quantile bin:\n{_table(table)}")


@tool(
    "find_outliers",
    "Outliers in a numeric column by the IQR rule (outside Q1 - 1.5*IQR and Q3 + 1.5*IQR), "
    "with counts, bounds and the most extreme values.",
    _col("column"),
    required=["column"],
)
def find_outliers(tb: Toolbox, column: str) -> ToolOutput:
    tb.data.require(column)
    s = tb.df[column].dropna()
    if not pd.api.types.is_numeric_dtype(s):
        raise ValueError(f"{column!r} is not numeric")
    q1, q3 = s.quantile([0.25, 0.75])
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    out = s[(s < lo) | (s > hi)]
    extremes = ", ".join(f"{v:g}" for v in pd.concat([out.nsmallest(3), out.nlargest(3)]).unique())
    return ToolOutput(
        f"{column}: {len(out)} outliers of {len(s)} ({100 * len(out) / max(1, len(s)):.1f}%). "
        f"Bounds [{lo:.4g}, {hi:.4g}]. Most extreme: {extremes or 'none'}"
    )


# --------------------------------------------------------------------------- statistics


@tool(
    "compare_groups",
    "Statistical test of whether `column` differs between the groups of `group_by`. Numeric "
    "column: Welch t-test + Mann-Whitney U with Cohen's d (2 groups) or ANOVA + Kruskal-Wallis "
    "(3+ groups). Categorical column: chi-square test with Cramer's V. Use it before claiming "
    "that a difference is real.",
    {**_col("column"), **_col("group_by")},
    required=["column", "group_by"],
)
def compare_groups(tb: Toolbox, column: str, group_by: str) -> ToolOutput:
    tb.data.require(column, group_by)
    df = tb.df[[column, group_by]].dropna()
    groups = {str(k): g[column] for k, g in df.groupby(group_by)}
    if len(groups) < 2:
        raise ValueError(f"{group_by!r} has fewer than two groups")
    if len(groups) > 20:
        raise ValueError(f"{group_by!r} has {len(groups)} groups; use a categorical column")

    if tb.data.kind(column) == "numeric":
        vals = list(groups.values())
        means = ", ".join(f"{k}: mean {v.mean():.4g} (n={len(v)})" for k, v in groups.items())
        if len(vals) == 2:
            a, b = vals
            t = stats.ttest_ind(a, b, equal_var=False)
            u = stats.mannwhitneyu(a, b)
            pooled = np.sqrt(
                ((len(a) - 1) * a.var() + (len(b) - 1) * b.var()) / (len(a) + len(b) - 2)
            )
            d = (a.mean() - b.mean()) / pooled if pooled else float("nan")
            body = (
                f"Welch t-test p={t.pvalue:.3g}; Mann-Whitney U p={u.pvalue:.3g}; "
                f"Cohen's d={d:.2f} (|d|: 0.2 small, 0.5 medium, 0.8 large)"
            )
        else:
            f = stats.f_oneway(*vals)
            k = stats.kruskal(*vals)
            body = f"ANOVA p={f.pvalue:.3g}; Kruskal-Wallis p={k.pvalue:.3g}"
        return ToolOutput(f"{column} by {group_by}: {means}\n{body}")

    table = pd.crosstab(df[group_by], df[column])
    chi2, p, dof, expected = stats.chi2_contingency(table)
    n = table.to_numpy().sum()
    v = np.sqrt(chi2 / (n * (min(table.shape) - 1))) if min(table.shape) > 1 else float("nan")
    warn = " (warning: some expected counts < 5)" if (expected < 5).any() else ""
    return ToolOutput(
        f"{column} vs {group_by}: chi-square={chi2:.2f}, dof={dof}, p={p:.3g}, "
        f"Cramer's V={v:.2f} (0.1 small, 0.3 medium, 0.5 large){warn}"
    )


# --------------------------------------------------------------------------- charts

PLOT_KINDS = ["histogram", "bar", "box", "scatter", "heatmap", "line"]


@tool(
    "plot",
    "Draw a chart, save it as PNG and return the image so you can look at it. Kinds: "
    "histogram (x numeric, optional hue), bar (x categorical; counts, or the mean of y; with "
    "hue: share of each hue level), box (x categorical, y numeric), scatter (x, y numeric, "
    "optional hue), heatmap (correlation matrix of numeric columns), line (x, y). "
    "Give every chart a title that states the finding.",
    {
        "kind": {"type": "string", "enum": PLOT_KINDS},
        **_col("x", "Column for the x axis (not needed for heatmap)"),
        **_col("y", "Column for the y axis, where the kind uses one"),
        **_col("hue", "Optional categorical column to split by colour"),
        "title": {"type": "string"},
        "bins": {"type": "integer", "minimum": 5, "maximum": 80, "default": 30},
    },
    required=["kind", "title"],
)
def plot(
    tb: Toolbox,
    kind: str,
    title: str,
    x: str | None = None,
    y: str | None = None,
    hue: str | None = None,
    bins: int = 30,
) -> ToolOutput:
    if kind not in PLOT_KINDS:
        raise ValueError(f"kind must be one of {PLOT_KINDS}")
    tb.data.require(x, y, hue)
    if kind != "heatmap" and not x:
        raise ValueError(f"{kind} needs x")
    if kind in ("box", "scatter", "line") and not y:
        raise ValueError(f"{kind} needs y")
    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=100)
    try:
        _draw(tb, fig, ax, kind, x, y, hue, bins)
        ax.set_title(title, fontsize=11)
        fig.tight_layout()
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", f"{kind}_{x or ''}_{y or ''}_{hue or ''}").strip("_")
        path = tb.out_dir / "charts" / f"{len(tb.charts) + 1:02d}_{slug}.png"
        fig.savefig(path)
    finally:
        # close even when drawing fails, or every failed call leaks a figure
        plt.close(fig)
    tb.charts.append(path)
    rel = path.relative_to(tb.out_dir).as_posix()
    return ToolOutput(f"Saved chart {rel}. Reference it in the report as ![{title}]({rel}).", path)


def _draw(
    tb: Toolbox, fig, ax, kind: str, x: str | None, y: str | None, hue: str | None, bins: int
) -> None:
    """Draw one chart of `kind` on `ax` (arguments already validated by `plot`)."""
    df = tb.df
    if kind == "histogram":
        if hue:
            for level, g in df.groupby(hue):
                ax.hist(g[x].dropna(), bins=bins, alpha=0.55, label=f"{hue}={level}")
            ax.legend(title=hue, bbox_to_anchor=(1.01, 1), loc="upper left")
        else:
            ax.hist(df[x].dropna(), bins=bins)
        ax.set_xlabel(x)
        ax.set_ylabel("count")
    elif kind == "bar":
        if hue:
            pct = pd.crosstab(df[x], df[hue], normalize="index") * 100
            pct.plot(kind="bar", ax=ax, rot=0)
            ax.legend(title=hue, bbox_to_anchor=(1.01, 1), loc="upper left")
            ax.set_ylabel(f"% within {x}")
        elif y:
            df.groupby(x)[y].mean().plot(kind="bar", ax=ax, rot=0)
            ax.set_ylabel(f"mean {y}")
        else:
            df[x].value_counts().sort_index().plot(kind="bar", ax=ax, rot=0)
            ax.set_ylabel("count")
        ax.set_xlabel(x)
    elif kind == "box":
        levels = sorted(df[x].dropna().unique(), key=str)
        ax.boxplot(
            [df.loc[df[x] == lv, y].dropna() for lv in levels], tick_labels=[str(v) for v in levels]
        )
        ax.set_xlabel(x)
        ax.set_ylabel(y)
    elif kind == "scatter":
        if hue:
            for level, g in df.groupby(hue):
                ax.scatter(g[x], g[y], s=12, alpha=0.6, label=f"{hue}={level}")
            ax.legend(title=hue, bbox_to_anchor=(1.01, 1), loc="upper left")
        else:
            ax.scatter(df[x], df[y], s=12, alpha=0.6)
        ax.set_xlabel(x)
        ax.set_ylabel(y)
    elif kind == "line":
        d = df[[x, y]].dropna().sort_values(x)
        ax.plot(d[x], d[y])
        ax.set_xlabel(x)
        ax.set_ylabel(y)
    elif kind == "heatmap":
        corr = df[tb.data.numeric_columns()].corr(method="spearman")
        im = ax.imshow(corr, cmap="RdBu_r", vmin=-1, vmax=1)
        ax.set_xticks(range(len(corr)), corr.columns, rotation=45, ha="right", fontsize=8)
        ax.set_yticks(range(len(corr)), corr.columns, fontsize=8)
        for i in range(len(corr)):
            for j in range(len(corr)):
                v = corr.iat[i, j]
                color = "white" if abs(v) > 0.6 else "black"
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7, color=color)
        fig.colorbar(im, ax=ax)
