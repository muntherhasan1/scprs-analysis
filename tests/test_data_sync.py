"""Tests for the serve-DB sync between the pipeline and the deployed Spaces."""

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


def test_publish_serve_db_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        data_sync.publish_serve_db(tmp_path / "nope.db", "acme/data")
