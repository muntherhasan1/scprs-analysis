"""Local SQLite data model for SCPRS purchase data.

Builds a queryable `purchases` table (one row per purchase document) from the
SCPRS summary extract, with sensible indexes and a couple of rollup views.
Supports multiple business units in one database; re-running a business unit
refreshes just that unit's rows.

CLI:
    # Build / refresh the model for one business unit + date range:
    python -m src.model build 8660 01/01/2016 07/08/2026

    # Run an ad-hoc query:
    python -m src.model query "SELECT supplier_name, COUNT(*) n, SUM(grand_total) v
                               FROM purchases WHERE business_unit='8660'
                               GROUP BY 1 ORDER BY v DESC LIMIT 10"

    # Show schema + a data summary:
    python -m src.model info
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import scprs

DB_PATH = scprs.DATA_DIR / "scprs.db"

# Column order for the purchases table (snake_case of the summary extract).
_PURCHASE_COLUMNS = [
    "business_unit",
    "department",
    "department_name",
    "purchase_document",
    "associated_pos",
    "first_item_title",
    "start_date",
    "end_date",
    "grand_total",
    "supplier_id",
    "supplier_name",
    "certification_type",
    "acquisition_type_sub_type",
    "acquisition_method",
    "lpa_contract_id",
    "buyer_name",
    "buyer_email",
    "status",
    "version",
]

_VIEWS = {
    "v_supplier_totals": """
        SELECT business_unit, supplier_id, supplier_name,
               COUNT(*) AS document_count, SUM(grand_total) AS total_value
        FROM purchases GROUP BY business_unit, supplier_id, supplier_name
    """,
    "v_method_totals": """
        SELECT business_unit, acquisition_method,
               COUNT(*) AS document_count, SUM(grand_total) AS total_value
        FROM purchases GROUP BY business_unit, acquisition_method
    """,
    "v_monthly_totals": """
        SELECT business_unit, substr(start_date, 1, 7) AS month,
               COUNT(*) AS document_count, SUM(grand_total) AS total_value
        FROM purchases GROUP BY business_unit, month
    """,
}


def _snake(col: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(col)).strip("_").lower()
    return re.sub(r"_+", "_", s)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def _ensure_schema(con: sqlite3.Connection) -> None:
    cols = ",\n  ".join(
        f"{c} {'REAL' if c == 'grand_total' else 'INTEGER' if c == 'version' else 'TEXT'}"
        for c in _PURCHASE_COLUMNS
    )
    con.execute(f"CREATE TABLE IF NOT EXISTS purchases (\n  {cols}\n)")
    for col in (
        "business_unit",
        "start_date",
        "supplier_name",
        "acquisition_method",
        "supplier_id",
    ):
        con.execute(f"CREATE INDEX IF NOT EXISTS ix_purchases_{col} ON purchases({col})")
    for name, sql in _VIEWS.items():
        con.execute(f"DROP VIEW IF EXISTS {name}")
        con.execute(f"CREATE VIEW {name} AS {sql}")


def build_db(
    business_unit: str,
    from_date: str,
    to_date: str,
    *,
    db_path: Path = DB_PATH,
    log=print,
) -> tuple[int, list[str]]:
    """Fetch (with auto-chunking) and load one business unit into the model."""
    log(f"Collecting SCPRS summary for BU {business_unit} {from_date}..{to_date}")
    df, warnings = scprs.download_range(business_unit, from_date, to_date, kind="summary", log=log)
    if df.empty:
        log("No records for that business unit + date range.")
        return 0, warnings

    df = df.rename(columns={c: _snake(c) for c in df.columns})
    df["business_unit"] = business_unit
    # Dates -> ISO strings so SQLite sorts/filters them lexically.
    for dcol in ("start_date", "end_date"):
        if dcol in df.columns:
            df[dcol] = df[dcol].dt.strftime("%Y-%m-%d")
    df = df.reindex(columns=_PURCHASE_COLUMNS)

    con = _connect(db_path)
    try:
        _ensure_schema(con)
        con.execute("DELETE FROM purchases WHERE business_unit = ?", (business_unit,))
        df.to_sql("purchases", con, if_exists="append", index=False)
        con.commit()
    finally:
        con.close()
    log(f"Loaded {len(df)} rows into {db_path}")
    return len(df), warnings


# --- PO Details drill-down tables (richer than the CSV exports) ---
_DETAILS_SCHEMA = {
    "document_details": [
        "business_unit",
        "purchase_document",
        "department_name",
        "version",
        "bill_code",
        "status",
        "acquisition_type",
        "acquisition_method",
        "start_date",
        "end_date",
        "merchandise_amount",
        "freight_tax_misc",
        "grand_total",
        "lpa_contract_id",
        "supplier_name",
        "buyer_name",
        "buyer_email",
    ],
    "document_lines": [
        "business_unit",
        "purchase_document",
        "line_number",
        "item_id",
        "item_description",
        "unspsc",
        "unspsc_description",
        "unit_of_measure",
        "quantity",
        "unit_price",
        "line_status",
    ],
    "document_pos": [
        "business_unit",
        "purchase_document",
        "po_id",
        "buyer",
        "start_date",
        "po_total",
        "po_status",
    ],
}
_REAL_COLUMNS = {
    "merchandise_amount",
    "freight_tax_misc",
    "grand_total",
    "quantity",
    "unit_price",
    "po_total",
}


def _money(v):
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


def _iso(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v).strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return v


def _ensure_details_schema(con: sqlite3.Connection) -> None:
    for table, cols in _DETAILS_SCHEMA.items():
        defs = ",\n  ".join(f"{c} {'REAL' if c in _REAL_COLUMNS else 'TEXT'}" for c in cols)
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n  {defs}\n)")
        for col in ("business_unit", "purchase_document"):
            con.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_{col} ON {table}({col})")


def build_details_db(
    business_unit: str,
    from_date: str,
    to_date: str,
    *,
    db_path: Path = DB_PATH,
    max_docs: int | None = None,
    log=print,
) -> dict:
    """Drill into each document's PO Details page and load the three detail tables.

    Idempotent per document: reloading a document replaces its detail rows.
    """
    import pandas as pd

    docs = scprs.collect_po_details(business_unit, from_date, to_date, max_docs=max_docs, log=log)
    if not docs:
        return {"documents": 0, "lines": 0, "pos": 0}

    det, lines, pos = [], [], []
    for d in docs:
        h = d["header"]
        doc = h.get("purchase_document") or d["document"]
        det.append(
            {
                "business_unit": business_unit,
                "purchase_document": doc,
                "department_name": h.get("department_name"),
                "version": h.get("version"),
                "bill_code": h.get("bill_code"),
                "status": h.get("status"),
                "acquisition_type": h.get("acquisition_type"),
                "acquisition_method": h.get("acquisition_method"),
                "start_date": _iso(h.get("start_date")),
                "end_date": _iso(h.get("end_date")),
                "merchandise_amount": _money(h.get("merchandise_amount")),
                "freight_tax_misc": _money(h.get("freight_tax_misc")),
                "grand_total": _money(h.get("grand_total")),
                "lpa_contract_id": h.get("lpa_contract_id"),
                "supplier_name": h.get("supplier_name"),
                "buyer_name": h.get("buyer_name"),
                "buyer_email": h.get("buyer_email"),
            }
        )
        for ln in d["lines"]:
            lines.append(
                {
                    "business_unit": business_unit,
                    "purchase_document": doc,
                    "line_number": ln.get("line_number"),
                    "item_id": ln.get("item_id"),
                    "item_description": ln.get("item_description"),
                    "unspsc": ln.get("unspsc"),
                    "unspsc_description": ln.get("unspsc_description"),
                    "unit_of_measure": ln.get("unit_of_measure"),
                    "quantity": _money(ln.get("quantity")),
                    "unit_price": _money(ln.get("unit_price")),
                    "line_status": ln.get("line_status"),
                }
            )
        for po in d["pos"]:
            pos.append(
                {
                    "business_unit": business_unit,
                    "purchase_document": doc,
                    "po_id": po.get("po_id"),
                    "buyer": po.get("buyer"),
                    "start_date": _iso(po.get("start_date")),
                    "po_total": _money(po.get("po_total")),
                    "po_status": po.get("po_status"),
                }
            )

    con = _connect(db_path)
    try:
        _ensure_details_schema(con)
        loaded = [r["purchase_document"] for r in det]
        ph = ",".join("?" * len(loaded))
        for table, rows in (
            ("document_details", det),
            ("document_lines", lines),
            ("document_pos", pos),
        ):
            # `table` is an internal constant (loop above), never user input; values parameterized.
            q = f"DELETE FROM {table} WHERE purchase_document IN ({ph})"  # noqa: S608 # nosec
            con.execute(q, loaded)
            frame = pd.DataFrame(rows).reindex(columns=_DETAILS_SCHEMA[table])
            frame.to_sql(table, con, if_exists="append", index=False)
        con.commit()
    finally:
        con.close()
    counts = {"documents": len(det), "lines": len(lines), "pos": len(pos)}
    log(f"Loaded {counts} into {db_path}")
    return counts


def query(sql: str, *, db_path: Path = DB_PATH, params: tuple = ()):
    """Run a read query and return a DataFrame."""
    import pandas as pd

    con = _connect(db_path)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Build / query the SCPRS data model.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="Fetch a business unit + date range into the model")
    b.add_argument("business_unit")
    b.add_argument("from_date")
    b.add_argument("to_date")
    dt = sub.add_parser("details", help="Drill into each document's PO Details page")
    dt.add_argument("business_unit")
    dt.add_argument("from_date")
    dt.add_argument("to_date")
    dt.add_argument("--max-docs", type=int, default=None)
    q = sub.add_parser("query", help="Run a SQL query against the model")
    q.add_argument("sql")
    sub.add_parser("info", help="Show schema and a data summary")
    args = ap.parse_args()

    if args.cmd == "build":
        n, warnings = build_db(args.business_unit, args.from_date, args.to_date)
        for w in warnings:
            print("WARNING:", w)
    elif args.cmd == "details":
        build_details_db(args.business_unit, args.from_date, args.to_date, max_docs=args.max_docs)
    elif args.cmd == "query":
        import pandas as pd

        pd.set_option("display.max_columns", 40)
        pd.set_option("display.width", 200)
        print(query(args.sql).to_string(index=False))
    elif args.cmd == "info":
        summary = query(
            "SELECT business_unit, COUNT(*) documents, "
            "MIN(start_date) first, MAX(start_date) last, "
            "ROUND(SUM(grand_total),0) total_value "
            "FROM purchases GROUP BY business_unit ORDER BY business_unit"
        )
        print("purchases columns:", ", ".join(_PURCHASE_COLUMNS))
        print("views:", ", ".join(_VIEWS))
        print("\nloaded business units:\n", summary.to_string(index=False))


if __name__ == "__main__":
    _cli()
