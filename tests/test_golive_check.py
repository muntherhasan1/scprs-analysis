"""Offline tests for the post-refresh go-live verification."""

import sqlite3

import pytest

from src import golive_check


def _serve_db(path, *, docs=5, lines=9, enriched=3):
    """A minimal serve DB exposing the lv_* views the marker query reads."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE lv_fact_document (line_count INTEGER)")
    con.executemany(
        "INSERT INTO lv_fact_document VALUES (?)",
        [(1,)] * enriched + [(0,)] * (docs - enriched),
    )
    con.execute("CREATE TABLE lv_fact_line (x INTEGER)")
    con.executemany("INSERT INTO lv_fact_line VALUES (?)", [(1,)] * lines)
    con.commit()
    con.close()


def test_local_markers_reads_counts(tmp_path):
    db = tmp_path / "warehouse-serve.db"
    _serve_db(db, docs=5, lines=9, enriched=3)
    assert golive_check.local_markers(db) == {
        "documents": 5,
        "line_rows": 9,
        "enriched_docs": 3,
    }


def test_main_skips_without_token(monkeypatch, capsys):
    monkeypatch.delenv("MCP_VERIFY_TOKEN", raising=False)
    assert golive_check.main([]) == 0
    assert "SKIPPED" in capsys.readouterr().out


def _wire(monkeypatch, tmp_path, served):
    """Stub the network-facing pieces; the local serve DB is real."""
    db = tmp_path / "warehouse-serve.db"
    _serve_db(db, docs=5, lines=9, enriched=3)
    monkeypatch.setenv("MCP_VERIFY_TOKEN", "tok")
    monkeypatch.setattr(golive_check, "wait_for_space", lambda space, timeout_s: None)

    async def fake_served(url, token, timeout_s):
        return served

    monkeypatch.setattr(golive_check, "served_markers", fake_served)
    return ["--serve-db", str(db)]


def test_main_verifies_matching_snapshot(monkeypatch, tmp_path, capsys):
    argv = _wire(monkeypatch, tmp_path, {"documents": 5, "line_rows": 9, "enriched_docs": 3})
    assert golive_check.main(argv) == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_main_exits_one_on_verified_mismatch(monkeypatch, tmp_path, capsys):
    """The 2026-07-20 signature: Space up but serving the previous snapshot —
    positive evidence, the only case the workflow rolls back on."""
    argv = _wire(monkeypatch, tmp_path, {"documents": 5, "line_rows": 7, "enriched_docs": 2})
    assert golive_check.main(argv) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_main_exits_two_when_observation_impossible(monkeypatch, tmp_path, capsys):
    """Boot timeout / unreachable endpoint proves nothing about the data — exit 2
    so the workflow fails WITHOUT rolling back (#45: no rollback on absence of
    evidence; the old behavior reverted good data on a mere token rotation)."""
    db = tmp_path / "warehouse-serve.db"
    _serve_db(db)
    monkeypatch.setenv("MCP_VERIFY_TOKEN", "tok")

    def never_running(space, timeout_s):
        raise TimeoutError("acme/space still BUILDING after 1500s")

    monkeypatch.setattr(golive_check, "wait_for_space", never_running)
    assert golive_check.main(["--serve-db", str(db)]) == 2
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out and "roll" in out


def test_wait_for_space_survives_transient_poll_errors(monkeypatch):
    """One API blip mid-poll must not abort the wait (live incident: ConnectError
    80s into a 20-min reboot window failed the whole go-live check)."""
    import huggingface_hub

    calls = {"n": 0}

    class FlakyApi:
        def space_info(self, space):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("reset by peer")

            class Info:
                class runtime:
                    stage = "RUNNING"

            return Info()

    monkeypatch.setattr(huggingface_hub, "HfApi", FlakyApi)
    golive_check.wait_for_space("acme/space", timeout_s=30, poll_s=0)  # must not raise
    assert calls["n"] == 3


def test_wait_for_space_raises_on_terminal_stage(monkeypatch):
    import huggingface_hub

    class FakeRuntime:
        stage = "RUNTIME_ERROR"

    class FakeInfo:
        runtime = FakeRuntime()

    class FakeApi:
        def space_info(self, space):
            return FakeInfo()

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    with pytest.raises(RuntimeError, match="RUNTIME_ERROR"):
        golive_check.wait_for_space("acme/space", timeout_s=5)
