# CSV Analyst Agent

An LLM agent that analyzes a CSV file on its own. It explores the data with a set
of **safe, typed analysis tools**, checks data quality, draws charts and **looks at
them**, backs its claims with **statistical tests**, and writes a report with
findings and recommendations.

Built directly on the Anthropic Messages API (tool use + an agent loop), with no
agent framework, so every step of the loop is visible in the code.

> Türkçe özet en altta.

## How it works

```
question + CSV
     │
     ▼
┌─────────────┐   tool_use: compare_groups(Age, HeartDisease)   ┌───────────────┐
│   Claude    │ ───────────────────────────────────────────────►│   Toolbox     │
│ (plans the  │                                                 │ pandas/scipy/ │
│  analysis)  │ ◄───────────────────────────────────────────────│ matplotlib    │
└─────────────┘   tool_result: "p=3e-12, Cohen's d=0.9" (+ PNG) └───────────────┘
     │   ... repeats until the model stops calling tools (or hits the step limit)
     ▼
report.md + report.html (charts embedded) + steps.jsonl (audit log)
```

1. The model gets the task and the tool definitions (JSON schemas).
2. It replies with `tool_use` blocks. The loop runs each tool and sends the results
   back as `tool_result` blocks. **Charts go back as images**, so the model reads
   the chart it just drew instead of guessing.
3. A failed tool call (wrong column name, wrong chart arguments) goes back with
   `is_error: true` and the list of valid columns, and the model corrects itself.
4. When the model answers without calling a tool, that answer is the report. At
   the step limit it is asked to write the report from what it already has. If the
   report is cut off at the output token limit, it is flagged as incomplete
   (raise it with `--max-tokens`).

## Tools

| Tool | What it does |
|---|---|
| `describe_dataset` | Size, column types, missing values, distinct values, duplicates, sample rows |
| `column_summary` | Statistics or value counts for one column, including the number of exact zeros (often disguised missing values) |
| `correlations` | Spearman/Pearson correlations, with a target or the strongest pairs |
| `group_stats` | A numeric column summarized per group |
| `crosstab` | Two categorical columns, counts and row percentages |
| `binned_rate` | Target rate per quantile bin of a numeric column (non-linear effects) |
| `find_outliers` | IQR outliers with bounds and the most extreme values |
| `compare_groups` | Welch t-test + Mann-Whitney U + Cohen's d, ANOVA + Kruskal-Wallis, or chi-square + Cramér's V |
| `plot` | histogram, bar, box, scatter, heatmap, line, saved as PNG and returned to the model |

**Why tools instead of letting the model write pandas code?** Generated code
needs a sandbox, can fail in many ways, and is hard to audit. Typed tools are
safe to run on any machine, they validate their input, and every call is logged
with its arguments. The trade-off is flexibility: the agent can only do what the
toolbox offers.

The system prompt sets the analyst's working method: data quality first, test
before claiming, "associated with" rather than "causes", and every number in the
report taken from a tool result.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[app,dev]"
copy .env.example .env      # macOS/Linux: cp .env.example .env -> add ANTHROPIC_API_KEY
```

## Usage

```bash
csvagent analyze heart.csv
csvagent analyze heart.csv -q "Kalp hastalığıyla en çok ilişkili faktörler neler?"
csvagent analyze heart.csv --max-steps 30 --model claude-haiku-5-5

streamlit run app/streamlit_app.py     # upload a CSV, watch the steps live, download the report
```

Each run writes to `reports/<name>_<time>/`:

- `report.md`: the report plus a table of every tool call
- `report.html`: a single shareable file with the charts embedded
- `charts/*.png`
- `steps.jsonl`: the full audit log (tool, input, output, duration)

The report is written in the language of the question.

## Example: heart failure dataset

The [Heart Failure Prediction dataset](https://www.kaggle.com/datasets/fedesoriano/heart-failure-prediction)
(918 patients, 11 features) is a good test, because it has a realistic trap: many
`Cholesterol` values are recorded as 0, which is a missing value and not a real
measurement.

<!-- After running: paste 2-3 findings from your report here, with one chart. -->

_Run `csvagent analyze heart.csv` and add a short excerpt of the report here._

## Tests

```bash
pytest -q     # uses a fake Anthropic client and a synthetic dataset; no API key needed
ruff check .
```

The agent tests replay scripted model responses and check the protocol details:
tool results go back in one user message in the right order, charts are sent as
image blocks, tool errors are returned instead of raised, and the step limit
forces a final report. CI runs lint and tests on every push.

## Project layout

```
src/csvagent/
  data.py     CSV loading (delimiter sniffing, Turkish encodings), column kinds
  tools.py    the toolbox: typed analysis and chart tools with JSON schemas
  agent.py    the tool-use loop
  report.py   Markdown/HTML report and step log
  cli.py      `csvagent analyze`
app/streamlit_app.py
tests/
```

## Limitations

- The agent can only analyze what the toolbox supports. No modelling, joins or
  time-series tools yet.
- Many tests in one run inflate false positives (multiple comparisons). The
  report shows the p-values, so the reader can judge.
- Long runs with many charts use more tokens, because each image goes back to the model.

---

## Türkçe özet

Bir CSV dosyasını kendi başına analiz eden bir LLM ajanı. Veriyi güvenli, tipli
analiz araçlarıyla inceliyor ve önce veri kalitesine bakıyor. Grafik çiziyor ve
çizdiği grafiği görüntü olarak geri alıp yorumluyor. İddialarını istatistiksel
testlerle (t-testi, Mann-Whitney, ANOVA, ki-kare, etki büyüklüğü) destekliyor.
Sonunda bulgular ve öneriler içeren bir rapor yazıyor.

Model kod çalıştırmıyor, yalnızca tanımlı araçları çağırabiliyor. Bu yüzden her
adım güvenli ve kayıt altında. Ajan döngüsü, bir framework kullanılmadan
doğrudan Anthropic Messages API ile yazıldı.
