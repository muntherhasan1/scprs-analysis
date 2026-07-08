"""Smoke tests for the secure config loader."""

import pytest

from src import config


def test_require_missing_raises(monkeypatch):
    monkeypatch.delenv("SOME_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variable"):
        config.require("SOME_SECRET")


def test_require_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_SECRET", "abc123")
    assert config.require("SOME_SECRET") == "abc123"
