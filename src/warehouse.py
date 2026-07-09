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
import sqlite3
from datetime import datetime
from pathlib import Path

from . import scprs

SOURCE_DB = scprs.DATA_DIR / "scprs.db"
WAREHOUSE_DB = scprs.DATA_DIR / "warehouse.db"
DEPARTMENTS_CSV = Path(__file__).resolve().parent.parent / "references" / "departments.csv"

_BRONZE_SOURCES = {
    "bronze_purchases": "purchases",
    "bronze_document_details": "document_details",
    "bronze_document_lines": "document_lines",
    "bronze_document_pos": "document_pos",
}


def _connect(wh_path: Path = WAREHOUSE_DB, source_path: Path = SOURCE_DB) -> sqlite3.Connection:
    con = sqlite3.connect(wh_path)
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
def build_bronze(con: sqlite3.Connection, batch: str, ts: str) -> dict:
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
def build_silver(con: sqlite3.Connection, ts: str) -> dict:
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
    # attach line/PO counts and the line-reconciliation DQ flag
    for col, tbl, have in (
        ("line_count", "bronze_document_lines", have_lines),
        ("associated_po_count", "bronze_document_pos", have_pos),
    ):
        con.execute(f"ALTER TABLE silver_document ADD COLUMN {col} INTEGER DEFAULT 0")
        if have:
            con.execute(
                f"""UPDATE silver_document SET {col} = (
                        SELECT COUNT(*) FROM {tbl} t
                        WHERE t.business_unit = silver_document.business_unit
                          AND t.purchase_document = silver_document.purchase_document)"""
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
    else:
        con.execute("CREATE TABLE silver_line (business_unit TEXT, purchase_document TEXT)")

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
    else:
        con.execute(
            "CREATE TABLE silver_associated_po (business_unit TEXT, purchase_document TEXT)"
        )

    # data-quality flag: enriched line items should reconcile to merchandise amount
    con.execute("ALTER TABLE silver_document ADD COLUMN dq_line_reconciles INTEGER")
    con.execute(
        """UPDATE silver_document SET dq_line_reconciles = CASE
             WHEN is_enriched = 0 OR merchandise_amount IS NULL THEN NULL
             WHEN ABS(COALESCE((SELECT SUM(line_amount) FROM silver_line l
                     WHERE l.business_unit = silver_document.business_unit
                       AND l.purchase_document = silver_document.purchase_document), 0)
                  - merchandise_amount) < 1 THEN 1
             ELSE 0 END"""
    )
    return {
        t: _count(con, t)
        for t in ("silver_department", "silver_document", "silver_line", "silver_associated_po")
    }


# --------------------------------------------------------------------------- #
# Gold: Kimball star schema (surrogate-keyed dims + facts + marts)
# --------------------------------------------------------------------------- #
def build_gold(con: sqlite3.Connection, ts: str) -> dict:
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


def _build_dim(con, table, key, natural_cols, distinct_sql, ts):
    con.execute(f"DROP TABLE IF EXISTS {table}")
    cols = ", ".join(natural_cols)
    con.execute(
        f"CREATE TABLE {table} AS "  # noqa: S608 - table/column names are internal constants
        f"SELECT ROW_NUMBER() OVER (ORDER BY {cols}) AS {key}, {cols}, ? AS dw_loaded_at "
        f"FROM ({distinct_sql})",
        (ts,),
    )
    con.execute(f"CREATE UNIQUE INDEX ix_{table} ON {table}({cols})")  # noqa: S608


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
    }
    for name, sql in marts.items():
        con.execute(f"DROP VIEW IF EXISTS {name}")
        con.execute(f"CREATE VIEW {name} AS {sql}")


# --------------------------------------------------------------------------- #
# Data quality + batch control
# --------------------------------------------------------------------------- #
def _ensure_control(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS dw_batch (batch_id TEXT PRIMARY KEY, started_at TEXT, "
        "finished_at TEXT, status TEXT, row_counts TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS dw_dq_results (batch_id TEXT, check_name TEXT, scope TEXT, "
        "severity TEXT, failed_count INTEGER, passed INTEGER, run_at TEXT)"
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


def run_dq(con: sqlite3.Connection, batch: str, ts: str) -> list[dict]:
    _ensure_control(con)
    results = []
    for name, severity, scope, sql in _DQ_CHECKS:
        try:
            failed = con.execute(sql).fetchone()[0] or 0
        except sqlite3.OperationalError as e:
            failed, scope = -1, f"{scope} (error: {e})"
        passed = 1 if failed == 0 else 0
        con.execute(
            "INSERT INTO dw_dq_results VALUES (?, ?, ?, ?, ?, ?, ?)",
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


def build_all(*, wh_path: Path = WAREHOUSE_DB, source_path: Path = SOURCE_DB, log=print) -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    batch = "batch_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    con = _connect(wh_path, source_path)
    try:
        _ensure_control(con)
        con.execute(
            "INSERT INTO dw_batch (batch_id, started_at, status) VALUES (?, ?, 'running')",
            (batch, ts),
        )
        log(f"[{batch}] bronze...")
        counts = build_bronze(con, batch, ts)
        con.commit()
        log(f"[{batch}] silver...")
        counts |= build_silver(con, ts)
        con.commit()
        log(f"[{batch}] gold...")
        counts |= build_gold(con, ts)
        con.commit()
        log(f"[{batch}] data quality...")
        dq = run_dq(con, batch, ts)
        fin = datetime.now().isoformat(timespec="seconds")
        import json

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
