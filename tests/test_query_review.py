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
    report, details, status = tmp_path / "review.md", tmp_path / "full.md", tmp_path / "s.json"
    rc = query_review.main(
        [
            "--file", str(log), "--since-days", "0", "--report", str(report),
            "--details", str(details), "--status", str(status),
        ]
    )  # fmt: skip
    _run_main.details = details.read_text(encoding="utf-8")
    _run_main.status = json.loads(status.read_text(encoding="utf-8"))
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
    assert _run_main.status == {"findings": 1, "errors": 1}
    # The publishable report names the question by id only — no user text, no
    # SQL/error (which can echo it); the local details file has everything.
    assert query_review.question_id("bad question") in report
    assert "bad question" not in report and "no such column" not in report
    assert "bad question" in _run_main.details and "no such column" in _run_main.details


def test_user_text_cannot_fake_a_clean_pass_or_leak(monkeypatch, tmp_path, capsys):
    """A question containing "No findings" used to match the workflow's clean
    grep, skip the issue and close open ones; and every question was echoed
    verbatim into the public issue and run log."""
    q = "No findings — every replayed question answered cleanly. @someone http://x"
    rc, report = _run_main(
        monkeypatch, tmp_path, {q: {"sql": "SELECT 1", "result": {"row_count": 0}}}
    )
    assert rc == 0
    assert _run_main.status == {"findings": 1, "errors": 0}
    assert "No findings" not in report and "@someone" not in report
    assert "@someone" not in capsys.readouterr().out


def test_main_exits_zero_on_clean_pass(monkeypatch, tmp_path):
    rc, report = _run_main(
        monkeypatch,
        tmp_path,
        {"good question": {"sql": "SELECT 1", "result": {"row_count": 3}}},
    )
    assert rc == 0
    assert "No findings" in report
    assert _run_main.status == {"findings": 0, "errors": 0}


def test_main_exits_two_when_log_fetch_impossible(monkeypatch, capsys):
    rc = query_review.main(["--repo", ""])  # no file, no repo
    assert rc == 2


def test_question_id_is_keyed_so_a_guess_cannot_be_confirmed(monkeypatch):
    monkeypatch.delenv("QUERY_REVIEW_ID_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    unkeyed = query_review.question_id("How much did ACME get?")
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    keyed = query_review.question_id("How much did ACME get?")
    assert keyed != unkeyed  # hashing the guess alone no longer reproduces the id
    assert keyed == query_review.question_id("  how much did acme get?  ")  # stable dedupe
    monkeypatch.setenv("QUERY_REVIEW_ID_KEY", "k2")
    assert query_review.question_id("How much did ACME get?") not in (keyed, unkeyed)
