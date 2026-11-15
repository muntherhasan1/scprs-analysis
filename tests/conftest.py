"""Suite-wide fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_failure_evidence(tmp_path_factory, monkeypatch):
    """Scrapers capture failure evidence (src/failure_evidence.py) into
    data/failure-evidence/ — a test exercising a failure path must not write
    there, and the per-process capture cap must not leak between tests (a test
    would pass or fail depending on how many captures ran before it)."""
    from src import failure_evidence

    monkeypatch.setattr(
        failure_evidence, "EVIDENCE_DIR", tmp_path_factory.mktemp("failure-evidence")
    )
    failure_evidence.PageWatch.reset_limit()
