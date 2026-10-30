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
them. Treating that as an error blocked the publish, and the next run fetched
the same unpublished state, re-drilled the same day and failed again — a
livelock. A percentage tolerance does not fix it: line dollars sit in a few
huge documents (4260 16-93286 alone is $22.9B, ~14% of the total), so one
amendment can move the total by more than any tolerance that still catches
loss, and a per-step tolerance lets slow loss through one sliver at a time.

So every detail load logs, in the same transaction, exactly what it changed in
these metrics (`redrill_log`, written by `model.build_details_db`). The check
subtracts the logged changes between the two snapshots and requires the
UNEXPLAINED change to be non-negative: a re-drill may shrink the totals by any
amount it accounts for, and a single unexplained missing line is an error.
Windows that start before the log existed fall back to the old 1% tolerance.

A violation that a human has investigated and accepts (e.g. a deliberate
cleanup) is published with `check --acknowledge "<reason>"` — the
`contracts_ack` input of a manual Enrich run. Errors are then reported as
acknowledged warnings and the run publishes, which makes that state the new
baseline. A very large jump in one step is flagged `warn` — usually just a big
backfill, occasionally a duplicate bulk insert worth a look.

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

# Transition fallback ONLY: the largest one-step decrease tolerated for the
# re-drill metrics when the older snapshot predates `redrill_log` (so the
# window's re-drills can't be accounted for). Basis (prod, 2026-10-29): the
# worst re-drill sheds ~130 duplicate detail rows of 355,847 (0.04%).
REDRILL_SHRINK_FRAC = 0.01

# Snapshot marker: present when `redrill_log` existed at capture time, so the
# window starting at this snapshot is fully covered by the log.
LOG_MARKER = "redrill_log_v1"

# Float slack for line dollars (sums of unit_price * quantity re-added in a
# different order); counts are exact.
_AMOUNT_EPSILON = 1.0

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
    # The log table is created with the detail tables; once it exists every
    # later load is logged, so later windows can be fully explained.
    if (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'redrill_log'"
        ).fetchone()
        or not out["drilled_docs"]
    ):
        out[LOG_MARKER] = 1
    return out


def explained_deltas(con: sqlite3.Connection, after: str, upto: str) -> dict[str, float]:
    """Sum of the logged per-load metric changes with after < logged_at <= upto."""
    names = [n for n, _sql, drop in _METRICS if drop not in (None, 0.0)]
    try:
        row = con.execute(
            f"SELECT {', '.join(f'COALESCE(SUM({n}), 0)' for n in names)} "  # noqa: S608 - internal constant names
            "FROM redrill_log WHERE logged_at > ? AND logged_at <= ?",
            (after, upto),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    return dict(zip(names, row, strict=True))


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
    explained: dict[str, float] | None = None,
) -> list[Finding]:
    """Compare two metric snapshots and return findings, most-severe first.

    Strictly monotonic metrics may never decrease. Re-drill metrics may change
    by whatever the window's detail loads logged (`explained`); any
    unexplained decrease is an `error`. When `prev` predates the log, they
    fall back to the 1% per-step tolerance. Any metric that grew by more than
    `jump_frac` in one step is a `warn`."""
    max_drop = {name: drop for name, _sql, drop in _METRICS}
    logged = explained is not None and bool(prev.get(LOG_MARKER))
    findings: list[Finding] = []
    for name, now in curr.items():
        before = prev.get(name)
        if before is None or name == LOG_MARKER:
            continue
        delta = now - before
        drop = max_drop.get(name)
        if drop not in (None, 0.0) and logged:
            accounted = (explained or {}).get(name, 0)
            unexplained = delta - accounted
            slack = _AMOUNT_EPSILON if name == "line_amount_total" else 0
            if unexplained < -slack:
                findings.append(
                    Finding(
                        "metric_decreased",
                        "error",
                        name,
                        f"{name} {before:g} -> {now:g} ({delta:g}); logged re-drills "
                        f"account for {accounted:g}, leaving {unexplained:g} unexplained.",
                    )
                )
                continue
        elif drop is not None and delta < 0 and -delta > abs(before) * drop:
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
            continue
        if before > 0 and delta / before > jump_frac:
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


def check(
    con: sqlite3.Connection,
    *,
    jump_frac: float = DEFAULT_JUMP_FRAC,
    acknowledge: str | None = None,
) -> list[Finding]:
    """Diff the two most recent snapshots. With fewer than two, there is no
    baseline yet, so nothing can be wrong. `acknowledge` (a human's reason)
    downgrades errors to warnings so an investigated state can publish."""
    snaps = latest_two(con)
    if len(snaps) < 2:
        return [Finding("no_baseline", "ok", "-", "Fewer than two snapshots; nothing to compare.")]
    (ts_new, curr), (ts_old, prev) = snaps
    findings = diff(
        prev, curr, jump_frac=jump_frac, explained=explained_deltas(con, ts_old, ts_new)
    )
    if acknowledge:
        for f in findings:
            if f.severity == "error":
                f.severity = "warn"
                f.detail += f" [ACKNOWLEDGED: {acknowledge}]"
    return findings


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
    ap.add_argument(
        "--acknowledge",
        metavar="REASON",
        help="Report errors as acknowledged warnings (exit 0) — only after a human "
        "has investigated the violation; the published state becomes the new baseline",
    )
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
        findings = check(con, jump_frac=args.jump_frac, acknowledge=args.acknowledge)
    finally:
        con.close()
    if args.json:
        print(json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        _print(findings)
    return 1 if any(f.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
