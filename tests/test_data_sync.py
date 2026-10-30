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


def _fake_head_api(monkeypatch, sha="headsha1234567890"):
    """Fake HfApi whose dataset_info reports a fixed HEAD sha (the CAS base)."""
    import huggingface_hub

    class FakeApi:
        def dataset_info(self, repo, token=None):
            class Info:
                pass

            info = Info()
            info.sha = sha
            return info

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    return sha


def test_fetch_operational_db_atomic_fetch(tmp_path, monkeypatch):
    import huggingface_hub

    remote = tmp_path / "scprs.db"
    remote.write_bytes(b"operational-db-bytes")
    monkeypatch.setenv("SCPRS_DATASET", "acme/scprs-operational-db")
    monkeypatch.setenv("HF_SCPRS_TOKEN", "op-token")
    sha = _fake_head_api(monkeypatch)
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
    # CAS wiring (#48): download pinned to the resolved HEAD, sha recorded for publish
    assert captured["revision"] == sha
    assert data_sync._rev_sidecar(dest).read_text() == sha


def test_fetch_operational_db_without_head_still_fetches(tmp_path, monkeypatch):
    """CAS is protection, not a prerequisite: an unreachable HEAD lookup falls back
    to a plain main fetch with no sidecar (publish then skips the guard)."""
    import huggingface_hub

    remote = tmp_path / "scprs.db"
    remote.write_bytes(b"bytes")

    class BrokenApi:
        def dataset_info(self, repo, token=None):
            raise RuntimeError("404 Repo Not Found")

    monkeypatch.setattr(huggingface_hub, "HfApi", BrokenApi)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda **kw: str(remote))

    dest = tmp_path / "out" / "scprs.db"
    assert data_sync.fetch_operational_db(dest, repo="acme/op") is True
    assert not data_sync._rev_sidecar(dest).exists()


def test_fetch_operational_db_wraps_error(tmp_path, monkeypatch):
    import huggingface_hub

    _fake_head_api(monkeypatch)

    def boom(**kw):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", boom)
    with pytest.raises(data_sync.WarehouseFetchError) as ei:
        data_sync.fetch_operational_db(tmp_path / "scprs.db", repo="acme/scprs-operational-db")
    assert "READ" in str(ei.value)


def test_publish_operational_db_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        data_sync.publish_operational_db(tmp_path / "nope.db", "acme/scprs-operational-db")


def test_publish_operational_db_compare_and_swaps_then_spends_sidecar(tmp_path, monkeypatch):
    """The publish passes the fetched revision as parent_commit and removes the
    sidecar afterwards (a CAS base must never be reused across runs)."""
    db = tmp_path / "scprs.db"
    db.write_bytes(b"x")
    data_sync._rev_sidecar(db).write_text("basesha")
    captured = {}

    def fake_upload(path, repo, filename, token, message, parent_commit=None):
        captured.update(parent_commit=parent_commit)
        return "https://hf/commit/ok"

    monkeypatch.setattr(data_sync, "_upload_db", fake_upload)
    assert data_sync.publish_operational_db(db, "acme/op") == "https://hf/commit/ok"
    assert captured["parent_commit"] == "basesha"
    assert not data_sync._rev_sidecar(db).exists()


def test_publish_operational_db_lost_update_guard(tmp_path, monkeypatch):
    """A 412 (HEAD moved past the fetched base) surfaces as an actionable
    lost-update error instead of clobbering the other writer's publish (#48)."""
    db = tmp_path / "scprs.db"
    db.write_bytes(b"x")
    data_sync._rev_sidecar(db).write_text("basesha")

    def conflict(path, repo, filename, token, message, parent_commit=None):
        raise RuntimeError("412 Client Error: Precondition Failed")

    monkeypatch.setattr(data_sync, "_upload_db", conflict)
    with pytest.raises(data_sync.WarehouseFetchError, match="lost-update"):
        data_sync.publish_operational_db(db, "acme/op")
    assert data_sync._rev_sidecar(db).exists()  # base kept — nothing was published


