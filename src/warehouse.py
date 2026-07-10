"""Medallion data warehouse for SCPRS (Bronze -> Silver -> Gold).

Reads the operational store (data/scprs.db) and builds a layered analytical
warehouse in data/warehouse.db, following data-warehousing best practices:

  bronze_*  Raw, append-shaped snapshot of each source table, untransformed,
            stamped with load lineage (_batch_id, _loaded_at, _source).
  silver_*  Cleaned + conformed + typed + deduplicated. Amounts/dates cast,
            acquisition strings parsed, NULLs defaulted to explicit "Unknown"
            members, data-quality flags computed. One row per business entity.
  gold      Kimball star schema: conformed dimensions with surrogate keys
            (dim_*) and fact tables at declared grains (fact_*), plus mart
            views for common analysis.

Control/audit: every build is a batch logged in `dw_batch`; data-quality
results land in `dw_dq_results`. Rebuilds are idempotent (full refresh of
bronze/silver/gold from source); the control tables are append-only history.

Grains:
  fact_document      one row per purchase document (all documents)
  fact_line          one row per document line item (enriched documents only)
  fact_associated_po one row per associated PO transaction (enriched only)

CLI:
  python -m src.warehouse build     # build all layers from data/scprs.db
  python -m src.warehouse dq         # run data-quality checks
  python -m src.warehouse info       # layer table row counts + last batch
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import scprs, supplier_master

SOURCE_DB = scprs.DATA_DIR / "scprs.db"
WAREHOUSE_DB = scprs.DATA_DIR / "warehouse.db"
ENRICHMENT_DB = scprs.DATA_DIR / "supplier_enrichment.db"  # web-researched supplier profiles
DEPARTMENTS_CSV = Path(__file__).resolve().parent.parent / "references" / "departments.csv"
ABBREVIATIONS_CSV = Path(__file__).resolve().parent.parent / "references" / "abbreviations.csv"


def load_abbreviations(path: Path = ABBREVIATIONS_CSV) -> dict[str, str]:
    """Load the term->abbreviation dictionary that standardizes gold physical names."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {
            r["term"].strip().lower(): r["abbreviation"].strip().lower()
            for r in csv.DictReader(fh)
            if r.get("term") and r.get("abbreviation")
        }


def abbreviate(name: str, abbr: dict[str, str]) -> str:
    """Return the standardized physical form of a snake_case column name.

    A full-name match wins first (so multi-word phrases like `business_unit -> bu`
    apply), otherwise each `_`-separated token is abbreviated independently and
    unknown tokens pass through unchanged. Deterministic and dictionary-driven.
    """
    key = name.strip().lower()
    if key in abbr:
        return abbr[key]
    return "_".join(abbr.get(tok, tok) for tok in key.split("_"))


_BRONZE_SOURCES = {
    "bronze_purchases": "purchases",
    "bronze_document_details": "document_details",
    "bronze_document_lines": "document_lines",
    "bronze_document_pos": "document_pos",
}


