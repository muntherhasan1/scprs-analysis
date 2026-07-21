"""Offline tests for the code-deploy verification (Wave 3)."""

import pytest

from src import deploy_check


class _RT:
    def __init__(self, stage, sha=None):
        self.stage = stage
        self.raw = {"stage": stage, "sha": sha} if sha is not None else {"stage": stage}


class _Info:
    def __init__(self, rt):
        self.runtime = rt


def test_healthz_url_derived_from_space():
    assert (
        deploy_check._healthz_url("munther-hasan/scprs-warehouse-mcp")
        == "https://munther-hasan-scprs-warehouse-mcp.hf.space/healthz"
    )


def test_runtime_sha_prefers_raw_then_attr():
    assert deploy_check._runtime_sha(_RT("RUNNING", "abc123")) == "abc123"

    class Attr:
        raw = None
        sha = "fromattr"

    assert deploy_check._runtime_sha(Attr()) == "fromattr"


def test_wait_for_build_returns_when_running_on_expected_sha():
    class Api:
        def space_info(self, space):
            return _Info(_RT("RUNNING", "deadbeef"))

    # returns without raising (and without sleeping — already on the sha).
    deploy_check.wait_for_build("acme/space", "deadbeef", api=Api(), poll_s=0, timeout_s=1)


def test_wait_for_build_ignores_old_sha_until_new(monkeypatch):
    """RUNNING on the OLD sha is not accepted — must wait for the deployed sha."""
    seq = [_RT("RUNNING", "oldsha"), _RT("BUILDING"), _RT("RUNNING", "newsha")]

    class Api:
        def __init__(self):
            self.i = 0

        def space_info(self, space):
            rt = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return _Info(rt)

    monkeypatch.setattr(deploy_check.time, "sleep", lambda *_: None)
    deploy_check.wait_for_build("acme/space", "newsha", api=Api(), poll_s=0, timeout_s=5)


def test_wait_for_build_raises_on_build_error():
    class Api:
        def space_info(self, space):
            return _Info(_RT("BUILD_ERROR"))

    with pytest.raises(RuntimeError, match="BUILD_ERROR"):
        deploy_check.wait_for_build("acme/space", "x", api=Api(), poll_s=0, timeout_s=1)


def test_wait_for_build_times_out(monkeypatch):
    class Api:
        def space_info(self, space):
            return _Info(_RT("RUNNING", "wrongsha"))

    monkeypatch.setattr(deploy_check.time, "sleep", lambda *_: None)
    with pytest.raises(TimeoutError):
        deploy_check.wait_for_build("acme/space", "target", api=Api(), poll_s=0, timeout_s=0.05)


def test_check_healthz_success(monkeypatch):
    import httpx

    class Resp:
        status_code = 200

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp())
    deploy_check.check_healthz("https://x/healthz", timeout_s=1, poll_s=0)


def test_check_healthz_times_out_on_non_200(monkeypatch):
    import httpx

    class Resp:
        status_code = 503

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(deploy_check.time, "sleep", lambda *_: None)
    with pytest.raises(TimeoutError):
        deploy_check.check_healthz("https://x/healthz", timeout_s=0.05, poll_s=0)


def test_verify_deploy_defaults_sha_to_latest_commit(monkeypatch):
    calls = {}

    class Commit:
        commit_id = "latestcommit"

    class Api:
        def list_repo_commits(self, space, repo_type=None):
            calls["listed"] = space
            return [Commit()]

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(
        deploy_check, "wait_for_build", lambda space, sha, api=None: calls.update(sha=sha)
    )
    monkeypatch.setattr(deploy_check, "check_healthz", lambda url: calls.update(url=url))
    sha = deploy_check.verify_deploy("munther-hasan/scprs-warehouse-mcp")
    assert sha == "latestcommit"
    assert calls["sha"] == "latestcommit"  # defaulted to the Space's latest commit
    assert calls["url"].endswith("scprs-warehouse-mcp.hf.space/healthz")
