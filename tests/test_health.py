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
    """A unit enriched before but stale for >unit_stale_hours while days remain."""
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02", "2026-01-03"])
    # One day done 3 days ago; two still pending -> stalled.
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=72))

    findings = _evaluate(db, stale_hours=48, unit_stale_hours=48)
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

    findings = _evaluate(db, stale_hours=48, unit_stale_hours=48)
    assert "enrichment_stalled" not in _by_check(findings)


def test_unit_waiting_for_rotation_is_not_stalled(tmp_path):
    """The noise fix: one-unit-per-run rotation means a unit idle for days is
    normal while ANY unit advances. Under the split defaults (48h global /
    168h per-unit) a 100h-idle unit with a fresh sibling raises nothing."""
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "2660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "2660", ["2026-01-01"], NOW - timedelta(hours=100))
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=6))

    findings = _evaluate(db)  # library defaults — the shipped behavior
    assert not [f for f in findings if f.severity == "error"]


def test_unit_stalled_past_unit_threshold_is_error(tmp_path):
    """Past the per-unit threshold the alarm still fires — scoped to that unit,
    with the global check quiet because a sibling is advancing."""
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "2660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "2660", ["2026-01-01"], NOW - timedelta(hours=200))
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=6))

    checks = _by_check(_evaluate(db))
    assert "pipeline_idle" not in checks
    assert [f.scope for f in checks["enrichment_stalled"]] == ["2660"]


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


def _next_bu(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return health.next_bu(con)
    finally:
        con.close()


def test_next_bu_prefers_never_enriched_unit(tmp_path):
    """A unit with no enrichment at all is infinitely stale — picked first."""
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=100))
    _add_summary(db, "2720", ["2026-01-01", "2026-01-02"])  # never enriched

    assert _next_bu(db) == "2720"


def test_next_bu_picks_least_recently_advanced(tmp_path):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=6))
    _add_summary(db, "2660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "2660", ["2026-01-01"], NOW - timedelta(hours=90))

    assert _next_bu(db) == "2660"


def test_next_bu_skips_fully_covered_units(tmp_path):
    """A fully enriched unit never gets picked, however old its timestamps."""
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=500))
    _add_summary(db, "2660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "2660", ["2026-01-01"], NOW - timedelta(hours=6))

    assert _next_bu(db) == "2660"


def test_next_bu_none_when_nothing_pending(tmp_path):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=1))

    assert _next_bu(db) is None


def test_main_next_bu_prints_unit_and_exits_zero(tmp_path, capsys):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "2720", ["2026-01-01"])

    assert health.main(["--db", str(db), "--next-bu"]) == 0
    assert capsys.readouterr().out.strip() == "2720"


def test_main_next_bu_exits_one_when_covered(tmp_path, capsys):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01"])
    _mark_done(db, "8660", ["2026-01-01"], NOW - timedelta(hours=1))

    assert health.main(["--db", str(db), "--next-bu"]) == 1
    assert capsys.readouterr().out.strip() == ""


def test_main_next_bu_exits_two_when_picker_crashes(tmp_path, capsys):
    """A crashed picker (missing/corrupt store) must exit 2, never 1 — exit 1
    means 'nothing pending' and would make the runner silently fall back."""
    missing = tmp_path / "does-not-exist.db"

    assert health.main(["--db", str(missing), "--next-bu"]) == 2
    assert capsys.readouterr().out.strip() == ""  # no unit printed


def test_main_exits_nonzero_on_error(tmp_path):
    db = tmp_path / "scprs.db"
    _seed(db)
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01"], datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert health.main(["--db", str(db), "--json"]) == 1


def test_main_accepts_unit_stale_hours_flag(tmp_path, capsys):
    """--unit-stale-hours reaches evaluate(): a unit idle a few hours trips a
    1h per-unit threshold. main() uses the real clock, so timestamps are seeded
    relative to now — the sibling done minutes ago keeps the global check quiet."""
    db = tmp_path / "scprs.db"
    _seed(db)
    real_now = datetime.now(timezone.utc)
    _add_summary(db, "2660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "2660", ["2026-01-01"], real_now - timedelta(hours=3))
    _add_summary(db, "8660", ["2026-01-01", "2026-01-02"])
    _mark_done(db, "8660", ["2026-01-01"], real_now - timedelta(minutes=10))

    assert health.main(["--db", str(db), "--json", "--unit-stale-hours", "1"]) == 1
    out = capsys.readouterr().out
    assert '"enrichment_stalled"' in out and '"2660"' in out
