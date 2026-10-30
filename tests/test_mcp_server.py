"""MCP server: tools run off the event loop, websocket auth, capability-file TTL."""

import asyncio
import os
import threading
import time

import pytest

from src import auth, mcp_server, query_log
from src import warehouse_query as wq


def test_tools_keep_their_signatures_and_docs():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert set(tools["run_sql"].inputSchema["properties"]) == {"query", "max_rows"}
    assert set(tools["generate_chart"].inputSchema["properties"]) == {
        "sql",
        "kind",
        "title",
        "x",
        "y",
    }
    assert "read-only" in tools["run_sql"].description


def test_slow_tool_calls_overlap_instead_of_blocking_the_loop(monkeypatch):
    # Before: FastMCP ran sync tools inline on the event loop, so two 0.4s queries
    # took 0.8s and froze every other request (incl. /healthz) meanwhile.
    loop_threads, tool_threads = set(), set()

    def slow_select(query, max_rows=200):
        tool_threads.add(threading.get_ident())
        time.sleep(0.4)
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

    monkeypatch.setattr(wq, "run_select", slow_select)
    monkeypatch.setattr(query_log, "_append", lambda entry: None)

    async def main():
        loop_threads.add(threading.get_ident())
        ticks = 0

        async def heartbeat():  # stands in for /healthz on the same loop
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        t0 = time.monotonic()
        await asyncio.gather(
            mcp_server.mcp.call_tool("run_sql", {"query": "SELECT 1"}),
            mcp_server.mcp.call_tool("run_sql", {"query": "SELECT 2"}),
        )
        elapsed = time.monotonic() - t0
        hb.cancel()
        return elapsed, ticks

    elapsed, ticks = asyncio.run(main())
    assert elapsed < 0.7  # the two calls overlapped
    assert ticks >= 4  # the loop kept serving while they ran
    assert tool_threads and not (tool_threads & loop_threads)


def test_threaded_tool_still_records_the_callers_principal(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        wq, "run_select", lambda q, max_rows=200: {"columns": [], "rows": [], "row_count": 0}
    )
    monkeypatch.setattr(query_log, "_append", lambda entry: captured.update(entry))

    async def main():
        auth.current_principal.set("carol")
        await mcp_server.mcp.call_tool("run_sql", {"query": "SELECT 1"})

    asyncio.run(main())
    assert captured.get("principal") == "carol"


def test_report_section_count_is_capped():
    sections = "[" + ",".join(['{"heading":"h","sql":"SELECT 1"}'] * 13) + "]"
    with pytest.raises(Exception, match="at most 12 sections"):
        asyncio.run(
            mcp_server.mcp.call_tool("generate_report", {"title": "t", "sections_json": sections})
        )


def _ws(mw, headers):
    sent = []

    async def app(scope, receive, send):
        sent.append({"type": "through"})

    mw._app = app

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "websocket.connect"}

    asyncio.run(mw({"type": "websocket", "path": "/ws", "headers": headers}, receive, send))
    return sent


def test_websocket_scope_requires_a_token():
    mw = mcp_server.BearerAuthMiddleware(app=None, tokens={"s1": "alice"})
    assert _ws(mw, []) == [{"type": "websocket.close", "code": 1008}]
    assert _ws(mw, [(b"authorization", b"Bearer nope")])[0]["type"] == "websocket.close"
    assert _ws(mw, [(b"authorization", b"Bearer s1")]) == [{"type": "through"}]


class _Req:
    def __init__(self, name):
        self.path_params = {"name": name}


def test_capability_files_expire_and_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "REPORT_TTL_S", 3600)
    fresh_url = mcp_server._save_capability_file(b"new", ".png")
    fresh = fresh_url.rsplit("/", 1)[1]
    old_name = "A" * 24 + ".png"  # the shape of a name the server writes
    old, foreign = tmp_path / old_name, tmp_path / "notes.txt"
    stale = time.time() - 7200
    for path in (old, foreign):
        path.write_bytes(b"old")
        os.utime(path, (stale, stale))

    assert mcp_server._serve_file(_Req(fresh)).status_code == 200
    assert mcp_server._serve_file(_Req(old_name)).status_code == 404

    mcp_server._save_capability_file(b"another", ".png")  # writes prune expired files
    assert not old.exists()
    assert (tmp_path / fresh).exists()
    # Pruning only touches files the server wrote, even in a shared directory.
    assert foreign.exists()


