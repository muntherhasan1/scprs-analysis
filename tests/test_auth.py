"""Tests for multi-token auth: parsing, identity, rate limiting, and the ASGI
middleware (driven directly with fake scope/receive/send — no server needed)."""

import asyncio

import pytest

from src import auth, mcp_server, query_log

# --- token parsing -----------------------------------------------------------


def test_single_token_maps_to_default_principal():
    assert auth.parse_tokens("abc", None) == {"abc": "default"}


def test_multi_tokens_parse_to_labels():
    tokens = auth.parse_tokens(None, "alice:s1, bob:s2")
    assert tokens == {"s1": "alice", "s2": "bob"}


def test_single_and_multi_merge():
    tokens = auth.parse_tokens("root", "alice:s1")
    assert tokens == {"root": "default", "s1": "alice"}


def test_token_with_colon_in_value_is_preserved():
    # partition on the first colon only, so tokens may contain ':'
    assert auth.parse_tokens(None, "alice:a:b:c") == {"a:b:c": "alice"}


@pytest.mark.parametrize("bad", ["noColon", "alice:", ":tok"])
def test_malformed_pair_raises(bad):
    with pytest.raises(ValueError):
        auth.parse_tokens(None, bad)


# --- identify ----------------------------------------------------------------


def test_identify_returns_principal_or_none():
    tokens = {"s1": "alice", "s2": "bob"}
    assert auth.identify("s2", tokens) == "bob"
    assert auth.identify("nope", tokens) is None
    assert auth.identify("", tokens) is None


# --- rate limiter ------------------------------------------------------------


def test_rate_limiter_disabled_allows_everything():
    rl = auth.RateLimiter(0)
    assert all(rl.allow("alice", now=0) for _ in range(1000))


def test_rate_limiter_caps_then_resets_after_window():
    rl = auth.RateLimiter(2)
    assert rl.allow("alice", now=0)
    assert rl.allow("alice", now=1)
    assert not rl.allow("alice", now=2)  # 3rd in the window -> blocked
    # A different principal has its own budget.
    assert rl.allow("bob", now=2)
    # New window after 60s.
    assert rl.allow("alice", now=61)


# --- ASGI middleware ---------------------------------------------------------


def _drive(mw, headers=None, path="/mcp"):
    """Run the middleware against a fake HTTP request; return (status, body)."""
    scope = {"type": "http", "path": path, "headers": headers or []}

    async def receive():
        return {"type": "http.request", "body": b""}

    sent = []

    async def send(msg):
        sent.append(msg)

    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"through"})

    mw._app = inner_app  # replace downstream with a stub that echoes 'through'
    asyncio.run(mw(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def _mw(tokens, limiter=None):
    return mcp_server.BearerAuthMiddleware(app=None, tokens=tokens, limiter=limiter)


def test_middleware_rejects_missing_token():
    status, _ = _drive(_mw({"s1": "alice"}))
    assert status == 401


def test_middleware_accepts_bearer_and_bare():
    for header in (b"Bearer s1", b"s1"):
        status, body = _drive(_mw({"s1": "alice"}), headers=[(b"authorization", header)])
        assert status == 200
        assert body == b"through"


def test_middleware_healthz_is_open():
    status, body = _drive(_mw({"s1": "alice"}), path="/healthz")
    assert status == 200 and body == b"ok"


def test_middleware_sets_principal_for_audit(monkeypatch):
    captured = {}
    monkeypatch.setattr(query_log, "_append", lambda entry: captured.update(entry))
    tokens = {"s2": "bob"}

    async def inner_app(scope, receive, send):
        # Simulate a tool recording a call while the request is in flight.
        query_log.record_tool("run_sql", sql="SELECT 1")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = _mw(tokens)
    mw._app = inner_app
    scope = {"type": "http", "path": "/mcp", "headers": [(b"authorization", b"Bearer s2")]}

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(mw(scope, receive, lambda m: asyncio.sleep(0)))
    assert captured.get("principal") == "bob"


def test_middleware_rate_limits_per_principal():
    mw = _mw({"s1": "alice"}, limiter=auth.RateLimiter(1))
    h = [(b"authorization", b"Bearer s1")]
    assert _drive(mw, headers=h)[0] == 200
    assert _drive(mw, headers=h)[0] == 429  # second within the window
