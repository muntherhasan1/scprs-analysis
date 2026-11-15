"""Offline tests for the pipeline-failure triage brain (Wave 4)."""

import json

from src import triage


def test_report_matches_specific_step_hint():
    r = triage.build_report(
        "Enrich (Wave 2)",
        ["Wave-1 checks (gate the publish)"],
        "https://run/1",
    )
    assert r["title"] == "⚠️ Pipeline failure: Enrich (Wave 2)"
    assert r["marker"] == "<!-- pipeline-failure:Enrich (Wave 2) -->"
    # marker leads the body (used for idempotent issue matching).
    assert r["body"].startswith(r["marker"])
    assert "integrity gate" in r["body"]  # the Wave-1 checks hint
    assert "https://run/1" in r["body"]
    assert "`Wave-1 checks (gate the publish)`" in r["body"]


def test_report_deploy_verify_names_the_packaging_gap():
    r = triage.build_report("Deploy MCP Space", ["Verify deploy went live"], "https://run/2")
    # encodes the 2026-07-21 incident lesson.
    assert "COPIES" in r["body"] and "requirements-mcp.txt" in r["body"]


def test_report_covers_ground_truth_recon():
    r = triage.build_report("Ground-truth recon", ["Probe the live site"], "https://run/15")
    # the repair path is the load-bearing hint.
    assert "Summary build" in r["body"] and "SETTLED month" in r["body"]


def test_report_covers_summary_build():
    r = triage.build_report(
        "Summary build", ["Build summaries (per-unit shrink gate)"], "https://run/13"
    )
    # the replace-semantics footgun is the load-bearing hint.
    assert "REPLACES" in r["body"] and "shrink gate" in r["body"]
    r = triage.build_report("Summary build", ["Set up job"], "https://run/14")
    assert "untouched" in r["body"]


def test_report_covers_query_review():
    r = triage.build_report("Query review", ["Replay logged questions"], "https://run/11")
    # infra vs content regression is the load-bearing distinction.
    assert "GEMINI_API_KEY" in r["body"] and "content regression" in r["body"]
    r = triage.build_report("Query review", ["Set up job"], "https://run/12")
    assert "Read-only job" in r["body"]  # the _default frames it


def test_report_covers_warehouse_refresh():
    r = triage.build_report(
        "Warehouse refresh (Wave 2c)",
        ["Rebuild warehouse + export serve DB"],
        "https://run/16",
        conclusion="cancelled",
    )
    # the growth diagnosis path is the load-bearing hint (the 2026-08 kills).
    assert "build-timings trend" in r["body"] and "dw_dq_results" in r["body"]
    assert "was cancelled (timeout?)" in r["body"]
    assert r["marker"] == "<!-- pipeline-failure:Warehouse refresh (Wave 2c) -->"
    # read-only over scprs.db — a failed refresh must never implicate enrichment.
    r = triage.build_report("Warehouse refresh (Wave 2c)", ["Set up job"], "https://run/17")
    assert "no enrichment progress is at risk" in r["body"]


def test_report_falls_back_to_default_when_no_step_matches():
    r = triage.build_report("CMAS refresh (Wave 2)", ["Set up job"], "https://run/3")
    # no specific hint for "Set up job" -> the workflow's _default frames it.
    assert "nothing overwrote the good copy" in r["body"]


def test_report_handles_no_failed_steps():
    r = triage.build_report("Enrich (Wave 2)", [], "https://run/4")
    assert "_unknown — see the run_" in r["body"]
    assert "upload-on-success" in r["body"]  # still gives the _default context


def test_report_dedupes_hints_across_steps():
    # two steps mapping to the same key must not duplicate the hint.
    r = triage.build_report(
        "Deploy MCP Space",
        ["Verify deploy went live", "Verify deploy went live (retry)"],
        "https://run/5",
    )
    assert r["body"].count("Most common cause") == 1


def test_report_unknown_workflow_is_graceful():
    r = triage.build_report("Some Other Workflow", ["step"], "https://run/6")
    assert "No triage hint" in r["body"]


def test_report_covers_mcp_image_and_ci():
    r = triage.build_report("MCP image", ["Build image"], "https://run/9")
    assert "next auto-deploy" in r["body"]
    r = triage.build_report("CI", ["Dependency vulnerability scan (pip-audit)"], "https://run/10")
    assert "pip-audit" in r["body"]


