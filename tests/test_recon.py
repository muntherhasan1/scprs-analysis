"""Offline tests for the ground-truth reconciliation probe. The live-site call
is injected (``site_count_fn``), so these cover sampling and verdict logic —
the parts that decide whether real data loss alarms."""

import sqlite3
from datetime import date

from src import model, recon

TODAY = date(2026, 7, 28)


def _seed(path, rows):
    con = sqlite3.connect(path)
    model._ensure_schema(con)
    con.executemany(
        "INSERT INTO purchases (business_unit, purchase_document, version, start_date) "
        "VALUES (?, ?, '1', ?)",
        rows,
    )
    con.commit()
    con.close()
    return path


def _con(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def test_pick_windows_samples_policy_not_store(tmp_path):
    """A month the store LOST must still be a candidate — sampling comes from
    the policy window, not from months the store happens to contain."""
    db = _seed(tmp_path / "s.db", [("5180", "D1", "2022-01-10")])  # store holds ONE month
    con = _con(db)
    try:
        windows = recon.pick_windows(con, 500, today=TODAY, seed=1)
    finally:
        con.close()
    months = {(y, m) for _, y, m in windows}
    assert len(months) > 12  # far beyond the single stored month
    assert all(bu == "5180" for bu, _, _ in windows)


def test_settled_gap_is_error(tmp_path):
    """The headline case: a settled month where the site has more rows."""
    db = _seed(tmp_path / "s.db", [("5180", f"D{i}", "2024-03-05") for i in range(90)])
    con = _con(db)
    try:
        f = recon.check_window(
            con, "5180", 2024, 3, today=TODAY, site_count_fn=lambda *a: (100, False)
        )
    finally:
        con.close()
    assert (f.check, f.severity) == ("ground_truth_gap", "error")
    assert "missing 10" in f.detail


def test_recent_month_gap_is_only_a_warning(tmp_path):
    db = _seed(tmp_path / "s.db", [("5180", "D1", "2026-07-01")])
    con = _con(db)
    try:
        f = recon.check_window(
            con, "5180", 2026, 7, today=TODAY, site_count_fn=lambda *a: (10, False)
        )
    finally:
        con.close()
    assert (f.check, f.severity) == ("recent_drift", "warn")


def test_small_settled_gap_within_tolerance_is_warn(tmp_path):
    db = _seed(tmp_path / "s.db", [("5180", f"D{i}", "2024-03-05") for i in range(99)])
    con = _con(db)
    try:
        f = recon.check_window(
            con, "5180", 2024, 3, today=TODAY, site_count_fn=lambda *a: (100, False)
        )
    finally:
        con.close()
    assert (f.check, f.severity) == ("small_gap", "warn")  # 1% < 2% tolerance


def test_match_and_store_ahead_and_truncated(tmp_path):
    db = _seed(tmp_path / "s.db", [("5180", f"D{i}", "2024-03-05") for i in range(5)])
    con = _con(db)
    try:
        ok = recon.check_window(
            con, "5180", 2024, 3, today=TODAY, site_count_fn=lambda *a: (5, False)
        )
        ahead = recon.check_window(
            con, "5180", 2024, 3, today=TODAY, site_count_fn=lambda *a: (3, False)
        )
        trunc = recon.check_window(
            con, "5180", 2024, 3, today=TODAY, site_count_fn=lambda *a: (0, True)
        )
    finally:
        con.close()
    assert (ok.check, ok.severity) == ("match", "ok")
    assert (ahead.check, ahead.severity) == ("store_ahead", "warn")
    assert (trunc.check, trunc.severity) == ("window_truncated", "warn")


def test_main_exit_codes(tmp_path, monkeypatch):
    db = _seed(tmp_path / "s.db", [("5180", f"D{i}", "2024-03-05") for i in range(90)])
    # Error finding -> 1 (targeted window, injected site count via monkeypatch).
    monkeypatch.setattr(recon, "_site_count", lambda *a: (100, False))
    assert recon.main(["--db", str(db), "--bu", "5180", "--month", "2024-03"]) == 1
    # Probe crash (missing store) -> 2, never 1.
    assert recon.main(["--db", str(tmp_path / "missing.db"), "--samples", "1"]) == 2
