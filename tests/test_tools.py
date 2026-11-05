import numpy as np
import pytest

from csvagent.data import Dataset, load_csv
from csvagent.tools import PLOT_KINDS, Toolbox


def test_kinds(data):
    assert data.kind("Age") == "numeric"
    assert data.kind("HeartDisease") == "categorical"  # 0/1 integer
    assert data.kind("ChestPainType") == "categorical"


def test_load_csv_sniffs_semicolons_and_turkish_encoding(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes("şehir;değer\nİstanbul;1\nİzmir;2\n".encode("cp1254"))
    df = load_csv(p)
    assert list(df.columns) == ["şehir", "değer"]
    assert df["şehir"].tolist() == ["İstanbul", "İzmir"]


def test_specs_are_valid_anthropic_tool_definitions(toolbox):
    specs = toolbox.specs()
    names = {s["name"] for s in specs}
    assert {"describe_dataset", "compare_groups", "plot"} <= names
    for s in specs:
        assert s["input_schema"]["type"] == "object"
        assert set(s["input_schema"]["required"]) <= set(s["input_schema"]["properties"])
        assert len(s["description"]) > 30


def test_describe_dataset(toolbox):
    out = toolbox.run("describe_dataset", {})
    assert "300 rows x 6 columns" in out.text
    assert "Cholesterol" in out.text and "Sample rows" in out.text


def test_column_summary_flags_zeros(toolbox):
    out = toolbox.run("column_summary", {"column": "Cholesterol"})
    assert "zeros" in out.text
    cat = toolbox.run("column_summary", {"column": "Sex"})
    assert "share_%" in cat.text


def test_unknown_column_error_lists_columns(toolbox):
    with pytest.raises(KeyError, match="available"):
        toolbox.run("column_summary", {"column": "Chol"})


def test_correlations_with_target(toolbox):
    out = toolbox.run("correlations", {"target": "HeartDisease"})
    assert "Age" in out.text and "MaxHR" in out.text
    assert "q_fdr" in out.text and "3 tests" in out.text


@pytest.mark.parametrize("method", ["pearson", "spearman"])
def test_correlation_pvalues_match_scipy(data, method):
    from scipy import stats

    from csvagent.tools import _corr_pvalues

    df = data.df.copy()
    df.loc[::7, "MaxHR"] = np.nan  # pairwise-complete n must be used
    pair = df[["MaxHR", "HeartDisease"]].dropna()
    test = stats.pearsonr if method == "pearson" else stats.spearmanr
    expected = test(pair["MaxHR"], pair["HeartDisease"])
    r = df["MaxHR"].corr(df["HeartDisease"], method=method)
    assert r == pytest.approx(expected.statistic)
    assert _corr_pvalues(r, len(pair)) == pytest.approx(expected.pvalue, rel=1e-6)


def test_correlations_skip_constant_columns_and_use_pairwise_n(data, tmp_path):
    df = data.df.copy()
    df["Constant"] = 1.0
    df.loc[::7, "MaxHR"] = np.nan
    out = Toolbox(Dataset(df, "d"), tmp_path).run("correlations", {}).text
    assert "Constant" not in out
    assert "6 tests" in out  # 4 non-constant numeric columns -> 6 pairs
    assert " 257" in out and " 300" in out  # MaxHR pairs have fewer rows


def test_group_stats_crosstab_binned_rate(toolbox):
    assert "mean" in toolbox.run("group_stats", {"group_by": "Sex", "column": "Age"}).text
    ct = toolbox.run("crosstab", {"row": "ChestPainType", "column": "HeartDisease"})
    assert "Row %" in ct.text
    br = toolbox.run("binned_rate", {"column": "Age", "target": "HeartDisease", "bins": 4})
    assert br.text.count("\n") >= 5


def test_outliers(toolbox):
    out = toolbox.run("find_outliers", {"column": "Cholesterol"})
    assert "outliers" in out.text  # the injected zeros sit far below Q1


def test_compare_groups_numeric_and_categorical(toolbox):
    num = toolbox.run("compare_groups", {"column": "Age", "group_by": "HeartDisease"})
    assert "Welch" in num.text and "Cohen's d" in num.text
    many = toolbox.run("compare_groups", {"column": "Age", "group_by": "ChestPainType"})
    assert "ANOVA" in many.text
    cat = toolbox.run("compare_groups", {"column": "Sex", "group_by": "HeartDisease"})
    assert "chi-square" in cat.text and "Cramer" in cat.text


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "histogram", "x": "Age", "hue": "HeartDisease"},
        {"kind": "bar", "x": "ChestPainType", "hue": "HeartDisease"},
        {"kind": "bar", "x": "Sex", "y": "Age"},
        {"kind": "box", "x": "HeartDisease", "y": "MaxHR"},
        {"kind": "scatter", "x": "Age", "y": "MaxHR", "hue": "HeartDisease"},
        {"kind": "heatmap"},
        {"kind": "line", "x": "Age", "y": "MaxHR"},
    ],
)
def test_every_plot_kind_saves_a_png(toolbox, args):
    out = toolbox.run("plot", {**args, "title": "test chart"})
    assert out.image is not None and out.image.exists()
    assert out.image.read_bytes()[:4] == b"\x89PNG"
    assert "charts/" in out.text and out.image in toolbox.charts


