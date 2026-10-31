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

import math
import os
import re
import sqlite3
import threading
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
# bound. No analytical use — reject outright. (A regex is a speed bump, not the
# defence: zeroblob/**/(n), "zeroblob"(n) and printf('%.*c', n, 'x') all evade
# it. The hard bounds are MAX_VALUE_BYTES and MAX_RESULT_BYTES below.)
_BLOB_DENY = re.compile(r"\b(randomblob|zeroblob)\s*\(", re.IGNORECASE)
# Largest string/blob SQLite may build on a query connection (SQLITE_LIMIT_LENGTH);
# anything bigger fails inside SQLite ("string or blob too big") before it is
# allocated. The longest real value (a line-item description) is a few KB.
MAX_VALUE_BYTES = 100_000
# Total size of the rows one query may return; fetching stops past it.
MAX_RESULT_BYTES = 5_000_000

# Row counts for the schema listings. COUNT(*) over every gold view costs ~60s on
# the serve DB (some marts are aggregate views), so it is paid once: serve-export
# stores the counts in this table, and anything without it (a local warehouse.db,
# or a serve DB published before the table existed) counts once per process on
# ONE background thread, each object under the query deadline. Callers wait at
# most ROW_COUNT_WAIT_S and otherwise get the counts as unknown (None) — they
# never block a worker for the full count. Not a gold_/lv_ name, so it never
# appears in the allowlist or the schema shown to a model.
ROW_COUNTS_TABLE = "serve_row_counts"
ROW_COUNT_WAIT_S = 2.0
_row_count_cache: dict[tuple, dict[str, int]] = {}
_row_count_pending: set[tuple] = set()
_row_count_cond = threading.Condition()

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
        "group by canonical_name. first_item_title says what a contract is for (on every "
        "document) — search it with LIKE for keyword questions. award_class is the governed "
        "competition class (State Contracting Manual codes): C competitive, L leveraged "
        "agreement (CMAS, statewide contracts), N non-competitive (incl. Fair and Reasonable), "
        "X not procurement (interagency, assistance, leases), U unknown — use it, not "
        "competitive_flag, for competitive or sole-source shares."
    ),
    "gold_procurement": (
        "gold_document minus award_class X (interagency agreements, subvention/local "
        "assistance, leases, reviewed pass-throughs). Use for 'how much does the state buy' "
        "totals, rankings and shares; use gold_document only when asked about everything "
        "registered in SCPRS."
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
    "gold_eprocure_posted_opportunity": (
        "Solicitations currently OPEN for bidding on Cal eProcure (live Posted events) — "
        "THE mart for 'what's open for bid / current solicitations / opportunities'."
    ),
    "gold_eprocure_event": (
        "All Cal eProcure solicitation events, posted + historical, one row per "
        "(department, event). No award/winner data — for spend use gold_document."
    ),
    "gold_supplier_certification": (
        "SB/DVBE certification (Cal eProcure registry) per canonical supplier, with their "
        "SCPRS document_count/total_value — THE mart for 'spend with small-business or "
        "DVBE certified suppliers'. Do NOT use gold_supplier_master.sb_dvbe (sparse "
        "web-research notes, not the registry)."
    ),
    "gold_contract_amendments": (
        "Contract growth via amendments, derived from captured version history — covers "
        "only documents re-drilled at 2+ versions (sparse; deepest for BU 8660). Caveat "
        "coverage when ranking 'contracts that grew the most'."
    ),
}


def connect() -> sqlite3.Connection:
    """Open warehouse.db read-only. Writes are impossible on this connection."""
    uri = f"file:{WAREHOUSE_DB.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    return con


def _set_deadline(con: sqlite3.Connection) -> None:
    """Interrupt (raises OperationalError) once past QUERY_TIMEOUT_S; the handler
    is polled every ~10k VM ops during execute and fetch."""
    if QUERY_TIMEOUT_S > 0:
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)


