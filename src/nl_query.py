"""Natural-language → SQL over the gold warehouse, using a free-tier LLM.

The model only *writes* SQL and phrases the final answer; every query it produces
is executed through the hardened read-only guard in
``warehouse_query.run_select`` (single ``SELECT``/``WITH``, read-only
connection — a write is impossible). The provider is Google Gemini, whose free
tier is genuinely free: with no billing account attached the key can only be
**rate-limited**, never billed. Set ``GEMINI_API_KEY`` (and optionally
``GEMINI_MODEL``).

We call the Gemini REST endpoint directly with a fresh, self-contained
``httpx.Client`` per request rather than the ``google-genai`` SDK — the SDK keeps
a stateful httpx client whose lifecycle breaks inside Gradio's threaded worker
("Cannot send a request, as the client has been closed"). Stateless calls avoid
that and drop a dependency.
"""

from __future__ import annotations

import json
import os
import time

import httpx

from . import warehouse_query as wq

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_GENERATE = _BASE + "/{model}:generateContent"

# Ranked candidate model ids (best first), cached after the first ListModels
# lookup so we don't re-list on every request.
_ranked: list[str] = []

# Status codes worth retrying / falling back on: rate limit and server overload.
_TRANSIENT = {429, 500, 503}

_GUIDE = """You translate questions about California SCPRS procurement data into SQLite SQL.

Rules:
- Return exactly ONE read-only statement: a SELECT, or WITH ... SELECT. Never a
  write, never multiple statements, no trailing semicolon, no SQL comments.
- Query ONLY the views listed in the schema below. Prefer the gold_* marts and
  lv_* views — they have friendly column names.
- For vendor/supplier spend totals use the CANONICAL marts
  (gold_canonical_supplier_spend, gold_supplier_master); the per-supplier marts
  double-count vendors that registered more than once.
- Canonical marts are keyed by canonical_id/canonical_name (one row per real
  company); per-supplier marts (gold_supplier_specialization, gold_supplier_profile,
  ...) are keyed by supplier_id. canonical_id and supplier_id are DIFFERENT — never
  join on them. To attach a per-supplier attribute (e.g. what they supply) to
  canonical spend, LEFT JOIN on name (UPPER(canonical_name)=UPPER(supplier_name)) so
  the top suppliers are not dropped.
- "Contracts" vs "purchases": gold_contract_vs_standalone has one row per
  document_type; the values are 'contract (has POs)' and 'standalone'. When asked
  specifically about contracts, filter WHERE document_type LIKE 'contract%' — do
  NOT sum both rows (that counts all documents, not just contracts).
- Line-level questions that combine supplier + category + time use gold_line_item
  (supplier_name, category, unspsc, line_amount, start_date, calendar_year,
  fiscal_year). fiscal_year is the California fiscal year (Jul 1–Jun 30, labelled
  by the year it ends in, so July 2021 is fiscal_year 2022).
- For "the past N fiscal years", don't hardcode years — filter
  fiscal_year > (SELECT MAX(fiscal_year) FROM gold_line_item) - N.
- Broad category words like "IT" or "IT Services" are NOT stored literally in
  category (it holds granular UNSPSC descriptions). OR-match several terms, e.g.
  (UPPER(category) LIKE '%INFORMATION TECHNOLOGY%' OR UPPER(category) LIKE '%SOFTWARE%'
  OR UPPER(category) LIKE '%COMPUTER%' OR UPPER(category) LIKE '%TELECOMMUNICATION%').
- Dollar amounts are plain numbers. Use LIMIT for "top N" questions.
- When filtering by a name or text the user typed, match LOOSELY, not with
  equality: use WHERE UPPER(col) LIKE UPPER('%value%'). Stored names are often
  longer and upper-cased (e.g. the user's "MAXIMUS" is "MAXIMUS HUMAN SERVICES
  INC"), so `= 'MAXIMUS'` would wrongly find nothing.
- If the question cannot be answered from these views, return exactly: NO_QUERY

Output ONLY the SQL (or NO_QUERY) — no markdown code fences, no explanation."""


def _require_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set — get a free key at aistudio.google.com/apikey"
        )
    return key


def _available_models(key: str) -> list[str]:
    """Model ids this key may call via generateContent (no ``models/`` prefix)."""
    with httpx.Client(timeout=30) as client:
        resp = client.get(_BASE, headers={"x-goog-api-key": key})
    resp.raise_for_status()
    return [
        m["name"].removeprefix("models/")
        for m in resp.json().get("models", [])
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]


def _score(m: str):
    ml = m.lower()
    # Tuple sorts ascending: stable before preview, -latest before dated, full
    # flash before lite, shorter (alias) before long dated ids.
    return ("preview" in ml or "exp" in ml, "latest" not in ml, "lite" in ml, len(m))


def _ranked_models(key: str) -> list[str]:
    """Candidate models best-first: an explicit ``GEMINI_MODEL`` override, else a
    ranked list of flash-tier models this key can call.

    Model aliases get retired (``gemini-2.5-flash`` became unavailable to new
    keys) and individual models get transiently overloaded, so we keep a ranked
    fallback list rather than betting on one name — self-healing across both.
    """
    override = os.environ.get("GEMINI_MODEL")
    if override:
        return [override]
    if not _ranked:
        models = _available_models(key)
        candidates = [m for m in models if "flash" in m.lower()] or models
        _ranked.extend(sorted(candidates, key=_score))
    return _ranked


def _post(key: str, model: str, prompt: str) -> httpx.Response:
    with httpx.Client(timeout=60) as client:
        return client.post(
            _GENERATE.format(model=model),
            headers={"x-goog-api-key": key},  # header, not URL param — keeps the key out of logs
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )


def _text(resp: httpx.Response) -> str:
    candidates = resp.json().get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def _generate(prompt: str) -> str:
    """Call Gemini, retrying transient overloads and falling back across models.

    Each candidate model is tried up to 3 times with linear backoff on a
    transient status (429/500/503); a non-transient failure (e.g. 404) moves
    straight to the next model. Raises with the last error if all are exhausted.
    """
    key = _require_key()
    last = "no model available"
    for model in _ranked_models(key)[:4]:
        for attempt in range(3):
            resp = _post(key, model, prompt)
            if resp.status_code == 200:
                return _text(resp)
            last = f"{resp.status_code} (model {model}): {resp.text[:150]}"
            if resp.status_code in _TRANSIENT:
                time.sleep(1.5 * (attempt + 1))
                continue
            break  # non-transient — try the next model
    raise RuntimeError(f"Gemini API unavailable after retries — {last}")


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
    return _extract_sql(_generate(prompt))


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
    return _generate(prompt).strip()


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
