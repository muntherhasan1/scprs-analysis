"""Sync the slim serving DB between the data pipeline and the deployed front ends.

The always-on Spaces (MCP server, web app) do **not** bake the warehouse into
their image; they FETCH ``warehouse-serve.db`` from a private HF Dataset at
startup (``ensure_local_db``). The data pipeline PUBLISHES the freshly built serve
DB to that same Dataset (``publish_serve_db``). This decouples data refreshes (a
dataset push + a cheap Space restart) from code deploys (an image rebuild), and
keeps the always-on Spaces independent of the sometimes-off machine that collects
the data — the Space pulls from the Dataset (HF infra), never from that machine.

Both functions are no-ops of a sort for local dev: ``ensure_local_db`` does nothing
unless ``WAREHOUSE_DATASET`` is set, so a local checkout keeps using its own
``data/warehouse.db``. The Space needs an HF token with read access to the private
dataset in ``HF_TOKEN``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

SERVE_FILENAME = "warehouse-serve.db"


def ensure_local_db(dest: Path) -> bool:
    """Fetch the serve DB from the ``WAREHOUSE_DATASET`` dataset into ``dest``.

    Returns True if a fetch happened, False when disabled (no ``WAREHOUSE_DATASET``)
    so the caller falls back to whatever is already at ``dest``. Atomic: downloads
    to a temp file then renames into place, so a reader never sees a half-written DB.
    """
    repo = os.environ.get("WAREHOUSE_DATASET")
    if not repo:
        return False
    from huggingface_hub import hf_hub_download

    # nosec B615 — intentionally track the latest revision (main) of our OWN private,
    # access-controlled dataset; pinning an immutable commit would defeat the
    # fetch-newest-on-restart refresh model this whole design is built around.
    cached = hf_hub_download(  # nosec B615
        repo_id=repo,
        filename=SERVE_FILENAME,
        repo_type="dataset",
        revision="main",
        token=os.environ.get("HF_TOKEN"),
    )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(cached, tmp)
    os.replace(tmp, dest)  # atomic on the same filesystem
    return True


def publish_serve_db(serve_path: Path, repo: str, token: str | None = None) -> str:
    """Upload the serve DB to the private HF Dataset ``repo`` (created if missing).

    Returns the commit URL. Called by the pipeline after a build; the token defaults
    to ``HF_TOKEN`` / the caller's cached HF login.
    """
    from huggingface_hub import HfApi, create_repo

    serve_path = Path(serve_path)
    if not serve_path.exists():
        raise FileNotFoundError(f"{serve_path} not found — run `warehouse serve-export` first")
    token = token or os.environ.get("HF_TOKEN")
    create_repo(repo, repo_type="dataset", private=True, exist_ok=True, token=token)
    info = HfApi().upload_file(
        path_or_fileobj=str(serve_path),
        path_in_repo=SERVE_FILENAME,
        repo_id=repo,
        repo_type="dataset",
        token=token,
        commit_message="Publish serve DB",
    )
    return getattr(info, "commit_url", str(info))


def _cli() -> None:
    import argparse

    from . import warehouse

    ap = argparse.ArgumentParser(description="Sync the slim serving DB with a private HF Dataset.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pub = sub.add_parser("publish", help="Upload data/warehouse-serve.db to the dataset")
    pub.add_argument("--dataset", default=os.environ.get("WAREHOUSE_DATASET"))
    args = ap.parse_args()
    if args.cmd == "publish":
        if not args.dataset:
            raise SystemExit("set --dataset or the WAREHOUSE_DATASET env var")
        url = publish_serve_db(warehouse.SERVE_DB, args.dataset)
        print(f"Published {warehouse.SERVE_DB.name} -> {args.dataset}: {url}")


if __name__ == "__main__":
    _cli()
