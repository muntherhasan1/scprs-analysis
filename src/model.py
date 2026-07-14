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
    df, warnings = scprs.download_range(business_unit, from_date, to_date, log=log)
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
        "document_version",
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
        "document_version",
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
        # idempotent migration: add any columns introduced since the table was created
        existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for c in cols:
            if c not in existing:
                con.execute(
                    f"ALTER TABLE {table} ADD COLUMN {c} "  # noqa: S608 - internal constants
                    f"{'REAL' if c in _REAL_COLUMNS else 'TEXT'}"
                )
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

    Idempotent per (business_unit, document): reloading a document replaces its
    detail rows for that business unit only.
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
                    "document_version": h.get("version"),
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
                    "document_version": h.get("version"),
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
            # Scope the delete to THIS business unit: the detail grain is
            # (business_unit, purchase_document), so a document number shared across
            # two BUs must not have one BU's reload wipe the other's rows.
            q = f"DELETE FROM {table} WHERE business_unit = ? AND purchase_document IN ({ph})"  # noqa: S608 # nosec
            con.execute(q, [business_unit, *loaded])
            frame = pd.DataFrame(rows).reindex(columns=_DETAILS_SCHEMA[table])
            frame.to_sql(table, con, if_exists="append", index=False)
        con.commit()
    finally:
        con.close()
    counts = {"documents": len(det), "lines": len(lines), "pos": len(pos)}
    log(f"Loaded {counts} into {db_path}")
    return counts


def _ensure_progress_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS details_progress ("
        "business_unit TEXT, day TEXT, documents INTEGER, lines INTEGER, pos INTEGER, "
        "completed_at TEXT, PRIMARY KEY (business_unit, day))"
    )


