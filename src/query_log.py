"""Optional persistent capture of web-app queries, for building a regression /
eval corpus from what people actually ask.

A Hugging Face Space filesystem is EPHEMERAL (wiped on restart/redeploy), so
records are appended to a local JSONL and synced to a **private** HF Dataset via
``CommitScheduler``. This is a no-op unless ``QUERY_LOG_DATASET`` is set (e.g.
``munther-hasan/scprs-query-log``); the Space also needs an HF **write** token in
``HF_TOKEN``. Capture must never break a user's query, so ``record`` swallows all
errors.

We store the question, the generated SQL, and outcome flags (row_count / error /
empty) — not the full result rows (leaner, and less to worry about privacy-wise).
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path

_LOCAL = Path(os.environ.get("QUERY_LOG_DIR", "query_logs"))
_LOGFILE = _LOCAL / "queries.jsonl"
_scheduler = None
_init_lock = threading.Lock()


def _scheduler_or_none():
    """Lazily create the CommitScheduler syncing the local JSONL to the private
    Dataset. Returns None (logging disabled) when ``QUERY_LOG_DATASET`` is unset."""
    global _scheduler
    repo = os.environ.get("QUERY_LOG_DATASET")
    if not repo:
        return None
    if _scheduler is None:
        with _init_lock:
            if _scheduler is None:
                from huggingface_hub import CommitScheduler

                _LOCAL.mkdir(parents=True, exist_ok=True)
                _scheduler = CommitScheduler(
                    repo_id=repo,
                    repo_type="dataset",
                    folder_path=str(_LOCAL),
                    path_in_repo="data",
                    every=int(os.environ.get("QUERY_LOG_EVERY_MIN", "5")),
                    private=True,
                    token=os.environ.get("HF_TOKEN"),
                )
    return _scheduler


def record(question: str, out: dict, prior_turns: int = 0) -> None:
    """Append one turn's capture to the log. Never raises."""
    try:
        scheduler = _scheduler_or_none()
        if scheduler is None:
            return
        result = out.get("result") or {}
        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "question": question,
            "sql": out.get("sql"),
            "row_count": result.get("row_count"),
            "error": result.get("error"),
            "empty": ("error" not in result) and result.get("row_count") == 0,
            "prior_turns": prior_turns,
        }
        # Hold the scheduler's lock so a commit never races a half-written line.
        with scheduler.lock:
            with _LOGFILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001, S110  # nosec B110 — capture must not break the app
        pass
