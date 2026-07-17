"""Tests for the serve-DB sync between the pipeline and the deployed Spaces."""

import sys

import pytest

from src import data_sync


def test_ensure_local_db_noop_without_dataset(tmp_path, monkeypatch):
    monkeypatch.delenv("WAREHOUSE_DATASET", raising=False)
    dest = tmp_path / "warehouse.db"
    assert data_sync.ensure_local_db(dest) is False
    assert not dest.exists()  # nothing fetched — caller falls back to a local DB


def test_ensure_local_db_atomic_fetch(tmp_path, monkeypatch):
    import huggingface_hub

    remote = tmp_path / "warehouse-serve.db"
    remote.write_bytes(b"serve-db-bytes")
    monkeypatch.setenv("WAREHOUSE_DATASET", "acme/data")
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda **kw: str(remote))

    dest = tmp_path / "out" / "warehouse.db"
    assert data_sync.ensure_local_db(dest) is True
    assert dest.read_bytes() == b"serve-db-bytes"
    assert not (dest.parent / (dest.name + ".tmp")).exists()  # temp renamed away, not left behind


def test_ensure_local_db_wraps_fetch_error_with_actionable_message(tmp_path, monkeypatch):
    import huggingface_hub

    monkeypatch.setenv("WAREHOUSE_DATASET", "acme/data")

    def boom(**kw):
        raise RuntimeError("401 Client Error: Repository Not Found")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", boom)
    with pytest.raises(data_sync.WarehouseFetchError) as ei:
        data_sync.ensure_local_db(tmp_path / "warehouse.db")
    assert "HF_TOKEN" in str(ei.value) and "READ" in str(ei.value)  # names the likely cause


def test_publish_serve_db_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        data_sync.publish_serve_db(tmp_path / "nope.db", "acme/data")


def test_publish_token_precedence(monkeypatch):
    monkeypatch.delenv("HF_WAREHOUSE_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert data_sync._publish_token() is None
    monkeypatch.setenv("HF_TOKEN", "cached-login")
    assert data_sync._publish_token() == "cached-login"  # falls back to HF_TOKEN
    monkeypatch.setenv("HF_WAREHOUSE_TOKEN", "dataset-write")
    assert data_sync._publish_token() == "dataset-write"  # dedicated token wins


def test_publish_cli_prefers_warehouse_token(monkeypatch):
    # The publish CLI must pass the dedicated warehouse-data token to publish_serve_db,
    # so a run never falls back to a Space-only cached login (which 403s on the dataset).
    monkeypatch.setenv("HF_WAREHOUSE_TOKEN", "dataset-write")
    monkeypatch.setenv("HF_TOKEN", "cached-login")
    captured = {}

    def recorder(serve_path, repo, token=None):
        captured.update(repo=repo, passed=token)
        return "https://hf/commit/abc"

    monkeypatch.setattr(data_sync, "publish_serve_db", recorder)
    monkeypatch.setattr(sys, "argv", ["data_sync", "publish", "--dataset", "acme/data"])
    data_sync._cli()
    assert captured["repo"] == "acme/data"
    assert captured["passed"] == "dataset-write"


def test_publish_cli_falls_back_to_hf_token(monkeypatch):
    monkeypatch.delenv("HF_WAREHOUSE_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", "cached-login")
    captured = {}

    def recorder(serve_path, repo, token=None):
        captured.update(passed=token)
        return "https://hf/commit/abc"

    monkeypatch.setattr(data_sync, "publish_serve_db", recorder)
    monkeypatch.setattr(sys, "argv", ["data_sync", "publish", "--dataset", "acme/data"])
    data_sync._cli()
    assert captured["passed"] == "cached-login"
