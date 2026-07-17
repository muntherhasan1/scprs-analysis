"""Shared, hardened read-only query surface over the SCPRS gold warehouse.

Single source of truth for the two front ends — the MCP server
(``src.mcp_server``) and the natural-language web app (``src.web_app``) — so the
security-critical query guard lives in exactly one place:

  * the SQLite connection is opened read-only (``?mode=ro``) — a write is
    physically impossible regardless of the SQL that arrives;
  * ``run_select`` accepts a single ``SELECT``/``WITH`` statement only;
  * object names interpolated into ``COUNT``/``PRAGMA`` are checked against the
    live allowlist of ``gold_*``/``lv_*``/``dim_*``/``fact_*`` objects from
    ``sqlite_master`` — never a raw caller string.

``WAREHOUSE_DB`` is env-overridable (``WAREHOUSE_DB``) so a container can point at
its baked-in copy; the default sits next to ``scprs.DATA_DIR`` without importing
the scraping stack.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WAREHOUSE_DB = Path(os.environ.get("WAREHOUSE_DB", str(DATA_DIR / "warehouse.db")))

# A single SELECT or CTE query, nothing else.
SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# Wall-clock cap so a single expensive read (recursive CTE, cartesian join) can't
# peg the CPU on a shared/scale-to-one host — the real DoS vector, reachable via
# the public web app. Interrupts via SQLite's progress handler. 0 disables.
QUERY_TIMEOUT_S = float(os.environ.get("QUERY_TIMEOUT_S", "10"))
# Memory bombs: randomblob/zeroblob can materialize huge cells the row cap can't
# bound. No analytical use — reject outright.
_BLOB_DENY = re.compile(r"\b(randomblob|zeroblob)\s*\(", re.IGNORECASE)

# Curated, human-authored hints surfaced by `list_marts` and `schema_for_llm` so an
# NL/MCP model picks the right mart *and dimension* instead of guessing (e.g. it must
# not answer "top suppliers for IT Services" off UNSPSC — that's a sparse enriched
# slice; "IT Services" is an `acquisition_type`). Internal constants only, never
# caller input. Keyed by object name; objects without an entry report "".
MART_DESCRIPTIONS: dict[str, str] = {
    "gold_document": (
        "Document grain (current version) with acquisition_type, acquisition_method, "
        "canonical_name, department_name, fiscal_year, grand_total — the base for most "
        "spend rollups. For 'top suppliers for <category>' filter acquisition_type and "
        "group by canonical_name."
    ),
    "gold_supplier_acquisition_profile": (
        "Supplier spend by acquisition_type — e.g. 'IT Services', 'IT Goods', "
        "'NON-IT Services', 'NON-IT Goods', 'Telecom'. The mart for "
        "'top suppliers for <category>' questions. Per supplier_id (double-counts "
        "split vendors); for a canonical rollup, sum gold_document by canonical_name."
    ),
    "gold_acquisition_spend": (
        "Spend (document_count + total_value) by acquisition_type + acquisition_method "
        "+ competitive_flag. Has no supplier dimension — for 'top suppliers for "
        "<category>' use gold_supplier_acquisition_profile or gold_document."
    ),
    "gold_supplier_unspsc_profile": (
        "Supplier spend by UNSPSC commodity code, built from ENRICHED line items only "
        "(~9.7k lines) — a sparse slice, NOT full document spend. Not the same as the "
        "'IT Services' acquisition_type; don't use it for acquisition-category questions."
    ),
    "gold_unspsc_spend": (
        "Spend by UNSPSC commodity code — enriched line items only (sparse). A product "
        "taxonomy, distinct from acquisition_type procurement categories."
    ),
    "gold_canonical_supplier_spend": (
        "Total spend per real company (canonical vendor, deduped registrations). "
        "Prefer over per-supplier_id marts, which double-count split vendors."
    ),
    "gold_supplier_master": "One row per canonical vendor (id/name crosswalk); deduped rollups.",
}


def connect() -> sqlite3.Connection:
    """Open warehouse.db read-only. Writes are impossible on this connection."""
    uri = f"file:{WAREHOUSE_DB.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def analytical_objects(con: sqlite3.Connection) -> set[str]:
    """The set of queryable marts / star tables, straight from sqlite_master.

    Used as an allowlist so object names interpolated into PRAGMA / COUNT
    statements are always trusted, never caller-supplied strings.
    """
    rows = con.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'view') "
        "AND (name LIKE 'gold_%' OR name LIKE 'lv_%' "
        "OR name LIKE 'dim_%' OR name LIKE 'fact_%')"
    ).fetchall()
    return {r[0] for r in rows}


def list_marts() -> list[dict]:
    """Analytical marts / star tables with row counts + curated hints (by name)."""
    with connect() as con:
        out = []
        for name in sorted(analytical_objects(con)):
            # name comes from the sqlite_master allowlist, not the caller.
            count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
            out.append(
                {"name": name, "rows": count, "description": MART_DESCRIPTIONS.get(name, "")}
            )
        return out


def describe(name: str) -> dict:
    """Columns (logical names) and row count for one mart or table."""
    with connect() as con:
        allowed = analytical_objects(con) | {"gold_data_dictionary"}
        if name not in allowed:
            return {"error": f"Unknown or non-analytical object: {name!r}"}
        # `name` is allowlisted above before any interpolation.
        cols = [
            {"name": r[1], "type": r[2]}
            for r in con.execute(f'PRAGMA table_info("{name}")')  # noqa: S608
        ]
        count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
        return {"name": name, "row_count": count, "columns": cols}


def distinct_values(table: str, column: str, max_values: int = 100) -> dict:
    """Distinct values of one column with row counts — the vocabulary of a
    low-cardinality categorical (e.g. ``acquisition_type``), so a model can filter
    on real values instead of guessing labels that don't exist.

    Both identifiers are validated first — ``table`` against the live allowlist and
    ``column`` against that table's real columns — so nothing caller-supplied is
    ever interpolated raw. High-cardinality columns are capped: ``truncated`` true
    means the column has more than ``max_values`` distinct values (i.e. it isn't a
    small categorical and a GROUP BY over it isn't meaningful here).
    """
    with connect() as con:
        if table not in analytical_objects(con):
            return {"error": f"Unknown or non-analytical object: {table!r}"}
        # `table` is allowlisted above before this interpolation.
        cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}  # noqa: S608
        if column not in cols:
            return {"error": f"Column {column!r} not found on {table!r}."}
        limit = max(1, min(max_values, 500))
        # Both identifiers validated above (allowlist + real columns); the limit is
        # a bound parameter. Values are returned as data, never interpolated.
        rows = con.execute(
            f'SELECT "{column}" AS value, COUNT(*) AS n FROM "{table}" '  # noqa: S608
            f'GROUP BY "{column}" ORDER BY n DESC LIMIT ?',
            (limit + 1,),
        ).fetchall()
        return {
            "table": table,
            "column": column,
            "values": [{"value": r[0], "count": r[1]} for r in rows[:limit]],
            "truncated": len(rows) > limit,
        }


def data_dictionary() -> list[dict]:
    """Logical↔physical column mapping for the abbreviated gold tables."""
    with connect() as con:
        return [
            {"table": r[0], "logical": r[1], "physical": r[2]}
            for r in con.execute(
                "SELECT table_name, logical_name, physical_name "
                "FROM gold_data_dictionary ORDER BY table_name, logical_name"
            )
        ]


def schema_for_llm() -> str:
    """Compact schema text for an NL→SQL model: one line per friendly view.

    Only the ``gold_*`` marts and ``lv_*`` logical views are listed — they use
    friendly (un-abbreviated) column names, so steering the model here avoids the
    abbreviated physical ``dim_*``/``fact_*`` columns entirely.
    """
    with connect() as con:
        names = sorted(n for n in analytical_objects(con) if n.startswith(("gold_", "lv_")))
        lines = []
        for name in names:
            # name is from the sqlite_master allowlist, not caller input.
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]  # noqa: S608
            count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
            desc = MART_DESCRIPTIONS.get(name)
            hint = f"  -- {desc}" if desc else ""
            lines.append(f"{name} ({count} rows): {', '.join(cols)}{hint}")
    return "\n".join(lines)


def run_select(query: str, max_rows: int = 200) -> dict:
    """Run one read-only ``SELECT``/``WITH`` query and return the rows.

    Only a single read-only statement is permitted; the connection cannot write.
    Results are capped at ``max_rows`` (1–1000); ``truncated`` flags the cap.
    """
    stripped = query.strip().rstrip(";").strip()
    if not SELECT_ONLY.match(stripped):
        return {"error": "Only a single SELECT/WITH query is permitted."}
    if ";" in stripped:
        return {"error": "Multiple statements are not allowed."}
    if _BLOB_DENY.search(stripped):
        return {"error": "randomblob/zeroblob are not permitted."}
    limit = max(1, min(max_rows, 1000))
    try:
        with connect() as con:
            if QUERY_TIMEOUT_S > 0:
                # Interrupt (raises OperationalError) once past the deadline; the
                # handler is polled every ~10k VM ops during execute and fetch.
                deadline = time.monotonic() + QUERY_TIMEOUT_S
                con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            cur = con.execute(stripped)  # read-only connection; writes raise
            rows = cur.fetchmany(limit)
            cols = [d[0] for d in cur.description] if cur.description else []
    except sqlite3.Error as exc:
        msg = "Query exceeded the time limit." if "interrupt" in str(exc).lower() else str(exc)
        return {"error": msg}
    return {
        "columns": cols,
        "rows": [dict(r) for r in rows],
        "row_count": len(rows),
        "truncated": len(rows) == limit,
    }
