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


# --------------------------------------------------------------- operational DB (Wave 2)


def test_fetch_operational_db_noop_without_dataset(tmp_path, monkeypatch):
    monkeypatch.delenv("SCPRS_DATASET", raising=False)
    dest = tmp_path / "scprs.db"
    assert data_sync.fetch_operational_db(dest) is False
    assert not dest.exists()  # nothing fetched — local dev keeps its own scprs.db


def test_fetch_operational_db_atomic_fetch(tmp_path, monkeypatch):
    import huggingface_hub

    remote = tmp_path / "scprs.db"
    remote.write_bytes(b"operational-db-bytes")
    monkeypatch.setenv("SCPRS_DATASET", "acme/scprs-operational-db")
    monkeypatch.setenv("HF_SCPRS_TOKEN", "op-token")
    captured = {}

    def fake_download(**kw):
        captured.update(kw)
        return str(remote)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    dest = tmp_path / "out" / "scprs.db"
    assert data_sync.fetch_operational_db(dest) is True
    assert dest.read_bytes() == b"operational-db-bytes"
    assert not (dest.parent / (dest.name + ".tmp")).exists()  # temp renamed away
    assert captured["filename"] == "scprs.db"
    assert captured["token"] == "op-token"  # noqa: S105 — test literal, dedicated op token used


def test_fetch_operational_db_wraps_error(tmp_path, monkeypatch):
    import huggingface_hub

    def boom(**kw):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", boom)
    with pytest.raises(data_sync.WarehouseFetchError) as ei:
        data_sync.fetch_operational_db(tmp_path / "scprs.db", repo="acme/scprs-operational-db")
    assert "READ" in str(ei.value)


def test_publish_operational_db_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        data_sync.publish_operational_db(tmp_path / "nope.db", "acme/scprs-operational-db")


def test_operational_token_precedence(monkeypatch):
    monkeypatch.delenv("HF_SCPRS_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert data_sync._operational_token() is None
    monkeypatch.setenv("HF_TOKEN", "cached-login")
    assert data_sync._operational_token() == "cached-login"  # falls back to HF_TOKEN
    monkeypatch.setenv("HF_SCPRS_TOKEN", "scprs-write")
    assert data_sync._operational_token() == "scprs-write"  # dedicated token wins


def test_publish_operational_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_SCPRS_TOKEN", "scprs-write")
    db = tmp_path / "scprs.db"
    db.write_bytes(b"x")
    captured = {}

    def recorder(db_path, repo, token=None):
        captured.update(repo=repo, path=str(db_path), passed=token)
        return "https://hf/commit/abc"

    monkeypatch.setattr(data_sync, "publish_operational_db", recorder)
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_sync", "publish-operational", "--dataset", "acme/op", "--path", str(db)],
    )
    data_sync._cli()
    assert captured["repo"] == "acme/op"
    assert captured["path"] == str(db)


def test_fetch_supplier_db_noop_without_dataset(tmp_path, monkeypatch):
    monkeypatch.delenv("SCPRS_DATASET", raising=False)
    assert data_sync.fetch_supplier_db(tmp_path / "supplier_enrichment.db") is False


def test_fetch_supplier_db_absent_file_is_false_not_error(tmp_path, monkeypatch):
    """The side input is optional (absent until first published) — a failed fetch
    must return False, not raise, so `fetch-operational` still succeeds."""
    import huggingface_hub

    def boom(**kw):
        raise RuntimeError("404 Entry Not Found")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", boom)
    dest = tmp_path / "supplier_enrichment.db"
    assert data_sync.fetch_supplier_db(dest, repo="acme/scprs-operational-db") is False
    assert not dest.exists()


def test_fetch_supplier_db_fetches_with_operational_token(tmp_path, monkeypatch):
    import huggingface_hub

    remote = tmp_path / "remote.db"
    remote.write_bytes(b"supplier-bytes")
    monkeypatch.setenv("HF_SCPRS_TOKEN", "op-token")
    captured = {}

    def fake_download(**kw):
        captured.update(kw)
        return str(remote)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    dest = tmp_path / "supplier_enrichment.db"
    assert data_sync.fetch_supplier_db(dest, repo="acme/scprs-operational-db") is True
    assert dest.read_bytes() == b"supplier-bytes"
    assert captured["filename"] == "supplier_enrichment.db"
    assert captured["token"] == "op-token"  # noqa: S105 — test literal


def test_publish_supplier_db_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        data_sync.publish_supplier_db(tmp_path / "nope.db", "acme/scprs-operational-db")


def test_restart_spaces_is_best_effort(monkeypatch):
    """One Space restarting and one failing must yield per-Space outcomes, no raise."""
    import huggingface_hub

    calls = []

    class FakeApi:
        def restart_space(self, repo_id, token=None):
            calls.append((repo_id, token))
            if "chat" in repo_id:
                raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setenv("HF_DEPLOY_TOKEN", "deploy-token")
    results = dict(
        data_sync.restart_spaces(("acme/scprs-warehouse-mcp", "acme/scprs-warehouse-chat"))
    )
    assert results["acme/scprs-warehouse-mcp"] == "restarted"
    assert results["acme/scprs-warehouse-chat"].startswith("FAILED:")
    assert all(token == "deploy-token" for _, token in calls)  # noqa: S105 — test literal


def test_deploy_token_precedence(monkeypatch):
    monkeypatch.delenv("HF_DEPLOY_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert data_sync._deploy_token() is None
    monkeypatch.setenv("HF_TOKEN", "cached-login")
    assert data_sync._deploy_token() == "cached-login"
    monkeypatch.setenv("HF_DEPLOY_TOKEN", "spaces-write")
    assert data_sync._deploy_token() == "spaces-write"  # dedicated token wins


def test_restart_spaces_cli_defaults_and_exit_zero(monkeypatch, capsys):
    captured = {}

    def recorder(spaces, token=None):
        captured["spaces"] = spaces
        return [(s, "FAILED: no token") for s in spaces]

    monkeypatch.setattr(data_sync, "restart_spaces", recorder)
    monkeypatch.setattr(sys, "argv", ["data_sync", "restart-spaces"])
    data_sync._cli()  # must not raise/exit non-zero even when every restart fails
    assert captured["spaces"] == data_sync.DEFAULT_SPACES
    assert "FAILED" in capsys.readouterr().out


def test_fetch_operational_cli(tmp_path, monkeypatch):
    captured = {}

    def recorder(dest, repo=None, token=None):
        captured.update(dest=str(dest), repo=repo)
        return True

    monkeypatch.setattr(data_sync, "fetch_operational_db", recorder)
    # The CLI also best-effort fetches the supplier side input — stub it offline.
    monkeypatch.setattr(data_sync, "fetch_supplier_db", lambda dest, repo=None: False)
    dest = tmp_path / "scprs.db"
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_sync", "fetch-operational", "--dataset", "acme/op", "--dest", str(dest)],
    )
    data_sync._cli()
    assert captured["repo"] == "acme/op"
    assert captured["dest"] == str(dest)