def enrich_details(
    business_unit: str,
    from_date: str,
    to_date: str,
    *,
    db_path: Path = DB_PATH,
    force: bool = False,
    limit: int | None = None,
    newest_first: bool = False,
    acq_type: str | None = None,
    log=print,
) -> dict:
    """Drill into PO Details one active day at a time, resuming across runs.

    Only days that actually have documents (distinct `start_date`s already in
    the `purchases` table) are visited, so build the summary first. Each
    completed day is recorded in `details_progress`; a re-run skips finished
    days. A day that errors is left unrecorded so it retries next run.
    `newest_first` processes the most recent days first (recent data priority).

    `acq_type` is an optional SQL LIKE pattern (e.g. "IT Services%") that narrows
    the run to days having at least one document of that acquisition type. Note
    the drill still loads *every* document on each selected day (the SCPRS search
    grid can't filter by acquisition type); the filter only chooses which days to
    visit. Day completion is recorded per business unit regardless of the filter,
    so a filtered and an unfiltered run share progress consistently.
    """
    con = _connect(db_path)
    # A day qualifies if it has any document of the requested acquisition type.
    # The pattern is a bound parameter (never interpolated), preserving the
    # parameterized-SQL invariant; only the ORDER BY direction is a literal.
    acq_clause = " AND acquisition_type_sub_type LIKE ?" if acq_type is not None else ""
    params = (business_unit, _iso(from_date), _iso(to_date))
    if acq_type is not None:
        params = (*params, acq_type)

    def _active_sql(order: str) -> str:
        # `order` is a literal ("ASC"/"DESC") and `acq_clause` is a fixed
        # constant; the only value bound here is the LIKE pattern (a parameter).
        return (
            "SELECT DISTINCT start_date FROM purchases WHERE business_unit = ? "  # noqa: S608
            "AND start_date BETWEEN ? AND ? AND start_date IS NOT NULL"
            f"{acq_clause} ORDER BY start_date {order}"
        )

    try:
        _ensure_progress_schema(con)
        try:
            active = [
                r[0]
                for r in con.execute(
                    _active_sql("DESC" if newest_first else "ASC"), params
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            active = []
        done = {
            r[0]
            for r in con.execute(
                "SELECT day FROM details_progress WHERE business_unit = ?", (business_unit,)
            ).fetchall()
        }
    finally:
        con.close()

    if not active:
        log("No active days in `purchases` for that BU/range. Build the summary first:")
        log(f"  python -m src.model build {business_unit} {from_date} {to_date}")
        return {"days_total": 0, "days_processed": 0, "days_remaining": 0}

    pending = active if force else [d for d in active if d not in done]
    log(f"{len(active)} active day(s); {len(active) - len(pending)} done; {len(pending)} pending")
    todo = pending[:limit] if limit is not None else pending

    processed = 0
    for iso_day in todo:
        mdy = datetime.strptime(iso_day, "%Y-%m-%d").strftime("%m/%d/%Y")
        log(f"[{processed + 1}/{len(todo)}] {iso_day}")
        try:
            counts = build_details_db(business_unit, mdy, mdy, db_path=db_path, log=lambda *a: None)
        except Exception as e:  # noqa: BLE001 - keep going; unrecorded day retries next run
            log(f"    ERROR: {repr(e)[:120]} (will retry next run)")
            continue
        con = _connect(db_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO details_progress VALUES (?, ?, ?, ?, ?, ?)",
                (
                    business_unit,
                    iso_day,
                    counts["documents"],
                    counts["lines"],
                    counts["pos"],
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            con.commit()
        finally:
            con.close()
        processed += 1
        log(f"    {counts}")

    return {
        "days_total": len(active),
        "days_processed": processed,
        "days_remaining": len(pending) - processed,
    }


def query(sql: str, *, db_path: Path = DB_PATH, params: tuple = ()):
    """Run a read query and return a DataFrame."""
    import pandas as pd

    con = _connect(db_path)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def document(doc_number: str, *, db_path: Path = DB_PATH):
    """Return {header, lines, pos} for a purchase document from the detail tables.

    Reproduces the site's PO Details page from the DB. Matches an exact id or a
    suffix (e.g. "63626"). Returns None if the document has not been enriched
    yet; raises if the id is ambiguous.
    """
    try:
        # A document can be enriched at several versions; show the current one.
        d = query(
            "SELECT * FROM document_details WHERE purchase_document = ? "
            "OR purchase_document LIKE ? ORDER BY CAST(version AS INTEGER) DESC",
            db_path=db_path,
            params=(doc_number, f"%{doc_number}"),
        )
    except Exception:  # noqa: BLE001 - detail tables may not exist yet
        return None
    if d.empty:
        return None
    ids = list(d["purchase_document"].unique())
    if len(ids) > 1:
        raise ValueError(f"'{doc_number}' matches multiple documents: {ids}")
    pd_id = ids[0]
    # Restrict line/PO rows to the current document version (max present; legacy
    # NULL rows collapse to -1, so single-version docs are unaffected).
    lines = query(
        "SELECT DISTINCT line_number, unspsc, quantity, unit_price, line_status, item_description "
        "FROM document_lines WHERE purchase_document = ? "
        "AND CAST(COALESCE(document_version, '-1') AS INTEGER) = "
        "(SELECT MAX(CAST(COALESCE(document_version, '-1') AS INTEGER)) FROM document_lines "
        "WHERE purchase_document = ?) ORDER BY CAST(line_number AS INT)",
        db_path=db_path,
        params=(pd_id, pd_id),
    )
    pos = query(
        "SELECT DISTINCT po_id, buyer, start_date, po_total, po_status "
        "FROM document_pos WHERE purchase_document = ? "
        "AND CAST(COALESCE(document_version, '-1') AS INTEGER) = "
        "(SELECT MAX(CAST(COALESCE(document_version, '-1') AS INTEGER)) FROM document_pos "
        "WHERE purchase_document = ?)",
        db_path=db_path,
        params=(pd_id, pd_id),
    )
    return {"header": d.iloc[0].to_dict(), "lines": lines, "pos": pos}


def _fmt_money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _print_document(doc: dict) -> None:
    """Print a document like the PO Details page: header + two sections."""
    h, lines, pos = doc["header"], doc["lines"], doc["pos"]
    print("FI$Cal SCPRS - PO Details")
    print(f"  Department        : {h['business_unit']}  {h.get('department_name') or ''}")
    print(
        f"  Purchase Document : {h['purchase_document']}   "
        f"Version {h.get('version')}   Bill Code {h.get('bill_code')}"
    )
    print(
        f"  Status            : {h.get('status')}   "
        f"{h.get('acquisition_type')} / {h.get('acquisition_method')}"
    )
    print(f"  Dates             : {h.get('start_date')}  ->  {h.get('end_date')}")
    print(f"  Supplier          : {h.get('supplier_name')}")
    print(f"  Buyer             : {h.get('buyer_name')}  {h.get('buyer_email') or ''}")
    print(
        f"  Amounts           : merchandise {_fmt_money(h.get('merchandise_amount'))}   "
        f"freight/tax/misc {_fmt_money(h.get('freight_tax_misc'))}   "
        f"grand total {_fmt_money(h.get('grand_total'))}"
    )

    print(f"\nPurchase Document Line Item Details ({len(lines)})")
    if not lines.empty:
        disp = lines.copy()
        disp["line_total"] = disp["unit_price"] * disp["quantity"]
        disp["description"] = disp["item_description"].str.slice(0, 44)
        cols = [
            "line_number",
            "unspsc",
            "quantity",
            "unit_price",
            "line_total",
            "line_status",
            "description",
        ]
        print(disp[cols].to_string(index=False))
        lt, merch = disp["line_total"].sum(), (h.get("merchandise_amount") or 0)
        flag = "OK" if abs(lt - merch) < 1 else "MISMATCH"
        print(f"  line total sum {_fmt_money(lt)}  vs merchandise {_fmt_money(merch)}  [{flag}]")

    print(f"\nAssociated Transactions ({len(pos)})")
    print(pos.to_string(index=False) if not pos.empty else "  (none - standalone document)")


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
    en = sub.add_parser("enrich", help="Day-by-day PO Details enrichment with resume")
    en.add_argument("business_unit")
    en.add_argument("from_date")
    en.add_argument("to_date")
    en.add_argument("--limit", type=int, default=None, help="Process at most N days this run")
    en.add_argument("--force", action="store_true", help="Re-process days already recorded")
    en.add_argument("--newest-first", action="store_true", help="Process most recent days first")
    en.add_argument(
        "--acq-type",
        default=None,
        help="Only visit days with a document whose acquisition_type_sub_type matches "
        "this SQL LIKE pattern, e.g. 'IT Services%%' (the drill still loads all docs on a day)",
    )
    dc = sub.add_parser("document", help="Show a document like the PO Details page")
    dc.add_argument("document", help="Purchase document id or suffix, e.g. 63626")
    dc.add_argument("--fetch", action="store_true", help="Drill it now if not yet enriched")
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
    elif args.cmd == "enrich":
        print(
            enrich_details(
                args.business_unit,
                args.from_date,
                args.to_date,
                limit=args.limit,
                force=args.force,
                newest_first=args.newest_first,
                acq_type=args.acq_type,
            )
        )
    elif args.cmd == "document":
        try:
            result = document(args.document)
        except ValueError as e:
            print(e)
            return
        if result is None and args.fetch:
            found = query(
                "SELECT business_unit, start_date FROM purchases "
                "WHERE purchase_document = ? OR purchase_document LIKE ?",
                params=(args.document, f"%{args.document}"),
            )
            if found.empty:
                print("Not in `purchases`; build the summary first or pass the exact id.")
            else:
                row = found.iloc[0]
                day = datetime.strptime(row["start_date"], "%Y-%m-%d").strftime("%m/%d/%Y")
                print(f"Enriching {row['business_unit']} {day} to fetch this document...")
                build_details_db(row["business_unit"], day, day, log=lambda *a: None)
                result = document(args.document)
        if result is None:
            print("Not enriched yet. Run one of:")
            print(f"  python -m src.model document {args.document} --fetch")
            print("  python -m src.model details <BU> <MM/DD/YYYY> <MM/DD/YYYY>")
        else:
            import pandas as pd

            pd.set_option("display.width", 200)
            _print_document(result)
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
