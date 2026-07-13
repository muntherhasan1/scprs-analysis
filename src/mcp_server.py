"""Read-only MCP server exposing the SCPRS gold warehouse to an MCP client.

Runs two ways from the same tool definitions:
  * **stdio** (default) — for a local MCP client such as Claude Code, wired via
    `.mcp.json`. No network, no auth.
  * **http** — a remote, bearer-token-gated Streamable HTTP endpoint so any MCP
    client (Claude Desktop/Code, Cursor, …) can query from anywhere. This is the
    "Model A" deployment: the server does **no** Anthropic API calls — each
    user's own MCP client does the natural-language reasoning — so there is no
    per-token metering on our side, only (free-tier) hosting.

Safety model — the server is query-only by construction:
  * The SQLite connection is opened in read-only URI mode (`?mode=ro`), so a
    write is physically impossible regardless of what SQL arrives.
  * `run_sql` additionally accepts a single `SELECT`/`WITH` statement only.
  * `describe_table` / row counts interpolate object names, but only after
    checking them against the live allowlist of `gold_*`/`lv_*`/`dim_*`/`fact_*`
    objects from `sqlite_master` — never raw client input.
  * In http mode every request must carry `Authorization: Bearer <MCP_AUTH_TOKEN>`
    (constant-time compared); `/healthz` is the only unauthenticated path.

Run:
    pip install mcp                       # one-time (free, open source)
    python -m src.warehouse build         # ensure data/warehouse.db exists
    python -m src.mcp_server              # stdio (local Claude Code)
    MCP_AUTH_TOKEN=... python -m src.mcp_server http   # remote HTTP endpoint

Env overrides: WAREHOUSE_DB (db path), MCP_AUTH_TOKEN (required for http),
HOST (default 0.0.0.0), PORT (default 8000), MCP_ALLOWED_HOSTS (comma-separated
Host allowlist; see `_transport_security` for the DNS-rebinding-guard rationale).
"""

from __future__ import annotations

import argparse
import hmac
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import warehouse_query as wq

mcp = FastMCP("scprs-warehouse")

# All query logic lives in the shared, hardened `warehouse_query` module so the
# read-only guard (SELECT-only, ?mode=ro, allowlisted object names) has exactly
# one implementation, reused by both this server and the web app. These tools are
# thin MCP wrappers; their docstrings are what the MCP client sees.


@mcp.tool()
def list_marts() -> list[dict]:
    """List the analytical marts and star-schema tables with row counts.

    Prefer the friendly ``gold_*`` mart views for most questions. Canonical
    vendor rollups (one row per real company) are
    ``gold_canonical_supplier_spend`` / ``gold_supplier_master`` — the
    per-supplier_id marts double-count vendors that registered more than once.
    """
    return wq.list_marts()


@mcp.tool()
def describe_table(name: str) -> dict:
    """Return the columns (logical names) and row count for one mart or table."""
    return wq.describe(name)


@mcp.tool()
def data_dictionary() -> list[dict]:
    """Logical↔physical column mapping for the abbreviated gold tables.

    The physical ``dim_*``/``fact_*`` tables use abbreviated columns
    (``grand_total``→``grand_tot``). Query the ``gold_*`` or ``lv_*`` views to
    use logical names directly, or consult this mapping when writing SQL
    straight against the star tables.
    """
    return wq.data_dictionary()


@mcp.tool()
def run_sql(query: str, max_rows: int = 200) -> dict:
    """Run one read-only ``SELECT``/``WITH`` query and return the rows.

    Prefer the ``gold_*``/``lv_*`` views (logical column names). Only a single
    read-only statement is permitted; the connection cannot write. Results are
    capped at ``max_rows`` (1–1000); ``truncated`` flags when the cap was hit.
    """
    return wq.run_select(query, max_rows=max_rows)


class BearerAuthMiddleware:
    """Pure-ASGI middleware gating every HTTP request behind a bearer token.

    Pure ASGI (not Starlette's BaseHTTPMiddleware) so it doesn't buffer or break
    the MCP Streamable HTTP / SSE responses. `/healthz` is exempt so the host's
    health checks don't need the secret. The token is compared in constant time.
    """

    def __init__(self, app, token: str, health_path: str = "/healthz") -> None:
        self._app = app
        self._expected = f"Bearer {token}"
        self._health_path = health_path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == self._health_path:
            await self._respond(send, 200, b"ok")
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode()
        if not provided or not hmac.compare_digest(provided, self._expected):
            await self._respond(
                send, 401, b"unauthorized", extra=[(b"www-authenticate", b"Bearer")]
            )
            return
        await self._app(scope, receive, send)

    @staticmethod
    async def _respond(send, status: int, body: bytes, extra=None) -> None:
        headers = [(b"content-type", b"text/plain; charset=utf-8"), *(extra or [])]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _transport_security() -> TransportSecuritySettings:
    """DNS-rebinding-guard config for the Streamable HTTP transport.

    The MCP SDK's guard defaults to accepting only ``localhost`` Host headers,
    which rejects any real deployment behind a reverse proxy (e.g. HF Spaces →
    ``421 Invalid Host header``). That guard exists to stop a malicious *web page*
    from driving a browser's requests into a localhost MCP server; it is not our
    threat model — this endpoint is remote, non-browser, and already gated by
    ``BearerAuthMiddleware`` on every path but ``/healthz``.

    So: if ``MCP_ALLOWED_HOSTS`` is set (comma-separated host or ``host:*``
    patterns), keep the guard on with that explicit allowlist — the stricter,
    preferred posture for a known hostname. Otherwise disable the Host/Origin
    check (Content-Type is still validated) so the service works behind whatever
    proxy host the platform assigns.
    """
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    # Accept the bare host and any port on it, and the matching https origins.
    allowed_hosts = [p for h in hosts for p in (h, f"{h}:*")]
    allowed_origins = [p for h in hosts for p in (f"https://{h}", f"https://{h}:*")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _require_db() -> None:
    if not wq.WAREHOUSE_DB.exists():
        raise SystemExit(
            f"warehouse.db not found at {wq.WAREHOUSE_DB}. "
            "Run `python -m src.warehouse build` first (or set WAREHOUSE_DB)."
        )


def serve_http() -> None:
    """Serve the tools over a bearer-token-gated Streamable HTTP endpoint."""
    import uvicorn

    _require_db()
    token = os.environ.get("MCP_AUTH_TOKEN")
    if not token:
        raise SystemExit(
            "MCP_AUTH_TOKEN is required in http mode — refusing to expose the "
            "endpoint unauthenticated. Set it to a long random secret."
        )
    # Bind all interfaces: intended for a containerized, token-gated service.
    host = os.environ.get("HOST", "0.0.0.0")  # noqa: S104  # nosec B104
    port = int(os.environ.get("PORT", "8000"))
    # Stateless: each request is self-contained, so a scale-to-zero host that
    # stops/starts (or runs multiple machines) never strands a session.
    mcp.settings.stateless_http = True
    mcp.settings.transport_security = _transport_security()
    app = BearerAuthMiddleware(mcp.streamable_http_app(), token)
    # MCP endpoint is served at /mcp (FastMCP default); clients send the token as
    # `Authorization: Bearer <MCP_AUTH_TOKEN>`.
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description="SCPRS read-only warehouse MCP server")
    parser.add_argument(
        "transport",
        nargs="?",
        default="stdio",
        choices=["stdio", "http"],
        help="stdio (default, local Claude Code) or http (remote, token-gated)",
    )
    args = parser.parse_args()
    if args.transport == "http":
        serve_http()
        return
    _require_db()
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
