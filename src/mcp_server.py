"""Read-only MCP server exposing the SCPRS gold warehouse to an MCP client.

Consumed by an MCP client such as Claude Code over stdio. It makes **no**
Anthropic API calls — the client (your Claude Code subscription) does the
reasoning; this server only answers structured queries against the local
warehouse. That keeps the whole loop inside the flat subscription, with no
per-token metering.

Safety model — the server is query-only by construction:
  * The SQLite connection is opened in read-only URI mode (`?mode=ro`), so a
    write is physically impossible regardless of what SQL arrives.
  * `run_sql` additionally accepts a single `SELECT`/`WITH` statement only.
  * `describe_table` / row counts interpolate object names, but only after
    checking them against the live allowlist of `gold_*`/`lv_*`/`dim_*`/`fact_*`
    objects from `sqlite_master` — never raw client input.

Run:
    pip install mcp                       # one-time (free, open source)
    python -m src.warehouse build         # ensure data/warehouse.db exists
    python -m src.mcp_server              # starts the stdio server

Then point an MCP client at it (see .mcp.json in the repo root for the Claude
Code wiring).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Same location as scprs.DATA_DIR, derived locally so this server has no
# dependency on the scraping stack (Playwright et al.) just to find the DB.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WAREHOUSE_DB = DATA_DIR / "warehouse.db"

mcp = FastMCP("scprs-warehouse")

# A single SELECT or CTE query, nothing else.
_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def _connect() -> sqlite3.Connection:
    """Open warehouse.db read-only. Writes are impossible on this connection."""
    uri = f"file:{WAREHOUSE_DB.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _analytical_objects(con: sqlite3.Connection) -> set[str]:
    """The set of queryable marts / star tables, straight from sqlite_master.

    Used as an allowlist so object names interpolated into PRAGMA / COUNT
    statements are always trusted, never client-supplied strings.
    """
    rows = con.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'view') "
        "AND (name LIKE 'gold_%' OR name LIKE 'lv_%' "
        "OR name LIKE 'dim_%' OR name LIKE 'fact_%')"
    ).fetchall()
    return {r[0] for r in rows}


@mcp.tool()
def list_marts() -> list[dict]:
    """List the analytical marts and star-schema tables with row counts.

    Prefer the friendly ``gold_*`` mart views for most questions. Canonical
    vendor rollups (one row per real company) are
    ``gold_canonical_supplier_spend`` / ``gold_supplier_master`` — the
    per-supplier_id marts double-count vendors that registered more than once.
    """
    with _connect() as con:
        out = []
        for name in sorted(_analytical_objects(con)):
            # name comes from the sqlite_master allowlist, not the client.
            count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
            out.append({"name": name, "rows": count})
        return out


@mcp.tool()
def describe_table(name: str) -> dict:
    """Return the columns (logical names) and row count for one mart or table."""
    with _connect() as con:
        allowed = _analytical_objects(con) | {"gold_data_dictionary"}
        if name not in allowed:
            return {"error": f"Unknown or non-analytical object: {name!r}"}
        # `name` is allowlisted above before any interpolation.
        cols = [
            {"name": r[1], "type": r[2]}
            for r in con.execute(f'PRAGMA table_info("{name}")')  # noqa: S608
        ]
        count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
        return {"name": name, "row_count": count, "columns": cols}


@mcp.tool()
def data_dictionary() -> list[dict]:
    """Logical↔physical column mapping for the abbreviated gold tables.

    The physical ``dim_*``/``fact_*`` tables use abbreviated columns
    (``grand_total``→``grand_tot``). Query the ``gold_*`` or ``lv_*`` views to
    use logical names directly, or consult this mapping when writing SQL
    straight against the star tables.
    """
    with _connect() as con:
        return [
            {"table": r[0], "logical": r[1], "physical": r[2]}
            for r in con.execute(
                "SELECT table_name, logical_name, physical_name "
                "FROM gold_data_dictionary ORDER BY table_name, logical_name"
            )
        ]


@mcp.tool()
def run_sql(query: str, max_rows: int = 200) -> dict:
    """Run one read-only ``SELECT``/``WITH`` query and return the rows.

    Prefer the ``gold_*``/``lv_*`` views (logical column names). Only a single
    read-only statement is permitted; the connection cannot write. Results are
    capped at ``max_rows`` (1–1000); ``truncated`` flags when the cap was hit.
    """
    stripped = query.strip().rstrip(";").strip()
    if not _SELECT_ONLY.match(stripped):
        return {"error": "Only a single SELECT/WITH query is permitted."}
    if ";" in stripped:
        return {"error": "Multiple statements are not allowed."}
    limit = max(1, min(max_rows, 1000))
    try:
        with _connect() as con:
            cur = con.execute(stripped)  # read-only connection; writes raise
            rows = cur.fetchmany(limit)
            cols = [d[0] for d in cur.description] if cur.description else []
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    return {
        "columns": cols,
        "rows": [dict(r) for r in rows],
        "row_count": len(rows),
        "truncated": len(rows) == limit,
    }


def main() -> None:
    if not WAREHOUSE_DB.exists():
        raise SystemExit(
            f"warehouse.db not found at {WAREHOUSE_DB}. "
            "Run `python -m src.warehouse build` first."
        )
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
