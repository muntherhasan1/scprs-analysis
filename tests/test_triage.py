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
            "Enrich (Wave 2)",
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
