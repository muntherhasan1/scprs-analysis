"""Unit tests for the shared, hardened read-only query surface.

No warehouse build needed — a tiny temp DB with a ``gold_*`` table exercises the
allowlist and the SELECT-only / single-statement / read-only guards that both the
MCP server and the web app depend on.
"""

import sqlite3

import pytest

from src import nl_query
from src import warehouse_query as wq


@pytest.fixture
def tiny_db(tmp_path, monkeypatch):
    path = tmp_path / "warehouse.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gold_spend (supplier TEXT, total REAL)")
    con.executemany(
        "INSERT INTO gold_spend VALUES (?, ?)",
        [("Acme", 100.0), ("Beta", 250.0), ("Gamma", 50.0)],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(wq, "WAREHOUSE_DB", path)
    return path


def test_analytical_objects_allowlists_gold(tiny_db):
    with wq.connect() as con:
        assert "gold_spend" in wq.analytical_objects(con)


def test_run_select_returns_rows(tiny_db):
    out = wq.run_select("SELECT supplier, total FROM gold_spend ORDER BY total DESC")
    assert "error" not in out
    assert out["columns"] == ["supplier", "total"]
    assert out["rows"][0] == {"supplier": "Beta", "total": 250.0}
    assert out["row_count"] == 3
    assert out["truncated"] is False


def test_run_select_truncates_at_cap(tiny_db):
    out = wq.run_select("SELECT * FROM gold_spend", max_rows=2)
    assert out["row_count"] == 2
    assert out["truncated"] is True


def test_run_select_rejects_non_select(tiny_db):
    assert "error" in wq.run_select("UPDATE gold_spend SET total = 0")
    assert "error" in wq.run_select("DELETE FROM gold_spend")


def test_run_select_rejects_multiple_statements(tiny_db):
    out = wq.run_select("SELECT 1; DROP TABLE gold_spend")
    assert "error" in out and "Multiple" in out["error"]


def test_run_select_readonly_blocks_writes_even_if_they_slip_through(tiny_db):
    # A CTE-shaped statement passes the SELECT_ONLY prefix but any write still
    # fails on the read-only connection.
    out = wq.run_select("WITH x AS (SELECT 1) INSERT INTO gold_spend VALUES ('z', 1)")
    assert "error" in out


def test_describe_rejects_unknown_object(tiny_db):
    assert "error" in wq.describe("sqlite_master")
    assert wq.describe("gold_spend")["row_count"] == 3


def test_schema_for_llm_lists_gold_views(tiny_db):
    text = wq.schema_for_llm()
    assert "gold_spend" in text
    assert "supplier" in text and "total" in text


def test_extract_sql_strips_fences():
    assert nl_query._extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert nl_query._extract_sql("SELECT 1;") == "SELECT 1"
    assert nl_query._extract_sql("  NO_QUERY  ") == "NO_QUERY"
