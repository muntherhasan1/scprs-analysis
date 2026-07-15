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

import gradio as gr

from . import nl_query, query_log
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


def _md_table(result: dict) -> str:
    """Render up to `_MAX_TABLE_ROWS` result rows as a Markdown table."""
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    if not cols:
        return ""
    head = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = [
        "| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |"
        for r in rows[:_MAX_TABLE_ROWS]
    ]
    table = "\n".join([head, sep, *body])
    extra = len(rows) - _MAX_TABLE_ROWS
    if extra > 0:
        table += f"\n\n_…and {extra} more row(s)._"
    elif result.get("truncated"):
        table += "\n\n_(results truncated at the row cap)_"
    return table


def _respond(message: str, history) -> str:
    """One chat turn → Markdown answer + collapsible SQL + result table."""
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
    if out.get("result") and out["result"].get("columns"):
        parts.append(_md_table(out["result"]))
    return "\n\n".join(p for p in parts if p)


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