def test_with_retries_retries_transient_and_not_deterministic(monkeypatch):
    monkeypatch.setattr(data_sync.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("503 Server Error")
        return "ok"

    assert data_sync._with_retries(flaky, what="t") == "ok"
    assert calls["n"] == 3

    def forbidden():
        calls["n"] += 1
        raise RuntimeError("403 Forbidden")

    calls["n"] = 0
    with pytest.raises(RuntimeError, match="403"):
        data_sync._with_retries(forbidden, what="t")
    assert calls["n"] == 1  # deterministic errors fail fast, no retry


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


class _PathInfo:
    def __init__(self, blob_id):
        self.blob_id = blob_id


def test_rollback_serve_db_reverts_to_previous_commit(tmp_path, monkeypatch):
    """Rollback re-publishes the previous DISTINCT serve DB as a new commit. A
    publish makes two commits (DB, then marts/), so the commit right before the
    marts commit still holds the bad DB and must be skipped."""
    import huggingface_hub

    prev_db = tmp_path / "good.db"
    prev_db.write_bytes(b"last-good-serve")
    dl = {}

    class Commit:
        def __init__(self, cid):
            self.commit_id = cid

    class FakeApi:
        def list_repo_commits(self, repo, repo_type=None, token=None):
            # newest first: marts commit, bad DB commit, prior marts, last-good DB
            return [
                Commit("martsbad"),
                Commit("badbadbad"),
                Commit("goodmarts"),
                Commit("goodgood1234"),
            ]

        def get_paths_info(self, repo, paths, revision=None, repo_type=None, token=None):
            bad = revision in ("martsbad", "badbadbad")
            return [_PathInfo("blob-bad" if bad else "blob-good")]

    def fake_download(**kw):
        dl.update(kw)
        return str(prev_db)

    up = {}

    def fake_upload(path, repo, filename, token, message):
        up.update(path=str(path), repo=repo, filename=filename, message=message)
        return "https://hf/commit/rollback"

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    monkeypatch.setattr(data_sync, "_upload_db", fake_upload)

    url = data_sync.rollback_serve_db("acme/warehouse-data", token="wh-token")  # noqa: S106
    assert url == "https://hf/commit/rollback"
    assert dl["revision"] == "goodmarts"  # newest revision holding the PRIOR DB
    assert dl["filename"] == "warehouse-serve.db"
    assert up["filename"] == "warehouse-serve.db"
    assert "goodmart" in up["message"]  # rollback commit names the target


def test_rollback_serve_db_raises_without_prior_revision(monkeypatch):
    import huggingface_hub

    class Commit:
        commit_id = "only"

    class FakeApi:
        def list_repo_commits(self, repo, repo_type=None, token=None):
            return [Commit(), Commit()]  # two commits, same DB: nothing to roll back to

        def get_paths_info(self, repo, paths, revision=None, repo_type=None, token=None):
            return [_PathInfo("same-blob")]

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    with pytest.raises(data_sync.WarehouseFetchError, match="no earlier revision"):
        data_sync.rollback_serve_db("acme/warehouse-data")


def test_rollback_serve_cli_reverts_then_restarts(monkeypatch, capsys):
    monkeypatch.setattr(data_sync, "rollback_serve_db", lambda repo, token=None: "https://hf/rb")
    monkeypatch.setattr(data_sync, "restart_spaces", lambda *a, **k: [("acme/mcp", "restarted")])
    monkeypatch.setattr(
        sys, "argv", ["data_sync", "rollback-serve", "--dataset", "acme/warehouse-data"]
    )
    data_sync._cli()
    out = capsys.readouterr().out
    assert "Rolled back" in out and "restarted" in out  # reverts data, then restarts


def test_restart_spaces_is_best_effort(monkeypatch):
    """One Space restarting and one failing must yield per-Space outcomes, no raise."""
    import huggingface_hub

    calls = []

    class FakeApi:
        def restart_space(self, repo_id, token=None, factory_reboot=False):
            calls.append((repo_id, token, factory_reboot))
            if "chat" in repo_id:
                raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setenv("HF_DEPLOY_TOKEN", "deploy-token")
    results = dict(
        data_sync.restart_spaces(("acme/scprs-warehouse-mcp", "acme/scprs-warehouse-chat"))
    )
    assert results["acme/scprs-warehouse-mcp"] == "restarted"
    assert results["acme/scprs-warehouse-chat"].startswith("FAILED:")
    assert all(token == "deploy-token" for _, token, _fr in calls)  # noqa: S105 — test literal
    # A plain restart reports success without re-running the boot fetch — the
    # go-live contract requires a factory reboot.
    assert all(factory for _, _, factory in calls)


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


def test_restart_spaces_cli_require_fails_on_that_space(monkeypatch):
    """--require turns the named space's failed restart into exit 1 so the
    workflow can distinguish 'never rebooted' from 'rebooted but stale' (#45)."""

    def recorder(spaces, token=None):
        return [("acme/mcp", "FAILED: 401"), ("acme/chat", "restarted")]

    monkeypatch.setattr(data_sync, "restart_spaces", recorder)
    monkeypatch.setattr(sys, "argv", ["data_sync", "restart-spaces", "--require", "acme/mcp"])
    with pytest.raises(SystemExit) as ei:
        data_sync._cli()
    assert ei.value.code == 1


def test_restart_spaces_cli_require_passes_when_that_space_restarts(monkeypatch, capsys):
    def recorder(spaces, token=None):
        return [("acme/mcp", "restarted"), ("acme/chat", "FAILED: 404")]

    monkeypatch.setattr(data_sync, "restart_spaces", recorder)
    monkeypatch.setattr(sys, "argv", ["data_sync", "restart-spaces", "--require", "acme/mcp"])
    data_sync._cli()  # chat (frozen) failing must not matter
    assert "restarted" in capsys.readouterr().out


def test_backup_operational_requires_write_token(monkeypatch):
    """A backup that can silently green-skip is worse than none: no
    HF_BACKUP_TOKEN must be a loud SystemExit, never a pass."""
    import pytest

    monkeypatch.delenv("HF_BACKUP_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="HF_BACKUP_TOKEN"):
        data_sync.backup_operational(
            "acme/backup",
            source_repo="acme/op",
            read_token="r",  # noqa: S106 - test stub, not a credential
        )


def test_backup_operational_copies_and_prunes(tmp_path, monkeypatch):
    uploads, deletes = [], []

    class FakeApi:
        def __init__(self, token=None):
            self.token = token

        def create_repo(self, repo, repo_type, private, exist_ok):
            pass

        def list_repo_files(self, repo, repo_type):
            if repo == "acme/op":
                return ["scprs.db", "cmas.db", "README.md"]
            # backup repo: two old snapshots + the fresh one
            return [
                "2026-06-01/scprs.db",
                "2026-06-08/scprs.db",
                "2026-07-28/scprs.db",
            ]

        def upload_file(self, *, path_or_fileobj, path_in_repo, repo_id, repo_type, commit_message):
            uploads.append(path_in_repo)

        def create_commit(self, *, repo_id, repo_type, operations, commit_message):
            deletes.extend(op.path_in_repo for op in operations)

    def fake_download(repo, name, repo_type, token=None, local_dir=None):
        from pathlib import Path

        p = Path(local_dir) / name
        p.write_bytes(b"db")
        return str(p)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    folder = data_sync.backup_operational(
        "acme/backup",
        source_repo="acme/op",
        read_token="r",  # noqa: S106 - test stub, not a credential
        write_token="w",  # noqa: S106 - test stub, not a credential
        keep=2,
        today="2026-07-28",
    )
    assert folder == "2026-07-28"
    assert uploads == ["2026-07-28/scprs.db", "2026-07-28/cmas.db"]  # .db only
    assert deletes == ["2026-06-01/"]  # newest 2 kept


def test_fetch_operational_cli(tmp_path, monkeypatch):
    captured = {}

    def recorder(dest, repo=None, token=None):
        captured.update(dest=str(dest), repo=repo)
        return True

    monkeypatch.setattr(data_sync, "fetch_operational_db", recorder)
    # The CLI also best-effort fetches the optional side inputs — stub them offline
    # and record which were attempted (a missing one means empty marts in CI, the
    # 2026-07-27 eprocure gap).
    side_inputs = set()

    def side_stub(name):
        def stub(dest, repo=None):
            side_inputs.add(name)
            return False

        return stub

    monkeypatch.setattr(data_sync, "fetch_supplier_db", side_stub("supplier"))
    monkeypatch.setattr(data_sync, "fetch_cmas_db", side_stub("cmas"))
    monkeypatch.setattr(data_sync, "fetch_eprocure_db", side_stub("eprocure"))
    dest = tmp_path / "scprs.db"
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_sync", "fetch-operational", "--dataset", "acme/op", "--dest", str(dest)],
    )
    data_sync._cli()
    assert captured["repo"] == "acme/op"
    assert captured["dest"] == str(dest)
    assert side_inputs == {"supplier", "cmas", "eprocure"}


def test_fetch_cmas_db_noop_without_dataset(tmp_path, monkeypatch):
    monkeypatch.delenv("SCPRS_DATASET", raising=False)
    assert data_sync.fetch_cmas_db(tmp_path / "cmas.db") is False


def test_fetch_cmas_db_absent_file_is_false_not_error(tmp_path, monkeypatch):
    """The CMAS side input is optional — a failed fetch returns False, not raises,
    so `fetch-operational` still succeeds and the build just skips CMAS."""
    import huggingface_hub

    def boom(**kw):
        raise RuntimeError("404 Entry Not Found")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", boom)
    dest = tmp_path / "cmas.db"
    assert data_sync.fetch_cmas_db(dest, repo="acme/scprs-operational-db") is False
    assert not dest.exists()


def test_fetch_cmas_db_fetches_with_operational_token(tmp_path, monkeypatch):
    import huggingface_hub

    remote = tmp_path / "remote.db"
    remote.write_bytes(b"cmas-bytes")
    monkeypatch.setenv("HF_SCPRS_TOKEN", "op-token")
    captured = {}

    def fake_download(**kw):
        captured.update(kw)
        return str(remote)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    dest = tmp_path / "cmas.db"
    assert data_sync.fetch_cmas_db(dest, repo="acme/scprs-operational-db") is True
    assert dest.read_bytes() == b"cmas-bytes"
    assert captured["filename"] == "cmas.db"
    assert captured["token"] == "op-token"  # noqa: S105 — test literal


def test_publish_cmas_db_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        data_sync.publish_cmas_db(tmp_path / "nope.db", "acme/scprs-operational-db")


def test_publish_cmas_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_SCPRS_TOKEN", "scprs-write")
    db = tmp_path / "cmas.db"
    db.write_bytes(b"x")
    captured = {}

    def recorder(db_path, repo, token=None):
        captured.update(repo=repo, path=str(db_path))
        return "https://hf/commit/abc"

    monkeypatch.setattr(data_sync, "publish_cmas_db", recorder)
    monkeypatch.setattr(
        sys, "argv", ["data_sync", "publish-cmas", "--dataset", "acme/op", "--path", str(db)]
    )
    data_sync._cli()
    assert captured["repo"] == "acme/op"
    assert captured["path"] == str(db)


def test_publish_app_refuses_without_meta(tmp_path):
    # An empty / half-written export must never reach the public dataset.
    (tmp_path / "opportunities.json").write_text("{}")
    with pytest.raises(FileNotFoundError):
        data_sync.publish_app(tmp_path, "acme/app")


def test_publish_app_uploads_json_and_prunes_stale(tmp_path, monkeypatch):
    (tmp_path / "meta.json").write_text("{}")
    captured = {}

    class FakeApi:
        def __init__(self, token=None):
            captured["token"] = token

        def upload_folder(self, **kw):
            captured.update(kw)
            return type("Info", (), {"commit_url": "https://hf/commit/app"})()

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    url = data_sync.publish_app(tmp_path, "acme/app", token="app-write")  # noqa: S106 — fake
    assert url == "https://hf/commit/app"
    assert captured["token"] == "app-write"  # noqa: S105 — fake token
    assert captured["repo_id"] == "acme/app" and captured["repo_type"] == "dataset"
    # Pages that left the export are deleted in the same commit; only JSON ships.
    assert captured["delete_patterns"] == ["*.json"]
    assert captured["allow_patterns"] == ["*.json"]


def test_publish_app_cli_prefers_app_token(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_APP_TOKEN", "app-write")
    monkeypatch.setenv("HF_TOKEN", "cached-login")
    captured = {}

    def recorder(app_dir, repo, token=None):
        captured.update(app_dir=app_dir, repo=repo, passed=token)
        return "https://hf/commit/app"

    monkeypatch.setattr(data_sync, "publish_app", recorder)
    monkeypatch.setattr(
        sys, "argv", ["data_sync", "publish-app", "--dataset", "acme/app", "--path", str(tmp_path)]
    )
    data_sync._cli()
    assert captured == {"app_dir": tmp_path, "repo": "acme/app", "passed": "app-write"}


# ------------------------------------------------------- persisted contract history


def _fake_history_api(monkeypatch, *, blob="blob-h1", info_error=None, commits=None):
    """Fake HfApi: dataset HEAD + the history file's blob id (None = absent)."""
    import huggingface_hub

    class FakeApi:
        def __init__(self, token=None):
            pass

        def dataset_info(self, repo, token=None):
            if info_error:
                raise info_error

            class Info:
                sha = "headsha"

            return Info()

        def get_paths_info(self, repo, paths, revision=None, repo_type=None, token=None):
            return [_PathInfo(blob)] if blob else []

        def create_commit(self, **kw):
            if commits is not None:
                commits.append(kw)

            class Info:
                commit_url = "https://hf/commit/both"

            return Info()

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)


def test_fetch_history_absent_starts_fresh(tmp_path, monkeypatch):
    _fake_history_api(monkeypatch, blob=None)
    dest = tmp_path / "warehouse-history.db"
    dest.write_bytes(b"stale local copy")
    assert data_sync.fetch_history_db(dest, "acme/wh", token="t") is False  # noqa: S106
    assert not dest.exists()  # never seed from a stale local file
    assert data_sync._rev_sidecar(dest).read_text() == "absent 0"


def test_fetch_history_fails_closed_when_dataset_unreadable(tmp_path, monkeypatch):
    _fake_history_api(monkeypatch, info_error=RuntimeError("401 Unauthorized"))
    with pytest.raises(data_sync.WarehouseFetchError, match="Refusing to start history fresh"):
        data_sync.fetch_history_db(tmp_path / "h.db", "acme/wh", token="t")  # noqa: S106


def test_fetch_history_fails_closed_when_download_fails(tmp_path, monkeypatch):
    import huggingface_hub

    _fake_history_api(monkeypatch, blob="blob-h1")

    def boom(**kw):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", boom)
    dest = tmp_path / "h.db"
    with pytest.raises(data_sync.WarehouseFetchError):
        data_sync.fetch_history_db(dest, "acme/wh", token="t")  # noqa: S106
    assert not dest.exists()


def test_fetch_history_downloads_at_head_and_records_blob(tmp_path, monkeypatch):
    import huggingface_hub

    remote = _history_file(tmp_path / "remote.db", 3)
    seen = {}

    def fake_download(**kw):
        seen.update(kw)
        return str(remote)

    _fake_history_api(monkeypatch, blob="blob-h1")
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    dest = tmp_path / "h.db"
    assert data_sync.fetch_history_db(dest, "acme/wh", token="t") is True  # noqa: S106
    assert dest.read_bytes() == remote.read_bytes()
    assert seen["revision"] == "headsha"
    assert data_sync._rev_sidecar(dest).read_text() == "blob-h1 3"  # blob + rows fetched


def _history_file(path, rows):
    """A minimal persisted-history SQLite file holding ``rows`` rows."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("CREATE TABLE dw_document_history (history_sk INTEGER PRIMARY KEY, x TEXT)")
    con.executemany("INSERT INTO dw_document_history (x) VALUES (?)", [("r",)] * rows)
    con.commit()
    con.close()
    return path


def test_publish_with_history_is_one_cas_commit_and_spends_sidecar(tmp_path, monkeypatch):
    serve = tmp_path / "warehouse-serve.db"
    serve.write_bytes(b"s")
    hist = _history_file(tmp_path / "warehouse-history.db", 5)
    data_sync._rev_sidecar(hist).write_text("blob-h1 3")  # fetched 3 rows; build added 2
    commits = []
    _fake_history_api(monkeypatch, blob="blob-h1", commits=commits)
    url = data_sync.publish_serve_db(serve, "acme/wh", token="t", history_path=hist)  # noqa: S106
    assert url == "https://hf/commit/both"
    assert len(commits) == 1
    paths = {op.path_in_repo for op in commits[0]["operations"]}
    assert paths == {"warehouse-serve.db", "warehouse-history.db"}
    # Pinned to the HEAD the guard checked: a commit landing in between makes the
    # Hub reject ours instead of being overwritten.
    assert commits[0]["parent_commit"] == "headsha"
    assert not data_sync._rev_sidecar(hist).exists()


def test_publish_skips_unchanged_history_and_uploads_serve_alone(tmp_path, monkeypatch):
    # Most refreshes append no history rows: re-uploading the identical ~0.6 GB
    # file four times a day only grows the dataset (every revision is kept).
    serve = tmp_path / "warehouse-serve.db"
    serve.write_bytes(b"s")
    hist = _history_file(tmp_path / "warehouse-history.db", 3)
    data_sync._rev_sidecar(hist).write_text("blob-h1 3")
    commits, uploads = [], []
    _fake_history_api(monkeypatch, blob="blob-h1", commits=commits)

    def fake_upload(path, repo, filename, token, message, parent_commit=None):
        uploads.append(filename)
        return "https://hf/commit/serve"

    monkeypatch.setattr(data_sync, "_upload_db", fake_upload)
    url = data_sync.publish_serve_db(serve, "acme/wh", token="t", history_path=hist)  # noqa: S106
    assert url == "https://hf/commit/serve"
    assert uploads == ["warehouse-serve.db"] and commits == []
    assert not data_sync._rev_sidecar(hist).exists()  # spent either way


def test_publish_with_history_refuses_without_fetch_or_on_change(tmp_path, monkeypatch):
    serve = tmp_path / "warehouse-serve.db"
    serve.write_bytes(b"s")
    hist = _history_file(tmp_path / "warehouse-history.db", 5)
    commits = []
    _fake_history_api(monkeypatch, blob="blob-h2", commits=commits)
    with pytest.raises(data_sync.WarehouseFetchError, match="not fetched this run"):
        data_sync.publish_serve_db(serve, "acme/wh", token="t", history_path=hist)  # noqa: S106
    data_sync._rev_sidecar(hist).write_text("blob-h1 3")  # dataset moved on to h2 since
    with pytest.raises(data_sync.WarehouseFetchError, match="lost-update guard"):
        data_sync.publish_serve_db(serve, "acme/wh", token="t", history_path=hist)  # noqa: S106
    assert commits == []


def test_publish_cli_with_history_passes_history_path(monkeypatch):
    monkeypatch.setenv("HF_WAREHOUSE_TOKEN", "dataset-write")
    captured = {}

    def recorder(serve_path, repo, token=None, history_path=None):
        captured.update(history=history_path)
        return "https://hf/commit/abc"

    monkeypatch.setattr(data_sync, "publish_serve_db", recorder)
    monkeypatch.setattr(
        sys, "argv", ["data_sync", "publish", "--with-history", "--dataset", "acme/data"]
    )
    data_sync._cli()
    assert captured["history"].name == "warehouse-history.db"


def test_publish_mirrors_the_marts_feed(tmp_path, monkeypatch):
    """A mart dropped from the export must also leave marts/ (else Power BI keeps
    reading its frozen last file with no error)."""
    serve = tmp_path / "warehouse-serve.db"
    serve.write_bytes(b"s")
    (tmp_path / "marts").mkdir()
    (tmp_path / "marts" / "gold_x.csv").write_text("a\n1\n")
    folders = []

    class FakeApi:
        def __init__(self, token=None):
            pass

        def upload_folder(self, **kw):
            folders.append(kw)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setattr(data_sync, "_upload_db", lambda *a, **k: "https://hf/commit/serve")
    data_sync.publish_serve_db(serve, "acme/wh", token="t")  # noqa: S106
    assert folders[0]["path_in_repo"] == "marts"
    assert folders[0]["delete_patterns"] == folders[0]["allow_patterns"]
    assert set(folders[0]["delete_patterns"]) == {"*.csv", "star/*.parquet"}


def _warehouse_history(path, docs):
    """A warehouse.db whose dw_document_history holds one row per (doc, total)."""
    import sqlite3

    from src import warehouse

    con = sqlite3.connect(path)
    warehouse._ensure_control(con)
    for doc, total in docs:
        con.execute(
            "INSERT INTO dw_document_history (business_unit, purchase_document, version, "
            "grand_total, content_sig, batch_id, observed_at) "
            "VALUES ('0001', ?, 0, ?, ?, 'b', '2026-07-09T00:00:00')",
            (doc, total, f"0|{total}"),
        )
    con.commit()
    con.close()
    return path


def _fake_merge_hub(monkeypatch, remote, commits):
    import shutil

    import huggingface_hub

    _fake_history_api(monkeypatch, blob="blob-h1", commits=commits)

    def fake_download(**kw):
        out = remote.with_name("dl.db")
        shutil.copyfile(remote, out)
        return str(out)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)


def test_merge_history_dry_run_counts_and_publishes_nothing(tmp_path, monkeypatch):
    # Dataset: A unchanged (same signature, so the laptop's earlier row stands)
    # and a later change to B -> merged = laptop A, laptop B, dataset B.
    local = _warehouse_history(tmp_path / "laptop.db", [("A", 10), ("B", 20)])
    remote = _warehouse_history(tmp_path / "remote.db", [("A", 10), ("B", 200)])
    commits = []
    _fake_merge_hub(monkeypatch, remote, commits)
    counts = data_sync.merge_history(
        local,
        "acme/wh",
        token="t",  # noqa: S106 - fake
        work_dir=tmp_path / "w",
    )
    assert counts == {
        "dataset_rows": 2,
        "local_rows": 2,
        "merged_rows": 3,
        "added_to_dataset": 1,
    }
    assert commits == []  # dry run by default


def test_merge_history_apply_is_a_history_only_cas_commit(tmp_path, monkeypatch):
    local = _warehouse_history(tmp_path / "laptop.db", [("A", 10)])
    remote = _warehouse_history(tmp_path / "remote.db", [("B", 200)])
    commits = []
    _fake_merge_hub(monkeypatch, remote, commits)
    counts = data_sync.merge_history(
        local,
        "acme/wh",
        token="t",  # noqa: S106 - fake
        work_dir=tmp_path / "w",
        apply=True,
    )
    assert counts["merged_rows"] == 2 and counts["commit"] == "https://hf/commit/both"
    assert [op.path_in_repo for op in commits[0]["operations"]] == ["warehouse-history.db"]
    assert commits[0]["parent_commit"] == "headsha"
