import pytest

from csvagent.data import Dataset, load_csv
from csvagent.tools import PLOT_KINDS


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