def test_report_cancelled_frames_timeout_and_keeps_step_hint():
    """A `timeout-minutes` kill concludes `cancelled` — the report must lead with
    the timeout framing (the 2026-07 enrich livelock lesson) and still include
    the step-specific hint for the killed step."""
    r = triage.build_report(
        "Enrich (Wave 2)",
        ["Enrich one slice (PO Details drill-down)"],
        "https://run/8",
        conclusion="cancelled",
    )
    assert "was cancelled (timeout?)" in r["body"]
    assert "timeout-minutes" in r["body"] and "zero progress" in r["body"]
    assert "headless scraper" in r["body"]  # the "Enrich one slice" hint still matches
    # idempotency marker unchanged, so an existing open issue still matches.
    assert r["marker"] == "<!-- pipeline-failure:Enrich (Wave 2) -->"


def test_cli_report_emits_json(capsys, monkeypatch):
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "triage",
            "report",
            "--workflow",
            "Warehouse refresh (Wave 2c)",
            "--run-url",
            "https://run/7",
            "--steps",
            "Verify go-live (Space serves this build)",
        ],
    )
    triage._cli()
    out = json.loads(capsys.readouterr().out)
    assert out["title"].startswith("⚠️ Pipeline failure")
    assert "evidence-graded" in out["body"]  # the go-live hint


def test_every_hinted_workflow_is_watched_by_triage():
    """A workflow with triage hints but missing from triage.yml's workflow_run
    list fails silently (Summary refresh, 2026-10-27..29: three red runs, no
    issue). Keep the two in lockstep."""
    from pathlib import Path

    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/triage.yml").read_text(
        encoding="utf-8"
    )
    watched = wf.split("workflows:", 1)[1].split("types:", 1)[0]
    missing = [name for name in triage._HINTS if f'"{name}"' not in watched]
    assert missing == []


def _job(start, end, steps=1):
    return {"started_at": start, "completed_at": end, "steps": [{}] * steps}


def test_classify_cancel_uses_job_time_not_queue_time():
    # CMAS run 40331142677: queued 46 min behind the writer lock, job ran 1 min.
    # The old run-level rule (startedAt == createdAt) counted the queue wait.
    short = [_job("2026-10-26T14:16:20Z", "2026-10-26T14:17:20Z")]
    assert triage.classify_cancel(short, 20) == "interrupted"
    # A CMAS timeout kill at its 20-min limit — under the old 30-min rule, so
    # it used to be skipped as "superseded".
    killed = [_job("2026-10-26T14:16:20Z", "2026-10-26T14:36:21Z")]
    assert triage.classify_cancel(killed, 20) == "timeout"
    # Never started: replaced as the group's single pending run.
    assert triage.classify_cancel([], 150) == "dropped"
    assert triage.classify_cancel([_job(None, None, steps=0)], 150) == "dropped"


def test_every_writer_alert_and_timeout_is_parseable_from_its_workflow():
    from pathlib import Path

    wfdir = Path(__file__).resolve().parent.parent / ".github/workflows"
    limits = {}
    for path in wfdir.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        name = text.split("name:", 1)[1].splitlines()[0].strip()
        limits[name] = triage.workflow_timeout_minutes(text)
    for name in triage.QUEUE_DROP_ALERTS:
        assert limits.get(name), f"{name}: queue-drop alert for a workflow with no timeout"
    assert limits["CMAS refresh (Wave 2)"] == 20


def test_dropped_run_report_explains_the_one_pending_rule():
    r = triage.build_report("Summary refresh", [], "https://run/9", conclusion="dropped")
    assert "was dropped from the writer queue" in r["body"]
    assert "ONE pending" in r["body"]
    assert r["marker"] == "<!-- pipeline-failure:Summary refresh -->"  # same issue key


def test_cli_classify_cancel_skips_routine_drops(tmp_path, capsys, monkeypatch):
    import sys

    wf = tmp_path / "wf.yml"
    wf.write_text("jobs:\n  x:\n    timeout-minutes: 95\n", encoding="utf-8")
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": []}), encoding="utf-8")

    def run(workflow):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "triage",
                "classify-cancel",
                "--workflow",
                workflow,
                "--workflow-file",
                str(wf),
                "--jobs-json",
                str(jobs),
            ],
        )
        triage._cli()
        return capsys.readouterr().out.strip()

    assert run("Summary refresh") == "dropped"
    assert run("Enrich (Wave 2)") == "skip"  # fires several times a day


