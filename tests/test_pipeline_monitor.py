"""Offline tests for the out-of-band serve-DB staleness check."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import huggingface_hub

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
