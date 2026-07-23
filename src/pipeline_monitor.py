"""Out-of-band serve-DB staleness check — the HfApi half of the pipeline monitor.

The pipeline's own health checks all run *inside* the enrich workflow, so any
failure that stops that workflow from running (or from reaching them) also
silences the alarm — the 2026-07-20..23 livelock served 3-day-stale data with
zero signal. `pipeline-monitor.yml` watches from outside on its own cron; this
module holds the piece that needs huggingface_hub: how old is the serve
dataset's newest commit? The run-recency and cron keep-alive checks live in the
workflow itself as `gh` CLI calls (they need only the Actions API).

    python -m src.pipeline_monitor serve-age --max-hours 14

Exits 0 when fresh; 1 when stale *or unreadable* — an unreadable dataset is
itself a finding (rotated token, deleted repo), never a pass.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

# The serve dataset publishes on every successful enrich run (every 6h), so a
# healthy pipeline commits at least 4x/day. 14h = two missed slots + runtime.
DEFAULT_MAX_HOURS = 14.0
DEFAULT_DATASET = "munther-hasan/scprs-warehouse-data"


def serve_age_hours(repo: str, token: str | None = None) -> float:
    """Hours since the serve dataset's newest commit (commits[0] is newest)."""
    from huggingface_hub import HfApi

    commits = HfApi().list_repo_commits(repo, repo_type="dataset", token=token)
    newest = commits[0].created_at
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Out-of-band pipeline staleness checks.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sa = sub.add_parser("serve-age", help="Age of the serve dataset's newest commit")
    sa.add_argument("--dataset", default=os.environ.get("WAREHOUSE_DATASET") or DEFAULT_DATASET)
    sa.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS)
    args = ap.parse_args(argv)

    token = os.environ.get("HF_WAREHOUSE_TOKEN") or os.environ.get("HF_TOKEN")
    try:
        age = serve_age_hours(args.dataset, token=token)
    except Exception as e:  # noqa: BLE001 - unreadable dataset is itself a finding
        print(f"STALE serve dataset {args.dataset} unreadable: {type(e).__name__}: {e}"[:300])
        return 1
    ok = age <= args.max_hours
    print(
        f"{'OK' if ok else 'STALE'} serve dataset {args.dataset} "
        f"last commit {age:.1f}h ago (max {args.max_hours:g}h)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
