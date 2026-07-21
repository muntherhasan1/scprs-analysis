"""Offline tests for the PR warehouse-output diff (Wave 3)."""

import sqlite3

from src import warehouse_diff


def _tiny_warehouse(path):
    """A minimal built-warehouse shape: a dim, a fact, a mart view, an lv_ view."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE dim_supplier (sup_key INTEGER, canon_id TEXT)")
    con.executemany("INSERT INTO dim_supplier VALUES (?, ?)", [(1, "A"), (2, "B"), (3, "A")])
    con.execute("CREATE VIEW lv_dim_supplier AS SELECT canon_id AS canonical_id FROM dim_supplier")
    con.execute("CREATE TABLE fact_document (grand_tot REAL)")
    con.executemany("INSERT INTO fact_document VALUES (?)", [(100.0,), (50.0,)])
    con.execute(
        "CREATE VIEW lv_fact_document AS SELECT grand_tot AS grand_total FROM fact_document"
    )
    con.execute("CREATE VIEW gold_cmas_agreement AS SELECT 1 AS agreement_number")
    con.commit()
    con.close()


def test_snapshot_captures_objects_columns_metrics(tmp_path):
    db = tmp_path / "warehouse.db"
    _tiny_warehouse(db)
    snap = warehouse_diff.snapshot(db)
    # gold/dim/fact objects are captured, sorted.
    assert "dim_supplier" in snap["objects"]
    assert "fact_document" in snap["objects"]
    assert "gold_cmas_agreement" in snap["objects"]
    # columns per object.
    assert snap["columns"]["dim_supplier"] == ["sup_key", "canon_id"]
    # row counts.
    assert snap["row_counts"]["dim_supplier"] == 3
    # metrics computed via logical views; unresolvable ones are None, not errors.
    assert snap["metrics"]["fact_document_rows"] == 2
    assert snap["metrics"]["total_grand_total"] == 150.0
    assert snap["metrics"]["canonical_suppliers"] == 2  # distinct canonical A, B
    assert snap["metrics"]["cmas_agreements"] == 1
    assert snap["metrics"]["departments"] is None  # dim_department absent -> None


def _snap(objects=None, columns=None, row_counts=None, metrics=None):
    return {
        "objects": objects or [],
        "columns": columns or {},
        "row_counts": row_counts or {},
        "metrics": metrics or {},
    }


def test_report_no_change():
    s = _snap(["gold_x"], {"gold_x": ["a"]}, {"gold_x": 10}, {"fact_document_rows": 5})
    out = warehouse_diff.report(s, s)
    assert "No change" in out


def test_report_added_and_removed_objects():
    base = _snap(["gold_old", "dim_supplier"])
    head = _snap(["gold_new", "dim_supplier"])
    out = warehouse_diff.report(base, head)
    assert "🟢 added `gold_new`" in out
    assert "🔴 removed `gold_old`" in out
    assert "removed object `gold_old`" in out  # flagged for a second look


def test_report_dropped_column_is_flagged():
    base = _snap(["gold_x"], {"gold_x": ["a", "b", "renamed"]})
    head = _snap(["gold_x"], {"gold_x": ["a", "b", "renamed_to"]})
    out = warehouse_diff.report(base, head)
    assert "+`renamed_to`" in out and "−`renamed`" in out
    assert "dropped column" in out  # the removal is flagged


def test_report_large_row_drop_flagged_small_change_not():
    base = _snap(["fact_document", "dim_x"], row_counts={"fact_document": 1000, "dim_x": 1000})
    head = _snap(["fact_document", "dim_x"], row_counts={"fact_document": 900, "dim_x": 999})
    out = warehouse_diff.report(base, head)
    # 10% drop on fact_document -> flagged with the warning marker.
    assert "⚠️" in out
    assert "`fact_document` rows 1,000 → 900" in out
    # 0.1% drop on dim_x -> shown but not in the flags section.
    assert "dim_x`: 1,000 → 999" in out


def test_report_metric_change():
    base = _snap(metrics={"total_grand_total": 1000.0, "cmas_agreements": 0})
    head = _snap(metrics={"total_grand_total": 1250.5, "cmas_agreements": 2396})
    out = warehouse_diff.report(base, head)
    assert "total_grand_total`: 1,000.00 → 1,250.50" in out
    assert "cmas_agreements`: 0 → 2,396" in out
