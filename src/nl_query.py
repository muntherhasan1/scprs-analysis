"""Natural-language → SQL over the gold warehouse, using a free-tier LLM.

The model only *writes* SQL and phrases the final answer; every query it produces
is executed through the hardened read-only guard in
``warehouse_query.run_select`` (single ``SELECT``/``WITH``, read-only
connection — a write is impossible). The provider is Google Gemini by default,
whose free tier is genuinely free: with no billing account attached the key can
only be **rate-limited**, never billed. Set ``GEMINI_API_KEY`` (and optionally
``GEMINI_MODEL``).
"""

from __future__ import annotations

import json
import os

from . import warehouse_query as wq

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_GUIDE = """You translate questions about California SCPRS procurement data into SQLite SQL.

Rules:
- Return exactly ONE read-only statement: a SELECT, or WITH ... SELECT. Never a
  write, never multiple statements, no trailing semicolon, no SQL comments.
- Query ONLY the views listed in the schema below. Prefer the gold_* marts and
  lv_* views — they have friendly column names.
- For vendor/supplier spend totals use the CANONICAL marts
  (gold_canonical_supplier_spend, gold_supplier_master); the per-supplier marts
  double-count vendors that registered more than once.
- Dollar amounts are plain numbers. Use LIMIT for "top N" questions.
- If the question cannot be answered from these views, return exactly: NO_QUERY

Output ONLY the SQL (or NO_QUERY) — no markdown code fences, no explanation."""


def _client():
    """A Gemini client from GEMINI_API_KEY (imported lazily so the web app can
    start and report a clear error if the key is missing)."""
    from google import genai

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set — get a free key at aistudio.google.com/apikey"
        )
    return genai.Client(api_key=key)


def _extract_sql(text: str) -> str:
    """Strip markdown fences / stray prose the model may wrap around the SQL."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t[:3].lower() == "sql":
            t = t[3:]
    return t.strip().rstrip(";").strip()


def generate_sql(question: str, schema: str) -> str:
    """Ask the model for a single read-only SQL statement (or ``NO_QUERY``)."""
    prompt = (
        f"{_GUIDE}\n\nSchema (view (row count): columns):\n{schema}\n\n"
        f"Question: {question}\nSQL:"
    )
    resp = _client().models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return _extract_sql(resp.text)


def summarize(question: str, result: dict) -> str:
    """Phrase a short natural-language answer from the actual query results."""
    preview = {
        "columns": result.get("columns"),
        "rows": result.get("rows", [])[:50],
        "truncated": result.get("truncated"),
    }
    prompt = (
        "Answer the user's question in 1–3 sentences using ONLY these query "
        "results. Include the key numbers; format large dollar amounts readably. "
        "If there are no rows, say no matching records were found.\n\n"
        f"Question: {question}\nResults (JSON): {json.dumps(preview, default=str)}\nAnswer:"
    )
    resp = _client().models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return (resp.text or "").strip()


def answer(question: str, max_rows: int = 200) -> dict:
    """Full turn: NL question → SQL → guarded execution → NL answer.

    Returns ``{answer, sql, result}``; ``sql``/``result`` may be ``None`` when the
    model declines (``NO_QUERY``). Never raises for a bad query — the guard turns
    that into ``result['error']`` and a friendly answer.
    """
    sql = generate_sql(question, wq.schema_for_llm())
    if not sql or sql.strip() == "NO_QUERY":
        return {
            "answer": "I can't answer that from the SCPRS warehouse. Try asking about "
            "suppliers, departments, spend, contracts, or line items.",
            "sql": None,
            "result": None,
        }
    result = wq.run_select(sql, max_rows=max_rows)
    if "error" in result:
        return {
            "answer": f"I wrote a query but it couldn't run ({result['error']}). "
            "Try rephrasing the question.",
            "sql": sql,
            "result": result,
        }
    return {"answer": summarize(question, result), "sql": sql, "result": result}
