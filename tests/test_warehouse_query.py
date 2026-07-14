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


def test_california_fiscal_year_boundary():
    import datetime

    assert nl_query._ca_fiscal_year(datetime.date(2026, 6, 30)) == 2026
    assert nl_query._ca_fiscal_year(datetime.date(2026, 7, 1)) == 2027
    assert nl_query._ca_fiscal_year(datetime.date(2026, 1, 1)) == 2026


def test_now_context_anchors_last_fiscal_year():
    # Must steer away from MAX(fiscal_year) and define last = current - 1.
    ctx = nl_query._now_context()
    assert "MAX(fiscal_year)" in ctx and "future" in ctx.lower()
    assert "last/previous fiscal year" in ctx


def test_history_context_extracts_prior_turn_and_sql():
    history = [
        {"role": "user", "content": "What did the state spend the most on last fiscal year?"},
        {
            "role": "assistant",
            "content": "Customs consulting, $156M.\n\n<details>x</details>\n\n"
            "```sql\nSELECT category FROM gold_line_item WHERE fiscal_year=2026\n```",
        },
    ]
    ctx = nl_query._history_context(history)
    assert "spend the most" in ctx  # prior question carried forward
    assert "fiscal_year=2026" in ctx  # prior SQL carried forward
    assert nl_query._history_context([]) == ""


def test_generate_sql_threads_history_and_date(monkeypatch):
    # End-to-end wiring: a follow-up question's prompt must carry the prior turn
    # and the current-date note (so "said funds" resolves and FY anchors right).
    captured = {}

    def fake_gen(prompt):
        captured["prompt"] = prompt
        return "SELECT 1"

    monkeypatch.setattr(nl_query, "_generate", fake_gen)
    history = [
        {"role": "user", "content": "top spend category last fiscal year"},
        {"role": "assistant", "content": "X.\n```sql\nSELECT 1 WHERE fiscal_year=2026\n```"},
    ]
    sql = nl_query.generate_sql("who received those funds?", "gold_x: a, b", history=history)
    assert sql == "SELECT 1"
    assert "who received those funds?" in captured["prompt"]
    assert "top spend category last fiscal year" in captured["prompt"]  # history threaded
    assert "current fiscal year" in captured["prompt"].lower()  # date anchor present


def test_query_log_records_and_never_raises(tmp_path, monkeypatch):
    import json
    import threading

    from src import query_log

    class _Dummy:
        lock = threading.Lock()

    monkeypatch.setattr(query_log, "_LOCAL", tmp_path)
    monkeypatch.setattr(query_log, "_LOGFILE", tmp_path / "queries.jsonl")
    monkeypatch.setattr(query_log, "_scheduler_or_none", lambda: _Dummy())
    query_log.record(
        "How much did we spend?", {"sql": "SELECT 1", "result": {"row_count": 3}}, prior_turns=2
    )
    lines = (tmp_path / "queries.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["question"] == "How much did we spend?"
    assert rec["row_count"] == 3 and rec["empty"] is False and rec["prior_turns"] == 2

    # Disabled (no dataset configured) is a silent no-op that must never raise.
    monkeypatch.setattr(query_log, "_scheduler_or_none", lambda: None)
    query_log.record("anything", {})
