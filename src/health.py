"""Operational-store health & freshness checks (Wave 1 observability).

The scrape/enrich pipeline used to fail *silently*: a scheduled job could die and
produce zero work for days with nothing noticing (the daily enrich job did exactly
this — a mangled `--newest-first` flag aborted every run for a week). Unit tests
and the warehouse DQ suite can't catch that, because the data already in the store
is internally consistent; what's wrong is that it *stopped growing*.

This module is the independent observer that closes that gap. It reads
`scprs.db` read-only and reports per-business-unit **freshness** — when each unit
was last enriched, how much of it is covered, and whether an in-progress unit has
gone stale while work remains. Findings are severity-tiered like the warehouse DQ
checks: an `error` finding exits non-zero so a scheduled CI job can alert on it.

    python -m src.health                 # human report; exit 1 on any error finding
    python -m src.health --json          # machine-readable, for a workflow to parse
    python -m src.health --stale-hours 72 --min-coverage 0.25

It intentionally does no network I/O and no writes, so it is fast, deterministic,
and safe to run anywhere (locally, in CI, against a copy).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .model import DB_PATH

# Defaults chosen for the daily cadence: the enrich job runs once a day, so a unit
# that still has work to do but hasn't advanced in two days is a real red flag.
DEFAULT_STALE_HOURS = 48.0
DEFAULT_MIN_COVERAGE = 0.10  # 10% — a rollout floor; raise as coverage matures.


@dataclass
class Finding:
    """One health observation. `severity` mirrors the warehouse DQ tiers:
    'error' means a scheduled run should alert (and this process exits non-zero);
    'warn' is informational; 'ok' is a healthy pass worth showing."""

    check: str
    severity: str  # 'error' | 'warn' | 'ok'
    scope: str  # which business unit (or 'pipeline' for the global check)
    detail: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str | None) -> datetime | None:
    """Parse a `details_progress.completed_at` ISO timestamp to an aware UTC
    datetime. Rows are written with local-offset ISO strings; a bare timestamp is
    assumed UTC. Returns None for a missing/unparseable value."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class _UnitState:
    business_unit: str
    summary_days: int
    enriched_days: int
    last_enriched: datetime | None

    @property
    def pending_days(self) -> int:
        return self.summary_days - self.enriched_days

    @property
    def coverage(self) -> float:
        return self.enriched_days / self.summary_days if self.summary_days else 0.0


def _load_state(con: sqlite3.Connection) -> list[_UnitState]:
    """Per-BU summary vs enriched day counts and the last enrichment timestamp.

    `purchases` holds the summary (distinct start_date = an active day to drill);
    `details_progress` records each finished day. Left join so units with no
    enrichment yet still appear (as 0 covered)."""
    rows = con.execute(
        """
        SELECT p.business_unit,
               COUNT(DISTINCT p.start_date)  AS summary_days,
               COUNT(DISTINCT dp.day)        AS enriched_days,
               MAX(dp.completed_at)          AS last_enriched
        FROM purchases p
        LEFT JOIN details_progress dp
          ON dp.business_unit = p.business_unit AND dp.day = p.start_date
        GROUP BY p.business_unit
        ORDER BY p.business_unit
        """
    ).fetchall()
    return [_UnitState(bu, sd, dd, _parse_ts(ts)) for bu, sd, dd, ts in rows]