def test_plot_validates_arguments(toolbox):
    with pytest.raises(ValueError, match="needs y"):
        toolbox.run("plot", {"kind": "box", "x": "Sex", "title": "t"})
    assert len(PLOT_KINDS) == 6


def _many_groups(tmp_path, levels: int = 40):
    """Toolbox over a frame where `country` has many levels, some with only 1-2 rows."""
    import pandas as pd

    rng = np.random.default_rng(1)
    sizes = [30] * 10 + [2] * (levels - 10)
    country = np.repeat([f"Country number {i}" for i in range(levels)], sizes)
    score = rng.normal(80, 3, len(country)) + np.repeat(np.arange(levels) % 5, sizes)
    df = pd.DataFrame({"country": country, "score": score})
    return Toolbox(Dataset(df, "many"), tmp_path), df


def test_compare_groups_with_many_groups_leaves_out_small_ones(tmp_path):
    from scipy import stats

    tb, df = _many_groups(tmp_path)
    out = tb.run("compare_groups", {"column": "score", "group_by": "country"}).text
    assert "10 groups: ANOVA" in out
    assert "Left out 30 groups with fewer than 5 rows (60 rows)" in out and "20 more" in out
    # eta-squared agrees with the F statistic: eta2 = F(k-1) / (F(k-1) + N - k)
    kept = [g["score"] for _, g in df.groupby("country") if len(g) >= 5]
    f = stats.f_oneway(*kept).statistic
    k, n = len(kept), sum(map(len, kept))
    eta2 = f * (k - 1) / (f * (k - 1) + n - k)
    assert f"eta-squared={eta2:.3f}" in out


def test_compare_groups_errors_when_too_few_groups_are_big_enough(tmp_path):
    tb, _ = _many_groups(tmp_path)
    with pytest.raises(ValueError, match="at least 100 rows"):
        tb.run("compare_groups", {"column": "score", "group_by": "country", "min_group_size": 100})


def test_bar_with_many_categories_is_horizontal_and_capped(tmp_path):
    import matplotlib.image as mpimg

    tb, _ = _many_groups(tmp_path)
    out = tb.run("plot", {"kind": "bar", "x": "country", "y": "score", "title": "t"})
    assert "Showing the 30 largest of 40 'country' groups." in out.text
    height, width = mpimg.imread(out.image).shape[:2]
    assert height > width  # tall figure: one horizontal bar per country


def test_failed_plot_closes_its_figure(toolbox):
    import matplotlib.pyplot as plt

    plt.close("all")
    with pytest.raises(TypeError):  # box plot of a text column fails while drawing
        toolbox.run("plot", {"kind": "box", "x": "Sex", "y": "ChestPainType", "title": "t"})
    assert plt.get_fignums() == []
    assert toolbox.charts == []


def test_dataset_from_csv(tmp_path, data):
    p = tmp_path / "heart.csv"
    data.df.to_csv(p, index=False)
    assert Dataset.from_csv(p).name == "heart"
