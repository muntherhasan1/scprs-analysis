"""Contract-delta / anomaly checks — the third Wave-1 observer.

The other two observers are point-in-time: `health.py` asks "did collection
stop?" and `canary.py` asks "did collection go wrong?". This one is about
*change over time*: it snapshots a few key metrics each run and compares the
newest snapshot to the previous one, so an anomalous movement between runs is
caught even when each snapshot, on its own, looks fine.

The core contract is monotonicity. The set of drilled documents and the
finished-day log only ever grow, so those metrics must never decrease. A
decrease means data was lost or overwritten (a truncated table, a botched
migration), which no internal-consistency check would catch because the
smaller dataset is still self-consistent. A decrease is therefore an `error`.

Row-level counts (detail rows, line items, POs, line dollars) are NOT strictly
monotonic: re-drilling a reopened day replaces each document's rows, so an
amendment with fewer lines, or one copy replacing a duplicated drill, shrinks
them a little. Treating that as an error blocked the publish, and the next run
fetched the same unpublished state, re-drilled the same day and failed again —
a livelock. Those metrics get a small per-step tolerance instead: a re-drill
moves them by a fraction of a percent, a lost table by far more. A very large
jump in one step is flagged `warn` — usually just a big backfill, occasionally
a duplicate bulk insert worth a look.

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

# Largest one-step decrease tolerated for metrics a legitimate re-drill can
# shrink. Basis (prod store, 2026-10-29): the worst re-drill sheds ~130
# duplicate detail rows (BU 8570, 2024-07-01) against 355,847 rows (0.04%);
# 1% (~18k line items, ~$1.7B of line dollars) leaves room for big amendments
# while a truncated or wiped table still trips it.
REDRILL_SHRINK_FRAC = 0.01

# (metric, SQL returning one number, largest tolerated one-step decrease as a
# fraction of the previous value). 0.0 = strictly monotonic: any decrease is
# an error. None = unconstrained (e.g. the summary, which a refresh can
# legitimately reshape) — watched only for large swings.
_METRICS: list[tuple[str, str, float | None]] = [
    (
        "drilled_docs",
        "SELECT COUNT(*) FROM (SELECT DISTINCT business_unit, purchase_document "
        "FROM document_details)",
        0.0,
    ),
    ("progress_days", "SELECT COUNT(*) FROM details_progress", 0.0),
    ("detail_docs", "SELECT COUNT(*) FROM document_details", REDRILL_SHRINK_FRAC),
    ("line_items", "SELECT COUNT(*) FROM document_lines", REDRILL_SHRINK_FRAC),
    ("assoc_pos", "SELECT COUNT(*) FROM document_pos", REDRILL_SHRINK_FRAC),
    (
        "line_amount_total",
        "SELECT COALESCE(SUM(unit_price * quantity), 0) FROM document_lines",
        REDRILL_SHRINK_FRAC,
    ),
    ("summary_rows", "SELECT COUNT(*) FROM purchases", None),
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
    for name, sql, _max_drop in _METRICS:
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

    A metric that decreased by more than its tolerated drop is an `error`; any
    metric that grew by more than `jump_frac` in one step is a `warn`."""
    max_drop = {name: drop for name, _sql, drop in _METRICS}
    findings: list[Finding] = []
    for name, now in curr.items():
        before = prev.get(name)
        if before is None:
            continue
        delta = now - before
        drop = max_drop.get(name)
        if drop is not None and delta < 0 and -delta > abs(before) * drop:
            allowed = (
                "must not shrink" if drop == 0 else f"more than the {drop:.0%} re-drill allowance"
            )
            findings.append(
                Finding(
                    "metric_decreased",
                    "error",
                    name,
                    f"{name} fell {before:g} -> {now:g} ({delta:g}); {allowed}.",
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
