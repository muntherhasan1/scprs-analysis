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


def test_run_select_rejects_blob_bombs(tiny_db):
    out = wq.run_select("SELECT randomblob(1000000)")
    assert "error" in out and "blob" in out["error"].lower()


def test_run_select_times_out_on_runaway_query(tiny_db, monkeypatch):
    monkeypatch.setattr(wq, "QUERY_TIMEOUT_S", 0.05)
    out = wq.run_select(
        "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT COUNT(*) FROM r"
    )
    assert "error" in out and "time limit" in out["error"].lower()


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


def test_list_marts_includes_description_field(tiny_db):
    marts = wq.list_marts()
    assert marts and all("description" in m for m in marts)
    # gold_spend isn't a curated mart -> empty hint, but the key is always present.
    spend = next(m for m in marts if m["name"] == "gold_spend")
    assert spend["description"] == ""


def test_distinct_values_lists_categorical_with_counts(tiny_db):
    out = wq.distinct_values("gold_spend", "supplier")
    assert "error" not in out
    assert out["table"] == "gold_spend" and out["column"] == "supplier"
    assert {v["value"] for v in out["values"]} == {"Acme", "Beta", "Gamma"}
    assert all(v["count"] == 1 for v in out["values"])
    assert out["truncated"] is False


def test_distinct_values_flags_truncation(tiny_db):
    out = wq.distinct_values("gold_spend", "supplier", max_values=2)
    assert len(out["values"]) == 2 and out["truncated"] is True


def test_distinct_values_guards_table_and_column(tiny_db):
    # Non-allowlisted table and a fabricated column are both rejected before any
    # interpolation — the same guard the other query surfaces rely on.
    assert "error" in wq.distinct_values("sqlite_master", "name")
    assert "error" in wq.distinct_values("gold_spend", "no_such_col")


def test_row_counts_are_computed_once_per_db(tiny_db, monkeypatch):
    # COUNT(*) over the gold views is ~60s on the real serve DB; it must not run
    # on every NL question.
    calls = []
    real = wq._count_all

    def counting(con, **kw):
        calls.append(1)
        return real(con, **kw)

    monkeypatch.setattr(wq, "_count_all", counting)
    wq.schema_for_llm()
    wq.schema_for_llm()
    wq.list_marts()
    assert wq.describe("gold_spend")["row_count"] == 3
    assert len(calls) == 1


def test_row_counts_prefer_the_stored_table(tiny_db, monkeypatch):
    assert wq.record_row_counts(tiny_db) == 1
    # A stored count is used as-is — no COUNT(*) at request time.
    con = sqlite3.connect(tiny_db)
    con.execute("UPDATE serve_row_counts SET row_count = 42")
    con.commit()
    con.close()

    def fail(con, **kw):
        pytest.fail("counted at request time")

    monkeypatch.setattr(wq, "_count_all", fail)
    text = wq.schema_for_llm()
    assert "gold_spend (42 rows)" in text
    # The counts table is not an analytical object a model would be shown.
    assert "serve_row_counts" not in text


