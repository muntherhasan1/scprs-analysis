"""Deploy the remote MCP server to a Hugging Face Space via the HF API.

Preferred over `sync.sh` (git push): an `hf auth login` OAuth token authenticates
the HF API (`upload_folder`) but *not* git-over-HTTPS basic auth, so the git path
fails with "Invalid username or password" unless you use a classic Write token and
let it set a git credential. This script sidesteps git entirely — it assembles the
Space folder (same file set as `sync.sh`) and pushes it through `HfApi`, which also
handles LFS for `data/warehouse.db` via `.gitattributes`.

Note: HF Docker Spaces on free cpu-basic require a **PRO** subscription (~$9/mo) —
only *static* Spaces are free. `create_repo` returns HTTP 402 without PRO.

One-time prereqs:
    pip install huggingface_hub
    hf auth login                                  # or: huggingface-cli login
    # subscribe to PRO: https://huggingface.co/pro  (needed for Docker Spaces)

Deploy / redeploy (after `python -m src.warehouse build`):
    HF_SPACE=<user>/scprs-warehouse-mcp python deploy/hf-space/deploy.py

Optional, in the same run (else set them in Space Settings → Variables and secrets):
    MCP_AUTH_TOKEN=<long-random>  ...python deploy/hf-space/deploy.py   # bearer secret
    # MCP_ALLOWED_HOSTS defaults to "<user>-<space>.hf.space" so the SDK's
    # DNS-rebinding Host guard stays on with the real proxied hostname.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from huggingface_hub import HfApi, create_repo

ROOT = Path(__file__).resolve().parents[2]

# (src relative to ROOT, dest relative to Space root) — mirrors sync.sh.
COPIES = [
    ("deploy/hf-space/README.md", "README.md"),
    ("deploy/hf-space/.gitattributes", ".gitattributes"),
    ("Dockerfile.mcp", "Dockerfile"),  # HF builds ./Dockerfile
    ("requirements-mcp.txt", "requirements-mcp.txt"),
    ("src/mcp_server.py", "src/mcp_server.py"),
    ("src/warehouse_query.py", "src/warehouse_query.py"),  # shared query guard
    ("src/__init__.py", "src/__init__.py"),
    ("data/warehouse.db", "data/warehouse.db"),  # LFS via .gitattributes
]


def main() -> None:
    repo = os.environ.get("HF_SPACE")
    if not repo:
        sys.exit("set HF_SPACE=<user>/<space>, e.g. muntherhasan1/scprs-warehouse-mcp")
    db = ROOT / "data" / "warehouse.db"
    if not db.exists():
        sys.exit(f"{db} not found — build it first: python -m src.warehouse build")

    api = HfApi()
    create_repo(repo, repo_type="space", space_sdk="docker", exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="hf-space-"))
    try:
        for src, dest in COPIES:
            d = work / dest
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / src, d)
        commit = api.upload_folder(
            folder_path=str(work),
            repo_id=repo,
            repo_type="space",
            commit_message="Deploy SCPRS warehouse MCP server",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # Keep the SDK's DNS-rebinding Host guard on with the real proxied hostname.
    user, name = repo.split("/", 1)
    default_host = f"{user}-{name}.hf.space"
    allowed_hosts = os.environ.get("MCP_ALLOWED_HOSTS", default_host)
    api.add_space_variable(repo, "MCP_ALLOWED_HOSTS", allowed_hosts)
    token = os.environ.get("MCP_AUTH_TOKEN")
    if token:
        api.add_space_secret(repo, "MCP_AUTH_TOKEN", token)

    print(f"Deployed: {commit.commit_url if hasattr(commit, 'commit_url') else commit}")
    print(f"Endpoint: https://{default_host}/mcp")
    if not token:
        print(
            "Next: add the MCP_AUTH_TOKEN secret in Space Settings "
            "(the container won't start without it)."
        )


if __name__ == "__main__":
    main()
