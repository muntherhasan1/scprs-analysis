"""Ground-truth reconciliation: does the store still match the live site?

Every existing quality gate is *internal* — DQ tiers, monotonic contracts,
parse-drift canaries — so a store that silently LOST data stays green as long
as what remains is self-consistent. That is exactly how the 2026-07 per-FY
rebuild bug erased six years per department for two weeks without an alarm.

This module closes that class: sample a few random (business_unit, month)
windows from the coverage POLICY window (not from what the store happens to
hold — a vanished month must still be sampled), re-export each window from the
live SCPRS site, and compare row counts against ``purchases``.

Finding semantics:
  * ``ground_truth_gap`` (error) — the site has materially more rows than the
    store for a SETTLED month (older than ``settle_days``). Settled months only
    accrete slowly; a gap past ``tolerance_pct`` means lost or never-loaded data.
  * ``recent_drift`` (warn) — same gap on a recent month; expected, the site
    accretes ahead of our scrape cadence.
  * ``store_ahead`` (warn) — the store has MORE rows than the site: either the
    site pruned documents or our store double-loaded; worth eyes either way.
  * ``window_truncated`` (warn) — a sampled month hit the 65k export cap and
    cannot be compared (split the window manually if it recurs).

Exit contract (health-style): 0 = clean / warn-only, 1 = error finding(s),
2 = the probe itself failed (site unreachable, store missing).

    python -m src.recon --samples 5            # weekly cron default
    python -m src.recon --bu 5180 --month 2024-03   # targeted spot-check
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import random
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .model import DB_PATH

# The coverage policy: every loaded unit is expected to hold this window (the
# last-5 + next-2 fiscal-year priority range; 8660 additionally holds 2016+,
# which sampling from the policy window still validates).
POLICY_START = datetime.date(2021, 7, 1)
DEFAULT_SAMPLES = 5
DEFAULT_TOLERANCE_PCT = 2.0
DEFAULT_SETTLE_DAYS = 60


@dataclass
class Finding:
    check: str
    severity: str  # 'error' | 'warn' | 'ok'
    scope: str  # "BU YYYY-MM"
    detail: str


def _loaded_bus(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT business_unit FROM purchases ORDER BY 1")]


def _month_bounds(year: int, month: int) -> tuple[str, str, str, str]:
    """(from MM/DD/YYYY, to MM/DD/YYYY, iso_first, iso_last) for one month."""
    last = calendar.monthrange(year, month)[1]
    return (
        f"{month:02d}/01/{year}",
        f"{month:02d}/{last}/{year}",
        f"{year}-{month:02d}-01",
        f"{year}-{month:02d}-{last:02d}",
    )


def pick_windows(
    con: sqlite3.Connection, n: int, *, today: datetime.date | None = None, seed: int | None = None
) -> list[tuple[str, int, int]]:
    """``n`` random (bu, year, month) samples across loaded units x the POLICY
    window up to the current month. Sampling from policy — not from months the
    store contains — is the point: a month the store LOST entirely must remain
    a candidate. Seeded by the ISO week by default so a weekly cron probes a
    fresh-but-reproducible set."""
    today = today or datetime.date.today()
    months: list[tuple[int, int]] = []
    cur = POLICY_START
    while (cur.year, cur.month) <= (today.year, today.month):
        months.append((cur.year, cur.month))
        cur = (cur.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)
    pool = [(bu, y, m) for bu in _loaded_bus(con) for (y, m) in months]
    if not pool:
        return []
    default_seed = today.isocalendar()[1] * 10_000 + today.year
    rng = random.Random(seed if seed is not None else default_seed)  # noqa: S311 # nosec B311 - window sampling, not crypto
    return rng.sample(pool, min(n, len(pool)))


def _site_count(bu: str, from_date: str, to_date: str) -> tuple[int, bool]:
    """(row count, truncated) for one window from the LIVE site."""
    from . import scprs

    ex = scprs.download_extract(bu, from_date, to_date)
    if ex.no_records:
        return 0, False
    df = scprs.load_extract(ex.path)
    return len(df), ex.truncated


def check_window(
    con: sqlite3.Connection,
    bu: str,
    year: int,
    month: int,
    *,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    settle_days: int = DEFAULT_SETTLE_DAYS,
    today: datetime.date | None = None,
    site_count_fn=None,
) -> Finding:
    """Reconcile one (bu, month) window: live site vs store.

    ``site_count_fn`` resolves LATE (module lookup at call time, not a bound
    default) so tests can monkeypatch ``_site_count`` — a def-time default
    silently kept the real scraper and hit the live site from the test suite."""
    site_count_fn = site_count_fn or _site_count
    today = today or datetime.date.today()
    from_date, to_date, iso_first, iso_last = _month_bounds(year, month)
    scope = f"{bu} {year}-{month:02d}"
    store = con.execute(
        "SELECT COUNT(*) FROM purchases WHERE business_unit=? AND start_date BETWEEN ? AND ?",
        (bu, iso_first, iso_last),
    ).fetchone()[0]
    site, truncated = site_count_fn(bu, from_date, to_date)
    if truncated:
        return Finding(
            "window_truncated",
            "warn",
            scope,
            f"site export hit the row cap (store={store}) — window not comparable",
        )
    if site == store:
        return Finding("match", "ok", scope, f"site={site} store={store}")
    if store > site:
        return Finding(
            "store_ahead",
            "warn",
            scope,
            f"store={store} > site={site} — site pruned documents or the store double-loaded",
        )
    gap = site - store
    gap_pct = 100.0 * gap / site
    settled = (today - datetime.date.fromisoformat(iso_last)).days > settle_days
    detail = f"site={site} store={store} (missing {gap}, {gap_pct:.1f}%)"
    if settled and gap_pct > tolerance_pct:
        return Finding("ground_truth_gap", "error", scope, detail + " on a SETTLED month")
    return Finding("recent_drift" if not settled else "small_gap", "warn", scope, detail)


def run(
    db: Path,
    *,
    samples: int = DEFAULT_SAMPLES,
    seed: int | None = None,
    targeted: tuple[str, int, int] | None = None,
    site_count_fn=None,
    log=print,
) -> list[Finding]:
    site_count_fn = site_count_fn or _site_count
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        windows = [targeted] if targeted else pick_windows(con, samples, seed=seed)
        findings = []
        for i, (bu, y, m) in enumerate(windows, 1):
            log(f"[{i}/{len(windows)}] probing BU {bu} {y}-{m:02d}...")
            f = check_window(con, bu, y, m, site_count_fn=site_count_fn)
            log(f"  {f.check} ({f.severity}): {f.detail}")
            findings.append(f)
        return findings
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ground-truth reconciliation vs the live SCPRS site")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument("--seed", type=int, default=None, help="override the weekly sampling seed")
    ap.add_argument("--bu", help="targeted spot-check: business unit (with --month)")
    ap.add_argument("--month", help="targeted spot-check: YYYY-MM (with --bu)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    targeted = None
    if args.bu and args.month:
        y, m = args.month.split("-")
        targeted = (args.bu, int(y), int(m))

    try:
        findings = run(args.db, samples=args.samples, seed=args.seed, targeted=targeted)
    except Exception as exc:  # noqa: BLE001 - infra failure gets its own exit code
        print(f"recon could not run: {type(exc).__name__}: {exc}")
        return 2
    if args.json:
        import json

        print(json.dumps([asdict(f) for f in findings], indent=2))
    errors = sum(1 for f in findings if f.severity == "error")
    warns = sum(1 for f in findings if f.severity == "warn")
    print(f"\n{len(findings)} window(s) probed: {errors} error(s), {warns} warning(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
