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

import datetime
import json
import os
import re
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
- FOLLOW-UPS refine the PREVIOUS query — they do not start over. When the question
  refers to the last result ("who received those funds?", "break it down", "just
  for IT", "and last year?"), REUSE the previous query's mart and its EXACT WHERE
  filters — the same category string verbatim, the same fiscal_year, the same
  document_type, AND the same specific entity ("their"/"them"/"that supplier" =
  the supplier/canonical_name from the prior turn) — changing ONLY what is newly
  asked (e.g. GROUP BY supplier_name
  instead of category). Do NOT broaden a specific category to its parent, switch to
  an all-time/different mart, or drop the year. Your totals MUST reconcile with the
  previous answer: the recipients of a $61M category must sum to about $61M, not
  more. The previous SQL and result are given below when present — build on them.
- gold_document is the PRIMARY, COMPLETE source for spend / supplier / category /
  department / time questions: one row per purchase document with grand_total,
  supplier_name, canonical_name, acquisition_type, acquisition_sub_type,
  department_name, status, start_date, calendar_year, fiscal_year. Every document
  has a grand_total — SUM(grand_total) for spend. fiscal_year is the California
  fiscal year (Jul 1-Jun 30, labelled by the year it ends in).
- gold_line_item covers ONLY line-enriched documents (~13%; the biggest vendors
  often have ZERO lines), so NEVER use it for spend totals, "top suppliers", or
  "what did X spend" — it badly undercounts and returns empty for major vendors.
  Use it ONLY for genuine item-level detail (unspsc, item_description, unit_price).
  For everything else use gold_document. The line-DERIVED supplier profiles
  (gold_supplier_unspsc_profile, gold_supplier_specialization) share this ~13%
  sparsity and are EMPTY for many big vendors.
- "What does supplier X supply / buy / provide?" -> use gold_document
  acquisition_type / acquisition_sub_type (complete), NOT the line-level UNSPSC
  profiles (which are empty for many vendors).
- Vendor rollups (one row per real company): GROUP BY canonical_name on
  gold_document. gold_canonical_supplier_spend / gold_supplier_master give all-time
  canonical totals when NO category/time filter is needed. Do not join canonical
  marts (canonical_id) to per-supplier_id marts — the keys differ.
- For procurement-CATEGORY questions ("IT Services", "IT Goods", "Telecom",
  "NON-IT Services"...), filter gold_document.acquisition_type with '=' or a PREFIX
  LIKE ('IT Services%') — NOT '%IT Services%', which also matches 'NON-IT Services'.
- "Contracts" vs "purchases": gold_contract_vs_standalone has one row per
  document_type ('contract (has POs)' / 'standalone'). When asked specifically
  about contracts, filter WHERE document_type LIKE 'contract%' — don't sum both.
- For fiscal-year windows ("last fiscal year", "this year", "past N fiscal
  years"), anchor to the CURRENT-DATE note below. Do NOT use MAX(fiscal_year) as
  "now" — the data has future-dated contracts, so MAX is a future year.
- gold_acquisition_unspsc bridges the taxonomies: which UNSPSC codes flow through
  each acquisition_type/acquisition_sub_type (item-level; use for "what UNSPSC
  codes are under X").
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


def _ca_fiscal_year(d: datetime.date) -> int:
    """California fiscal year for a date: Jul 1-Jun 30, labelled by the year it
    ends in (so 2026-07-01 is FY2027, 2026-06-30 is FY2026)."""
    return d.year + (1 if d.month >= 7 else 0)


def _now_context() -> str:
    """Today's date + the derived California fiscal year, so 'last fiscal year'
    etc. anchor to the real calendar instead of MAX(fiscal_year) (which the data's
    future-dated contracts push into a future year)."""
    today = datetime.date.today()
    cur_fy = _ca_fiscal_year(today)
    return (
        f"CURRENT-DATE note: today is {today.isoformat()}; California fiscal years "
        f"run Jul 1-Jun 30 and are labelled by the year they end in, so the current "
        f"fiscal year is FY{cur_fy}. The data includes FUTURE-dated contracts, so "
        f"MAX(fiscal_year) is a future year, not 'now'. Interpret: 'this/current "
        f"fiscal year' = {cur_fy}; 'last/previous fiscal year' = {cur_fy - 1}; "
        f"'past N fiscal years' = the N most recent COMPLETE years, "
        f"fiscal_year BETWEEN {cur_fy}-N AND {cur_fy - 1}."
    )


def _history_context(history) -> str:
    """Compact transcript of prior turns so follow-ups ('those', 'said funds')
    resolve. Gradio 'messages' history is a list of {role, content} dicts; the
    assistant content carries the answer + the SQL it ran."""
    turns = []
    for msg in history or []:
        if isinstance(msg, dict):
            turns.append((msg.get("role", ""), str(msg.get("content", ""))))
        elif isinstance(msg, (list, tuple)) and len(msg) == 2:
            turns.append(("user", str(msg[0])))
            turns.append(("assistant", str(msg[1])))
    lines = []
    for role, content in turns[-6:]:
        content = content.strip()
        if role == "assistant":
            sql = re.search(r"```sql\n(.*?)\n```", content, re.S)
            ans = content.split("<details>")[0].strip().replace("\n", " ")[:200]
            tail = f" [SQL used: {sql.group(1).strip()}]" if sql else ""
            # The result table (after the SQL <details>) carries the exact filter
            # values (e.g. the full category string) a follow-up must reuse.
            after = content.split("</details>", 1)[-1].strip() if "</details>" in content else ""
            rows = f" [result: {after[:300]}]" if after else ""
            lines.append(f"ASSISTANT: {ans}{tail}{rows}")
        elif role == "user":
            lines.append(f"USER: {content[:200]}")
    if not lines:
        return ""
    return (
        "Earlier in this conversation (resolve follow-ups like 'those'/'said funds'/"
        "'them' against it, then write a fresh standalone query):\n" + "\n".join(lines)
    )


def generate_sql(question: str, schema: str, history=None) -> str:
    """Ask the model for a single read-only SQL statement (or ``NO_QUERY``)."""
    parts = [_GUIDE, _now_context()]
    hist = _history_context(history)
    if hist:
        parts.append(hist)
    parts.append(f"Schema (view (row count): columns):\n{schema}")
    parts.append(f"Question: {question}\nSQL:")
    return _extract_sql(_generate("\n\n".join(parts)))


def summarize(question: str, result: dict, history=None) -> str:
    """Phrase a short natural-language answer from the actual query results."""
    preview = {
        "columns": result.get("columns"),
        "rows": result.get("rows", [])[:50],
        "truncated": result.get("truncated"),
    }
    parts = [_now_context()]
    hist = _history_context(history)
    if hist:
        parts.append(hist)
    parts.append(
        "Answer the user's question in 1–3 sentences using ONLY these query "
        "results. Include the key numbers; format large dollar amounts readably. "
        "If you name a fiscal year, use the one implied by the question and the "
        "CURRENT-DATE note (don't guess). If there are no rows, say no matching "
        "records were found.\n\n"
        f"Question: {question}\nResults (JSON): {json.dumps(preview, default=str)}\nAnswer:"
    )
    return _generate("\n\n".join(parts)).strip()


def answer(question: str, history=None, max_rows: int = 200) -> dict:
    """Full turn: NL question → SQL → guarded execution → NL answer.

    ``history`` is the prior conversation (Gradio messages) so follow-ups resolve.
    Returns ``{answer, sql, result}``; ``sql``/``result`` may be ``None`` when the
    model declines (``NO_QUERY``). Never raises for a bad query — the guard turns
    that into ``result['error']`` and a friendly answer.
    """
    sql = generate_sql(question, wq.schema_for_llm(), history=history)
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
    return {"answer": summarize(question, result, history=history), "sql": sql, "result": result}