def evaluate(
    con: sqlite3.Connection,
    *,
    stale_hours: float = DEFAULT_STALE_HOURS,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    now: datetime | None = None,
) -> list[Finding]:
    """Run every freshness check and return findings, most-severe first.

    The checks, and why each exists:
      * enrichment_stalled (error) — a unit that has been enriched before and still
        has pending days, but hasn't advanced within `stale_hours`. This is the
        exact shape of the silent daily-job failure.
      * pipeline_idle (error) — belt-and-suspenders: *nothing anywhere* has been
        recorded within `stale_hours` while pending work exists. Catches a fully
        dead scheduler even if per-unit logic is fooled.
      * coverage_below_target (warn) — a unit under `min_coverage`; informational
        during rollout, not a failure.
      * not_started (warn) — a unit with summary days but no enrichment yet.
    """
    now = now or _now()
    units = _load_state(con)
    findings: list[Finding] = []

    total_pending = sum(u.pending_days for u in units)
    last_activity = max((u.last_enriched for u in units if u.last_enriched), default=None)

    # Global idle check first — it frames everything else.
    if total_pending > 0:
        if last_activity is None:
            findings.append(
                Finding(
                    "pipeline_idle",
                    "error",
                    "pipeline",
                    f"{total_pending} day(s) pending but nothing has ever been enriched.",
                )
            )
        else:
            idle_h = (now - last_activity).total_seconds() / 3600
            if idle_h > stale_hours:
                findings.append(
                    Finding(
                        "pipeline_idle",
                        "error",
                        "pipeline",
                        f"No enrichment recorded in {idle_h:.0f}h (threshold {stale_hours:.0f}h) "
                        f"while {total_pending} day(s) remain pending.",
                    )
                )

    for u in units:
        if u.pending_days > 0 and u.enriched_days > 0 and u.last_enriched is not None:
            idle_h = (now - u.last_enriched).total_seconds() / 3600
            if idle_h > stale_hours:
                findings.append(
                    Finding(
                        "enrichment_stalled",
                        "error",
                        u.business_unit,
                        f"Last advanced {idle_h:.0f}h ago (threshold {stale_hours:.0f}h); "
                        f"{u.pending_days} of {u.summary_days} day(s) still pending.",
                    )
                )
        if u.summary_days > 0 and u.enriched_days == 0:
            findings.append(
                Finding(
                    "not_started",
                    "warn",
                    u.business_unit,
                    f"{u.summary_days} day(s) in the summary, none enriched yet.",
                )
            )
        elif u.coverage < min_coverage:
            findings.append(
                Finding(
                    "coverage_below_target",
                    "warn",
                    u.business_unit,
                    f"Coverage {u.coverage:.0%} below target {min_coverage:.0%} "
                    f"({u.enriched_days}/{u.summary_days} day(s)).",
                )
            )

    if not findings:
        findings.append(Finding("all_fresh", "ok", "pipeline", "All units fresh and covered."))

    order = {"error": 0, "warn": 1, "ok": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.scope))
    return findings


def next_bu(con: sqlite3.Connection) -> str | None:
    """The business unit most in need of enrichment, or None when nothing pends.

    Selection order: units with pending days only; never-enriched first (they are
    infinitely stale), then least-recently-advanced, then most pending, then BU
    code for determinism. This is what lets a scheduled runner cure its own
    staleness findings — each run advances the worst unit, so the pick rotates
    as `last_enriched` timestamps update."""
    candidates = [u for u in _load_state(con) if u.pending_days > 0]
    if not candidates:
        return None
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(
        key=lambda u: (
            u.last_enriched is not None,
            u.last_enriched or epoch,
            -u.pending_days,
            u.business_unit,
        )
    )
    return candidates[0].business_unit


def _print_report(findings: list[Finding]) -> None:
    # ASCII markers, not emoji: this runs on the Windows console (cp1252), where
    # emoji raise UnicodeEncodeError.
    tag = {"error": "[ERROR]", "warn": "[WARN] ", "ok": "[OK]   "}
    width = max((len(f.check) for f in findings), default=10)
    for f in findings:
        print(f"{tag.get(f.severity, '[?]')} {f.check.ljust(width)}  {f.scope:<10}  {f.detail}")
    errors = sum(1 for f in findings if f.severity == "error")
    warns = sum(1 for f in findings if f.severity == "warn")
    print(f"\n{errors} error(s), {warns} warning(s).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SCPRS operational-store health & freshness checks.")
    ap.add_argument("--stale-hours", type=float, default=DEFAULT_STALE_HOURS)
    ap.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)
    ap.add_argument("--db", type=Path, default=DB_PATH, help="Path to scprs.db")
    ap.add_argument("--json", action="store_true", help="Emit findings as JSON")
    ap.add_argument(
        "--next-bu",
        action="store_true",
        help="Print only the business unit most in need of enrichment "
        "(exit 1 if nothing is pending) — for scheduled runners.",
    )
    args = ap.parse_args(argv)

    # Read-only: never let a health check mutate the store it inspects.
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if args.next_bu:
            bu = next_bu(con)
            if bu is None:
                return 1
            print(bu)
            return 0
        findings = evaluate(con, stale_hours=args.stale_hours, min_coverage=args.min_coverage)
    finally:
        con.close()

    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        _print_report(findings)

    # Exit non-zero on any error finding so a scheduled CI job can alert on it.
    return 1 if any(f.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
