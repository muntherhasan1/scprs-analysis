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
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WAREHOUSE_DB = Path(os.environ.get("WAREHOUSE_DB", str(DATA_DIR / "warehouse.db")))

# A single SELECT or CTE query, nothing else.
SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


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
    """Analytical marts / star tables with row counts (sorted by name)."""
    with connect() as con:
        out = []
        for name in sorted(analytical_objects(con)):
            # name comes from the sqlite_master allowlist, not the caller.
            count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
            out.append({"name": name, "rows": count})
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
            lines.append(f"{name} ({count} rows): {', '.join(cols)}")
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
    limit = max(1, min(max_rows, 1000))
    try:
        with connect() as con:
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
