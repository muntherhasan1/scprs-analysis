"""Offline test for the medallion warehouse: build from a seeded source DB."""

import sqlite3

from src import model, warehouse


def _seed_source(path):
    con = sqlite3.connect(path)
    model._ensure_schema(con)
    model._ensure_details_schema(con)
    # doc A: two versions -> must collapse to the current version (v2, grand_total 150)
    con.executemany(
        "INSERT INTO purchases (business_unit, purchase_document, version, grand_total, "
        "start_date, acquisition_type_sub_type, acquisition_method, supplier_id, supplier_name, "
        "buyer_name, buyer_email, status, department_name, associated_pos) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "8660",
                "A",
                "1",
                100.0,
                "2021-02-18",
                "IT Services_Software",
                "Formal - COMPETITIVE",
                "S1",
                "Acme",
                "Bob",
                "b@x",
                "Active",
                "PUC",
                None,
            ),
            (
                "8660",
                "A",
                "2",
                150.0,
                "2021-02-18",
                "IT Services_Software",
                "Formal - COMPETITIVE",
                "S1",
                "Acme",
                "Bob",
                "b@x",
                "Active",
                "PUC",
                None,
            ),
            # doc B: enriched contract with an associated PO
            (
                "8660",
                "B",
                "1",
                5000.0,
                "2021-03-01",
                "NON-IT Services_Consulting",
                "NON-COMPETITIVELY BID (NCB)",
                "S2",
                "Beta",
                "Sue",
                "s@x",
                "Active",
                "PUC",
                "0000009",
            ),
        ],
    )
    con.execute(
        "INSERT INTO document_details (business_unit, purchase_document, version, bill_code, "
        "merchandise_amount, freight_tax_misc, grand_total, start_date, acquisition_type, "
        "acquisition_method, supplier_name, buyer_name) "
        "VALUES ('8660','B','1','059000',5000.0,0.0,5000.0,'2021-03-01','NON-IT Services',"
        "'NON-COMPETITIVELY BID (NCB)','Beta','Sue')"
    )
    con.executemany(
        "INSERT INTO document_lines (business_unit, purchase_document, document_version, "
        "line_number, unspsc, unspsc_description, quantity, unit_price, line_status) "
        "VALUES ('8660','B','1',?,?,?,?,?,'Active')",
        [("1", "43230000", "Software", 1.0, 2000.0), ("2", "81111508", "Services", 3.0, 1000.0)],
    )
    con.execute(
        "INSERT INTO document_pos (business_unit, purchase_document, document_version, po_id, "
        "buyer, start_date, po_total, po_status) "
        "VALUES ('8660','B','1','P1','Sue','2021-03-02',5000.0,'Closed')"
    )
    con.commit()
    con.close()


