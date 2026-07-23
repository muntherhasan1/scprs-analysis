"""Automated pipeline-failure triage — Wave 4 (self-triaging pipeline).

When an unattended pipeline workflow fails, the `Pipeline triage` GitHub workflow
feeds this module the run context (workflow name, failed step names, run URL) and
posts the result as a GitHub issue. This module is the **triage brain**: a
self-contained hints map that encodes the operational knowledge we've accumulated
— what each failure *means* and what to check — so a failure becomes an actionable
issue, not just a red X. No LLM, no network here; pure `(context) -> issue text`.

The issue is idempotent (a hidden marker per workflow lets the workflow update an
existing open issue instead of spamming) and auto-closes when a later run of the
same workflow succeeds — so the issue list reflects *current* pipeline health.

    python -m src.triage report --workflow "Enrich (Wave 2)" \
        --run-url <url> --steps "Wave-1 checks (gate the publish)"
    # -> {"title": ..., "body": ..., "marker": ...} as JSON
"""

from __future__ import annotations

import argparse
import json

# Per-workflow triage hints. `_default` frames the failure; the other keys are
# substrings matched against the failed step name(s) to add specific guidance.
_HINTS: dict[str, dict[str, str]] = {
    "Enrich (Wave 2)": {
        "_default": (
            "The scheduled enrichment run failed. `scprs.db` is only published on "
            "success (upload-on-success), so no partial data was written and the "
            "next cron retries safely."
        ),
        "Enrich one slice": (
            "The headless scraper failed — the FI$Cal SCPRS site may be throttling "
            "or blocking Actions' cloud IPs, or a page changed. A transient failure "
            "self-heals on the next run; a persistent one needs a look at "
            "`src/scprs.py` (the parse quirks) and the run logs."
        ),
        "Wave-1 checks": (
            "An **integrity gate** tripped: `contracts` (a monotonic metric shrank) "
            "or `canary` (live parse-drift vs the golden fixture). The publish was "
            "correctly blocked — this is a real data-quality signal. Inspect the "
            "enriched data / `dw_dq_results` before the next run."
        ),
        "Rebuild warehouse": (
            "The warehouse build failed its error-tier DQ gate — check "
            "`dw_dq_results`. `scprs.db` is already published, so enrichment "
            "progress is safe; only the serve refresh was blocked."
        ),
        "Publish serve DB": (
            "The serve-DB publish failed — check the `HF_WAREHOUSE_TOKEN` scope and "
            "the serve dataset."
        ),
        "Verify go-live": (
            "The Space isn't verifiably serving the new build. Exit codes are "
            "evidence-graded: a **verified mismatch** (rc=1) triggers auto-rollback "
            "to the last-good serve revision; an **inconclusive** check (rc=2 — boot "
            "timeout / unreachable) fails without touching the publish. Check the "
            "step log for which case this was, then the Space's runtime stage."
        ),
        "restart-token problem": (
            "The MCP Space restart never happened — almost always a missing/rotated/"
            "mis-scoped `HF_DEPLOY_TOKEN`. The data IS published and nothing was "
            "rolled back; fix the token (write scope on the Space repo) or reboot the "
            "Space manually to serve the new snapshot."
        ),
        "canary target-not-found": (
            "The canary's fixture document is no longer findable on the site (archive "
            "purge or availability change) — the parser is unproven, not indicted, and "
            "the run's enrichment WAS published. Recapture the fixture from an "
            "already-drilled document: `python -m src.canary --capture --document <id>`."
        ),
    },
    "CMAS refresh (Wave 2)": {
        "_default": (
            "The CMAS refresh failed. `cmas.db` is only published on success, so "
            "nothing overwrote the good copy."
        ),
        "Extract CMAS": (
            "The CMAS Power BI extract failed — the US-Gov Power BI endpoint may be "
            "blocking Actions' cloud IPs, or the embed / model schema changed. See "
            "`docs/CMAS.md` and re-run the recon (token → modelsAndExploration → "
            "QES query)."
        ),
        "Sanity-check": (
            "The extract returned 0 rows — the source or the query shape changed; "
            "nothing was published (upload-on-success held)."
        ),
    },
    "MCP image": {
        "_default": (
            "The MCP server container failed to build or boot its smoke test on "
            "main — the **next auto-deploy of the Space will likely break**. Check "
            "the Docker build log first; if the build passed but boot failed, it's "
            "usually a new module-level import of a file not in `deploy.py`'s "
            "`COPIES` or a dep missing from `requirements-mcp.txt`."
        ),
    },
    "CI": {
        "_default": (
            "Main-branch CI failed (lint / bandit / pip-audit / secret scan / "
            "tests). If the merge was green, the likely cause is `pip-audit`: a "
            "newly published CVE fails a scheduled run with no code change — "
            "bump or pin the affected dependency."
        ),
    },
    "Deploy MCP Space": {
        "_default": "The MCP Space code deploy failed.",
        "Deploy MCP Space image": (
            "The upload / settings step failed — check the `HF_DEPLOY_TOKEN` scope "
            "(content + settings write on the Space repo)."
        ),
        "Verify deploy went live": (
            "The Space did not come back RUNNING + healthy on the new commit — "
            "likely BUILD_ERROR / RUNTIME_ERROR. **Most common cause:** a new "
            "module-level import of a file not in `deploy.py`'s `COPIES`, or a dep "
            "missing from `requirements-mcp.txt` (see the 2026-07-21 `config.py` "
            "incident). Verify the shipped boot chain imports cleanly."
        ),
    },
}


