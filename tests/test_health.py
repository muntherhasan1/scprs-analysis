"""Offline tests for the freshness/health observer.

Seeds a source DB with controlled `completed_at` timestamps and a fixed `now`, so
the staleness logic is deterministic. The headline case is the one that motivated
the module: a unit that was being enriched, then stopped advancing while work
remained — the silent daily-job failure.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from src import health, model

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _seed(path):
    con = sqlite3.connect(path)
    model._ensure_schema(con)
    model._ensure_progress_schema(con)
    con.close()


def _add_summary(path, bu, days):
    con = sqlite3.connect(path)
    con.executemany(
        "INSERT INTO purchases (business_unit, purchase_document, version, start_date) "
        "VALUES (?, ?, '1', ?)",
        [(bu, f"{bu}-{d}", d) for d in days],
    )
    con.commit()
    con.close()


def _mark_done(path, bu, days, completed_at):
    con = sqlite3.connect(path)
    con.executemany(
        "INSERT INTO details_progress (business_unit, day, documents, lines, pos, completed_at) "
        "VALUES (?, ?, 1, 1, 0, ?)",
        [(bu, d, completed_at.isoformat()) for d in days],
    )
    con.commit()
    con.close()


def _evaluate(path, **kw):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return health.evaluate(con, now=NOW, **kw)
    finally:
        con.close()


def _by_check(findings):
    out = {}
    for f in findings:
        out.setdefault(f.check, []).append(f)
    return out


def test_stalled_unit_is_an_error(tmp_path):
    """A unit enriched before but stale for >stale_hours while days remain."""
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02", "2026-01-03"])
    # One day done 3 days ago; two still pending -> stalled.
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=72))

    findings = _evaluate(db, stale_hours=48)
    checks = _by_check(findings)
    assert "enrichment_stalled" in checks
    stalled = checks["enrichment_stalled"][0]
    assert stalled.severity == "error"
    assert stalled.scope == "8660"


def test_recently_advanced_unit_is_not_stalled(tmp_path):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02", "2026-01-03"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=6))

    findings = _evaluate(db, stale_hours=48)
    assert "enrichment_stalled" not in _by_check(findings)


def test_fully_covered_unit_has_no_error(tmp_path):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01", "2026-01-02"], NOW - timedelta(hours=200))

    findings = _evaluate(db, stale_hours=48)
    # No pending days -> old timestamps are fine; nothing is an error.
    assert not [f for f in findings if f.severity == "error"]


def test_not_started_unit_is_a_warning_not_an_error(tmp_path):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "2660", ["2026-01-01", "2026-01-02"])  # never enriched

    findings = _evaluate(db, stale_hours=48)
    checks = _by_check(findings)
    assert checks["not_started"][0].severity == "warn"
    # "Not started" during rollout must not gate as stalled.
    assert "enrichment_stalled" not in checks


def test_pipeline_idle_when_everything_stops(tmp_path):
    """The global belt-and-suspenders: pending work but no activity anywhere."""
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=100))

    findings = _evaluate(db, stale_hours=48)
    checks = _by_check(findings)
    assert checks["pipeline_idle"][0].severity == "error"


def test_main_exits_nonzero_on_error(tmp_path):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01"], datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert health.main(["--db", str(db), "--json"]) == 1
