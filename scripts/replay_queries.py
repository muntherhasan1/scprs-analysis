"""Replay captured questions through the app to catch errors / empties / drift.

Reads the questions logged by ``src/query_log.py`` (from the private query-log
Dataset, or a local JSONL), de-duplicates them, runs each back through
``nl_query.answer`` exactly as the web app would, and reports which ones ERROR,
come back EMPTY, or look off — so real user questions become a regression corpus.

Needs GEMINI_API_KEY (it calls the model, same as the app).

Usage:
    # from the logged Dataset (default repo = $QUERY_LOG_DATASET):
    GEMINI_API_KEY=...  python scripts/replay_queries.py --limit 100
    # from a local capture file:
    GEMINI_API_KEY=...  python scripts/replay_queries.py --file query_logs/queries.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import nl_query  # noqa: E402


def _load_questions(file: str | None, repo: str | None) -> list[str]:
    lines: list[str] = []
    if file:
        lines = Path(file).read_text(encoding="utf-8").splitlines()
    else:
        if not repo:
            sys.exit("set --file, or QUERY_LOG_DATASET / --repo for the logged Dataset")
        from huggingface_hub import hf_hub_download

        # Our own private log dataset; intentionally want the latest on main.
        path = hf_hub_download(repo, "data/queries.jsonl", repo_type="dataset", revision="main")  # nosec B615
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    seen, questions = set(), []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            q = (json.loads(line).get("question") or "").strip()
        except json.JSONDecodeError:
            continue
        if q and q.lower() not in seen:
            seen.add(q.lower())
            questions.append(q)
    return questions


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay logged questions as a regression check")
    ap.add_argument("--file", help="local queries.jsonl instead of the Dataset")
    ap.add_argument("--repo", default=os.environ.get("QUERY_LOG_DATASET"), help="query-log Dataset")
    ap.add_argument("--limit", type=int, default=0, help="max unique questions (0 = all)")
    args = ap.parse_args()

    questions = _load_questions(args.file, args.repo)
    if args.limit:
        questions = questions[: args.limit]
    print(f"Replaying {len(questions)} unique question(s)\n")

    errors, empties = [], []
    for i, q in enumerate(questions, 1):
        out = nl_query.answer(q)  # single-turn replay
        result = out.get("result") or {}
        if "error" in result:
            tag, _ = "ERROR", errors.append((q, result["error"]))
        elif out.get("sql") is None:
            tag = "NO_QUERY"
        elif result.get("row_count") == 0:
            tag, _ = "EMPTY", empties.append((q, out.get("sql")))
        else:
            tag = f"ok ({result.get('row_count')} rows)"
        print(f"[{i}/{len(questions)}] {tag}: {q}")

    print(f"\nSummary: {len(questions)} replayed | {len(errors)} errored | {len(empties)} empty")
    for q, err in errors:
        print(f"  ERROR  {q}\n         -> {err}")
    for q, sql in empties:
        print(f"  EMPTY  {q}\n         -> {sql}")


if __name__ == "__main__":
    main()
