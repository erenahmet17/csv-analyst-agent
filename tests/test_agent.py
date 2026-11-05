import pytest

from csvagent.agent import CSVAgent, guess_language
from csvagent.report import split_markdown_images, write_report


def test_agent_runs_tools_and_returns_report(toolbox, fake):
    client = fake.Client(
        [
            fake.response(fake.text("Let me look."), fake.tool_use("t1", "describe_dataset")),
            fake.response(
                fake.tool_use("t2", "compare_groups", column="Age", group_by="HeartDisease"),
                fake.tool_use("t3", "plot", kind="box", x="HeartDisease", y="Age", title="Age"),
            ),
            fake.response(
                fake.text("# Report\n\nOlder patients...\n\n![Age](charts/01_x.png)"),
                stop="end_turn",
            ),
        ]
    )
    steps = []
    result = CSVAgent(toolbox, client=client, model="fake").run("analyze", on_step=steps.append)

    assert result.stop_reason == "done"
    assert result.report.startswith("# Report")
    assert [s.tool for s in result.steps] == ["describe_dataset", "compare_groups", "plot"]
    assert len(steps) == 3 and result.charts and result.turns == 3
    assert result.input_tokens == 300

    # tool results go back in ONE user message, in order, and the chart as an image block
    last_user = client.requests[2]["messages"][-1]
    assert last_user["role"] == "user"
    ids = [b["tool_use_id"] for b in last_user["content"]]
    assert ids == ["t2", "t3"]
    plot_content = last_user["content"][1]["content"]
    assert plot_content[1]["type"] == "image"
    assert plot_content[1]["source"]["media_type"] == "image/png"
    # tool definitions are sent on every call
    assert any(t["name"] == "plot" for t in client.requests[0]["tools"])


def test_tool_errors_are_sent_back_not_raised(toolbox, fake):
    client = fake.Client(
        [
            fake.response(fake.tool_use("t1", "column_summary", column="Chol")),
            fake.response(fake.tool_use("t2", "column_summary", column="Cholesterol")),
            fake.response(fake.text("done"), stop="end_turn"),
        ]
    )
    result = CSVAgent(toolbox, client=client).run("x")
    assert result.steps[0].is_error and "available" in result.steps[0].output
    err = client.requests[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True
    assert not result.steps[1].is_error


def test_unknown_tool_name_is_an_error_result(toolbox, fake):
    client = fake.Client(
        [
            fake.response(fake.tool_use("t1", "run_python", code="x")),
            fake.response(fake.text("ok"), stop="end_turn"),
        ]
    )
    result = CSVAgent(toolbox, client=client).run("x")
    assert result.steps[0].is_error and "unknown tool" in result.steps[0].output


def test_step_limit_forces_a_final_report(toolbox, fake):
    loop = [fake.response(fake.tool_use(f"t{i}", "describe_dataset")) for i in range(3)]
    client = fake.Client(loop + [fake.response(fake.text("partial report"), stop="end_turn")])
    result = CSVAgent(toolbox, client=client, max_steps=3).run("x")
    assert result.stop_reason == "step_limit"
    assert result.report == "partial report"
    final = client.requests[-1]
    assert final["tool_choice"] == {"type": "none"}
    # the instruction rides in the same user message as the last tool results, after them
    last = final["messages"][-1]["content"]
    assert last[0]["type"] == "tool_result" and last[-1]["type"] == "text"


def test_report_cut_off_at_max_tokens_is_flagged(toolbox, fake):
    client = fake.Client([fake.response(fake.text("# Report\n\nHalf a sent"), stop="max_tokens")])
    result = CSVAgent(toolbox, client=client).run("x")
    assert result.truncated and result.stop_reason == "done"
    md = write_report(result, toolbox.out_dir, "d")["markdown"].read_text(encoding="utf-8")
    assert md.startswith("> **Incomplete report:**")


def test_complete_report_is_not_flagged(toolbox, fake):
    client = fake.Client([fake.response(fake.text("full report"), stop="end_turn")])
    result = CSVAgent(toolbox, client=client).run("x")
    assert not result.truncated
    md = write_report(result, toolbox.out_dir, "d")["markdown"].read_text(encoding="utf-8")
    assert "Incomplete report" not in md


def test_default_model_is_read_at_call_time(toolbox, fake, monkeypatch):
    # The Streamlit app imports the agent before load_dotenv(); the env var set
    # afterwards must still win.
    monkeypatch.setenv("CSVAGENT_MODEL", "from-env")
    assert CSVAgent(toolbox, client=fake.Client([])).model == "from-env"
    assert CSVAgent(toolbox, client=fake.Client([]), model="explicit").model == "explicit"


def test_task_tells_the_model_todays_date(toolbox, fake):
    from datetime import date

    client = fake.Client([fake.response(fake.text("ok"), stop="end_turn")])
    CSVAgent(toolbox, client=client, today=date(2026, 11, 5)).run("x")
    task = client.requests[0]["messages"][0]["content"]
    assert "Today's date: 2026-11-05." in task
    # the language rule sits right after the request, where the model reads it
    assert task.index("Request: x") < task.index("same language as the Request")


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Analyze this data and find what is linked to the score.", "English"),
        ("Kalp hastalığıyla en çok ilişkili faktörler neler?", "Turkish"),
        ("bu veriyi analiz et ve sonuçları yaz", "Turkish"),  # no Turkish letters
        ("Analysiere diese Daten und finde Muster.", None),  # unknown: model decides
        ("x", None),
    ],
)
def test_guess_language(question, expected):
    assert guess_language(question) == expected


