"""Tests for web-researched supplier profiles and their warehouse integration."""

import sqlite3

from src import model, supplier_research, warehouse


def test_save_profiles_roundtrip(tmp_path):
    db = tmp_path / "enr.db"
    n = supplier_research.save_profiles(
        [
            {
                "supplier_name": "ACME INC",
                "org_type": "For-profit",
                "hq_state": "CA",
                "confidence": 0.9,
                "sources": ["https://example.gov/acme"],
            }
        ],
        db_path=db,
    )
    assert n == 1
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT org_type, hq_state, confidence, sources FROM supplier_web_profile "
            "WHERE supplier_name='ACME INC'"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "For-profit" and row[1] == "CA" and row[2] == 0.9
    assert "example.gov" in row[3]  # sources list serialized to JSON


def test_warehouse_folds_in_profiles(tmp_path):
    # source with a supplier whose name matches a researched profile
    src = tmp_path / "scprs.db"
    con = model._connect(src)
    model._ensure_schema(con)
    con.execute(
        "INSERT INTO purchases (business_unit, purchase_document, version, grand_total, "
        "start_date, acquisition_type_sub_type, acquisition_method, supplier_id, supplier_name, "
        "status) VALUES ('8660','D1','1',1000.0,'2025-01-05','IT Services_x',"
        "'Formal - COMPETITIVE','S9','GLOBEX CORP','Active')"
    )
    con.commit()
    con.close()

    enr = tmp_path / "enr.db"
    supplier_research.save_profiles(
        [
            {
                "supplier_name": "GLOBEX CORP",
                "org_type": "For-profit",
                "hq_city": "Fresno",
                "hq_state": "CA",
                "confidence": 0.8,
            }
        ],
        db_path=enr,
    )

    wh = tmp_path / "warehouse.db"
    warehouse.build_all(wh_path=wh, source_path=src, enrichment_db=enr, log=lambda *a: None)

    con = sqlite3.connect(wh)
    try:
        row = con.execute(
            "SELECT total_value, org_type, hq_state, profile_confidence "
            "FROM gold_supplier_enriched WHERE supplier_name='GLOBEX CORP'"
        ).fetchone()
    finally:
        con.close()
    assert row == (1000.0, "For-profit", "CA", 0.8)  # internal metric + researched firmographics
