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
    q = sub.add_parser("query", help="Run a SQL query against the model")
    q.add_argument("sql")
    sub.add_parser("info", help="Show schema and a data summary")
    args = ap.parse_args()

    if args.cmd == "build":
        n, warnings = build_db(args.business_unit, args.from_date, args.to_date)
        for w in warnings:
            print("WARNING:", w)
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
