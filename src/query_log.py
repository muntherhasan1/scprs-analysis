"""Optional persistent capture of front-end queries — the NL web app's questions
and the MCP tools' calls — for a regression/eval corpus and an audit trail of what
each front end asked the warehouse.

A Hugging Face Space filesystem is EPHEMERAL (wiped on restart/redeploy), so
records are appended to a local JSONL and synced to a **private** HF Dataset via
``CommitScheduler``. This is a no-op unless ``QUERY_LOG_DATASET`` is set (e.g.
``munther-hasan/scprs-query-log``); the Space also needs an HF **write** token —
prefer a dedicated, write-scoped ``QUERY_LOG_TOKEN`` so the Space's ``HF_TOKEN``
can stay read-only (it only needs read on the serve-DB dataset for ``data_sync``),
falling back to ``HF_TOKEN``. Capture must never break a user's query, so writes
swallow all errors.

Each record carries a ``source`` (``"web"`` for the NL app via ``record``, ``"mcp"``
for a tool call via ``record_tool``). We store the question or tool call, the SQL,
and outcome flags (row_count / error / empty) — never the full result rows (leaner,
and less to worry about privacy-wise). For the MCP path there is no natural-language
question to capture (that stays on the client/Copilot side); the SQL the client sent
is what we can see and record.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path

_LOCAL = Path(os.environ.get("QUERY_LOG_DIR", "query_logs"))


def _logfile_name() -> str:
    """Per-writer log filename.

    Independent Spaces (the web app and the MCP server) can point at the SAME
    dataset, but ``CommitScheduler`` syncs a folder by overwriting each file
    path — so if both wrote ``queries.jsonl`` they'd clobber each other's log.
    Namespace the file by the HF ``SPACE_ID`` (auto-set per Space) so each writer
    owns its own ``queries-<space>.jsonl``; records still carry ``source`` for
    filtering once merged. Override explicitly with ``QUERY_LOG_FILE``."""
    name = os.environ.get("QUERY_LOG_FILE")
    if name:
        return name
    space = os.environ.get("SPACE_ID")
    if space:
        return "queries-" + space.replace("/", "-") + ".jsonl"
    return "queries.jsonl"


_LOGFILE = _LOCAL / _logfile_name()
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
                    token=_write_token(),
                )
    return _scheduler


def _write_token() -> str | None:
    """The HF token used to WRITE the log dataset. Prefer a dedicated, write-scoped
    ``QUERY_LOG_TOKEN`` so ``HF_TOKEN`` can stay read-only (it only needs read on the
    serve-DB dataset for data_sync). Falls back to ``HF_TOKEN`` for simple setups."""
    return os.environ.get("QUERY_LOG_TOKEN") or os.environ.get("HF_TOKEN")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _append(rec: dict) -> None:
    """Append one record to the JSONL and let the scheduler sync it. Never raises.

    A no-op when ``QUERY_LOG_DATASET`` is unset (no scheduler)."""
    try:
        scheduler = _scheduler_or_none()
        if scheduler is None:
            return
        # Hold the scheduler's lock so a commit never races a half-written line.
        with scheduler.lock:
            with _LOGFILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001, S110  # nosec B110 — capture must not break the app
        pass


def record(question: str, out: dict, prior_turns: int = 0) -> None:
    """Append one NL web-app turn's capture to the log. Never raises."""
    result = out.get("result") or {}
    _append(
        {
            "ts": _now(),
            "source": "web",
            "question": question,
            "sql": out.get("sql"),
            "row_count": result.get("row_count"),
            "error": result.get("error"),
            "empty": ("error" not in result) and result.get("row_count") == 0,
            "prior_turns": prior_turns,
        }
    )


def record_tool(tool: str, *, source: str = "mcp", **fields) -> None:
    """Append one MCP tool call to the audit log. Never raises.

    ``fields`` are tool-specific — e.g. ``sql``, ``row_count``, ``error`` for
    ``run_sql``; ``kind``/``title`` for ``generate_chart``; ``title``/``sqls`` for
    ``generate_report``. The authenticated ``principal`` (which named token made
    the call) is stamped automatically when known. Result rows are never
    recorded, mirroring ``record``'s privacy posture."""
    from . import auth

    entry = {"ts": _now(), "source": source, "tool": tool, **fields}
    principal = auth.current_principal.get()
    if principal:
        entry.setdefault("principal", principal)
    _append(entry)
