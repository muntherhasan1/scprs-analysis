"""Replay captured questions through the app to catch errors / empties / drift.

Thin CLI wrapper — the logic lives in ``src/query_review.py`` (tested, and run
weekly by the ``query-review`` workflow). Reads every ``data/*.jsonl`` in the
private query-log Dataset (the per-Space files ``queries-<space>.jsonl`` plus
the legacy ``queries.jsonl``), or a local capture file, and replays the
questions through ``nl_query.answer`` exactly as the web app would.

Needs GEMINI_API_KEY (it calls the model, same as the app).

Usage:
    # from the logged Dataset (default repo = $QUERY_LOG_DATASET):
    GEMINI_API_KEY=...  python scripts/replay_queries.py --limit 100
    # from a local capture file:
    GEMINI_API_KEY=...  python scripts/replay_queries.py --file query_logs/queries.jsonl
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import query_review  # noqa: E402

if __name__ == "__main__":
    sys.exit(query_review.main())
