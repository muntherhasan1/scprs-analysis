"""Offline tests for the out-of-band serve-DB staleness check."""

import os
import shutil
import subprocess  # nosec B404 - runs the workflow step's own script in a test
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import huggingface_hub
import pytest
import yaml

from src import pipeline_monitor


def _fake_api(created_at):
    class FakeApi:
        def list_repo_commits(self, repo, repo_type, token=None):
            assert repo_type == "dataset"
            return [SimpleNamespace(created_at=created_at, commit_id="new")]

    return FakeApi


def test_serve_age_hours_fresh(monkeypatch):
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    monkeypatch.setattr(huggingface_hub, "HfApi", _fake_api(two_hours_ago))
    age = pipeline_monitor.serve_age_hours("x/serve")
    assert 1.9 < age < 2.1


def test_serve_age_hours_naive_datetime_treated_as_utc(monkeypatch):
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    monkeypatch.setattr(huggingface_hub, "HfApi", _fake_api(naive))
    age = pipeline_monitor.serve_age_hours("x/serve")
    assert 2.9 < age < 3.1


def test_cli_ok_and_stale(monkeypatch, capsys):
    monkeypatch.setattr(pipeline_monitor, "serve_age_hours", lambda repo, token=None: 5.0)
    assert pipeline_monitor._cli(["serve-age", "--max-hours", "14"]) == 0
    assert capsys.readouterr().out.startswith("OK ")

    monkeypatch.setattr(pipeline_monitor, "serve_age_hours", lambda repo, token=None: 20.0)
    assert pipeline_monitor._cli(["serve-age", "--max-hours", "14"]) == 1
    assert capsys.readouterr().out.startswith("STALE ")


def test_cli_unreadable_dataset_is_a_finding(monkeypatch, capsys):
    """A rotated token / deleted repo must read as STALE (exit 1), never a pass."""

    def boom(repo, token=None):
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(pipeline_monitor, "serve_age_hours", boom)
    assert pipeline_monitor._cli(["serve-age"]) == 1
    assert "unreadable" in capsys.readouterr().out


def _monitor_steps():
    wf = yaml.safe_load(Path(".github/workflows/pipeline-monitor.yml").read_text(encoding="utf-8"))
    return wf["jobs"]["monitor"]["steps"]


def _stamp(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_rare_writer_step_flags_stale_and_missing_runs(tmp_path):
    """Runs the workflow step's own bash against a fake `gh`: CMAS last succeeded
    70h ago (> 57h), eProcure 30h ago (fresh), the backup never."""
    script = next(s for s in _monitor_steps() if s.get("id") == "rare")["run"]
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        f'  *"CMAS refresh"*) echo {_stamp(70)} ;;\n'
        f'  *"eProcure"*) echo {_stamp(30)} ;;\n'
        '  *) echo "" ;;\n'
        "esac\n",
        newline="\n",
    )
    gh.chmod(0o755)
    (tmp_path / "step.sh").write_text(script, encoding="utf-8", newline="\n")
    out = tmp_path / "out.txt"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": "out.txt",
    }
    subprocess.run([shutil.which("bash"), "step.sh"], cwd=tmp_path, env=env, check=True)  # noqa: S603
    finding = out.read_text(encoding="utf-8")
    assert "'CMAS refresh (Wave 2)' run was 70h ago (threshold 57h)" in finding
    assert "eProcure" not in finding
    assert "no successful 'Operational backup' run" in finding


def test_rare_writer_finding_gates_alert_ping_and_recovery():
    by_name = {s["name"]: s.get("if", "") for s in _monitor_steps()}
    assert "steps.rare.outputs.finding != ''" in by_name["Alert on findings"]
    assert "steps.rare.outputs.finding != ''" in by_name["Fail the run on findings"]
    assert "steps.rare.outputs.finding == ''" in by_name["Dead-man's switch ping"]
    assert "steps.rare.outputs.finding == ''" in by_name["Resolve on recovery"]
