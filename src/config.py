"""Centralized, safe configuration loading.

Secrets come from environment variables (loaded from a git-ignored .env in
local dev, or from the deployment platform's secret store in production).
Nothing sensitive is ever hard-coded or logged.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Loads .env if present (local dev). In production, real env vars / a secret
# manager take precedence and .env simply won't exist.
load_dotenv()


def require(name: str) -> str:
    """Return a required secret, failing loudly if it is missing.

    Prevents the app from silently running with an empty credential.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it in your .env (local) or secret store (deployed)."
        )
    return value


# Example accessors — extend as needed.
def db_url() -> str:
    return require("SCPRS_DB_URL")


def api_key() -> str:
    return require("SCPRS_API_KEY")


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