def test_report_language_is_named_in_the_task(toolbox, fake):
    def task_for(language=None):
        client = fake.Client([fake.response(fake.text("ok"), stop="end_turn")])
        CSVAgent(toolbox, client=client, language=language).run("Analyze this and the rest")
        return client.requests[0]["messages"][0]["content"]

    assert task_for().endswith("every chart title in English.")  # guessed
    assert task_for("Spanish").endswith("every chart title in Spanish.")  # explicit wins


def test_images_can_be_disabled(toolbox, fake):
    client = fake.Client(
        [
            fake.response(fake.tool_use("t1", "plot", kind="heatmap", title="corr")),
            fake.response(fake.text("ok"), stop="end_turn"),
        ]
    )
    CSVAgent(toolbox, client=client, send_images=False).run("x")
    content = client.requests[1]["messages"][-1]["content"][0]["content"]
    assert [b["type"] for b in content] == ["text"]


def test_write_report(toolbox, fake, tmp_path):
    client = fake.Client(
        [
            fake.response(fake.tool_use("t1", "plot", kind="heatmap", title="Correlations")),
            fake.response(
                fake.text("## Findings\n\n![Correlations](charts/01_heatmap.png)"), stop="end_turn"
            ),
        ]
    )
    result = CSVAgent(toolbox, client=client, model="fake").run("x")
    paths = write_report(result, toolbox.out_dir, "heart_synthetic")
    md = paths["markdown"].read_text()
    assert "## Analysis steps" in md and "`plot`" in md
    html = paths["html"].read_text()
    assert "data:image/png;base64," in html  # chart embedded, file is self-contained
    assert paths["log"].read_text().count("\n") == 1


def test_split_markdown_images():
    parts = split_markdown_images("a\n![t](charts/1.png)\nb")
    assert parts == [("text", "a\n"), ("image", "charts/1.png"), ("text", "\nb")]


def test_unknown_block_types_are_passed_through(toolbox, fake):
    class Thinking:
        type = "thinking"

        def model_dump(self, exclude_none=True):
            return {"type": "thinking", "thinking": "plan", "signature": "sig"}

    client = fake.Client(
        [
            fake.response(Thinking(), fake.tool_use("t1", "describe_dataset")),
            fake.response(fake.text("ok"), stop="end_turn"),
        ]
    )
    result = CSVAgent(toolbox, client=client).run("x")
    assert result.report == "ok"
    assistant = client.requests[1]["messages"][1]["content"]
    assert assistant[0] == {"type": "thinking", "thinking": "plan", "signature": "sig"}
