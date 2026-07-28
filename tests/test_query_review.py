"""Offline tests for the weekly query-review loop (log merge, selection,
classification, wrong-mart smells, exit contract). All network and LLM calls
are monkeypatched — nothing here touches HF or Gemini."""

import json
from datetime import datetime, timedelta, timezone

from src import query_review

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _rec(question, ts=None, **extra):
    r = {"source": "web", "question": question, "ts": (ts or NOW).isoformat()}
    r.update(extra)
    return r


# ---------------------------------------------------------------- log loading


def test_load_log_records_merges_all_per_space_files(monkeypatch, tmp_path):
    """The bug this fixes: only data/queries.jsonl was read, missing the
    per-Space files each front end actually writes."""
    files = {
        "data/queries.jsonl": [_rec("legacy q")],
        "data/queries-munther-hasan-scprs-warehouse-chat.jsonl": [_rec("chat q")],
        "data/queries-munther-hasan-scprs-warehouse-mcp.jsonl": [
            {"source": "mcp", "tool": "run_sql", "sql": "SELECT 1", "ts": NOW.isoformat()}
        ],
        "README.md": None,  # non-jsonl repo files must be ignored
    }

    class FakeApi:
        def __init__(self, token=None):
            pass

        def list_repo_files(self, repo, repo_type):
            return list(files)

    def fake_download(repo, name, repo_type, revision, token=None):
        p = tmp_path / name.replace("/", "_")
        p.write_text("\n".join(json.dumps(r) for r in files[name]) + "\nnot json\n")
        return str(p)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    records = query_review.load_log_records("acme/log")
    questions = {r.get("question") for r in records if r.get("question")}
    assert questions == {"legacy q", "chat q"}
    # The MCP audit record is loaded but carries no question — it must fall
    # out at selection, not crash loading. (Cross-file order tracks filename
    # sort and is not meaningful.)
    assert sorted(query_review.select_questions(records)) == ["chat q", "legacy q"]


# ------------------------------------------------------------------ selection


def test_select_questions_filters_since_and_dedupes_and_limits():
    records = [
        _rec("Old question", ts=NOW - timedelta(days=30)),
        _rec("Fresh question"),
        _rec("FRESH QUESTION"),  # case-insensitive dupe
        _rec("Another fresh one"),
        {"source": "web", "question": "no ts record"},  # kept: unparseable ts
    ]
    got = query_review.select_questions(records, since=NOW - timedelta(days=8))
    assert got == ["Fresh question", "Another fresh one", "no ts record"]
    assert query_review.select_questions(records, since=NOW - timedelta(days=8), limit=1) == [
        "Fresh question"
    ]


# -------------------------------------------------------------- classification


def test_classify_tags():
    assert query_review.classify({"sql": "SELECT 1", "result": {"error": "boom"}}) == "ERROR"
    assert query_review.classify({"sql": None, "result": None}) == "NO_QUERY"
    assert query_review.classify({"sql": "SELECT 1", "result": {"row_count": 0}}) == "EMPTY"
    assert query_review.classify({"sql": "SELECT 1", "result": {"row_count": 5}}) == "ok"


# --------------------------------------------------------------------- smells


def test_smells_flags_sb_dvbe_and_line_marts_for_spend():
    assert query_review.smells(
        "how much spend goes to certified suppliers?",
        "SELECT SUM(total_value) FROM gold_supplier_master WHERE sb_dvbe != ''",
    )
    assert query_review.smells(
        "top suppliers by total spend",
        "SELECT supplier, SUM(line_total) FROM gold_line_item GROUP BY supplier",
    )


def test_smells_clean_for_document_grain_and_item_detail():
    assert (
        query_review.smells(
            "top suppliers by total spend",
            "SELECT canonical_name, SUM(grand_total) FROM gold_document GROUP BY canonical_name",
        )
        == []
    )
    # Genuine item-level detail on a line mart is the mart's intended use.
    assert (
        query_review.smells(
            "what items appear on document 123?",
            "SELECT item_description FROM gold_line_item WHERE purchase_document = '123'",
        )
        == []
    )


# ------------------------------------------------------------------- exit codes


def _run_main(monkeypatch, tmp_path, answers):
    """Drive main() against a local file with nl_query.answer stubbed."""
    log = tmp_path / "queries.jsonl"
    log.write_text("\n".join(json.dumps(_rec(q)) for q in answers))
    it = iter(answers.values())
    from src import nl_query

    monkeypatch.setattr(nl_query, "answer", lambda q, history=None: next(it))
    report = tmp_path / "review.md"
    rc = query_review.main(["--file", str(log), "--since-days", "0", "--report", str(report)])
    return rc, report.read_text(encoding="utf-8")


def test_main_exits_one_on_replay_error(monkeypatch, tmp_path):
    rc, report = _run_main(
        monkeypatch,
        tmp_path,
        {
            "good question": {"sql": "SELECT 1", "result": {"row_count": 3}},
            "bad question": {"sql": "SELECT x", "result": {"error": "no such column"}},
        },
    )
    assert rc == 1
    assert "bad question" in report and "no such column" in report


def test_main_exits_zero_on_clean_pass(monkeypatch, tmp_path):
    rc, report = _run_main(
        monkeypatch,
        tmp_path,
        {"good question": {"sql": "SELECT 1", "result": {"row_count": 3}}},
    )
    assert rc == 0
    assert "No findings" in report


def test_main_exits_two_when_log_fetch_impossible(monkeypatch, capsys):
    rc = query_review.main(["--repo", ""])  # no file, no repo
    assert rc == 2