def build_report(
    workflow: str, failed_steps: list[str], run_url: str, conclusion: str = "failure"
) -> dict[str, str]:
    """Build the triage issue (title, body, marker) for a failed workflow run."""
    hints = _HINTS.get(workflow, {})
    steps = [s.strip() for s in failed_steps if s.strip()]

    specific: list[str] = []
    if conclusion == "cancelled":
        # A `timeout-minutes` kill concludes `cancelled`, not `failure` — the
        # signature of a hung or over-budget step (the 2026-07 enrich livelock:
        # 12 straight 90-min kills, silently untriaged). Frame it first.
        specific.append(
            "The run was **cancelled — most likely the job `timeout-minutes` killing a "
            "hung or over-budget step** (a manual cancel looks identical). Steps that "
            "publish on success did NOT run, so no partial data was written — but that "
            "also means repeated timeouts make **zero progress** and will recur until "
            "the underlying slowness is fixed. Check how far the killed step's log got."
        )
    for step in steps:
        for key, hint in hints.items():
            if key != "_default" and key.lower() in step.lower() and hint not in specific:
                specific.append(hint)
    if not specific and hints.get("_default"):
        specific.append(hints["_default"])
    if not specific:
        specific.append("No triage hint for this workflow — inspect the run logs.")

    verb = "was cancelled (timeout?)" if conclusion == "cancelled" else "failed"
    marker = f"<!-- pipeline-failure:{workflow} -->"
    steps_line = ", ".join(f"`{s}`" for s in steps) if steps else "_unknown — see the run_"
    body = (
        f"{marker}\n"
        f"## ⚠️ `{workflow}` {verb}\n\n"
        f"**Run:** {run_url}\n\n"
        f"**Failed step(s):** {steps_line}\n\n"
        f"### Likely cause & what to check\n" + "\n".join(f"- {h}" for h in specific) + "\n\n---\n"
        "_Automated triage. This issue **auto-closes** when a later run of this "
        "workflow succeeds._"
    )
    return {"title": f"⚠️ Pipeline failure: {workflow}", "body": body, "marker": marker}


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Build a pipeline-failure triage issue.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report", help="Emit the triage issue as JSON")
    rep.add_argument("--workflow", required=True)
    rep.add_argument("--run-url", required=True)
    rep.add_argument("--steps", default="", help="comma-separated failed step names")
    rep.add_argument(
        "--conclusion",
        default="failure",
        help="workflow_run conclusion (`failure` or `cancelled` — a timeout kill)",
    )
    args = ap.parse_args()
    if args.cmd == "report":
        report = build_report(args.workflow, args.steps.split(","), args.run_url, args.conclusion)
        print(json.dumps(report))


if __name__ == "__main__":
    _cli()
