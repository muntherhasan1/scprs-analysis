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
    (constant-time compared). The only unauthenticated paths are `/healthz` and
    `/files/<unguessable>` — capability URLs for `generate_report` output, where
    the random path is itself the access token so a browser/Copilot can open the
    report page inline.

Beyond querying, two tools turn results into visuals over public data:
`generate_chart` (a read-only SELECT → a PNG image) and `generate_report`
(sections of SELECTs + prose → a self-contained HTML report served at a `/files/`
URL). Rendering is matplotlib (Agg); the server still makes no LLM calls.

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
import secrets
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import query_log
from . import warehouse_query as wq

mcp = FastMCP("scprs-warehouse")

# Where generate_report writes its HTML; served back at an unauthenticated
# capability URL (/files/<unguessable>). Env-overridable so the container can
# point at a writable dir (the image's WORKDIR is root-owned).
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "reports"))

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
    result = wq.run_select(query, max_rows=max_rows)
    query_log.record_tool(
        "run_sql",
        sql=query,
        row_count=result.get("row_count"),
        error=result.get("error"),
    )
    return result


@mcp.tool()
def generate_chart(sql: str, kind: str = "bar", title: str = "", x: str = "", y: str = "") -> dict:
    """Render a chart from one read-only ``SELECT`` and return a link to the PNG.

    ``kind`` is ``bar``, ``line``, or ``pie``. By default the first column is the
    label axis and the first numeric column is the value axis; override with ``x``
    / ``y`` (column names). Write the SQL to return the label + value columns you
    want plotted, e.g. ``SELECT canonical_name, total_value FROM
    gold_canonical_supplier_spend ORDER BY total_value DESC LIMIT 10``.

    Returns ``{"chart_url": ...}`` — a public image URL. Show it to the user (as a
    Markdown image ``![title](chart_url)`` and/or a clickable link); a URL renders
    in far more clients than inline MCP image content does.
    """
    from . import charting

    result = wq.run_select(sql, max_rows=100)
    query_log.record_tool(
        "generate_chart",
        sql=sql,
        row_count=result.get("row_count"),
        error=result.get("error"),
        kind=kind,
        title=title,
    )
    if "error" in result:
        raise ValueError(result["error"])
    png = charting.render_chart(
        result["columns"], result["rows"], kind=kind, title=title, x=x or None, y=y or None
    )
    return {"chart_url": _save_capability_file(png, ".png")}


@mcp.tool()
def generate_report(title: str, sections_json: str) -> dict:
    """Build a self-contained HTML executive report and return its URL.

    ``sections_json`` is a JSON **array string** — a flat string param (Copilot
    Studio and most clients build these far more reliably than nested objects).
    Each item is an object:
      ``{"heading": str, "sql": str, "narrative": str, "chart": "bar|line|pie|none"}``
    where ``sql`` is a single read-only ``SELECT``/``WITH`` and ``narrative`` is
    prose YOU write to interpret the numbers. Example::

        [{"heading":"Top suppliers","sql":"SELECT canonical_name, SUM(grand_total)
          AS spend FROM gold_document GROUP BY canonical_name ORDER BY spend DESC
          LIMIT 10","narrative":"Golden State Connect leads.","chart":"bar"}]

    Each section's SQL runs through the read-only guard; charts are embedded in the
    HTML (no external assets). Returns ``{report_url, sections}`` — a shareable link.
    """
    import json

    from . import charting

    try:
        sections = json.loads(sections_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"sections_json must be valid JSON: {exc}") from exc
    if not isinstance(sections, list):
        raise ValueError("sections_json must be a JSON array of {heading, sql, narrative, chart}")

    built = []
    section_sqls = []
    for s in sections:
        if not isinstance(s, dict):
            continue
        heading = str(s.get("heading", ""))
        sql = str(s.get("sql", ""))
        section_sqls.append(sql)
        narrative = str(s.get("narrative", ""))
        chart = str(s.get("chart", "none")).lower()
        res = wq.run_select(sql, max_rows=200)
        if "error" in res:
            built.append(
                {
                    "heading": heading,
                    "narrative": f"(query error: {res['error']})",
                    "columns": [],
                    "rows": [],
                }
            )
            continue
        png = None
        if chart in charting.CHART_KINDS:
            try:
                png = charting.render_chart(res["columns"], res["rows"], kind=chart, title=heading)
            except ValueError:
                png = None  # not chartable (e.g. no numeric column) — table only
        built.append(
            {
                "heading": heading,
                "narrative": narrative,
                "columns": res["columns"],
                "rows": res["rows"],
                "chart_png": png,
            }
        )
    html_doc = charting.build_report_html(title, built)
    query_log.record_tool(
        "generate_report",
        title=title,
        sections=len(built),
        sqls=section_sqls,
    )
    return {
        "report_url": _save_capability_file(html_doc.encode("utf-8"), ".html"),
        "sections": len(built),
    }


