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
        "line_number, item_description, unspsc, unspsc_description, quantity, unit_price, "
        "line_status) VALUES ('8660','B','1',?,?,?,?,?,?,'Active')",
        [
            ("1", "Dell laptop", "43230000", "Software", 1.0, 2000.0),
            ("2", "Consulting hours", "81111508", "Services", 3.0, 1000.0),
        ],
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

        # California fiscal year (Jul-Jun, labelled by the year it ends in) holds
        # for every real date; gold_line_item carries it for time+category queries.
        # Physical dim_date cols are abbreviated: full_date->full_dt, year->yr, etc.
        assert (
            con.execute(
                "SELECT COUNT(*) FROM dim_date WHERE full_dt IS NOT NULL AND "
                "fiscal_yr <> yr + (CASE WHEN mth >= 7 THEN 1 ELSE 0 END)"
            ).fetchone()[0]
            == 0
        )
        li_cols = {r[1] for r in con.execute("PRAGMA table_info(gold_line_item)")}
        assert "fiscal_year" in li_cols
        # Curated acquisition taxonomy on the line mart + its crosswalk to UNSPSC.
        assert {"acquisition_type", "acquisition_sub_type"} <= li_cols
        assert con.execute("SELECT COUNT(*) FROM gold_acquisition_unspsc").fetchone()[0] >= 1
        # gold_document is the COMPLETE document-grain mart (dated + canonical +
        # acquisition taxonomy): one row per document, unlike sparse gold_line_item.
        doc_cols = {r[1] for r in con.execute("PRAGMA table_info(gold_document)")}
        assert {"canonical_name", "acquisition_type", "fiscal_year", "grand_total"} <= doc_cols
        assert (
            con.execute("SELECT COUNT(*) FROM gold_document").fetchone()[0]
            == con.execute("SELECT COUNT(*) FROM fact_document").fetchone()[0]
        )

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

        # line-item free-text description carried into gold as a degenerate attribute
        # (physical col abbreviated to item_desc; gold_line_item exposes it friendly)
        assert "item_desc" in {r[1] for r in con.execute("PRAGMA table_info(fact_line)")}
        assert (
            con.execute(
                "SELECT item_description FROM gold_line_item "
                "WHERE purchase_document='B' AND unit_price=2000.0"
            ).fetchone()[0]
            == "Dell laptop"
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

        # Schema standard: surrogate PK + audit columns + CLOB long-text.
        fl = {r[1]: r[2] for r in con.execute("PRAGMA table_info(fact_line)")}  # name -> type
        assert fl["item_desc"] == "CLOB"  # long free-text declared CLOB
        assert "dw_batch_id" in fl and "dw_loaded_at" in fl  # audit columns
        assert fl["ln_amt"] != "TEXT"  # numeric affinity preserved (not forced to TEXT)
        assert [r[1] for r in con.execute("PRAGMA table_info(fact_line)") if r[5]] == ["ln_sk"]
        # silver keeps logical names; long text is CLOB there too
        sl = {r[1]: r[2] for r in con.execute("PRAGMA table_info(silver_line)")}
        assert sl["item_description"] == "CLOB" and "line_sk" in sl and "dw_batch_id" in sl
        # dim_unspsc category label declared CLOB; append-only history has a surrogate key
        assert (
            dict((r[1], r[2]) for r in con.execute("PRAGMA table_info(dim_unspsc)"))["unspsc_desc"]
            == "CLOB"
        )
        assert "history_sk" in {r[1] for r in con.execute("PRAGMA table_info(dw_document_history)")}
    finally:
        con.close()

    # No error-severity data-quality failures
    errors = [d for d in result["dq"] if not d["passed"] and d["severity"] == "error"]
    assert errors == []


def test_contract_change_capture(tmp_path):
    """Append-only history records amendments; the change-log derives the transition."""
    src, wh = tmp_path / "scprs.db", tmp_path / "warehouse.db"
    no_enrich = tmp_path / "no_enrich.db"

    con = sqlite3.connect(src)
    model._ensure_schema(con)
    con.execute(
        "INSERT INTO purchases (business_unit, purchase_document, version, grand_total, status, "
        "start_date, end_date, supplier_id, supplier_name, acquisition_type_sub_type, "
        "acquisition_method, department_name) VALUES ('8660','D','1',100000.0,'Active',"
        "'2021-01-01','2022-01-01','S1','Acme','IT Services','Formal - COMPETITIVE','PUC')"
    )
    con.commit()
    con.close()

    warehouse.build_all(wh_path=wh, source_path=src, enrichment_db=no_enrich, log=lambda *a: None)
    con = sqlite3.connect(wh)
    assert con.execute("SELECT COUNT(*) FROM dw_document_history").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM gold_contract_change_log").fetchone()[0] == 0
    con.close()

    # Amend the contract: value up, status changed, term extended, version bumped.
    con = sqlite3.connect(src)
    con.execute(
        "UPDATE purchases SET version='2', grand_total=150000.0, status='Expired', "
        "end_date='2023-01-01' WHERE purchase_document='D'"
    )
    con.commit()
    con.close()

    warehouse.build_all(wh_path=wh, source_path=src, enrichment_db=no_enrich, log=lambda *a: None)
    con = sqlite3.connect(wh)
    # history appended (append-only: the original v1 snapshot is retained)
    assert con.execute("SELECT COUNT(*) FROM dw_document_history").fetchone()[0] == 2
    row = con.execute(
        "SELECT from_version, to_version, value_delta, from_status, to_status, change_summary "
        "FROM gold_contract_change_log WHERE purchase_document='D'"
    ).fetchone()
    assert row[0] == 1 and row[1] == 2  # v1 -> v2
    assert row[2] == 50000.0  # value delta
    assert (row[3], row[4]) == ("Active", "Expired")
    assert "value +50000" in row[5] and "term extended" in row[5]
    # amendment rollup: current version 2 = 2 amendments, value growth captured
    amend = con.execute(
        "SELECT amendment_count, value_growth FROM gold_contract_amendments "
        "WHERE purchase_document='D'"
    ).fetchone()
    assert amend == (2, 50000.0)
    con.close()

    # Idempotent: rebuilding with no source change appends nothing.
    warehouse.build_all(wh_path=wh, source_path=src, enrichment_db=no_enrich, log=lambda *a: None)
    con = sqlite3.connect(wh)
    assert con.execute("SELECT COUNT(*) FROM dw_document_history").fetchone()[0] == 2
    con.close()


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
