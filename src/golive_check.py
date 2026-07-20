"""Post-refresh go-live verification: does the deployed Space serve THIS build?

The Wave 1 observability instinct extended to deploys (retrospective action #4).
The refresh chain can report success end-to-end while the Space silently keeps
serving its previous boot-time snapshot — exactly what happened on 2026-07-20,
when a plain (non-factory) restart left three "successful" refreshes invisible.
This check turns that silent staleness into a loud CI failure.

How it works, over the same channel a real MCP client uses:
  1. Compute freshness markers (document / line / enriched-doc counts) from the
     locally built ``warehouse-serve.db`` — the file the run just published.
  2. Wait for the Space to come back from its factory reboot (stage RUNNING).
  3. Run the same marker query through the Space's token-gated ``run_sql`` MCP
     tool, retrying while the app inside the RUNNING container finishes booting.
  4. Compare. Mismatch or timeout -> exit 1 (the workflow fails loudly).

Needs ``MCP_VERIFY_TOKEN`` (any valid ``MCP_AUTH_TOKENS`` entry's token). Without
it the check prints a skip note and exits 0, so the workflow still passes on
forks / before the secret exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import config  # noqa: F401 — .env in local dev

DEFAULT_SPACE = "munther-hasan/scprs-warehouse-mcp"
DEFAULT_URL = "https://munther-hasan-scprs-warehouse-mcp.hf.space/mcp"

# One row of monotonically growing counts — enough to tell any two builds apart.
# lv_* views exist in the serve DB, so the same SQL runs locally and remotely.
MARKER_SQL = (
    "SELECT (SELECT COUNT(*) FROM lv_fact_document) AS documents, "
    "(SELECT COUNT(*) FROM lv_fact_line) AS line_rows, "
    "(SELECT COUNT(*) FROM lv_fact_document WHERE line_count > 0) AS enriched_docs"
)


def local_markers(serve_db: Path) -> dict:
    """The just-built serve DB's freshness markers (read-only)."""
    con = sqlite3.connect(f"file:{serve_db}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        return dict(con.execute(MARKER_SQL).fetchone())
    finally:
        con.close()


def wait_for_space(space: str, timeout_s: float = 1200, poll_s: float = 20) -> None:
    """Block until the Space's runtime stage is RUNNING (a factory reboot passes
    through a full image rebuild first). Raises TimeoutError / RuntimeError."""
    from huggingface_hub import HfApi

    api = HfApi()
    deadline = time.monotonic() + timeout_s
    stage = "?"
    while time.monotonic() < deadline:
        stage = str(api.space_info(space).runtime.stage)
        if stage == "RUNNING":
            return
        if stage in ("RUNTIME_ERROR", "BUILD_ERROR", "PAUSED"):
            raise RuntimeError(f"{space} entered terminal stage {stage} while waiting")
        time.sleep(poll_s)
    raise TimeoutError(f"{space} still {stage} after {timeout_s:.0f}s")


async def served_markers(url: str, token: str, timeout_s: float = 300, poll_s: float = 15) -> dict:
    """The markers the live Space actually serves, via its `run_sql` MCP tool.

    Retries while the app inside the freshly RUNNING container finishes booting
    (the stage flips to RUNNING slightly before the server accepts requests)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool("run_sql", {"query": MARKER_SQL})
                    payload = res.structuredContent or json.loads(res.content[0].text)
                    return dict(payload["rows"][0])
        except Exception as exc:  # noqa: BLE001 — boot-window retries; re-raised on deadline
            last_exc = exc
            await asyncio.sleep(poll_s)
    raise TimeoutError(f"Space MCP endpoint not serving after {timeout_s:.0f}s: {last_exc}")


def main(argv: list[str] | None = None) -> int:
    from . import warehouse

    ap = argparse.ArgumentParser(description="Verify the deployed Space serves this build")
    ap.add_argument("--space", default=DEFAULT_SPACE)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--serve-db", type=Path, default=warehouse.SERVE_DB)
    ap.add_argument("--boot-timeout", type=float, default=1200, help="seconds to wait for RUNNING")
    ap.add_argument("--serve-timeout", type=float, default=300, help="seconds to wait for the app")
    args = ap.parse_args(argv)

    token = os.environ.get("MCP_VERIFY_TOKEN")
    if not token:
        print("go-live check SKIPPED: MCP_VERIFY_TOKEN not set")
        return 0

    expected = local_markers(args.serve_db)
    print(f"expected (this build): {expected}")
    wait_for_space(args.space, timeout_s=args.boot_timeout)
    served = asyncio.run(served_markers(args.url, token, timeout_s=args.serve_timeout))
    print(f"served   (live Space): {served}")

    if served == expected:
        print("go-live VERIFIED: the Space serves this build")
        return 0
    print(
        "go-live FAILED: the Space is serving a different snapshot than this run "
        "published — the restart did not take effect or fetched a stale revision"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