# -------------------------------------------------------------- failure evidence

_DIGEST = (
    "failure evidence [events-historical-2026-attempt1]: title='Event Search'; "
    "url=https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx; "
    "1 request(s) in flight, oldest 118s: POST https://caleprocure.ca.gov/nlx3/psc/x"
)


def test_report_quotes_failure_evidence_and_links_the_artifact():
    r = triage.build_report(
        "eProcure registry refresh",
        ["Extract events (Posted + recent Historical years)"],
        "https://run/9",
        evidence=[_DIGEST],
        evidence_url="https://run/9/artifacts/77",
    )
    body = r["body"]
    assert "### What the page showed" in body
    assert f"```text\n{_DIGEST}\n```" in body
    assert "https://run/9/artifacts/77" in body
    # evidence leads: it comes before the generic hints
    assert body.index("What the page showed") < body.index("Likely cause")


def test_report_evidence_is_fenced_capped_and_cannot_break_out():
    """Page text comes from an outside site: it must stay inside the code fence
    (no rendered markdown, no @mentions) and the section stays bounded."""
    hostile = "failure evidence [x]: title='```\n@someone **bold**'" + "y" * 2000
    r = triage.build_report("CI", [], "https://run/1", evidence=[hostile] * 12)
    section = r["body"].split("### What the page showed", 1)[1].split("###", 1)[0]
    assert section.count("```") == 2  # only our own fence
    assert section.count("failure evidence [x]") == triage.EVIDENCE_LINES
    assert all(len(line) <= triage.EVIDENCE_LINE_CHARS for line in section.splitlines())


def test_report_without_evidence_is_unchanged():
    r = triage.build_report("CI", [], "https://run/1")
    assert "What the page showed" not in r["body"]


def test_triage_step_gathers_evidence_from_the_failed_run(tmp_path):
    """Runs triage.yml's own step against a fake `gh`: the evidence line in the
    failed job's log and the failure-evidence artifact reach the new issue."""
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parent.parent
    wf = yaml.safe_load((repo / ".github/workflows/triage.yml").read_text(encoding="utf-8"))
    script = next(s for s in wf["jobs"]["triage"]["steps"] if s["name"] == "Triage")["run"]
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  "api repos/o/r/actions/runs/1/jobs"*) echo 11 ;;\n'
        f'  "api repos/o/r/actions/jobs/11/logs"*) cat "{tmp_path / "job.log"}" ;;\n'
        '  "api repos/o/r/actions/runs/1/artifacts"*) echo 77 ;;\n'
        '  "issue list"*) echo "" ;;\n'
        '  "run view"*) echo "Extract events (Posted + recent Historical years)" ;;\n'
        '  "issue create"*) shift 2; while [ $# -gt 0 ]; do\n'
        '      [ "$1" = "--body" ] && printf "%s" "$2" > body.md; shift; done ;;\n'
        "esac\n",
        newline="\n",
    )
    gh.chmod(0o755)
    (tmp_path / "job.log").write_text(
        "2026-11-10T13:16:12.8123Z events historical:2026: attempt failed\n"
        f"2026-11-10T13:16:12.9123Z {_DIGEST}\n",
        encoding="utf-8",
    )
    (tmp_path / "step.sh").write_text(script, encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "PATH": os.pathsep.join(
            [str(tmp_path), str(Path(sys.executable).parent), os.environ["PATH"]]
        ),
        "PYTHONPATH": str(repo),
        "GITHUB_REPOSITORY": "o/r",
        "WF": "eProcure registry refresh",
        "WF_PATH": ".github/workflows/eprocure-refresh.yml",
        "CONCLUSION": "failure",
        "RUN_URL": "https://github.com/o/r/actions/runs/1",
        "RUN_ID": "1",
    }
    subprocess.run([shutil.which("bash"), "step.sh"], cwd=tmp_path, env=env, check=True)  # noqa: S603
    body = (tmp_path / "body.md").read_text(encoding="utf-8")
    assert _DIGEST in body
    assert "https://github.com/o/r/actions/runs/1/artifacts/77" in body
