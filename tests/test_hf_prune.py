"""Dataset-history pruning (data_sync.prune_history + hf-storage-prune.yml)."""

import pytest

from src import data_sync


def _fake_prune_api(monkeypatch, *, n_commits=30, head="h1", moved_to=None, after_tree=None):
    """Fake HfApi for prune_history; records squash calls."""
    import huggingface_hub

    calls = {"squash": 0}
    tree = {"scprs.db": "blob-db", "cmas.db": "blob-cmas"}

    class Commit:
        def __init__(self, cid):
            self.commit_id = cid

    class FakeApi:
        def __init__(self, token=None):
            pass

        def list_repo_commits(self, repo, repo_type=None, token=None):
            if calls["squash"]:
                return [Commit("squashed")]
            return [Commit(head)] + [Commit(f"c{i}") for i in range(n_commits - 1)]

        def dataset_info(self, repo, token=None, expand=None):
            return type("Info", (), {"sha": moved_to or head, "used_storage": 483_400_000_000})()

        def list_repo_refs(self, repo, repo_type=None, token=None):
            return type("Refs", (), {"branches": [], "converts": [], "tags": []})()

        def list_repo_tree(self, repo, recursive=True, revision=None, repo_type=None, token=None):
            t = after_tree if (calls["squash"] and after_tree is not None) else tree
            return [type("F", (), {"path": p, "blob_id": b})() for p, b in t.items()]

        def super_squash_history(self, repo, **kw):
            calls["squash"] += 1
            calls["kw"] = kw

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    return calls


def test_prune_is_a_dry_run_by_default(monkeypatch):
    calls = _fake_prune_api(monkeypatch)
    out = data_sync.prune_history("acme/op", token="t")  # noqa: S106 - fake
    assert out["action"] == "dry-run" and out["commits"] == 30
    assert out["used_storage_gb"] == 483.4
    assert calls["squash"] == 0


def test_prune_skips_short_histories(monkeypatch):
    calls = _fake_prune_api(monkeypatch, n_commits=5)
    out = data_sync.prune_history("acme/op", token="t", apply=True)  # noqa: S106 - fake
    assert out["action"] == "skip" and calls["squash"] == 0


def test_prune_apply_squashes_main_and_verifies_files(monkeypatch):
    calls = _fake_prune_api(monkeypatch)
    out = data_sync.prune_history("acme/op", token="t", apply=True)  # noqa: S106 - fake
    assert out["action"] == "squashed" and out["commits_after"] == 1
    assert calls["squash"] == 1
    assert calls["kw"]["branch"] == "main" and calls["kw"]["repo_type"] == "dataset"


def test_prune_refuses_when_head_moved(monkeypatch):
    calls = _fake_prune_api(monkeypatch, moved_to="h2")
    with pytest.raises(data_sync.PruneError, match="HEAD moved"):
        data_sync.prune_history("acme/op", token="t", apply=True)  # noqa: S106 - fake
    assert calls["squash"] == 0


def test_prune_fails_loudly_if_files_change(monkeypatch):
    _fake_prune_api(monkeypatch, after_tree={"scprs.db": "blob-OTHER"})
    with pytest.raises(data_sync.PruneError, match="files differ"):
        data_sync.prune_history("acme/op", token="t", apply=True)  # noqa: S106 - fake


def test_prune_workflow_is_report_only_unless_opted_in_and_holds_writer_locks():
    from pathlib import Path

    wf = (
        Path(__file__).resolve().parent.parent / ".github/workflows/hf-storage-prune.yml"
    ).read_text(encoding="utf-8")
    # Squashing is irreversible: dispatch defaults to report-only, and a schedule
    # only applies with an explicit repo variable.
    assert "default: false" in wf
    assert "vars.HF_PRUNE_APPLY == 'true'" in wf
    # An applying job serializes with that dataset's CAS publishers.
    assert "&& 'scprs-operational-writer' ||" in wf
    assert "&& 'warehouse-serve-publisher' ||" in wf
    assert "--apply" in wf and "Guard -- a recent successful backup exists" in wf
    assert "Guard -- the last warehouse refresh succeeded" in wf
