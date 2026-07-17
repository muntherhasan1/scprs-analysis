"""Offline tests for the source canary.

The scoring core (`signature`, `compare`) is pure. The orchestration (`run`) is
exercised with a faked drill via monkeypatch, so the retry/tri-state logic is
covered without ever touching the network — only the real Playwright call in
production is unmocked.
"""

import sqlite3

from src import canary, model, scprs

GOLDEN = {"line_count": 7, "grand_total": 20985.98, "line_amount_sum": 19297.45}


def _fixture(**over):
    f = {
        "business_unit": "8660",
        "document": "15TG6140",
        "search_date": "03/16/2016",
        "signature": GOLDEN,
        "money_tol": 0.01,
    }
    f.update(over)
    return f


def _drill_row(doc="15TG6140", grand="20,985.98", lines=None):
    lines = (
        lines
        if lines is not None
        else [
            {"unit_price": f"{p:.2f}", "quantity": "1"}
            for p in (7215.25, 7238.05, 165.0, 1545.65, 1579.0, 1450.0, 104.5)
        ]
    )
    return {"document": doc, "header": {"grand_total": grand}, "lines": lines, "pos": []}


# --- pure core ---------------------------------------------------------------


def test_signature_reduces_to_invariants():
    sig = canary.signature(20985.98, [(7215.25, 1), (7238.05, 1), (165.0, 2)])
    assert sig["line_count"] == 3
    assert sig["grand_total"] == 20985.98
    assert sig["line_amount_sum"] == round(7215.25 + 7238.05 + 330.0, 2)


def test_compare_identical_is_clean():
    assert canary.compare(GOLDEN, dict(GOLDEN)) == []


def test_compare_flags_line_count_and_totals():
    drifted = {"line_count": 6, "grand_total": 20985.98, "line_amount_sum": 19297.45}
    diffs = canary.compare(GOLDEN, drifted)
    assert any("line_count" in d for d in diffs)


def test_compare_money_within_tolerance_passes():
    close = {"line_count": 7, "grand_total": 20985.985, "line_amount_sum": 19297.45}
    assert canary.compare(GOLDEN, close, money_tol=0.01) == []


def test_sig_from_drill_parses_money_strings():
    sig = canary._sig_from_drill(_drill_row())
    assert sig == GOLDEN


# --- orchestration with a faked drill ----------------------------------------


def test_run_pass(monkeypatch):
    monkeypatch.setattr(scprs, "collect_po_details", lambda *a, **k: [_drill_row()])
    out = canary.run(_fixture(), retries=0, backoff=0)
    assert out.status == canary.PASS


def test_run_fail_on_drift(monkeypatch):
    # A dropped grid line: 6 lines instead of 7 -> deterministic FAIL, no retry.
    bad = _drill_row(lines=[{"unit_price": "1.00", "quantity": "1"}] * 6)
    monkeypatch.setattr(scprs, "collect_po_details", lambda *a, **k: [bad])
    out = canary.run(_fixture(), retries=2, backoff=0)
    assert out.status == canary.FAIL
    assert "line_count" in out.detail


def test_run_unavailable_on_transient_error(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("navigation timeout")

    monkeypatch.setattr(scprs, "collect_po_details", boom)
    out = canary.run(_fixture(), retries=1, backoff=0)
    # Infra failure must NOT masquerade as parse drift.
    assert out.status == canary.UNAVAILABLE


def test_run_fail_when_document_missing(monkeypatch):
    monkeypatch.setattr(scprs, "collect_po_details", lambda *a, **k: [_drill_row(doc="OTHER")])
    out = canary.run(_fixture(), retries=0, backoff=0)
    assert out.status == canary.FAIL


def test_run_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("reset")
        return [_drill_row()]

    monkeypatch.setattr(scprs, "collect_po_details", flaky)
    out = canary.run(_fixture(), retries=2, backoff=0)
    assert out.status == canary.PASS
    assert calls["n"] == 2


# --- capture round-trip from a seeded DB -------------------------------------


def test_capture_from_db(tmp_path):
    db = tmp_path / "scprs.db"
    con = sqlite3.connect(db)
    model._ensure_schema(con)
    model._ensure_details_schema(con)
    con.execute(
        "INSERT INTO document_details (business_unit, purchase_document, version, start_date, "
        "grand_total, supplier_name) VALUES ('8660','D1','0','2016-03-16',300.0,'ACME')"
    )
    con.executemany(
        "INSERT INTO document_lines (business_unit, purchase_document, document_version, "
        "line_number, unit_price, quantity) VALUES ('8660','D1','0',?,?,?)",
        [("1", 100.0, 1.0), ("2", 100.0, 2.0)],
    )
    con.commit()
    con.close()

    fx = canary.capture("8660", "D1", db_path=db)
    assert fx["search_date"] == "03/16/2016"
    assert fx["signature"] == {"line_count": 2, "grand_total": 300.0, "line_amount_sum": 300.0}