def _connect(wh_path: Path = WAREHOUSE_DB, source_path: Path = SOURCE_DB) -> sqlite3.Connection:
    con = sqlite3.connect(wh_path, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("ATTACH DATABASE ? AS src", (str(source_path),))
    return con


def _src_has(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _count(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


# --------------------------------------------------------------------------- #
# Bronze: raw snapshot + lineage
# --------------------------------------------------------------------------- #
def build_bronze(
    con: sqlite3.Connection, batch: str, ts: str, enrichment_db: Path = ENRICHMENT_DB
) -> dict:
    counts = {}
    for bronze, source in _BRONZE_SOURCES.items():
        con.execute(f"DROP TABLE IF EXISTS {bronze}")
        if _src_has(con, source):
            con.execute(
                f"CREATE TABLE {bronze} AS "  # noqa: S608 - table names are internal constants
                f"SELECT *, ? AS _batch_id, ? AS _loaded_at, ? AS _source FROM src.{source}",
                (batch, ts, f"scprs.db:{source}"),
            )
        counts[bronze] = _count(con, bronze) if _src_has_local(con, bronze) else 0

    # web-researched supplier profiles (kept in a separate enrichment store; read
    # with its own connection to avoid cross-database attach locks).
    con.execute("DROP TABLE IF EXISTS bronze_supplier_web")
    web_cols, web_rows = None, []
    if enrichment_db.exists():
        enr = sqlite3.connect(enrichment_db, timeout=30)
        try:
            if enr.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supplier_web_profile'"
            ).fetchone():
                cur = enr.execute("SELECT * FROM supplier_web_profile")
                web_cols = [d[0] for d in cur.description]
                web_rows = cur.fetchall()
        finally:
            enr.close()
    if web_cols:
        defs = ", ".join(f"{c} {'REAL' if c == 'confidence' else 'TEXT'}" for c in web_cols)
        con.execute(
            f"CREATE TABLE bronze_supplier_web ({defs}, _batch_id TEXT, _loaded_at TEXT, _source TEXT)"
        )
        ph = ", ".join("?" * (len(web_cols) + 3))
        con.executemany(
            f"INSERT INTO bronze_supplier_web VALUES ({ph})",
            [[*r, batch, ts, "supplier_enrichment.db"] for r in web_rows],
        )
    else:
        con.execute(
            "CREATE TABLE bronze_supplier_web (supplier_name TEXT, description TEXT, "
            "org_type TEXT, hq_city TEXT, hq_state TEXT, website TEXT, parent_affiliation TEXT, "
            "sb_dvbe TEXT, confidence REAL)"
        )
    counts["bronze_supplier_web"] = _count(con, "bronze_supplier_web")
    return counts


def _src_has_local(con: sqlite3.Connection, table: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


# --------------------------------------------------------------------------- #
# Silver: clean, conform, type, default, flag
# --------------------------------------------------------------------------- #
def build_silver(con: sqlite3.Connection, batch: str, ts: str) -> dict:
    # Drop derived views up front: they are rebuilt in gold, and leaving stale ones
    # would let a broken view block the ALTER ... RENAME steps in _finalize below.
    for (v,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND (name LIKE 'gold_%' OR name LIKE 'lv_%')"
    ).fetchall():
        con.execute(f"DROP VIEW IF EXISTS {v}")  # noqa: S608 - internal view names
    have_details = _src_has_local(con, "bronze_document_details")
    have_lines = _src_has_local(con, "bronze_document_lines")
    have_pos = _src_has_local(con, "bronze_document_pos")

    # -- silver_department: conformed reference dimension from references/ + data
    con.execute("DROP TABLE IF EXISTS silver_department")
    con.execute(
        "CREATE TABLE silver_department (business_unit TEXT PRIMARY KEY, department_name TEXT, "
        "dw_loaded_at TEXT)"
    )
    if DEPARTMENTS_CSV.exists():
        with DEPARTMENTS_CSV.open(encoding="utf-8") as fh:
            rows = [(r["code"], r["name"], ts) for r in csv.DictReader(fh)]
        con.executemany("INSERT OR IGNORE INTO silver_department VALUES (?, ?, ?)", rows)
    # add any business units present in data but missing from the reference list
    con.execute(
        "INSERT OR IGNORE INTO silver_department "
        "SELECT DISTINCT business_unit, MAX(department_name), ? FROM bronze_purchases "
        "GROUP BY business_unit",
        (ts,),
    )

    # -- silver_document: one row per (business_unit, purchase_document); merges the
    #    authoritative drill-down (document_details) over the summary (purchases).
    det = "bronze_document_details" if have_details else None
    con.execute("DROP TABLE IF EXISTS silver_document")
    d_col = (lambda c: f"d.{c}") if det else (lambda c: "NULL")
    # Source grain is (document, version): collapse to the current (latest) version
    # so silver_document is one row per document. Details are deduped the same way
    # (the results grid can list a document once per version -> repeated drills).
    rank = (
        "ROW_NUMBER() OVER (PARTITION BY business_unit, purchase_document "
        "ORDER BY CAST(version AS INTEGER) DESC, rowid DESC)"
    )
    ctes = f"WITH pur AS (SELECT *, {rank} AS _rn FROM bronze_purchases)"
    from_clause = "FROM pur p"
    where_clause = "WHERE p._rn = 1"
    if det:
        ctes += f", det AS (SELECT *, {rank} AS _rn FROM {det})"
        from_clause += (
            " LEFT JOIN det d ON d.business_unit = p.business_unit "
            "AND d.purchase_document = p.purchase_document AND d._rn = 1"
        )
    con.execute(
        f"""
        CREATE TABLE silver_document AS
        {ctes}
        SELECT
          p.business_unit,
          p.purchase_document,
          COALESCE({d_col('department_name')}, p.department_name, 'Unknown') AS department_name,
          COALESCE(p.supplier_id, 'UNKNOWN')                                 AS supplier_id,
          COALESCE({d_col('supplier_name')}, p.supplier_name, 'Unknown')     AS supplier_name,
          COALESCE({d_col('buyer_name')}, p.buyer_name, 'Unknown')           AS buyer_name,
          COALESCE({d_col('buyer_email')}, p.buyer_email, '')                AS buyer_email,
          -- acquisition type: prefer drill-down; else split summary "type_subtype"
          COALESCE(
            {d_col('acquisition_type')},
            CASE WHEN instr(p.acquisition_type_sub_type, '_') > 0
                 THEN substr(p.acquisition_type_sub_type, 1, instr(p.acquisition_type_sub_type,'_')-1)
                 ELSE p.acquisition_type_sub_type END,
            'Unknown') AS acquisition_type,
          COALESCE(
            CASE WHEN instr(p.acquisition_type_sub_type, '_') > 0
                 THEN substr(p.acquisition_type_sub_type, instr(p.acquisition_type_sub_type,'_')+1)
                 ELSE NULL END,
            'N/A') AS acquisition_sub_type,
          COALESCE({d_col('acquisition_method')}, p.acquisition_method, 'Unknown') AS acquisition_method,
          CASE
            WHEN COALESCE({d_col('acquisition_method')}, p.acquisition_method) LIKE '%NON-COMPETITIVELY BID%'
              THEN 'Non-Competitive'
            WHEN COALESCE({d_col('acquisition_method')}, p.acquisition_method) LIKE '%COMPETITIVE%'
              THEN 'Competitive'
            ELSE 'Other' END AS competitive_flag,
          COALESCE({d_col('status')}, p.status)               AS status,
          CAST(COALESCE({d_col('version')}, p.version) AS INTEGER) AS version,
          {d_col('bill_code')}                                AS bill_code,
          COALESCE({d_col('lpa_contract_id')}, p.lpa_contract_id) AS lpa_contract_id,
          COALESCE({d_col('start_date')}, p.start_date)       AS start_date,
          COALESCE({d_col('end_date')}, p.end_date)           AS end_date,
          {d_col('merchandise_amount')}                       AS merchandise_amount,
          {d_col('freight_tax_misc')}                         AS freight_tax_misc,
          COALESCE({d_col('grand_total')}, p.grand_total)     AS grand_total,
          CASE WHEN {('d.purchase_document' if det else 'NULL')} IS NOT NULL THEN 1 ELSE 0 END
                                                              AS is_enriched,
          -- classification available for ALL docs (drill-down POs are enriched-only)
          CASE WHEN p.associated_pos IS NOT NULL AND TRIM(p.associated_pos) <> ''
               THEN 1 ELSE 0 END                              AS has_associated_pos
        {from_clause}
        {where_clause}
        """
    )

    # -- silver_line
    con.execute("DROP TABLE IF EXISTS silver_line")
    if have_lines:
        # Keep only the current document version's lines (a document can be drilled
        # at several versions); DISTINCT then collapses identical repeat drills.
        con.execute(
            """
            CREATE TABLE silver_line AS
            SELECT DISTINCT business_unit, purchase_document, line_number, item_id, item_description,
                   unspsc, unspsc_description, unit_of_measure, quantity, unit_price,
                   ROUND(quantity * unit_price, 2) AS line_amount, line_status
            FROM (
              SELECT business_unit, purchase_document,
                     CAST(line_number AS INTEGER) AS line_number,
                     item_id, item_description,
                     COALESCE(unspsc, 'UNKNOWN') AS unspsc,
                     COALESCE(unspsc_description, 'Unknown') AS unspsc_description,
                     unit_of_measure,
                     CAST(quantity AS REAL) AS quantity,
                     CAST(unit_price AS REAL) AS unit_price,
                     line_status,
                     CAST(COALESCE(document_version, '-1') AS INTEGER) AS _v,
                     MAX(CAST(COALESCE(document_version, '-1') AS INTEGER))
                       OVER (PARTITION BY business_unit, purchase_document) AS _maxv
              FROM bronze_document_lines
            )
            WHERE _v = _maxv
            """
        )
    else:  # no enriched lines yet: empty table with the full schema downstream expects
        con.execute(
            "CREATE TABLE silver_line (business_unit TEXT, purchase_document TEXT, "
            "line_number INTEGER, item_id TEXT, item_description TEXT, unspsc TEXT, "
            "unspsc_description TEXT, unit_of_measure TEXT, quantity REAL, unit_price REAL, "
            "line_amount REAL, line_status TEXT)"
        )

    # -- silver_associated_po
    con.execute("DROP TABLE IF EXISTS silver_associated_po")
    if have_pos:
        con.execute(
            """
            CREATE TABLE silver_associated_po AS
            SELECT DISTINCT business_unit, purchase_document, po_id, buyer, start_date,
                   po_total, po_status
            FROM (
              SELECT business_unit, purchase_document, po_id,
                     COALESCE(buyer, 'Unknown') AS buyer, start_date,
                     CAST(po_total AS REAL) AS po_total, po_status,
                     CAST(COALESCE(document_version, '-1') AS INTEGER) AS _v,
                     MAX(CAST(COALESCE(document_version, '-1') AS INTEGER))
                       OVER (PARTITION BY business_unit, purchase_document) AS _maxv
              FROM bronze_document_pos
            )
            WHERE _v = _maxv
            """
        )
    else:  # no enriched POs yet: empty table with the full schema downstream expects
        con.execute(
            "CREATE TABLE silver_associated_po (business_unit TEXT, purchase_document TEXT, "
            "po_id TEXT, buyer TEXT, start_date TEXT, po_total REAL, po_status TEXT)"
        )

    # attach line/PO counts from the deduped, version-correct silver tables in one
    # pass each (UPDATE ... FROM aggregate; avoids a per-row correlated subquery)
    for col, tbl in (
        ("line_count", "silver_line"),
        ("associated_po_count", "silver_associated_po"),
    ):
        con.execute(f"ALTER TABLE silver_document ADD COLUMN {col} INTEGER DEFAULT 0")
        con.execute(
            f"UPDATE silver_document SET {col} = c.n FROM "
            f"(SELECT business_unit, purchase_document, COUNT(*) AS n FROM {tbl} "
            f"GROUP BY business_unit, purchase_document) c "
            f"WHERE silver_document.business_unit = c.business_unit "
            f"AND silver_document.purchase_document = c.purchase_document"
        )

    # data-quality flag: enriched line items should reconcile to merchandise amount.
    # Only checked when the header reports a non-zero merchandise amount -- some
    # older contracts carry $0 header totals with the value in the line items, so
    # there is nothing to reconcile against (flag stays NULL, not a failure).
    con.execute("ALTER TABLE silver_document ADD COLUMN dq_line_reconciles INTEGER")
    con.execute(
        """UPDATE silver_document SET dq_line_reconciles = CASE
             WHEN is_enriched = 0 OR COALESCE(merchandise_amount, 0) = 0 THEN NULL
             WHEN ABS(COALESCE((SELECT SUM(line_amount) FROM silver_line l
                     WHERE l.business_unit = silver_document.business_unit
                       AND l.purchase_document = silver_document.purchase_document), 0)
                  - merchandise_amount) < 1 THEN 1
             ELSE 0 END"""
    )
    # surrogate key + audit columns (+ CLOB on the long free-text line columns)
    _finalize(con, "silver_document", "document_sk", batch, ts)
    _finalize(
        con,
        "silver_line",
        "line_sk",
        batch,
        ts,
        clob_cols=("item_description", "unspsc_description"),
    )
    _finalize(con, "silver_associated_po", "po_sk", batch, ts)
    return {
        t: _count(con, t)
        for t in ("silver_department", "silver_document", "silver_line", "silver_associated_po")
    }


# --------------------------------------------------------------------------- #
# Gold: Kimball star schema (surrogate-keyed dims + facts + marts)
# --------------------------------------------------------------------------- #
def build_gold(con: sqlite3.Connection, batch: str, ts: str) -> dict:
    # -- dim_date (spine over the observed date range, plus an Unknown member)
    con.execute("DROP TABLE IF EXISTS dim_date")
    con.execute(
        "CREATE TABLE dim_date (date_key INTEGER PRIMARY KEY, full_date TEXT, year INTEGER, "
        "quarter INTEGER, month INTEGER, month_name TEXT, day INTEGER, day_of_week TEXT)"
    )
    con.execute("INSERT INTO dim_date VALUES (0, NULL, NULL, NULL, NULL, 'Unknown', NULL, NULL)")
    bounds = con.execute(
        "SELECT MIN(d), MAX(d) FROM ("
        "  SELECT start_date d FROM silver_document WHERE start_date LIKE '____-__-__'"
        "  UNION SELECT start_date FROM silver_associated_po WHERE start_date LIKE '____-__-__')"
    ).fetchone()
    if bounds and bounds[0]:
        con.execute(
            """
            WITH RECURSIVE dates(dt) AS (
              SELECT ? UNION ALL SELECT date(dt, '+1 day') FROM dates WHERE dt < ?
            )
            INSERT INTO dim_date
            SELECT CAST(strftime('%Y%m%d', dt) AS INTEGER), dt,
                   CAST(strftime('%Y', dt) AS INTEGER),
                   (CAST(strftime('%m', dt) AS INTEGER) + 2) / 3,
                   CAST(strftime('%m', dt) AS INTEGER),
                   CASE strftime('%m', dt)
                     WHEN '01' THEN 'Jan' WHEN '02' THEN 'Feb' WHEN '03' THEN 'Mar'
                     WHEN '04' THEN 'Apr' WHEN '05' THEN 'May' WHEN '06' THEN 'Jun'
                     WHEN '07' THEN 'Jul' WHEN '08' THEN 'Aug' WHEN '09' THEN 'Sep'
                     WHEN '10' THEN 'Oct' WHEN '11' THEN 'Nov' ELSE 'Dec' END,
                   CAST(strftime('%d', dt) AS INTEGER),
                   CASE strftime('%w', dt)
                     WHEN '0' THEN 'Sun' WHEN '1' THEN 'Mon' WHEN '2' THEN 'Tue'
                     WHEN '3' THEN 'Wed' WHEN '4' THEN 'Thu' WHEN '5' THEN 'Fri' ELSE 'Sat' END
            FROM dates
            """,
            (bounds[0], bounds[1]),
        )

    # -- conformed dimensions with surrogate keys
    _build_dim(
        con,
        "dim_department",
        "dept_key",
        ["business_unit", "department_name"],
        "SELECT business_unit, department_name FROM silver_department",
        ts,
    )
    _build_dim(
        con,
        "dim_supplier",
        "supplier_key",
        ["supplier_id", "supplier_name"],
        "SELECT DISTINCT supplier_id, supplier_name FROM silver_document",
        ts,
    )
    _apply_supplier_master(con)  # canonical entity + parent attributes on dim_supplier
    _build_dim(
        con,
        "dim_buyer",
        "buyer_key",
        ["buyer_name", "buyer_email"],
        "SELECT DISTINCT buyer_name, buyer_email FROM silver_document",
        ts,
    )
    _build_dim(
        con,
        "dim_acquisition",
        "acq_key",
        ["acquisition_type", "acquisition_sub_type", "acquisition_method", "competitive_flag"],
        "SELECT DISTINCT acquisition_type, acquisition_sub_type, acquisition_method, "
        "competitive_flag FROM silver_document",
        ts,
    )
    _build_dim(
        con,
        "dim_unspsc",
        "unspsc_key",
        ["unspsc", "unspsc_description"],
        "SELECT DISTINCT unspsc, unspsc_description FROM silver_line",
        ts,
        clob_cols=("unspsc_description",),
    )

    # -- fact_document (grain: one purchase document)
    con.execute("DROP TABLE IF EXISTS fact_document")
    con.execute(
        """
        CREATE TABLE fact_document AS
        SELECT
          s.business_unit || '|' || s.purchase_document AS document_bk,   -- degenerate key
          s.purchase_document, s.bill_code, s.status, s.version, s.is_enriched,
          s.has_associated_pos,
          COALESCE(dt.date_key, 0)  AS start_date_key,
          dep.dept_key, sup.supplier_key, buy.buyer_key, acq.acq_key,
          s.merchandise_amount, s.freight_tax_misc, s.grand_total,
          s.line_count, s.associated_po_count, s.dq_line_reconciles
        FROM silver_document s
        LEFT JOIN dim_date dt  ON dt.full_date = s.start_date
        LEFT JOIN dim_department dep ON dep.business_unit = s.business_unit
        LEFT JOIN dim_supplier sup ON sup.supplier_id = s.supplier_id AND sup.supplier_name = s.supplier_name
        LEFT JOIN dim_buyer buy ON buy.buyer_name = s.buyer_name AND buy.buyer_email = s.buyer_email
        LEFT JOIN dim_acquisition acq ON acq.acquisition_type = s.acquisition_type
             AND acq.acquisition_sub_type = s.acquisition_sub_type
             AND acq.acquisition_method = s.acquisition_method
             AND acq.competitive_flag = s.competitive_flag
        """
    )

    # -- fact_line (grain: one document line item; doc-level dims inherited from parent)
    con.execute("DROP TABLE IF EXISTS fact_line")
    con.execute(
        """
        CREATE TABLE fact_line AS
        SELECT
          l.business_unit || '|' || l.purchase_document AS document_bk,
          l.purchase_document, l.line_number, l.line_status,
          l.item_description,   -- degenerate attribute: free-text line description
          COALESCE(dt.date_key, 0) AS start_date_key,
          dep.dept_key, sup.supplier_key, buy.buyer_key, acq.acq_key, uns.unspsc_key,
          l.quantity, l.unit_price, l.line_amount
        FROM silver_line l
        JOIN silver_document s ON s.business_unit = l.business_unit
             AND s.purchase_document = l.purchase_document
        LEFT JOIN dim_date dt  ON dt.full_date = s.start_date
        LEFT JOIN dim_department dep ON dep.business_unit = s.business_unit
        LEFT JOIN dim_supplier sup ON sup.supplier_id = s.supplier_id AND sup.supplier_name = s.supplier_name
        LEFT JOIN dim_buyer buy ON buy.buyer_name = s.buyer_name AND buy.buyer_email = s.buyer_email
        LEFT JOIN dim_acquisition acq ON acq.acquisition_type = s.acquisition_type
             AND acq.acquisition_sub_type = s.acquisition_sub_type
             AND acq.acquisition_method = s.acquisition_method
             AND acq.competitive_flag = s.competitive_flag
        LEFT JOIN dim_unspsc uns ON uns.unspsc = l.unspsc AND uns.unspsc_description = l.unspsc_description
        """
    )

    # -- fact_associated_po (grain: one associated PO transaction)
    con.execute("DROP TABLE IF EXISTS fact_associated_po")
    con.execute(
        """
        CREATE TABLE fact_associated_po AS
        SELECT
          a.business_unit || '|' || a.purchase_document AS document_bk,
          a.purchase_document, a.po_id, a.po_status,
          COALESCE(dt.date_key, 0) AS start_date_key,
          dep.dept_key,
          a.po_total
        FROM silver_associated_po a
        LEFT JOIN dim_date dt ON dt.full_date = a.start_date
        LEFT JOIN dim_department dep ON dep.business_unit = a.business_unit
        """
    )

    # surrogate key + audit columns on the facts (CLOB on the free-text line desc)
    _finalize(con, "fact_document", "document_sk", batch, ts)
    _finalize(con, "fact_line", "line_sk", batch, ts, clob_cols=("item_description",))
    _finalize(con, "fact_associated_po", "po_sk", batch, ts)
    _abbreviate_gold(con, load_abbreviations())  # abbreviate physical cols + build lv_ views
    _build_marts(con)
    return {
        t: _count(con, t)
        for t in (
            "dim_date",
            "dim_department",
            "dim_supplier",
            "dim_buyer",
            "dim_acquisition",
            "dim_unspsc",
            "fact_document",
            "fact_line",
            "fact_associated_po",
        )
    }


def _apply_supplier_master(con, master: dict | None = None) -> None:
    """Add canonical-entity attributes to dim_supplier from the curated crosswalk.

    Every supplier defaults to being its own canonical entity; the crosswalk
    (references/supplier_master.csv) remaps duplicate registrations of one vendor
    to a shared canonical_id/canonical_name and tags corporate parents. The parent
    is then propagated to every registration of the same canonical entity so any
    grouping by canonical_id sees it.
    """
    master = supplier_master.load_master() if master is None else master
    for col in ("canonical_id", "canonical_name", "parent_name"):
        con.execute(f"ALTER TABLE dim_supplier ADD COLUMN {col} TEXT")  # noqa: S608 - constants
    con.execute(
        "UPDATE dim_supplier SET canonical_id = supplier_id, canonical_name = supplier_name"
    )
    if not master:
        return
    con.execute(
        "CREATE TEMP TABLE _xref (supplier_id TEXT PRIMARY KEY, canonical_id TEXT, "
        "canonical_name TEXT, parent_name TEXT)"
    )
    con.executemany(
        "INSERT OR REPLACE INTO _xref VALUES (?, ?, ?, ?)",
        [
            (sid, v["canonical_id"], v["canonical_name"] or None, v["parent_name"])
            for sid, v in master.items()
        ],
    )
    # Remap the registrations named in the crosswalk (values bound; names literal).
    con.execute(
        "UPDATE dim_supplier SET "
        "  canonical_id = COALESCE("
        "    (SELECT x.canonical_id FROM _xref x WHERE x.supplier_id = dim_supplier.supplier_id),"
        "    canonical_id), "
        "  canonical_name = COALESCE("
        "    (SELECT x.canonical_name FROM _xref x WHERE x.supplier_id = dim_supplier.supplier_id),"
        "    canonical_name), "
        "  parent_name = "
        "    (SELECT x.parent_name FROM _xref x WHERE x.supplier_id = dim_supplier.supplier_id) "
        "WHERE supplier_id IN (SELECT supplier_id FROM _xref)"
    )
    # Propagate a parent to every sibling registration of the same canonical entity.
    con.execute(
        "UPDATE dim_supplier SET parent_name = "
        "  (SELECT MAX(d2.parent_name) FROM dim_supplier d2 "
        "   WHERE d2.canonical_id = dim_supplier.canonical_id) "
        "WHERE parent_name IS NULL"
    )
    con.execute("DROP TABLE _xref")


def _build_dim(con, table, key, natural_cols, distinct_sql, ts, clob_cols=()):
    con.execute(f"DROP TABLE IF EXISTS {table}")
    cols = ", ".join(natural_cols)
    # Explicit typed DDL (not CREATE AS SELECT) so the surrogate key is a real
    # INTEGER PRIMARY KEY and long-text columns can be declared CLOB.
    coldefs = ", ".join(f'{c} {"CLOB" if c in clob_cols else "TEXT"}' for c in natural_cols)
    con.execute(
        f"CREATE TABLE {table} ({key} INTEGER PRIMARY KEY, {coldefs}, "  # noqa: S608 - constants
        "dw_loaded_at TEXT)"
    )
    con.execute(
        f"INSERT INTO {table} ({key}, {cols}, dw_loaded_at) "  # noqa: S608 - internal constants
        f"SELECT ROW_NUMBER() OVER (ORDER BY {cols}), {cols}, ? FROM ({distinct_sql})",
        (ts,),
    )
    con.execute(f"CREATE UNIQUE INDEX ix_{table} ON {table}({cols})")  # noqa: S608


def _finalize(con, table, sk, batch, ts, clob_cols=()):
    """Rebuild a full-refresh table with a surrogate PK + audit + CLOB long-text.

    Adds an INTEGER PRIMARY KEY `sk` (auto-assigned), `dw_batch_id`/`dw_loaded_at`
    audit columns, and re-declares any `clob_cols` as CLOB (TEXT affinity in SQLite;
    portable to Oracle/Postgres). Every other column keeps its existing declared
    type, so numeric affinity is preserved. Call before the abbreviation pass.
    """
    info = con.execute(f'PRAGMA table_info("{table}")').fetchall()  # cid,name,type,notnull,dflt,pk
    names = [r[1] for r in info]
    defs = [f"{sk} INTEGER PRIMARY KEY"]
    for _cid, name, typ, *_rest in info:
        col_type = "CLOB" if name in clob_cols else typ
        defs.append(f'"{name}" {col_type}'.rstrip())
    defs += ["dw_batch_id TEXT", "dw_loaded_at TEXT"]
    collist = ", ".join(f'"{n}"' for n in names)
    con.execute(f"DROP TABLE IF EXISTS {table}__new")
    con.execute(f"CREATE TABLE {table}__new ({', '.join(defs)})")  # noqa: S608 - internal constants
    con.execute(
        f"INSERT INTO {table}__new ({collist}, dw_batch_id, dw_loaded_at) "  # noqa: S608
        f"SELECT {collist}, ?, ? FROM {table}",
        (batch, ts),
    )
    con.execute(f"DROP TABLE {table}")
    con.execute(f"ALTER TABLE {table}__new RENAME TO {table}")


# Physical gold tables whose columns are abbreviated. Marts and DQ read them via
# the friendly-named lv_ views (see _to_logical_views), so their SQL stays logical.
_GOLD_TABLES = (
    "dim_date",
    "dim_department",
    "dim_supplier",
    "dim_buyer",
    "dim_acquisition",
    "dim_unspsc",
    "fact_document",
    "fact_line",
    "fact_associated_po",
)


def _to_logical_views(sql: str) -> str:
    """Point a query at the friendly lv_ views instead of the abbreviated tables.

    Replaces each gold table name with its lv_ alias as a whole word. The table
    names are distinctive tokens (columns never share them), so this is safe and
    lets marts / DQ keep referencing logical column names.
    """
    for t in _GOLD_TABLES:
        sql = re.sub(rf"\b{t}\b", f"lv_{t}", sql)
    return sql


def _abbreviate_gold(con, abbr: dict[str, str]) -> None:
    """Abbreviate physical columns of the gold dim_/fact_ tables per the dictionary.

    Renames each column to its standardized form, then (re)creates a friendly-named
    view `lv_<table>` that aliases the abbreviated columns back to their logical
    names for the marts / DQ to read, and records the full mapping in
    `gold_data_dictionary`. Dependent views are dropped first so the renames (which
    SQLite would otherwise reject for breaking a view) have nothing to break.
    """
    for (view,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='view' "
        "AND (name LIKE 'gold_%' OR name LIKE 'lv_%')"
    ).fetchall():
        con.execute(f"DROP VIEW IF EXISTS {view}")  # noqa: S608 - internal view names
    con.execute("DROP TABLE IF EXISTS gold_data_dictionary")
    con.execute(
        "CREATE TABLE gold_data_dictionary (table_name TEXT, logical_name TEXT, physical_name TEXT)"
    )
    dd_rows = []
    for t in _GOLD_TABLES:
        logical_cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        physical = {}
        for c in logical_cols:
            p = abbreviate(c, abbr)
            if p != c and p in physical.values():
                raise ValueError(f"abbreviation collision in {t}: '{c}' and another map to '{p}'")
            if p != c:
                con.execute(f'ALTER TABLE {t} RENAME COLUMN "{c}" TO "{p}"')  # noqa: S608
            physical[c] = p
            dd_rows.append((t, c, p))
        select = ", ".join(f'"{physical[c]}" AS "{c}"' for c in logical_cols)
        con.execute(f"DROP VIEW IF EXISTS lv_{t}")
        con.execute(f"CREATE VIEW lv_{t} AS SELECT {select} FROM {t}")  # noqa: S608
    con.executemany("INSERT INTO gold_data_dictionary VALUES (?, ?, ?)", dd_rows)


def _build_marts(con):
    marts = {
        "gold_supplier_spend": """
            SELECT sup.supplier_id, sup.supplier_name,
                   COUNT(*) AS document_count, SUM(f.grand_total) AS total_value
            FROM fact_document f JOIN dim_supplier sup ON sup.supplier_key = f.supplier_key
            GROUP BY sup.supplier_id, sup.supplier_name""",
        "gold_monthly_spend": """
            SELECT d.year, d.month, d.month_name,
                   COUNT(*) AS document_count, SUM(f.grand_total) AS total_value
            FROM fact_document f JOIN dim_date d ON d.date_key = f.start_date_key
            GROUP BY d.year, d.month, d.month_name""",
        "gold_acquisition_spend": """
            SELECT a.acquisition_method, a.competitive_flag,
                   COUNT(*) AS document_count, SUM(f.grand_total) AS total_value
            FROM fact_document f JOIN dim_acquisition a ON a.acq_key = f.acq_key
            GROUP BY a.acquisition_method, a.competitive_flag""",
        "gold_unspsc_spend": """
            SELECT u.unspsc, u.unspsc_description,
                   COUNT(*) AS line_count, SUM(f.line_amount) AS total_value
            FROM fact_line f JOIN dim_unspsc u ON u.unspsc_key = f.unspsc_key
            GROUP BY u.unspsc, u.unspsc_description""",
        "gold_contract_vs_standalone": """
            SELECT CASE WHEN has_associated_pos = 1 THEN 'contract (has POs)'
                        ELSE 'standalone' END AS document_type,
                   COUNT(*) AS document_count, SUM(grand_total) AS total_value,
                   ROUND(AVG(grand_total), 0) AS avg_value
            FROM fact_document GROUP BY document_type""",
        # -- competitive intelligence --------------------------------------- #
        # Vendor scorecard: volume, reach, and how "captured" the awards are.
        "gold_supplier_profile": """
            SELECT s.supplier_id, s.supplier_name,
                   COUNT(*) AS award_count,
                   ROUND(SUM(f.grand_total), 0) AS total_value,
                   ROUND(AVG(f.grand_total), 0) AS avg_award,
                   COUNT(DISTINCT f.dept_key) AS department_count,
                   MIN(dd.full_date) AS first_award, MAX(dd.full_date) AS last_award,
                   SUM(f.has_associated_pos) AS contract_count,
                   ROUND(100.0 * SUM(CASE WHEN a.competitive_flag = 'Non-Competitive'
                         THEN f.grand_total ELSE 0 END) / NULLIF(SUM(f.grand_total), 0), 1)
                         AS pct_noncompetitive_value
            FROM fact_document f
            JOIN dim_supplier s ON s.supplier_key = f.supplier_key
            JOIN dim_acquisition a ON a.acq_key = f.acq_key
            LEFT JOIN dim_date dd ON dd.date_key = f.start_date_key
            GROUP BY s.supplier_id, s.supplier_name""",
        # Spend rolled up to the canonical vendor (merges duplicate supplier_ids).
        # registration_count > 1 means several SCPRS ids were consolidated here.
        "gold_canonical_supplier_spend": """
            SELECT s.canonical_id, s.canonical_name,
                   COUNT(DISTINCT s.supplier_id) AS registration_count,
                   COUNT(*) AS document_count, ROUND(SUM(f.grand_total), 0) AS total_value
            FROM fact_document f JOIN dim_supplier s ON s.supplier_key = f.supplier_key
            GROUP BY s.canonical_id, s.canonical_name""",
        # Canonical vendor scorecard: deduped metrics + parent + web firmographics.
        "gold_supplier_master": """
            WITH agg AS (
              SELECT s.canonical_id, s.canonical_name, MAX(s.parent_name) AS parent_name,
                     COUNT(DISTINCT s.supplier_id) AS registration_count,
                     COUNT(*) AS award_count, ROUND(SUM(f.grand_total), 0) AS total_value,
                     COUNT(DISTINCT f.dept_key) AS department_count,
                     ROUND(100.0 * SUM(CASE WHEN a.competitive_flag = 'Non-Competitive'
                           THEN f.grand_total ELSE 0 END) / NULLIF(SUM(f.grand_total), 0), 1)
                           AS pct_noncompetitive_value
              FROM fact_document f
              JOIN dim_supplier s ON s.supplier_key = f.supplier_key
              JOIN dim_acquisition a ON a.acq_key = f.acq_key
              GROUP BY s.canonical_id, s.canonical_name)
            SELECT agg.canonical_id, agg.canonical_name, agg.parent_name,
                   agg.registration_count, agg.award_count, agg.total_value,
                   agg.department_count, agg.pct_noncompetitive_value,
                   w.org_type, w.hq_city, w.hq_state, w.sb_dvbe, w.website,
                   w.confidence AS profile_confidence
            FROM agg
            LEFT JOIN bronze_supplier_web w
              ON UPPER(w.supplier_name) = UPPER(agg.canonical_name)""",
        # Supplier share of each department's total spend.
        "gold_supplier_share": """
            WITH v AS (
              SELECT dep.business_unit, f.supplier_key, SUM(f.grand_total) AS val
              FROM fact_document f JOIN dim_department dep ON dep.dept_key = f.dept_key
              GROUP BY dep.business_unit, f.supplier_key)
            SELECT v.business_unit, s.supplier_id, s.supplier_name,
                   ROUND(v.val, 0) AS total_value,
                   ROUND(100.0 * v.val / SUM(v.val) OVER (PARTITION BY v.business_unit), 2)
                         AS share_pct
            FROM v JOIN dim_supplier s ON s.supplier_key = v.supplier_key
            WHERE v.val > 0""",
        # Market concentration (HHI) per department x acquisition type.
        # HHI: <1500 competitive, 1500-2500 moderate, >2500 concentrated.
        "gold_market_concentration": """
            WITH v AS (
              SELECT dep.business_unit, a.acquisition_type, f.supplier_key,
                     SUM(f.grand_total) AS val
              FROM fact_document f
              JOIN dim_department dep ON dep.dept_key = f.dept_key
              JOIN dim_acquisition a ON a.acq_key = f.acq_key
              GROUP BY 1, 2, 3),
            sh AS (
              SELECT business_unit, acquisition_type, val,
                     val / SUM(val) OVER (PARTITION BY business_unit, acquisition_type) AS share
              FROM v WHERE val > 0)
            SELECT business_unit, acquisition_type,
                   COUNT(*) AS supplier_count,
                   ROUND(SUM(share * share) * 10000, 0) AS hhi,
                   ROUND(MAX(share) * 100, 1) AS top_supplier_pct,
                   ROUND(SUM(val), 0) AS market_value
            FROM sh GROUP BY business_unit, acquisition_type""",
        # Unit-price spread per UNSPSC where 2+ suppliers compete (enriched lines).
        "gold_price_benchmark": """
            SELECT u.unspsc, u.unspsc_description,
                   COUNT(DISTINCT f.supplier_key) AS supplier_count, COUNT(*) AS line_count,
                   ROUND(MIN(f.unit_price), 2) AS min_price,
                   ROUND(AVG(f.unit_price), 2) AS avg_price,
                   ROUND(MAX(f.unit_price), 2) AS max_price
            FROM fact_line f JOIN dim_unspsc u ON u.unspsc_key = f.unspsc_key
            WHERE f.unit_price > 0
            GROUP BY u.unspsc, u.unspsc_description
            HAVING COUNT(DISTINCT f.supplier_key) >= 2""",
        # -- supplier category profiles (what each vendor actually supplies) -- #
        # Supplier x UNSPSC footprint with the category's share of the supplier's
        # enriched line spend (needs drill-down lines, so grows with enrichment).
        "gold_supplier_unspsc_profile": """
            WITH sc AS (
              SELECT f.supplier_key, u.unspsc, u.unspsc_description,
                     COUNT(*) AS line_count, SUM(f.line_amount) AS category_value
              FROM fact_line f JOIN dim_unspsc u ON u.unspsc_key = f.unspsc_key
              GROUP BY f.supplier_key, u.unspsc, u.unspsc_description)
            SELECT s.supplier_id, s.supplier_name, sc.unspsc, sc.unspsc_description,
                   sc.line_count, ROUND(sc.category_value, 0) AS category_value,
                   ROUND(100.0 * sc.category_value
                         / NULLIF(SUM(sc.category_value) OVER (PARTITION BY sc.supplier_key), 0), 1)
                         AS pct_of_supplier
            FROM sc JOIN dim_supplier s ON s.supplier_key = sc.supplier_key""",
        # One row per supplier: category breadth, primary category, specialization.
        "gold_supplier_specialization": """
            WITH sc AS (
              SELECT f.supplier_key, u.unspsc_description AS category, SUM(f.line_amount) AS val
              FROM fact_line f JOIN dim_unspsc u ON u.unspsc_key = f.unspsc_key
              GROUP BY f.supplier_key, u.unspsc_description),
            ranked AS (
              SELECT supplier_key, category, val,
                     SUM(val) OVER (PARTITION BY supplier_key) AS total,
                     COUNT(*) OVER (PARTITION BY supplier_key) AS category_count,
                     ROW_NUMBER() OVER (PARTITION BY supplier_key ORDER BY val DESC) AS rn
              FROM sc)
            SELECT s.supplier_id, s.supplier_name, r.category_count,
                   r.category AS primary_category, ROUND(r.total, 0) AS enriched_line_value,
                   ROUND(100.0 * r.val / NULLIF(r.total, 0), 1) AS primary_category_pct
            FROM ranked r JOIN dim_supplier s ON s.supplier_key = r.supplier_key
            WHERE r.rn = 1""",
        # Broad-coverage version from document-level acquisition type (all docs).
        "gold_supplier_acquisition_profile": """
            SELECT s.supplier_id, s.supplier_name, a.acquisition_type,
                   COUNT(*) AS document_count, ROUND(SUM(f.grand_total), 0) AS total_value
            FROM fact_document f
            JOIN dim_supplier s ON s.supplier_key = f.supplier_key
            JOIN dim_acquisition a ON a.acq_key = f.acq_key
            GROUP BY s.supplier_id, s.supplier_name, a.acquisition_type""",
        # Vendor scorecard + web-researched firmographics (with confidence).
        "gold_supplier_enriched": """
            SELECT p.supplier_id, p.supplier_name, p.award_count, p.total_value,
                   p.pct_noncompetitive_value,
                   w.org_type, w.hq_city, w.hq_state, w.sb_dvbe, w.website,
                   w.parent_affiliation, w.description, w.confidence AS profile_confidence
            FROM gold_supplier_profile p
            LEFT JOIN bronze_supplier_web w
              ON UPPER(w.supplier_name) = UPPER(p.supplier_name)""",
        # Denormalized line items: free-text description + category + price + vendor.
        # The item_description is a degenerate attribute on fact_line (79% unique,
        # so kept on the fact rather than forced into a dimension).
        "gold_line_item": """
            SELECT dep.business_unit, s.supplier_id, s.supplier_name,
                   f.purchase_document, f.line_number, f.item_description,
                   u.unspsc, u.unspsc_description AS category,
                   f.quantity, f.unit_price, f.line_amount, f.line_status
            FROM fact_line f
            JOIN dim_supplier s ON s.supplier_key = f.supplier_key
            JOIN dim_department dep ON dep.dept_key = f.dept_key
            JOIN dim_unspsc u ON u.unspsc_key = f.unspsc_key""",
        # -- contract change capture (from the append-only dw_document_history) -- #
        # One row per observed transition of a document: version bump, value change,
        # status change, term extension -- with a human-readable summary.
        "gold_contract_change_log": """
            WITH ordered AS (
              SELECT business_unit, purchase_document, version, grand_total, status,
                     end_date, supplier_name, observed_at,
                     LAG(version) OVER w AS prev_version,
                     LAG(grand_total) OVER w AS prev_grand_total,
                     LAG(status) OVER w AS prev_status,
                     LAG(end_date) OVER w AS prev_end_date
              FROM dw_document_history
              WINDOW w AS (PARTITION BY business_unit, purchase_document
                           ORDER BY CAST(version AS INTEGER), observed_at, rowid))
            SELECT business_unit, purchase_document,
                   prev_version AS from_version, version AS to_version,
                   ROUND(prev_grand_total, 2) AS from_value, ROUND(grand_total, 2) AS to_value,
                   ROUND(grand_total - prev_grand_total, 2) AS value_delta,
                   ROUND(100.0 * (grand_total - prev_grand_total)
                         / NULLIF(prev_grand_total, 0), 1) AS value_pct_change,
                   prev_status AS from_status, status AS to_status,
                   prev_end_date AS from_end_date, end_date AS to_end_date, observed_at,
                   TRIM(
                     CASE WHEN version <> prev_version
                          THEN 'v' || prev_version || '->' || version || ' ' ELSE '' END ||
                     CASE WHEN COALESCE(grand_total,0) <> COALESCE(prev_grand_total,0)
                          THEN 'value ' || printf('%+.0f', COALESCE(grand_total,0)
                               - COALESCE(prev_grand_total,0)) || ' ' ELSE '' END ||
                     CASE WHEN COALESCE(status,'') <> COALESCE(prev_status,'')
                          THEN 'status ' || COALESCE(prev_status,'?') || '->'
                               || COALESCE(status,'?') || ' ' ELSE '' END ||
                     CASE WHEN COALESCE(end_date,'') <> COALESCE(prev_end_date,'')
                          THEN CASE WHEN end_date > prev_end_date THEN 'term extended'
                                    ELSE 'term changed' END || ' ' ELSE '' END
                   ) AS change_summary
            FROM ordered
            WHERE prev_version IS NOT NULL""",
        # One row per contract: amendment count (= current version), value growth
        # where multiple snapshots were captured, and observation window.
        "gold_contract_amendments": """
            WITH h AS (
              SELECT business_unit, purchase_document, CAST(version AS INTEGER) AS v,
                     grand_total, observed_at,
                     ROW_NUMBER() OVER w_asc AS rn_first,
                     ROW_NUMBER() OVER w_desc AS rn_last
              FROM dw_document_history
              WINDOW w_asc AS (PARTITION BY business_unit, purchase_document
                               ORDER BY CAST(version AS INTEGER), observed_at, rowid),
                     w_desc AS (PARTITION BY business_unit, purchase_document
                                ORDER BY CAST(version AS INTEGER) DESC, observed_at DESC, rowid DESC))
            SELECT business_unit, purchase_document,
                   MAX(v) AS amendment_count,
                   COUNT(*) AS snapshots_captured,
                   ROUND(MAX(CASE WHEN rn_last = 1 THEN grand_total END), 2) AS current_value,
                   ROUND(MAX(CASE WHEN rn_last = 1 THEN grand_total END)
                         - MAX(CASE WHEN rn_first = 1 THEN grand_total END), 2) AS value_growth,
                   MIN(observed_at) AS first_observed, MAX(observed_at) AS last_observed
            FROM h
            GROUP BY business_unit, purchase_document
            HAVING MAX(v) > 0 OR COUNT(*) > 1""",
    }
    for name, sql in marts.items():
        con.execute(f"DROP VIEW IF EXISTS {name}")
        con.execute(f"CREATE VIEW {name} AS {_to_logical_views(sql)}")
    # Guard: every gold view must be queryable (catches a missed column reference).
    for (view,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'gold_%'"
    ).fetchall():
        con.execute(f"SELECT * FROM {view} LIMIT 0")  # noqa: S608 - internal view names


# --------------------------------------------------------------------------- #
# Data quality + batch control
# --------------------------------------------------------------------------- #
def _ensure_control(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS dw_batch (batch_id TEXT PRIMARY KEY, started_at TEXT, "
        "finished_at TEXT, status TEXT, row_counts TEXT)"
    )
    # Self-heal a legacy dw_dq_results created before the severity column existed:
    # its column layout differs, so the positional-era inserts would misalign. Drop
    # the stale table (audit history only) so it is recreated with the current schema.
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dw_dq_results'"
    ).fetchone() and "severity" not in {
        r[1] for r in con.execute("PRAGMA table_info(dw_dq_results)")
    }:
        con.execute("DROP TABLE dw_dq_results")
    con.execute(
        "CREATE TABLE IF NOT EXISTS dw_dq_results (batch_id TEXT, check_name TEXT, scope TEXT, "
        "severity TEXT, failed_count INTEGER, passed INTEGER, run_at TEXT)"
    )
    # Append-only snapshot history of each document's tracked attributes, so
    # amendments and value/status/term changes are captured over time. Unlike the
    # full-refresh bronze/silver/gold, this table is never dropped — it accumulates.
    # Migrate a legacy copy (created before history_sk) by copying its rows across.
    migrate_history = bool(
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dw_document_history'"
        ).fetchone()
    ) and "history_sk" not in {r[1] for r in con.execute("PRAGMA table_info(dw_document_history)")}
    if migrate_history:
        # drop views over the table first, else RENAME rewrites them to the temp name
        for v in ("gold_contract_change_log", "gold_contract_amendments"):
            con.execute(f"DROP VIEW IF EXISTS {v}")
        con.execute("ALTER TABLE dw_document_history RENAME TO dw_document_history__old")
    _HISTORY_COLS = (
        "business_unit, purchase_document, version, grand_total, status, start_date, "
        "end_date, supplier_id, supplier_name, acquisition, content_sig, batch_id, observed_at"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS dw_document_history ("
        "history_sk INTEGER PRIMARY KEY AUTOINCREMENT, "
        "business_unit TEXT, purchase_document TEXT, version INTEGER, grand_total REAL, "
        "status TEXT, start_date TEXT, end_date TEXT, "
        "supplier_id TEXT, supplier_name TEXT, acquisition TEXT, "
        "content_sig TEXT, batch_id TEXT, observed_at TEXT)"
    )
    if migrate_history:
        con.execute(
            f"INSERT INTO dw_document_history ({_HISTORY_COLS}) "  # noqa: S608 - fixed constant list
            f"SELECT {_HISTORY_COLS} FROM dw_document_history__old"
        )
        con.execute("DROP TABLE dw_document_history__old")
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_doc_history ON "
        "dw_document_history(business_unit, purchase_document, version)"
    )


# (name, severity, scope, sql-returning-a-failure-count). severity 'error' gates
# the build; 'warn' is an informational finding (e.g. real negative amounts).
_DQ_CHECKS = [
    (
        "no_null_document_key",
        "error",
        "silver_document",
        "SELECT COUNT(*) FROM silver_document WHERE purchase_document IS NULL OR business_unit IS NULL",
    ),
    (
        "unique_document_grain",
        "error",
        "silver_document",
        "SELECT COUNT(*) FROM (SELECT business_unit, purchase_document FROM silver_document "
        "GROUP BY business_unit, purchase_document HAVING COUNT(*) > 1)",
    ),
    (
        "document_grain_parity",
        "error",
        "distinct bronze docs vs silver_document",
        "SELECT ABS((SELECT COUNT(*) FROM (SELECT DISTINCT business_unit, purchase_document "
        "FROM bronze_purchases)) - (SELECT COUNT(*) FROM silver_document))",
    ),
    (
        "fact_document_dept_fk",
        "error",
        "fact_document",
        "SELECT COUNT(*) FROM fact_document WHERE dept_key IS NULL",
    ),
    (
        "fact_document_supplier_fk",
        "error",
        "fact_document",
        "SELECT COUNT(*) FROM fact_document WHERE supplier_key IS NULL",
    ),
    (
        "fact_document_acq_fk",
        "error",
        "fact_document",
        "SELECT COUNT(*) FROM fact_document WHERE acq_key IS NULL",
    ),
    (
        "fact_line_unspsc_fk",
        "error",
        "fact_line",
        "SELECT COUNT(*) FROM fact_line WHERE unspsc_key IS NULL",
    ),
    (
        "fact_line_document_subset",
        "error",
        "fact_line vs fact_document",
        "SELECT COUNT(*) FROM fact_line WHERE document_bk NOT IN "
        "(SELECT document_bk FROM fact_document)",
    ),
    (
        "line_items_reconcile",
        "warn",
        "silver_document (enriched)",
        "SELECT COUNT(*) FROM silver_document WHERE dq_line_reconciles = 0",
    ),
    (
        "non_negative_grand_total",
        "warn",
        "fact_document (credits/adjustments)",
        "SELECT COUNT(*) FROM fact_document WHERE grand_total < 0",
    ),
]


# Attributes whose change we track over time; their concatenation is the snapshot
# signature, so a new row is appended only when one of them actually changes.
_HISTORY_SIG = (
    "COALESCE(version,'')||'|'||COALESCE(grand_total,'')||'|'||COALESCE(status,'')||'|'||"
    "COALESCE(start_date,'')||'|'||COALESCE(end_date,'')||'|'||COALESCE(supplier_id,'')||'|'||"
    "COALESCE(acquisition_type_sub_type,'')||'|'||COALESCE(acquisition_method,'')"
)


def capture_document_history(con: sqlite3.Connection, batch: str, ts: str) -> int:
    """Append a snapshot of each document/version whose tracked attributes changed.

    Reads the current build's bronze_purchases and inserts into the append-only
    `dw_document_history` only rows whose (document, version, signature) is not
    already recorded. So the first build backfills every version present now
    (giving amendment history for multi-version documents immediately), and later
    builds append a new snapshot whenever a value, status, term, supplier, or
    version changes. Returns the number of snapshots appended. Idempotent.
    """
    _ensure_control(con)
    before = con.execute("SELECT COUNT(*) FROM dw_document_history").fetchone()[0]
    con.execute(
        f"""
        INSERT INTO dw_document_history
        (business_unit, purchase_document, version, grand_total, status, start_date, end_date,
         supplier_id, supplier_name, acquisition, content_sig, batch_id, observed_at)
        SELECT business_unit, purchase_document, version, grand_total, status,
               start_date, end_date, supplier_id, supplier_name,
               TRIM(COALESCE(acquisition_type_sub_type,'')||' / '||
                    COALESCE(acquisition_method,''), ' /') AS acquisition,
               {_HISTORY_SIG} AS content_sig, ? AS batch_id, ? AS observed_at
        FROM (SELECT DISTINCT business_unit, purchase_document, version, grand_total, status,
                     start_date, end_date, supplier_id, supplier_name,
                     acquisition_type_sub_type, acquisition_method
              FROM bronze_purchases) b
        WHERE NOT EXISTS (
          SELECT 1 FROM dw_document_history h
          WHERE h.business_unit = b.business_unit
            AND h.purchase_document = b.purchase_document
            AND h.version = b.version
            AND h.content_sig = {_HISTORY_SIG})
        """,  # noqa: S608 - signature is a fixed internal expression; values are bound
        (batch, ts),
    )
    con.commit()
    return con.execute("SELECT COUNT(*) FROM dw_document_history").fetchone()[0] - before


def run_dq(con: sqlite3.Connection, batch: str, ts: str) -> list[dict]:
    _ensure_control(con)
    results = []
    for name, severity, scope, sql in _DQ_CHECKS:
        try:
            failed = con.execute(_to_logical_views(sql)).fetchone()[0] or 0
        except sqlite3.OperationalError as e:
            failed, scope = -1, f"{scope} (error: {e})"
        passed = 1 if failed == 0 else 0
        con.execute(
            "INSERT INTO dw_dq_results "
            "(batch_id, check_name, scope, severity, failed_count, passed, run_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch, name, scope, severity, failed, passed, ts),
        )
        results.append(
            {
                "check": name,
                "severity": severity,
                "scope": scope,
                "failed": failed,
                "passed": bool(passed),
            }
        )
    con.commit()
    return results


def build_all(
    *,
    wh_path: Path = WAREHOUSE_DB,
    source_path: Path = SOURCE_DB,
    enrichment_db: Path = ENRICHMENT_DB,
    log=print,
) -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    # microseconds keep the batch id unique even for rapid successive rebuilds
    batch = "batch_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    con = _connect(wh_path, source_path)
    try:
        _ensure_control(con)
        con.execute(
            "INSERT INTO dw_batch (batch_id, started_at, status) VALUES (?, ?, 'running')",
            (batch, ts),
        )
        log(f"[{batch}] bronze...")
        counts = build_bronze(con, batch, ts, enrichment_db)
        con.commit()
        appended = capture_document_history(con, batch, ts)
        counts["dw_document_history_appended"] = appended
        log(f"[{batch}] history: +{appended} snapshot(s)")
        log(f"[{batch}] silver...")
        counts |= build_silver(con, batch, ts)
        con.commit()
        log(f"[{batch}] gold...")
        counts |= build_gold(con, batch, ts)
        con.commit()
        log(f"[{batch}] data quality...")
        dq = run_dq(con, batch, ts)
        fin = datetime.now().isoformat(timespec="seconds")
        con.execute(
            "UPDATE dw_batch SET finished_at=?, status=?, row_counts=? WHERE batch_id=?",
            (fin, "ok", json.dumps(counts), batch),
        )
        con.commit()
    finally:
        con.close()
    errors = [d for d in dq if not d["passed"] and d["severity"] == "error"]
    warns = [d for d in dq if not d["passed"] and d["severity"] == "warn"]
    log(f"[{batch}] done. rows={counts}")
    if warns:
        log(f"[{batch}] DQ warnings: {[(d['check'], d['failed']) for d in warns]}")
    if errors:
        log(f"[{batch}] DQ ERRORS: {[(d['check'], d['failed']) for d in errors]}")
    return {"batch": batch, "counts": counts, "dq": dq, "errors": errors}


def _cli() -> None:
    ap = argparse.ArgumentParser(description="SCPRS medallion warehouse (bronze/silver/gold).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="Build all layers from data/scprs.db")
    sub.add_parser("dq", help="Run data-quality checks against the current warehouse")
    sub.add_parser("info", help="Show layer row counts and last batch")
    args = ap.parse_args()

    if args.cmd == "build":
        build_all()
    elif args.cmd == "dq":
        con = _connect()
        try:
            ts = datetime.now().isoformat(timespec="seconds")
            for r in run_dq(con, "adhoc", ts):
                mark = "PASS" if r["passed"] else f"{r['severity'].upper()}({r['failed']})"
                print(f"  [{mark:>10}] {r['check']:<26} {r['scope']}")
        finally:
            con.close()
    elif args.cmd == "info":
        con = _connect()
        try:
            tables = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND (name LIKE 'bronze_%' OR name LIKE 'silver_%' OR name LIKE 'dim_%' "
                    "OR name LIKE 'fact_%' OR name LIKE 'dw_%') ORDER BY name"
                ).fetchall()
            ]
            for t in tables:
                print(f"  {t:<28} {_count(con, t):>10,} rows")
            last = con.execute(
                "SELECT batch_id, finished_at, status FROM dw_batch "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            print("\nlast batch:", last)
        finally:
            con.close()


if __name__ == "__main__":
    _cli()