def _json_safe(value):
    """inf/-inf/nan (e.g. ``SELECT 1e999``) as None: Python's JSON writes them as
    ``Infinity``/``NaN``, which strict JSON clients (Copilot Studio) reject."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _row_bytes(row: sqlite3.Row) -> int:
    return sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in row)


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


def _count_all(con: sqlite3.Connection, bounded: bool = False) -> dict[str, int]:
    """COUNT(*) of every allowlisted object (slow — see ROW_COUNTS_TABLE).

    ``bounded`` puts each COUNT under the query deadline and leaves an object that
    exceeds it out (unknown) — the request-time fallback. serve-export counts
    unbounded so the stored table is complete.
    """
    counts = {}
    for name in sorted(analytical_objects(con)):
        if bounded:
            _set_deadline(con)
        try:
            # Names come from the sqlite_master allowlist, never the caller.
            counts[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
        except sqlite3.OperationalError as exc:
            if not bounded or "interrupt" not in str(exc).lower():
                raise
    return counts


def _count_in_background(key: tuple) -> None:
    try:
        with connect() as con:
            counts = _count_all(con, bounded=True)
    except sqlite3.Error:
        counts = {}  # shown as unknown; a replaced DB file gets a fresh key and retry
    with _row_count_cond:
        _row_count_cache[key] = counts
        _row_count_pending.discard(key)
        _row_count_cond.notify_all()


def record_row_counts(db_path: Path) -> int:
    """Store every allowlisted object's row count in ``ROW_COUNTS_TABLE`` of the
    (writable) DB at ``db_path``. Run by serve-export so the Spaces never count at
    request time. Returns the number of objects counted."""
    con = sqlite3.connect(db_path)
    try:
        counts = _count_all(con)
        con.execute(f"DROP TABLE IF EXISTS {ROW_COUNTS_TABLE}")
        con.execute(f"CREATE TABLE {ROW_COUNTS_TABLE} (name TEXT PRIMARY KEY, row_count INTEGER)")
        con.executemany(f"INSERT INTO {ROW_COUNTS_TABLE} VALUES (?, ?)", counts.items())  # noqa: S608
        con.commit()
    finally:
        con.close()
    return len(counts)


def row_counts(con: sqlite3.Connection) -> dict[str, int]:
    """Row count per allowlisted object: the stored table when serve-export wrote
    one, else counted once per process per DB file (keyed by path, size, mtime so
    a replaced file is recounted) on a background thread. Objects not counted
    (yet, or past the deadline) are simply absent — callers show them as unknown."""
    try:
        st = WAREHOUSE_DB.stat()
        key = (str(WAREHOUSE_DB), st.st_size, st.st_mtime_ns)
    except OSError:
        key = (str(WAREHOUSE_DB), None, None)
    with _row_count_cond:
        if key in _row_count_cache:
            return _row_count_cache[key]
        stored = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ROW_COUNTS_TABLE,)
        ).fetchone()
        if stored:
            counts = dict(con.execute(f"SELECT name, row_count FROM {ROW_COUNTS_TABLE}"))  # noqa: S608
            _row_count_cache[key] = counts
            return counts
        if key not in _row_count_pending:  # one counter per DB file
            _row_count_pending.add(key)
            threading.Thread(
                target=_count_in_background, args=(key,), name="row-counts", daemon=True
            ).start()
        _row_count_cond.wait_for(lambda: key in _row_count_cache, timeout=ROW_COUNT_WAIT_S)
        return _row_count_cache.get(key, {})


def list_marts() -> list[dict]:
    """Analytical marts / star tables with row counts + curated hints (by name)."""
    with connect() as con:
        counts = row_counts(con)
        return [
            {"name": name, "rows": counts.get(name), "description": MART_DESCRIPTIONS.get(name, "")}
            for name in sorted(analytical_objects(con))
        ]


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
        count = row_counts(con).get(name)
        if name == "gold_data_dictionary":  # a small table outside the counted allowlist
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
        _set_deadline(con)  # a GROUP BY over a fact table is caller-triggered work
        # Both identifiers validated above (allowlist + real columns); the limit is
        # a bound parameter. Values are returned as data, never interpolated.
        try:
            rows = con.execute(
                f'SELECT "{column}" AS value, COUNT(*) AS n FROM "{table}" '  # noqa: S608
                f'GROUP BY "{column}" ORDER BY n DESC LIMIT ?',
                (limit + 1,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupt" not in str(exc).lower():
                raise
            return {"error": "Query exceeded the time limit."}
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
        counts = row_counts(con)
        lines = []
        for name in names:
            # name is from the sqlite_master allowlist, not caller input.
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]  # noqa: S608
            count = counts.get(name, "unknown")
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
            _set_deadline(con)
            cur = con.execute(stripped)  # read-only connection; writes raise
            rows, size = [], 0
            while len(rows) < limit and (row := cur.fetchone()) is not None:
                size += _row_bytes(row)
                if size > MAX_RESULT_BYTES:
                    return {"error": "Query result is too large; select fewer or shorter columns."}
                rows.append(row)
            cols = [d[0] for d in cur.description] if cur.description else []
    except sqlite3.Error as exc:
        text = str(exc).lower()
        if "interrupt" in text:
            msg = "Query exceeded the time limit."
        elif "too big" in text:
            msg = "Query builds a value larger than the limit."
        else:
            msg = str(exc)
        return {"error": msg}
    return {
        "columns": cols,
        "rows": [{k: _json_safe(v) for k, v in dict(r).items()} for r in rows],
        "row_count": len(rows),
        "truncated": len(rows) == limit,
    }
