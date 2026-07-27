"""Public natural-language web app over the SCPRS gold warehouse (Gradio).

Anyone with the URL can ask a question in plain English and see three things: a
short answer, the SQL the model wrote, and the result table. It is read-only and
free by construction — queries run through the hardened guard
(``src.warehouse_query``) and the language step is a free-tier provider
(``src.nl_query``). No login: the data is a public procurement portal.

Run locally:  ``python -m src.web_app``  (needs GEMINI_API_KEY set)
On a Hugging Face Gradio Space, ``app.py`` calls ``build_demo().launch()``.
"""

from __future__ import annotations

import os
import tempfile

import gradio as gr

from . import charting, nl_query, query_log
from . import warehouse_query as wq

_INTRO = """# 🏛️ Ask the SCPRS procurement warehouse

Type a question about California's SCPRS procurement data in plain English —
suppliers, departments, spend, contracts, line items. You'll get a short answer,
the SQL that produced it, and the underlying rows. **Read-only, public data.**
"""

_EXAMPLES = [
    "Which 10 suppliers had the highest total spend?",
    "What did the state spend the most on last fiscal year?",
    "Show total spend by department, highest first.",
    "How many contracts did MAXIMUS have and what were they worth?",
    "What are the largest contract amendments by value increase?",
]

_MAX_TABLE_ROWS = 20


def _cell(col: str, v) -> str:
    """One Markdown table cell: money/percent/count formatting from charting,
    with the characters that would break a Markdown table neutralised."""
    return charting._fmt_cell(col, v).replace("|", "&#124;").replace("\n", " ")


def _md_table(result: dict) -> str:
    """Render up to `_MAX_TABLE_ROWS` result rows as a Markdown table — money as
    $1,234,567.89, year-keyed tables newest-first."""
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    if not cols:
        return ""
    rows = charting._order_rows(cols, rows)
    head = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = [
        "| " + " | ".join(_cell(c, r.get(c)) for c in cols) + " |" for r in rows[:_MAX_TABLE_ROWS]
    ]
    table = "\n".join([head, sep, *body])
    extra = len(rows) - _MAX_TABLE_ROWS
    if extra > 0:
        table += f"\n\n_…and {extra} more row(s)._"
    elif result.get("truncated"):
        table += "\n\n_(results truncated at the row cap)_"
    return table


# A chart accompanies the answer when the question asks for a trend or a
# distribution, or when the result is plainly a time series. Rankings and plain
# lookups stay table-only — charting everything is noise.
_TREND_WORDS = (
    "trend",
    "over time",
    "monthly",
    "by month",
    "by year",
    "per year",
    "history",
    "growth",
    "timeline",
)
_DIST_WORDS = (
    "distribution",
    "share",
    "breakdown",
    "versus",
    " vs ",
    " vs?",
    "proportion",
    "split",
    "composition",
)
_TIME_COL_HINTS = ("year", "month", "date")


def _is_time_col(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _TIME_COL_HINTS)


def _maybe_chart_png(question: str, result: dict) -> bytes | None:
    """Render a PNG for trend/distribution-shaped turns; None when no chart fits."""
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    if len(rows) < 3:
        return None
    try:
        x, y = charting._pick_axes(cols, rows, None, None)
    except ValueError:
        return None
    ql = f" {question.lower()} "
    temporal = _is_time_col(x)
    if temporal or any(w in ql for w in _TREND_WORDS):
        kind = "line"
    elif any(w in ql for w in _DIST_WORDS):
        kind = "pie" if len(rows) <= 8 else "bar"
    else:
        return None
    data = rows
    if temporal:  # charts read left-to-right in time, even when the table is newest-first
        if all(charting._to_float(r.get(x)) is not None for r in rows):
            data = sorted(rows, key=lambda r: charting._to_float(r.get(x)))
        else:
            data = sorted(rows, key=lambda r: str(r.get(x)))
    try:
        return charting.render_chart(
            cols,
            data,
            kind=kind,
            title=question.strip().rstrip("?")[:70],
            x=x,
            y=y,
            max_points=60 if kind == "line" else 20,
        )
    except Exception:  # noqa: BLE001 — a chart is a bonus, never break the answer
        return None


def _respond(message: str, history):
    """One chat turn → Markdown answer + collapsible SQL + result table, plus a
    chart message when the question is trend/distribution-shaped."""
    message = (message or "").strip()
    if not message:
        return "Ask me something about the SCPRS procurement data."
    try:
        out = nl_query.answer(message, history=history)
    except Exception as exc:  # noqa: BLE001 — surface any provider/config error to the user
        return (
            "⚠️ The language service isn't available right now "
            f"({type(exc).__name__}: {exc}). "
            "If this is a fresh deploy, the `GEMINI_API_KEY` secret may be missing."
        )
    query_log.record(message, out, prior_turns=len(history or []) // 2)
    parts = [out["answer"]]
    if out.get("sql"):
        parts.append(f"<details><summary>SQL</summary>\n\n```sql\n{out['sql']}\n```\n</details>")
    png = None
    if out.get("result") and out["result"].get("columns"):
        parts.append(_md_table(out["result"]))
        png = _maybe_chart_png(message, out["result"])
    text = "\n\n".join(p for p in parts if p)
    if png is None:
        return text
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(png)
    return [
        gr.ChatMessage(content=text),
        gr.ChatMessage(content=gr.Image(value=fh.name)),
    ]


def build_demo() -> gr.Blocks:
    """Assemble the Gradio chat UI."""
    marts = len(wq.list_marts())
    with gr.Blocks(title="SCPRS Warehouse Chat", fill_height=True) as demo:
        gr.Markdown(_INTRO)
        # Gradio 6 dropped ChatInterface's `type` kwarg — the OpenAI-style
        # "messages" history format (list of {role, content}) is now the default,
        # which is what _respond / nl_query.answer already expect.
        gr.ChatInterface(
            fn=_respond,
            examples=_EXAMPLES,
            cache_examples=False,
        )
        note = (
            f"_Backed by {marts} analytical marts/tables. Answers are generated; "
            "verify anything material against the source SCPRS records._"
        )
        if os.environ.get("QUERY_LOG_DATASET"):
            note += "\n\n_Questions (and the generated SQL) are logged to improve the app._"
        gr.Markdown(note)
    return demo


if __name__ == "__main__":
    build_demo().launch()