def test_slow_count_fallback_never_blocks_callers(tiny_db, monkeypatch):
    # 2026-10 review: with no stored counts, the first caller held a global lock
    # for ~64s while every other schema call waited on it. Now one background
    # thread counts; callers wait at most ROW_COUNT_WAIT_S and see "unknown".
    import threading
    import time

    release, calls = threading.Event(), []
    real = wq._count_all

    def slow(con, **kw):
        calls.append(kw)
        release.wait(5)
        return real(con, **kw)

    monkeypatch.setattr(wq, "_count_all", slow)
    monkeypatch.setattr(wq, "ROW_COUNT_WAIT_S", 0.05)
    texts, t0 = [], time.monotonic()
    workers = [threading.Thread(target=lambda: texts.append(wq.schema_for_llm())) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(2)
    assert time.monotonic() - t0 < 1.5  # nobody waited for the count
    assert len(texts) == 8 and all("gold_spend (unknown rows)" in t for t in texts)
    assert calls == [{"bounded": True}]  # one bounded counter for the file

    release.set()
    deadline = time.monotonic() + 3
    while "(3 rows)" not in wq.schema_for_llm() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "gold_spend (3 rows)" in wq.schema_for_llm()
    assert wq.describe("gold_spend")["row_count"] == 3
    assert len(calls) == 1


def test_bounded_count_leaves_a_slow_object_unknown(tiny_db, monkeypatch):
    con = sqlite3.connect(tiny_db)
    con.execute(
        "CREATE VIEW gold_slow AS WITH RECURSIVE r(n) AS "
        "(SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 50000000) SELECT n FROM r"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(wq, "QUERY_TIMEOUT_S", 0.05)
    with wq.connect() as con:
        counts = wq._count_all(con, bounded=True)
    assert counts == {"gold_spend": 3}  # the slow view is left out, not waited on


def test_run_select_returns_strict_json(tiny_db):
    import json

    out = wq.run_select("SELECT 1e999 AS pos, -1e999 AS neg, 1.5 AS ok")
    assert out["rows"] == [{"pos": None, "neg": None, "ok": 1.5}]
    json.dumps(out, allow_nan=False)  # what strict clients (Copilot Studio) require


def test_distinct_values_honours_the_time_limit(tiny_db, monkeypatch):
    con = sqlite3.connect(tiny_db)
    con.executemany("INSERT INTO gold_spend VALUES (?, ?)", [(str(i), i) for i in range(50_000)])
    con.commit()
    con.close()
    monkeypatch.setattr(wq, "QUERY_TIMEOUT_S", 1e-9)
    assert wq.distinct_values("gold_spend", "supplier") == {
        "error": "Query exceeded the time limit."
    }


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


def test_record_tool_captures_mcp_call(tmp_path, monkeypatch):
    import json
    import threading

    from src import query_log

    class _Dummy:
        lock = threading.Lock()

    monkeypatch.setattr(query_log, "_LOGFILE", tmp_path / "queries.jsonl")
    monkeypatch.setattr(query_log, "_scheduler_or_none", lambda: _Dummy())
    query_log.record_tool("run_sql", sql="SELECT 1", row_count=3, error=None)
    rec = json.loads((tmp_path / "queries.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["source"] == "mcp" and rec["tool"] == "run_sql"
    assert rec["sql"] == "SELECT 1" and rec["row_count"] == 3

    # Disabled is a silent no-op that must never raise.
    monkeypatch.setattr(query_log, "_scheduler_or_none", lambda: None)
    query_log.record_tool("run_sql", sql="SELECT 1")


def test_write_token_prefers_dedicated_query_log_token(monkeypatch):
    from src import query_log

    monkeypatch.delenv("QUERY_LOG_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert query_log._write_token() is None
    monkeypatch.setenv("HF_TOKEN", "read-only")
    assert query_log._write_token() == "read-only"  # falls back to HF_TOKEN
    monkeypatch.setenv("QUERY_LOG_TOKEN", "write-scoped")
    assert query_log._write_token() == "write-scoped"  # dedicated token wins


def test_logfile_name_namespaced_per_space(monkeypatch):
    from src import query_log

    monkeypatch.delenv("QUERY_LOG_FILE", raising=False)
    monkeypatch.delenv("SPACE_ID", raising=False)
    assert query_log._logfile_name() == "queries.jsonl"
    # Two different Spaces must not collide on one dataset path.
    monkeypatch.setenv("SPACE_ID", "munther-hasan/scprs-warehouse-mcp")
    assert query_log._logfile_name() == "queries-munther-hasan-scprs-warehouse-mcp.jsonl"
    monkeypatch.setenv("SPACE_ID", "munther-hasan/scprs-warehouse-web")
    assert query_log._logfile_name() == "queries-munther-hasan-scprs-warehouse-web.jsonl"
    # Explicit override wins.
    monkeypatch.setenv("QUERY_LOG_FILE", "custom.jsonl")
    assert query_log._logfile_name() == "custom.jsonl"


def test_run_select_bounds_memory_whatever_the_spelling(tiny_db, monkeypatch):
    """The blob deny-list is evadable (comments, quoted names, printf padding);
    the SQLite length limit and the result-byte budget are the real bound."""
    for q in (
        "SELECT printf('%.*c', 200000000, 'x') AS s",
        'SELECT length("zeroblob"(200000000)) AS n',
        "SELECT zeroblob/**/(200000000) AS b",
    ):
        res = wq.run_select(q)
        # Refused, or SQLite yields NULL instead of building the value.
        assert "error" in res or res["rows"] == [{k: None for k in res["columns"]}], q
    monkeypatch.setattr(wq, "MAX_RESULT_BYTES", 50_000)
    res = wq.run_select(
        "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r WHERE i < 100) "
        "SELECT printf('%.*c', 90000, 'x') AS s FROM r"
    )
    assert res == {"error": "Query result is too large; select fewer or shorter columns."}
