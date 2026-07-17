"""Offline tests for the contract-delta / anomaly checks.

The diff logic is pure; the snapshot store is exercised against a seeded DB.
"""

import sqlite3

from src import contracts, model


def _by_check(findings):
    out = {}
    for f in findings:
        out.setdefault(f.check, []).append(f)
    return out


# --- pure diff logic ---------------------------------------------------------


def test_decrease_in_monotonic_metric_is_error():
    prev = {"line_items": 100, "line_amount_total": 5000.0}
    curr = {"line_items": 90, "line_amount_total": 5000.0}
    findings = contracts.diff(prev, curr)
    dec = _by_check(findings)["metric_decreased"][0]
    assert dec.severity == "error"
    assert dec.metric == "line_items"


def test_normal_growth_is_clean():
    prev = {"line_items": 100, "line_amount_total": 5000.0}
    curr = {"line_items": 110, "line_amount_total": 5400.0}
    findings = contracts.diff(prev, curr)
    assert [f for f in findings if f.severity != "ok"] == []


def test_large_jump_is_a_warning():
    prev = {"line_items": 100}
    curr = {"line_items": 300}  # +200% in one step
    findings = contracts.diff(prev, curr, jump_frac=0.5)
    jump = _by_check(findings)["metric_jumped"][0]
    assert jump.severity == "warn"


def test_non_monotonic_decrease_is_not_an_error():
    # summary_rows is non-monotonic: a re-summary can legitimately shrink it.
    prev = {"summary_rows": 1000}
    curr = {"summary_rows": 900}
    findings = contracts.diff(prev, curr)
    assert "metric_decreased" not in _by_check(findings)


def test_new_metric_absent_from_prev_is_skipped():
    prev = {"line_items": 100}
    curr = {"line_items": 100, "assoc_pos": 5}  # assoc_pos brand new
    findings = contracts.diff(prev, curr)
    assert [f for f in findings if f.severity != "ok"] == []


# --- snapshot store ----------------------------------------------------------


def _seed(path):
    con = sqlite3.connect(path)
    model._ensure_schema(con)
    model._ensure_details_schema(con)
    model._ensure_progress_schema(con)
    return con


def test_capture_and_check_round_trip(tmp_path):
    db = tmp_path / "scprs.db"
    con = _seed(db)
    con.executemany(
        "INSERT INTO document_lines (business_unit, purchase_document, document_version, "
        "line_number, unit_price, quantity) VALUES ('8660','D1','0',?,?,?)",
        [("1", 100.0, 1.0), ("2", 50.0, 2.0)],
    )
    con.commit()
    m1 = contracts.capture_metrics(con)
    contracts.record(con, m1, at="2026-07-16T00:00:00+00:00")
    assert m1["line_items"] == 2
    assert m1["line_amount_total"] == 200.0

    # A second snapshot after data was (wrongly) deleted -> decrease -> error.
    con.execute("DELETE FROM document_lines WHERE line_number = '2'")
    con.commit()
    m2 = contracts.capture_metrics(con)
    contracts.record(con, m2, at="2026-07-16T01:00:00+00:00")

    findings = contracts.check(con)
    con.close()
    assert any(f.severity == "error" and f.metric == "line_items" for f in findings)


def test_check_with_one_snapshot_has_no_baseline(tmp_path):
    db = tmp_path / "scprs.db"
    con = _seed(db)
    contracts.record(con, contracts.capture_metrics(con), at="2026-07-16T00:00:00+00:00")
    findings = contracts.check(con)
    con.close()
    assert findings[0].check == "no_baseline"
    assert not [f for f in findings if f.severity == "error"]


def test_main_check_exits_zero_without_baseline(tmp_path):
    db = tmp_path / "scprs.db"
    _seed(db).close()
    assert contracts.main(["check", "--db", str(db)]) == 0
