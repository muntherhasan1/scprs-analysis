"""Contract-delta / anomaly checks — the third Wave-1 observer.

The other two observers are point-in-time: `health.py` asks "did collection
stop?" and `canary.py` asks "did collection go wrong?". This one is about
*change over time*: it snapshots a few key metrics each run and compares the
newest snapshot to the previous one, so an anomalous movement between runs is
caught even when each snapshot, on its own, looks fine.

The core contract is monotonicity. The enrichment tables are append-only in
normal operation — a drill only ever *inserts* line items, POs, and progress
rows — so those metrics must never decrease. A decrease means data was lost or
overwritten (a bad re-scrape, a truncated table, a botched migration), which no
internal-consistency check would catch because the smaller dataset is still
self-consistent. A decrease is therefore an `error`. A very large jump in one
step is flagged `warn` — usually just a big backfill, occasionally a duplicate
bulk insert worth a look.

Snapshots are appended to `metric_snapshots` in scprs.db (they travel with the
store), so the history is available wherever the DB goes.

    python -m src.contracts capture   # record a snapshot of current metrics
    python -m src.contracts check     # compare the two latest; exit 1 on error
    python -m src.contracts check --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .model import DB_PATH

# (metric, SQL returning one number, monotonic?). Monotonic metrics must never
# decrease; a decrease is an error. Non-monotonic ones (e.g. the summary, which a
# re-build can legitimately reshape) are watched only for large swings.
_METRICS: list[tuple[str, str, bool]] = [
    ("detail_docs", "SELECT COUNT(*) FROM document_details", True),
    ("line_items", "SELECT COUNT(*) FROM document_lines", True),
    ("assoc_pos", "SELECT COUNT(*) FROM document_pos", True),
    ("progress_days", "SELECT COUNT(*) FROM details_progress", True),
    (
        "line_amount_total",
        "SELECT COALESCE(SUM(unit_price * quantity), 0) FROM document_lines",
        True,
    ),
    ("summary_rows", "SELECT COUNT(*) FROM purchases", False),
]

DEFAULT_JUMP_FRAC = 0.5  # a >50% one-step increase is worth a warn, not an error.


@dataclass
class Finding:
    check: str
    severity: str  # 'error' | 'warn' | 'ok'
    metric: str
    detail: str


def _ensure_snapshot_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS metric_snapshots ("
        "captured_at TEXT PRIMARY KEY, metrics TEXT)"
    )


def capture_metrics(con: sqlite3.Connection) -> dict[str, float]:
    """Compute the tracked metrics from the operational store."""
    out: dict[str, float] = {}
    for name, sql, _mono in _METRICS:
        try:
            out[name] = con.execute(sql).fetchone()[0] or 0
        except sqlite3.OperationalError:
            # A table may not exist yet on a brand-new store; treat as zero.
            out[name] = 0
    return out


def record(con: sqlite3.Connection, metrics: dict[str, float], *, at: str | None = None) -> str:
    """Append a snapshot. Returns its timestamp key."""
    _ensure_snapshot_schema(con)
    ts = at or datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT OR REPLACE INTO metric_snapshots (captured_at, metrics) VALUES (?, ?)",
        (ts, json.dumps(metrics)),
    )
    con.commit()
    return ts


def latest_two(con: sqlite3.Connection) -> list[tuple[str, dict[str, float]]]:
    """The two most recent snapshots, newest first (fewer if not enough exist).

    Read-only safe: if the table doesn't exist yet (no snapshot ever recorded),
    return nothing rather than trying to create it."""
    try:
        rows = con.execute(
            "SELECT captured_at, metrics FROM metric_snapshots ORDER BY captured_at DESC LIMIT 2"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(ts, json.loads(m)) for ts, m in rows]


def diff(
    prev: dict[str, float],
    curr: dict[str, float],
    *,
    jump_frac: float = DEFAULT_JUMP_FRAC,
) -> list[Finding]:
    """Compare two metric snapshots and return findings, most-severe first.

    A monotonic metric that decreased is an `error`; any metric that grew by more
    than `jump_frac` in one step is a `warn`."""
    monotonic = {name: mono for name, _sql, mono in _METRICS}
    findings: list[Finding] = []
    for name, now in curr.items():
        before = prev.get(name)
        if before is None:
            continue
        delta = now - before
        if monotonic.get(name) and delta < 0:
            findings.append(
                Finding(
                    "metric_decreased",
                    "error",
                    name,
                    f"{name} fell {before:g} -> {now:g} ({delta:g}); append-only must not shrink.",
                )
            )
        elif before > 0 and delta / before > jump_frac:
            findings.append(
                Finding(
                    "metric_jumped",
                    "warn",
                    name,
                    f"{name} rose {before:g} -> {now:g} (+{delta / before:.0%} in one step).",
                )
            )

    if not findings:
        findings.append(Finding("no_anomaly", "ok", "-", "No contract violations detected"))
    order = {"error": 0, "warn": 1, "ok": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.metric))
    return findings


def check(con: sqlite3.Connection, *, jump_frac: float = DEFAULT_JUMP_FRAC) -> list[Finding]:
    """Diff the two most recent snapshots. With fewer than two, there is no
    baseline yet, so nothing can be wrong."""
    snaps = latest_two(con)
    if len(snaps) < 2:
        return [Finding("no_baseline", "ok", "-", "Fewer than two snapshots; nothing to compare.")]
    (_ts_new, curr), (_ts_old, prev) = snaps
    return diff(prev, curr, jump_frac=jump_frac)


def _print(findings: list[Finding]) -> None:
    tag = {"error": "[ERROR]", "warn": "[WARN] ", "ok": "[OK]   "}
    for f in findings:
        print(f"{tag.get(f.severity, '[?]')} {f.check:<16}  {f.metric:<18}  {f.detail}")
    errors = sum(1 for f in findings if f.severity == "error")
    warns = sum(1 for f in findings if f.severity == "warn")
    print(f"\n{errors} error(s), {warns} warning(s).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SCPRS contract-delta / anomaly checks.")
    ap.add_argument("cmd", choices=("capture", "check"))
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--jump-frac", type=float, default=DEFAULT_JUMP_FRAC)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "capture":
        con = sqlite3.connect(args.db)
        try:
            metrics = capture_metrics(con)
            ts = record(con, metrics)
        finally:
            con.close()
        if args.json:
            print(json.dumps({"captured_at": ts, "metrics": metrics}, indent=2))
        else:
            print(f"Snapshot {ts}: {metrics}")
        return 0

    # check: read-only.
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        findings = check(con, jump_frac=args.jump_frac)
    finally:
        con.close()
    if args.json:
        print(json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        _print(findings)
    return 1 if any(f.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