def test_serve_file_404s_on_foreign_names_and_vanished_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "REPORTS_DIR", tmp_path)
    (tmp_path / "notes.txt").write_bytes(b"private")
    for name in ("notes.txt", "..", "x/../notes.txt", "B" * 24 + ".exe"):
        assert mcp_server._serve_file(_Req(name)).status_code == 404
    # Pruned between the link being issued and fetched: a 404, never a 500.
    assert mcp_server._serve_file(_Req("C" * 24 + ".html")).status_code == 404


def test_non_positive_report_ttl_falls_back_to_the_default():
    week = 7 * 86400
    assert mcp_server._ttl_seconds(None) == week
    assert mcp_server._ttl_seconds("0") == week  # 0 would 404 every new link
    assert mcp_server._ttl_seconds("-3") == week
    assert mcp_server._ttl_seconds("soon") == week
    assert mcp_server._ttl_seconds("2") == 2 * 86400


def test_one_principal_cannot_take_every_worker(monkeypatch):
    # 2026-10 review: 40 heavy calls on one token made another user's `select 1`
    # wait 115s. Each principal now holds at most MAX_CONCURRENT_PER_PRINCIPAL
    # worker threads; the rest of its calls queue without taking one.
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(mcp_server, "MAX_CONCURRENT_PER_PRINCIPAL", 2)
    monkeypatch.setattr(mcp_server, "_POOL", ThreadPoolExecutor(max_workers=4))
    monkeypatch.setattr(query_log, "_append", lambda entry: None)
    running, peak = {"hog": 0}, {"hog": 0}
    lock = threading.Lock()

    def select(query, max_rows=200):
        if query == "SLOW":
            with lock:
                running["hog"] += 1
                peak["hog"] = max(peak["hog"], running["hog"])
            time.sleep(0.3)
            with lock:
                running["hog"] -= 1
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

    monkeypatch.setattr(wq, "run_select", select)

    async def call(principal, query):
        auth.current_principal.set(principal)
        t0 = time.monotonic()
        await mcp_server.mcp.call_tool("run_sql", {"query": query})
        return time.monotonic() - t0

    async def main():
        hogs = [asyncio.create_task(call("hog", "SLOW")) for _ in range(8)]
        await asyncio.sleep(0.05)  # the hog's calls are all in flight
        other = await call("other", "FAST")
        await asyncio.gather(*hogs)
        return other

    other_wait = asyncio.run(main())
    assert peak["hog"] == 2
    # Without the cap the hog's 8 calls fill all 4 threads for two rounds and the
    # other user waits ~0.3-0.6s.
    assert other_wait < 0.15


def test_report_stops_running_sections_at_its_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "REPORT_TIMEOUT_S", 0.2)
    monkeypatch.setattr(query_log, "_append", lambda entry: None)
    ran = []

    def select(query, max_rows=200):
        ran.append(query)
        time.sleep(0.15)
        return {"columns": ["n"], "rows": [{"n": 1}], "row_count": 1, "truncated": False}

    monkeypatch.setattr(wq, "run_select", select)
    sections = "[" + ",".join(['{"heading":"h","sql":"SELECT 1"}'] * 6) + "]"
    asyncio.run(
        mcp_server.mcp.call_tool("generate_report", {"title": "t", "sections_json": sections})
    )
    assert len(ran) == 2  # only the sections started before the deadline ran…
    html_doc = next(tmp_path.glob("*.html")).read_text(encoding="utf-8")
    assert html_doc.count("report reached its time limit") == 4  # …the rest say so
