"""Deploy the natural-language web app to a Hugging Face Docker Space via the API.

Same approach as ``deploy/hf-space/deploy.py`` (the MCP server) — assemble the
Space folder and push it through ``HfApi.upload_folder`` (LFS handles
``data/warehouse.db`` via ``.gitattributes``), which authenticates with an
``hf auth login`` token where git-over-HTTPS does not.

Note: HF Docker Spaces on free cpu-basic require a **PRO** subscription (~$9/mo);
``create_repo`` returns HTTP 402 without it.

One-time prereqs:
    pip install huggingface_hub
    hf auth login                                  # or: huggingface-cli login
    # subscribe to PRO: https://huggingface.co/pro  (needed for Docker Spaces)

Deploy / redeploy (after `python -m src.warehouse build`):
    HF_SPACE=<user>/scprs-warehouse-chat python deploy/hf-chat/deploy.py

Optional, in the same run (else set it in Space Settings → Variables and secrets):
    GEMINI_API_KEY=<key>  ...python deploy/hf-chat/deploy.py   # free-tier LLM key
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from huggingface_hub import HfApi, create_repo

ROOT = Path(__file__).resolve().parents[2]

# (src relative to ROOT, dest relative to Space root).
COPIES = [
    ("deploy/hf-chat/README.md", "README.md"),
    ("deploy/hf-chat/.gitattributes", ".gitattributes"),
    ("Dockerfile.web", "Dockerfile"),  # HF builds ./Dockerfile
    ("requirements-web.txt", "requirements-web.txt"),
    ("app.py", "app.py"),
    ("src/web_app.py", "src/web_app.py"),
    ("src/nl_query.py", "src/nl_query.py"),
    ("src/query_log.py", "src/query_log.py"),
    ("src/warehouse_query.py", "src/warehouse_query.py"),
    ("src/data_sync.py", "src/data_sync.py"),  # fetch the serve DB from the dataset at boot
    ("src/__init__.py", "src/__init__.py"),
    # No warehouse.db is shipped — the Space fetches warehouse-serve.db from the
    # private WAREHOUSE_DATASET at startup (publish it with `python -m src.data_sync
    # publish`). Keeps the image tiny and data refreshes independent of code deploys.
]


def main() -> None:
    repo = os.environ.get("HF_SPACE")
    if not repo:
        sys.exit("set HF_SPACE=<user>/<space>, e.g. munther-hasan/scprs-warehouse-chat")

    api = HfApi()
    create_repo(repo, repo_type="space", space_sdk="docker", exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="hf-chat-"))
    try:
        for src, dest in COPIES:
            d = work / dest
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / src, d)
        commit = api.upload_folder(
            folder_path=str(work),
            repo_id=repo,
            repo_type="space",
            commit_message="Deploy SCPRS warehouse NL web app",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    key = os.environ.get("GEMINI_API_KEY")
    if key:
        api.add_space_secret(repo, "GEMINI_API_KEY", key)
    # The Space fetches the serve DB from this private dataset at startup; it also
    # needs an HF read token in the HF_TOKEN secret (set once in Space Settings).
    warehouse_dataset = os.environ.get("WAREHOUSE_DATASET")
    if warehouse_dataset:
        api.add_space_variable(repo, "WAREHOUSE_DATASET", warehouse_dataset)

    user, name = repo.split("/", 1)
    print(f"Deployed: {commit.commit_url if hasattr(commit, 'commit_url') else commit}")
    print(f"App: https://{user}-{name}.hf.space")
    if not key:
        print(
            "Next: add the GEMINI_API_KEY secret in Space Settings "
            "(free key at https://aistudio.google.com/apikey)."
        )
    if not warehouse_dataset:
        print("Note: set WAREHOUSE_DATASET (+ HF_TOKEN secret) so the Space can fetch its DB.")


if __name__ == "__main__":
    main()
