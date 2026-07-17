"""Sync (or rotate) ``MCP_AUTH_TOKENS`` from your git-ignored ``.env`` to the Space.

HF Space secrets are **write-only**: the UI's "Replace" hands you a blank field,
never shows the current value, and expects the *entire* new secret retyped. So
managing the ``label:token`` pairs by hand in the HF UI means keeping your own
copy anyway and re-entering the whole list to change one user.

Instead, keep the pairs in your git-ignored ``.env`` (the same place
``src/config.py`` reads local secrets; keep the master copy in your password
manager) and sync them here with one command. HF's API ``add_space_secret``
overwrites the whole secret, so the value never touches your shell history or the
repo, and you never retype in the UI.

Two modes:

  * **sync** (default) - push the current ``.env`` value as-is. Use after editing
    ``MCP_AUTH_TOKENS`` to add / remove / rotate a single user.
  * **--rotate** - issue a fresh token for **every** existing label, rewrite the
    ``MCP_AUTH_TOKENS`` line in ``.env`` in place (leaving other vars untouched),
    push it, and print the new label->token list to hand out. Run on your rotation
    schedule (~every 90 days) or immediately on a suspected leak.

Usage:
    HF_SPACE=<user>/scprs-warehouse-mcp python deploy/hf-space/set_tokens.py
    HF_SPACE=<user>/scprs-warehouse-mcp python deploy/hf-space/set_tokens.py --rotate

Editing one pair never disturbs the others - each token is independent. Validation
matches the server exactly (same ``auth.parse_tokens`` the container runs at boot),
so a malformed pair or a duplicated token value is caught **here**, before it can
break the Space's start. Needs the same ``hf auth login`` credential as
``deploy.py`` (a token with Space settings/secrets write scope).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import HfApi

# Run as a plain script (`python deploy/hf-space/set_tokens.py`): the script's own
# dir goes on sys.path, not the repo root, so add the root to import `src`.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src import auth  # noqa: E402  (must follow the sys.path insert above)

ENV_PATH = ROOT / ".env"
VAR = "MCP_AUTH_TOKENS"


def _new_token() -> str:
    return secrets.token_hex(24)  # 48 hex chars of CSPRNG entropy


def _parse_validated(value: str) -> dict[str, str]:
    """Parse+validate exactly as the server does at boot; exit cleanly on error."""
    raw_pairs = [p for p in (s.strip() for s in value.split(",")) if p]
    try:
        tokens = auth.parse_tokens(None, value)  # {token: label}
    except ValueError as e:
        sys.exit(f"Invalid {VAR} - fix .env and re-run: {e}")
    if len(tokens) != len(raw_pairs):
        sys.exit(
            "Duplicate token value across labels - they would collapse to one "
            "principal, silently locking someone out of their own identity. "
            "Give each user a unique token."
        )
    return tokens


def _rewrite_env_line(new_value: str) -> None:
    """Replace the MCP_AUTH_TOKENS line in .env in place, preserving other lines."""
    if not ENV_PATH.exists():
        sys.exit(f"No .env at {ENV_PATH} - nothing to rotate. Seed it first, then --rotate.")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.lstrip().startswith(f"{VAR}=") and not line.lstrip().startswith("#"):
            out.append(f"{VAR}={new_value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        sys.exit(f"No active {VAR}= line found in {ENV_PATH} to rotate.")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="issue a fresh token for every existing label, rewrite .env, then push",
    )
    args = parser.parse_args()

    repo = os.environ.get("HF_SPACE")
    if not repo:
        sys.exit("set HF_SPACE=<user>/<space>, e.g. munther-hasan/scprs-warehouse-mcp")

    load_dotenv(ENV_PATH)  # git-ignored .env
    value = os.environ.get(VAR)
    if not value:
        sys.exit(
            f"{VAR} not found. Add a line to your git-ignored .env:\n"
            f"  {VAR}=alice:<token>,bob:<token>\n"
            'Generate a token with: python -c "import secrets; print(secrets.token_hex(24))"'
        )

    tokens = _parse_validated(value)

    if args.rotate:
        labels = sorted(tokens.values())
        rotated = {label: _new_token() for label in labels}
        value = ",".join(f"{label}:{tok}" for label, tok in rotated.items())
        _parse_validated(value)  # re-validate the freshly built value before writing
        _rewrite_env_line(value)
        print(f"Rotated {len(rotated)} token(s); .env updated. New tokens to distribute:")
        for label, tok in rotated.items():
            print(f"  {label}: {tok}")
        print("(Share each via your password manager; users update their client's Bearer token.)")
    else:
        print(f"Parsed {len(tokens)} token(s) for principals: {', '.join(sorted(tokens.values()))}")

    HfApi().add_space_secret(repo, VAR, value)
    print(f"Pushed {VAR} to {repo}; the Space will restart to apply it.")
    print(
        "Verify after restart: each user's token should authenticate and record "
        "its principal in the audit log (not 'default')."
    )


if __name__ == "__main__":
    main()
