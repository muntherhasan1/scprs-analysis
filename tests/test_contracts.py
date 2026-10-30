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


def test_small_redrill_shrink_is_tolerated_but_lost_documents_are_not():
    """Re-drilling a reopened day replaces each document's rows: one copy for a
    duplicated drill, or an amendment with fewer lines, shrinks the row counts a
    little. Erroring on that blocked the publish, and the next run re-drilled the
    same unpublished day — a livelock. The drilled-document set never shrinks."""
    prev = {
        "drilled_docs": 350_789,
        "detail_docs": 355_847,
        "line_items": 1_820_319,
        "line_amount_total": 1.67e11,
    }
    redrill = {
        **prev,
        "detail_docs": 355_847 - 127,  # BU 8570 2024-07-01: 127 duplicate drills collapse
        "line_items": 1_820_319 - 600,
        "line_amount_total": 1.67e11 - 2.5e8,  # an amendment cut a contract's lines
    }
    assert "metric_decreased" not in _by_check(contracts.diff(prev, redrill))

    lost_doc = {**prev, "drilled_docs": 350_788}
    dec = _by_check(contracts.diff(prev, lost_doc))["metric_decreased"]
    assert [f.metric for f in dec] == ["drilled_docs"]

    wiped = {**prev, "line_items": 1_700_000}  # ~6.6% of lines gone in one step
    dec = _by_check(contracts.diff(prev, wiped))["metric_decreased"]
    assert [f.metric for f in dec] == ["line_items"] and "1% re-drill allowance" in dec[0].detail


def test_redrill_metric_change_does_not_false_alarm_on_upgrade():
    """The first snapshot after this change has `drilled_docs` but the previous
    one doesn't — it must be skipped, not compared against nothing."""
    prev = {"detail_docs": 355_847, "line_items": 1_820_319}
    curr = {"drilled_docs": 350_789, "detail_docs": 355_800, "line_items": 1_820_319}
    assert [f for f in contracts.diff(prev, curr) if f.severity != "ok"] == []


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


# --- explained re-drill deltas (redrill_log) ---------------------------------


def _drill(monkeypatch, db, bu, doc, amounts):
    """Load one document's drill with one line per amount (unit_price, qty 1)."""
    fixture = [
        {
            "document": doc,
            "header": {"purchase_document": doc, "version": "1", "grand_total": "$1"},
            "lines": [
                {"line_number": str(i), "unit_price": f"${a:,.2f}", "quantity": "1"}
                for i, a in enumerate(amounts, 1)
            ],
            "pos": [],
        }
    ]
    monkeypatch.setattr(model.scprs, "collect_po_details", lambda *a, **k: fixture)
    model.build_details_db(bu, "01/01/2025", "01/01/2025", db_path=db, log=lambda *a: None)


def _snap(db, at=None):
    con = sqlite3.connect(db)
    try:
        contracts.record(con, contracts.capture_metrics(con), at=at)
    finally:
        con.close()


def _check(db, **k):
    con = sqlite3.connect(db)
    try:
        return contracts.check(con, **k)
    finally:
        con.close()


def test_logged_redrill_explains_any_shrink_but_unlogged_loss_trips(tmp_path, monkeypatch):
    """One huge document (4260 16-93286 is $22.9B, ~14% of all line dollars)
    amended down by more than the old 1% global allowance must NOT block the
    publish — the load logged exactly what it changed. A single line deleted
    outside any logged load still must."""
    db = tmp_path / "scprs.db"
    _drill(monkeypatch, db, "4260", "BIG", [22.9e9, 1.0e9])
    _drill(monkeypatch, db, "4260", "SMALL", [100.0, 50.0])
    _snap(db)

    _drill(monkeypatch, db, "4260", "BIG", [5.0e9])  # amendment: -$18.9B, one line fewer
    _snap(db)
    assert [f for f in _check(db) if f.severity == "error"] == []

    con = sqlite3.connect(db)
    con.execute("DELETE FROM document_lines WHERE purchase_document = 'SMALL' AND line_number='2'")
    con.commit()
    con.close()
    _snap(db)
    errs = {f.metric for f in _check(db) if f.severity == "error"}
    # 1 line / $50 of 2 lines / ~$5B: far under any percentage, still caught.
    assert errs == {"line_items", "line_amount_total"}


def test_window_before_the_log_falls_back_to_the_percentage():
    prev = {"line_items": 1000}  # no LOG_MARKER: predates redrill_log
    curr = {"line_items": 995, contracts.LOG_MARKER: 1}
    assert "metric_decreased" not in _by_check(contracts.diff(prev, curr, explained={}))
    marked = {**prev, contracts.LOG_MARKER: 1}
    dec = _by_check(contracts.diff(marked, curr, explained={}))["metric_decreased"]
    assert [f.metric for f in dec] == ["line_items"]


def test_acknowledge_downgrades_errors_so_an_investigated_state_publishes(tmp_path):
    db = tmp_path / "scprs.db"
    con = _seed(db)
    con.execute(
        "INSERT INTO document_lines (business_unit, purchase_document, line_number, "
        "unit_price, quantity) VALUES ('8660','D1','1',100,1)"
    )
    con.commit()
    contracts.record(con, contracts.capture_metrics(con), at="2026-07-16T00:00:00+00:00")
    con.execute("DELETE FROM document_lines")
    con.commit()
    contracts.record(con, contracts.capture_metrics(con), at="2026-07-16T01:00:00+00:00")
    con.close()
    assert contracts.main(["check", "--db", str(db)]) == 1
    assert contracts.main(["check", "--db", str(db), "--acknowledge", "dedupe #999"]) == 0
    acked = [f for f in _check(db, acknowledge="dedupe #999") if "ACKNOWLEDGED" in f.detail]
    assert acked and all(f.severity == "warn" for f in acked)


def test_drill_metrics_match_the_contracts_metrics():
    """model logs deltas with the SAME SQL the check captures, or the
    subtraction is meaningless."""
    sql = {name: q for name, q, _drop in contracts._METRICS}
    assert {k: sql[k] for k in model._DRILL_METRICS} == model._DRILL_METRICS
    redrill = {n for n, _q, d in contracts._METRICS if d not in (None, 0.0)}
    assert redrill == set(model._DRILL_METRICS)