def _public_base_url() -> str:
    """Best guess at this server's externally reachable base URL, for report links.

    Prefer an explicit ``PUBLIC_BASE_URL``; else derive from the first
    ``MCP_ALLOWED_HOSTS`` host (which the deploy sets to the real proxy hostname);
    else fall back to localhost.
    """
    base = os.environ.get("PUBLIC_BASE_URL")
    if base:
        return base.rstrip("/")
    hosts = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    if hosts:
        return "https://" + hosts.split(",")[0].strip()
    return f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"


def _save_capability_file(data: bytes, suffix: str) -> str:
    """Write bytes to REPORTS_DIR under an unguessable name; return its /files/ URL.

    The random name is the access token (the /files/ path is unauthenticated), so
    charts/reports can be opened inline by a browser or Copilot.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    name = secrets.token_urlsafe(18) + suffix
    (REPORTS_DIR / name).write_bytes(data)
    return f"{_public_base_url()}/files/{name}"


class BearerAuthMiddleware:
    """Pure-ASGI middleware gating every HTTP request behind a bearer token.

    Pure ASGI (not Starlette's BaseHTTPMiddleware) so it doesn't buffer or break
    the MCP Streamable HTTP / SSE responses. `/healthz` is exempt so the host's
    health checks don't need the secret; `/files/*` is exempt because those are
    unguessable capability URLs (the path itself is the access token) so report
    pages/images can be fetched inline by a browser or Copilot. The token is
    accepted as ``Bearer <token>`` or bare ``<token>`` and compared in constant time.
    """

    def __init__(self, app, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/healthz":
            await self._respond(send, 200, b"ok")
            return
        if path.startswith("/files/"):
            await self._app(scope, receive, send)  # capability URL — no bearer needed
            return
        provided = dict(scope.get("headers") or []).get(b"authorization", b"").decode().strip()
        # Accept "Bearer <token>" or a bare "<token>": some connector UIs (e.g.
        # Copilot Studio's API-key-in-header) send the header value verbatim, so a
        # user who omits the "Bearer " prefix should still authenticate.
        if provided[:7].lower() == "bearer ":
            provided = provided[7:].strip()
        if not provided or not hmac.compare_digest(provided, self._token):
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
    from . import data_sync

    # On a Space, fetch the slim serve DB from the private dataset (no-op locally,
    # where WAREHOUSE_DATASET is unset and the local warehouse.db is used).
    try:
        data_sync.ensure_local_db(wq.WAREHOUSE_DB)
    except data_sync.WarehouseFetchError as exc:
        raise SystemExit(str(exc)) from exc  # clear boot error, not a raw traceback
    if not wq.WAREHOUSE_DB.exists():
        raise SystemExit(
            f"warehouse.db not found at {wq.WAREHOUSE_DB}. "
            "Run `python -m src.warehouse build` first (or set WAREHOUSE_DB), "
            "or set WAREHOUSE_DATASET + HF_TOKEN to fetch it from the dataset."
        )


def _serve_file(request):
    """Serve a generated report from REPORTS_DIR by its capability-URL name."""
    from starlette.responses import FileResponse, PlainTextResponse

    name = request.path_params["name"]
    if "/" in name or "\\" in name or ".." in name:  # no path traversal
        return PlainTextResponse("bad request", status_code=400)
    path = REPORTS_DIR / name
    if not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    media = "image/png" if name.endswith(".png") else "text/html"
    return FileResponse(str(path), media_type=media)


def serve_http() -> None:
    """Serve the tools over a bearer-token-gated Streamable HTTP endpoint."""
    import uvicorn
    from starlette.routing import Route

    from . import observability

    observability.init_sentry("mcp")  # optional; no-op unless SENTRY_DSN is set
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
    # Add the /files/<name> capability-URL route directly onto the MCP app's router
    # (not a Mount — a Mount wouldn't run the app's lifespan, which starts the MCP
    # session manager). It serves generated reports; everything else is /mcp.
    app = mcp.streamable_http_app()
    app.router.routes.insert(0, Route("/files/{name}", _serve_file))
    app = BearerAuthMiddleware(app, token)  # gates all but /healthz and /files/
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
