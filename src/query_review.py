"""Weekly review of real user questions: replay the query log, flag regressions.

The two serving front ends log every question and the SQL the model wrote
(``src/query_log.py``) but nothing reviewed those logs — the 2026-07-27 demo
audit found a materially wrong answer (SB/DVBE spend summed from a sparse
research column instead of the certification mart) purely by manual replay.
This module makes that review a scheduled loop: pull the per-Space log files
from the private dataset, replay the recent questions through
``nl_query.answer`` exactly as the web app would, and report which ones ERROR,
come back EMPTY, decline (NO_QUERY), or hit a known wrong-mart smell.

Only ``source: "web"`` records replay — MCP audit entries (``record_tool``)
carry raw SQL from the client and no natural-language question, so they fall
out of the question filter naturally.

Privacy: the questions come from the PUBLIC web app and the log is private, but
the workflow's issue, step summary and run log are public. So everything this
module prints and the ``--report`` file name each question only by a short hash
(``q:1a2b3c4d5e``) and carry no SQL or error text (both can echo what the user
typed). ``--details`` writes the full report with question text and SQL — for a
maintainer's local run against the private log, never for CI output.

Exit contract (mirrors ``src.health``): 0 = clean or warn-only findings
(empties/smells alert via the workflow's issue, not a red run); 1 = at least
one replay ERRORED — real user questions are failing against the current
warehouse; 2 = the reviewer itself could not run (log fetch, serve DB, or
GEMINI key), so the workflow can tell content findings from infrastructure.

    python -m src.query_review --limit 10 --report review.md --status status.json
    python -m src.query_review --details review-full.md   # local: full text, keep private
    python -m src.query_review --file query_logs/queries.jsonl   # offline
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import sys
from pathlib import Path


def load_log_records(repo: str, token: str | None = None) -> list[dict]:
    """All records from every ``data/*.jsonl`` in the query-log dataset.

    Each Space writes its own file (``queries-<space>.jsonl``, see
    ``query_log._logfile_name``) plus the legacy ``queries.jsonl`` — merging the
    glob is what the old replay script missed. Bad lines are tolerated: capture
    is best-effort at write time, so read must be too."""
    from huggingface_hub import HfApi, hf_hub_download

    names = fnmatch.filter(
        HfApi(token=token).list_repo_files(repo, repo_type="dataset"), "data/*.jsonl"
    )
    records: list[dict] = []
    for name in sorted(names):
        # Our own private log dataset; intentionally want the latest on main.
        path = hf_hub_download(repo, name, repo_type="dataset", revision="main", token=token)  # nosec B615
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_local_records(file: str) -> list[dict]:
    records = []
    for line in Path(file).read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def select_questions(
    records: list[dict], *, since: datetime.datetime | None = None, limit: int = 0
) -> list[str]:
    """Unique questions (case-insensitive, newest window first-seen order).

    ``since`` filters on the record's ``ts``; records without a parseable ts are
    kept (better to over-review than silently skip)."""
    seen: set[str] = set()
    questions: list[str] = []
    for rec in records:
        if since is not None:
            ts = rec.get("ts")
            if isinstance(ts, str):
                try:
                    if datetime.datetime.fromisoformat(ts) < since:
                        continue
                except ValueError:
                    pass
        q = (rec.get("question") or "").strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            questions.append(q)
    if limit:
        questions = questions[:limit]
    return questions


def question_id(question: str) -> str:
    """Stable short id for a question (case-insensitive, like the dedupe), so the
    public report can point at a finding without repeating what the user typed."""
    return "q:" + hashlib.sha256(question.strip().lower().encode("utf-8")).hexdigest()[:10]


def classify(out: dict) -> str:
    """Tag one ``nl_query.answer`` output: ERROR / NO_QUERY / EMPTY / ok."""
    result = out.get("result") or {}
    if "error" in result:
        return "ERROR"
    if out.get("sql") is None:
        return "NO_QUERY"
    if result.get("row_count") == 0:
        return "EMPTY"
    return "ok"


# Spend-shaped questions answered from line-derived marts undercount badly
# (~13% line coverage); sb_dvbe is a sparse research note, not the registry.
# These mirror the prompt guide in nl_query._GUIDE — a smell means the model
# ignored the guide and the guide (or mart descriptions) needs strengthening.
_SPEND_WORDS = ("spend", "spent", "total", "top supplier", "how much", "worth", "biggest")
_LINE_MARTS = ("gold_line_item", "gold_supplier_unspsc_profile", "gold_supplier_specialization")


def smells(question: str, sql: str | None) -> list[str]:
    """Wrong-mart heuristics for a generated query. Empty list = clean."""
    if not sql:
        return []
    found: list[str] = []
    sql_l, q_l = sql.lower(), question.lower()
    if re.search(r"\bsb_dvbe\b", sql_l):
        found.append(
            "uses gold_supplier_master.sb_dvbe (sparse research note) — "
            "certification questions belong on gold_supplier_certification"
        )
    if any(m in sql_l for m in _LINE_MARTS) and (
        "sum(" in sql_l or any(w in q_l for w in _SPEND_WORDS)
    ):
        found.append(
            "answers a spend-shaped question from line-derived marts "
            "(~13% coverage undercounts) — use gold_document / document-grain marts"
        )
    return found


def replay(questions: list[str]) -> list[dict]:
    """Run each question through the app's NL->SQL path, single-turn."""
    from . import nl_query

    results = []
    for i, q in enumerate(questions, 1):
        out = nl_query.answer(q)
        tag = classify(out)
        smell = smells(q, out.get("sql"))
        rows = (out.get("result") or {}).get("row_count")
        # Public run log: the id only, never the question text.
        print(
            f"[{i}/{len(questions)}] {tag}{' SMELL' if smell else ''}"
            f"{f' ({rows} rows)' if tag == 'ok' else ''}: {question_id(q)}"
        )
        results.append(
            {
                "id": question_id(q),
                "question": q,
                "tag": tag,
                "smells": smell,
                "sql": out.get("sql"),
                "error": (out.get("result") or {}).get("error"),
                "row_count": rows,
            }
        )
    return results


def finding_count(results: list[dict]) -> int:
    """Questions needing attention: any non-ok tag or a wrong-mart smell."""
    return sum(1 for r in results if r["tag"] != "ok" or r["smells"])


def render_markdown(results: list[dict], *, details: bool = False) -> str:
    """The review report: counts up top, then one section per finding class.

    By default it is safe to publish: each question appears as its id, with no
    SQL or error text. ``details=True`` adds the question, SQL and error — the
    private, local-only version."""
    by_tag: dict[str, list[dict]] = {}
    for r in results:
        by_tag.setdefault(r["tag"], []).append(r)
    smelly = [r for r in results if r["smells"]]

    lines = [
        "## Query review",
        "",
        f"Replayed **{len(results)}** unique logged question(s): "
        f"{len(by_tag.get('ok', []))} ok · {len(by_tag.get('ERROR', []))} error · "
        f"{len(by_tag.get('EMPTY', []))} empty · {len(by_tag.get('NO_QUERY', []))} declined · "
        f"{len(smelly)} wrong-mart smell(s).",
    ]
    sections = [
        ("ERROR", "Errored (the generated SQL failed to run)", lambda r: r["error"]),
        (
            "EMPTY",
            "Empty (ran fine, zero rows — often a filter/label mismatch)",
            lambda r: r["sql"],
        ),
        (
            "NO_QUERY",
            "Declined (model saw no way to answer — discoverability gap?)",
            lambda r: None,
        ),
    ]
    for tag, heading, detail in sections:
        if by_tag.get(tag):
            lines += ["", f"### {heading}", ""]
            for r in by_tag[tag]:
                lines.append(_label(r, details))
                d = detail(r) if details else None
                if d:
                    lines.append(f"  - `{d}`")
    if smelly:
        lines += ["", "### Wrong-mart smells", ""]
        for r in smelly:
            lines.append(_label(r, details))
            for s in r["smells"]:  # fixed heuristic text, never user input
                lines.append(f"  - {s}")
            if details and r["sql"]:
                lines.append(f"  - `{r['sql']}`")
    if not finding_count(results):
        lines += ["", "No findings — every replayed question answered cleanly. ✅"]
    elif not details:
        lines += [
            "",
            "Questions are listed by id only — the text is user input from the public app. "
            "To see them, run `python -m src.query_review --details review-full.md` locally "
            "with the query-log token and match the ids.",
        ]
    return "\n".join(lines) + "\n"


def _label(r: dict, details: bool) -> str:
    return f"- **{r['question']}** (`{r['id']}`)" if details else f"- `{r['id']}`"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay logged questions as a weekly regression check")
    ap.add_argument("--file", help="local queries.jsonl instead of the Dataset")
    ap.add_argument("--repo", default=os.environ.get("QUERY_LOG_DATASET"), help="query-log Dataset")
    ap.add_argument(
        "--since-days",
        type=float,
        default=8,
        help="look-back window (default 8: weekly cron + 1 day overlap; 0 = all)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=150,
        help="max unique questions per run — the free-tier LLM budget (0 = all)",
    )
    ap.add_argument(
        "--report", help="write the publishable report (question ids, no user text) here"
    )
    ap.add_argument(
        "--details",
        help="write the FULL report (question text + SQL) here — local use only, keep private",
    )
    ap.add_argument(
        "--status",
        help='write {"findings": n, "errors": n} JSON here (what CI branches on)',
    )
    args = ap.parse_args(argv)

    try:
        if args.file:
            records = load_local_records(args.file)
        else:
            if not args.repo:
                print("set --file, or QUERY_LOG_DATASET / --repo for the logged Dataset")
                return 2
            records = load_log_records(args.repo, token=os.environ.get("HF_TOKEN"))
    except Exception as exc:  # noqa: BLE001 - infra failure, distinct exit code
        print(f"query review could not fetch the log: {type(exc).__name__}: {exc}")
        return 2

    since = None
    if args.since_days:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=args.since_days
        )
    questions = select_questions(records, since=since, limit=args.limit)
    print(f"Replaying {len(questions)} unique question(s) from {len(records)} record(s)\n")

    try:
        results = replay(questions)
    except Exception as exc:  # noqa: BLE001 - e.g. missing GEMINI key / serve DB
        print(f"query review could not replay: {type(exc).__name__}: {exc}")
        return 2

    report = render_markdown(results)
    print("\n" + report)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    if args.details:
        Path(args.details).write_text(render_markdown(results, details=True), encoding="utf-8")
    if args.status:
        status = {
            "findings": finding_count(results),
            "errors": sum(1 for r in results if r["tag"] == "ERROR"),
        }
        Path(args.status).write_text(json.dumps(status), encoding="utf-8")
    return 1 if any(r["tag"] == "ERROR" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
