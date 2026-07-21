"""Verify a Space *code* deploy went live — Wave 3 (continuous code delivery).

`golive_check` verifies the Space serves fresh *data* (the serve DB markers). This
is its code-deploy sibling: after `deploy/hf-space/deploy.py` uploads new code and
the Space rebuilds its image, confirm the **new build is actually live** — the
Space is RUNNING on the just-pushed commit's sha (not the old image), and its
`/healthz` endpoint answers 200. A rebuild that fails (BUILD_ERROR / RUNTIME_ERROR)
or never becomes healthy fails the check loudly, so a broken deploy can't pass
silently.

    python -m src.deploy_check --space munther-hasan/scprs-warehouse-mcp

The expected sha defaults to the Space repo's latest commit (the one deploy.py just
pushed); pass `--sha` to pin it explicitly. The healthz URL is derived from the
Space id (`<user>-<name>.hf.space/healthz`) unless given.
"""

from __future__ import annotations

import argparse
import sys
import time

DEFAULT_SPACE = "munther-hasan/scprs-warehouse-mcp"


def _healthz_url(space: str) -> str:
    # HF serves a Space at https://<user>-<name>.hf.space
    return f"https://{space.replace('/', '-')}.hf.space/healthz"


def _runtime_sha(runtime) -> str | None:
    """The running image's source commit sha. Exposed on the runtime's raw dict in
    current huggingface_hub; fall back to a direct attribute for older versions."""
    raw = getattr(runtime, "raw", None)
    if isinstance(raw, dict) and raw.get("sha"):
        return raw["sha"]
    return getattr(runtime, "sha", None)


def wait_for_build(space, expected_sha, timeout_s=1500, poll_s=20, api=None) -> None:
    """Block until the Space is RUNNING on ``expected_sha``. Raises on a terminal
    build/runtime error or timeout (so a broken deploy fails loudly)."""
    from huggingface_hub import HfApi

    api = api or HfApi()
    deadline = time.monotonic() + timeout_s
    stage = "?"
    while time.monotonic() < deadline:
        rt = api.space_info(space).runtime
        stage = str(rt.stage)
        if stage in ("BUILD_ERROR", "RUNTIME_ERROR", "PAUSED"):
            raise RuntimeError(f"{space} entered terminal stage {stage} during deploy")
        if stage == "RUNNING" and _runtime_sha(rt) == expected_sha:
            return
        time.sleep(poll_s)
    raise TimeoutError(
        f"{space} not RUNNING on {expected_sha[:8]} after {timeout_s:.0f}s (stage {stage})"
    )


def check_healthz(url, timeout_s=180, poll_s=10) -> None:
    """Poll ``url`` until it returns 200 (the app finished booting after RUNNING)."""
    import httpx

    deadline = time.monotonic() + timeout_s
    last: object = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=15)
            if r.status_code == 200:
                return
            last = r.status_code
        except Exception as exc:  # noqa: BLE001 — boot-window retries; re-raised on deadline
            last = repr(exc)
        time.sleep(poll_s)
    raise TimeoutError(f"{url} not healthy after {timeout_s:.0f}s (last: {last})")


def verify_deploy(space, healthz_url=None, expected_sha=None) -> str:
    """Confirm the Space is RUNNING on the deployed commit and serving. Returns sha."""
    from huggingface_hub import HfApi

    api = HfApi()
    if expected_sha is None:
        expected_sha = api.list_repo_commits(space, repo_type="space")[0].commit_id
    wait_for_build(space, expected_sha, api=api)
    check_healthz(healthz_url or _healthz_url(space))
    return expected_sha


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify a Space code deploy is live and healthy.")
    ap.add_argument("--space", default=DEFAULT_SPACE)
    ap.add_argument("--healthz-url", default=None, help="default: derived from --space")
    ap.add_argument("--sha", default=None, help="deployed commit (default: the Space's latest)")
    args = ap.parse_args(argv)
    sha = verify_deploy(args.space, args.healthz_url, expected_sha=args.sha)
    print(f"deploy verified: {args.space} RUNNING on {sha[:8]}, /healthz ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