def test_warehouse_build(tmp_path):
    src, wh = tmp_path / "scprs.db", tmp_path / "warehouse.db"
    _seed_source(src)
    result = warehouse.build_all(
        wh_path=wh, source_path=src, enrichment_db=tmp_path / "no_enrich.db", log=lambda *a: None
    )

    con = sqlite3.connect(wh)
    try:
        # Silver document grain: one row per document (A's two versions collapsed)
        assert con.execute("SELECT COUNT(*) FROM silver_document").fetchone()[0] == 2
        assert (
            con.execute(
                "SELECT grand_total FROM silver_document WHERE purchase_document='A'"
            ).fetchone()[0]
            == 150.0
        )  # current version won
        # acquisition string parsed into type/sub_type
        assert con.execute(
            "SELECT acquisition_type, acquisition_sub_type FROM silver_document "
            "WHERE purchase_document='A'"
        ).fetchone() == ("IT Services", "Software")

        # dim_date spine populated (more than just the Unknown member)
        assert con.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0] > 1

        # Star integrity: no orphan foreign keys (physical cols are abbreviated)
        assert (
            con.execute(
                "SELECT COUNT(*) FROM fact_document WHERE dept_key IS NULL OR sup_key IS NULL "
                "OR acq_key IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute("SELECT COUNT(*) FROM fact_line WHERE unspsc_key IS NULL").fetchone()[0]
            == 0
        )

        # fact_line reconciles to merchandise amount (1*2000 + 3*1000 = 5000)
        assert (
            con.execute("SELECT ROUND(SUM(ln_amt), 2) FROM fact_line WHERE pur_doc='B'").fetchone()[
                0
            ]
            == 5000.0
        )

        # Gold mart: B classified as a contract (has associated POs)
        contract = con.execute(
            "SELECT document_count FROM gold_contract_vs_standalone "
            "WHERE document_type LIKE 'contract%'"
        ).fetchone()[0]
        assert contract == 1

        # Competitive-intelligence marts: B was NON-COMPETITIVELY BID -> 100%
        ncb = con.execute(
            "SELECT pct_noncompetitive_value FROM gold_supplier_profile WHERE supplier_id='S2'"
        ).fetchone()[0]
        assert ncb == 100.0
        # concentration mart computes an HHI per market
        assert (
            con.execute(
                "SELECT COUNT(*) FROM gold_market_concentration WHERE hhi IS NOT NULL"
            ).fetchone()[0]
            >= 1
        )
        # supplier category profile: S2's two enriched lines span two UNSPSC categories
        assert (
            con.execute(
                "SELECT category_count FROM gold_supplier_specialization WHERE supplier_id='S2'"
            ).fetchone()[0]
            == 2
        )
        # canonical layer wired in: unmapped suppliers are their own canonical entity
        # (physical dim_supplier columns are abbreviated: supplier_id->sup_id, etc.)
        assert (
            con.execute("SELECT canon_id FROM dim_supplier WHERE sup_id='S1'").fetchone()[0] == "S1"
        )
        assert (
            con.execute(
                "SELECT registration_count FROM gold_canonical_supplier_spend "
                "WHERE canonical_name='Acme'"
            ).fetchone()[0]
            == 1
        )

        # Abbreviation layer: physical columns abbreviated, lv_ view exposes logical
        # names, marts keep friendly output names, and the mapping is recorded.
        phys = {r[1] for r in con.execute("PRAGMA table_info(dim_supplier)")}
        assert {"sup_id", "sup_nm", "grand_tot"} & phys == {"sup_id", "sup_nm"}
        assert "supplier_id" not in phys  # logical name is gone from physical storage
        lv = {r[1] for r in con.execute("PRAGMA table_info(lv_dim_supplier)")}
        assert {"supplier_id", "supplier_name", "canonical_id"} <= lv  # friendly view
        mart = {r[1] for r in con.execute("PRAGMA table_info(gold_supplier_profile)")}
        assert "supplier_id" in mart and "total_value" in mart  # marts stay friendly
        assert (
            con.execute(
                "SELECT physical_name FROM gold_data_dictionary "
                "WHERE table_name='fact_document' AND logical_name='grand_total'"
            ).fetchone()[0]
            == "grand_tot"
        )
    finally:
        con.close()

    # No error-severity data-quality failures
    errors = [d for d in result["dq"] if not d["passed"] and d["severity"] == "error"]
    assert errors == []


def test_abbreviate():
    abbr = {"amount": "amt", "supplier": "sup", "name": "nm", "business_unit": "bu", "total": "tot"}
    # token-by-token replacement, unknown tokens pass through
    assert warehouse.abbreviate("merchandise_amount", abbr) == "merchandise_amt"
    assert warehouse.abbreviate("supplier_name", abbr) == "sup_nm"
    assert warehouse.abbreviate("grand_total", abbr) == "grand_tot"
    # full-name (phrase) match wins over token replacement
    assert warehouse.abbreviate("business_unit", abbr) == "bu"
    # nothing to abbreviate -> unchanged
    assert warehouse.abbreviate("po_id", abbr) == "po_id"


def test_load_abbreviations():
    abbr = warehouse.load_abbreviations()  # the real references/abbreviations.csv
    assert abbr["amount"] == "amt"
    assert abbr["supplier"] == "sup"
    assert warehouse.abbreviate("unit_price", abbr) == "unt_prc"
