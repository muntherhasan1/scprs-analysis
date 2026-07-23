"""Sync SQLite stores between the data pipeline and the deployed front ends / CI.

Two databases move through private HF Datasets:

* **Serve DB** (``warehouse-serve.db``) — the slim, gold-only serving copy. The
  always-on Spaces (MCP server, web app) do **not** bake it into their image; they
  FETCH it from a private HF Dataset at startup (``ensure_local_db``). The pipeline
  PUBLISHES the freshly built serve DB to that same Dataset (``publish_serve_db``).
  This decouples data refreshes (a dataset push + a cheap Space restart) from code
  deploys (an image rebuild), and keeps the Spaces independent of the sometimes-off
  collection machine — the Space pulls from the Dataset (HF infra), never the laptop.

* **Operational DB** (``scprs.db``) — the full operational store. Wave 2 moves the
  recurring enrichment off the laptop into GitHub Actions; CI becomes the single
  writer of ``scprs.db`` via a **download → mutate → upload-on-success** contract
  (``fetch_operational_db`` / ``publish_operational_db``), so the pipeline advances
  24/7 with no local machine running.

Both fetch functions are no-ops for local dev unless their dataset env var is set,
so a local checkout keeps using its own ``data/*.db``. A fetch needs an HF token with
read access to the private dataset; a publish needs read+write.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import config  # noqa: F401 — imported for its load_dotenv() side-effect (.env in local dev)

SERVE_FILENAME = "warehouse-serve.db"
OPERATIONAL_FILENAME = "scprs.db"
SUPPLIER_FILENAME = "supplier_enrichment.db"
CMAS_FILENAME = "cmas.db"  # CMAS side input, refreshed device-free by cmas-refresh.yml

# The always-on front ends that fetch the serve DB at boot. Restarting them is how
# a freshly published serve DB goes live; mirrors refresh_pipeline.ps1's defaults.
DEFAULT_SPACES = (
    "munther-hasan/scprs-warehouse-mcp",
    "munther-hasan/scprs-warehouse-chat",
)


class WarehouseFetchError(RuntimeError):
    """Fetching a DB from its dataset failed — usually the caller's HF token lacks
    READ access to the (private) dataset, or the dataset env var is wrong."""


# Substrings that mark an HF error as worth retrying (server hiccups, network
# blips). Deterministic failures — 401/403/404, a parent_commit conflict — are
# NOT retried: they'd fail identically and only delay the real error.
_TRANSIENT_MARKERS = ("500", "502", "503", "504", "timeout", "timed out", "connection", "reset")


def _is_transient(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(m in s for m in _TRANSIENT_MARKERS)


def _with_retries(fn, *, what: str, attempts: int = 3, base_delay: float = 5.0):
    """Run ``fn`` retrying only transient failures with linear backoff.

    One flaky HF 500 used to fail a whole 90-minute pipeline run (it self-healed
    six hours later but generated triage noise) — a couple of cheap retries
    absorb that class without masking real token/config errors."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below; non-transient re-raises
            if attempt == attempts - 1 or not _is_transient(exc):
                raise
            delay = base_delay * (attempt + 1)
            print(f"{what} hit a transient error ({type(exc).__name__}); retrying in {delay:.0f}s")
            time.sleep(delay)


def _download_db(
    repo: str, filename: str, dest: Path, token: str | None, revision: str = "main"
) -> None:
    """Atomically fetch ``filename`` from dataset ``repo`` into ``dest``.

    Downloads to a temp file then renames into place, so a reader never sees a
    half-written DB. Raises ``WarehouseFetchError`` with an actionable message if the
    download fails (so a token/config mistake shows a clear error, not a raw traceback).
    """
    from huggingface_hub import hf_hub_download

    try:
        # nosec B615 — by default track the latest revision (main) of our OWN
        # private, access-controlled dataset; pinning an immutable commit would
        # defeat the fetch-newest refresh model this design is built on. The
        # operational fetch passes an explicit sha so its publish can
        # compare-and-swap against the exact revision it read (#48).
        cached = _with_retries(
            lambda: hf_hub_download(  # nosec B615
                repo_id=repo,
                filename=filename,
                repo_type="dataset",
                revision=revision,
                token=token,
            ),
            what=f"download of {filename} from {repo}",
        )
    except Exception as exc:  # noqa: BLE001 — re-raised as an actionable error below
        raise WarehouseFetchError(
            f"Could not fetch {filename!r} from dataset {repo!r}: "
            f"{type(exc).__name__}: {exc}. Check that the HF token (HF_TOKEN) has READ "
            f"access to that (private) dataset and that the dataset id is correct."
        ) from exc
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(cached, tmp)
    os.replace(tmp, dest)  # atomic on the same filesystem


def _upload_db(
    path: Path,
    repo: str,
    filename: str,
    token: str | None,
    message: str,
    parent_commit: str | None = None,
) -> str:
    """Upload ``path`` to the private HF Dataset ``repo`` as ``filename`` (repo created
    if missing). Returns the commit URL.

    ``parent_commit`` makes the upload a compare-and-swap: the Hub rejects the
    commit (412) if the dataset HEAD moved past that revision, so a slower second
    writer fails instead of silently clobbering a newer publish (#48)."""
    from huggingface_hub import HfApi, create_repo

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    create_repo(repo, repo_type="dataset", private=True, exist_ok=True, token=token)
    info = _with_retries(
        lambda: HfApi().upload_file(
            path_or_fileobj=str(path),
            path_in_repo=filename,
            repo_id=repo,
            repo_type="dataset",
            token=token,
            commit_message=message,
            parent_commit=parent_commit,
        ),
        what=f"upload of {filename} to {repo}",
    )
    return getattr(info, "commit_url", str(info))


# --------------------------------------------------------------------------- serve DB


def ensure_local_db(dest: Path) -> bool:
    """Fetch the serve DB from the ``WAREHOUSE_DATASET`` dataset into ``dest``.

    Returns True if a fetch happened, False when disabled (no ``WAREHOUSE_DATASET``)
    so the caller falls back to whatever is already at ``dest``. The Space provides
    read access via ``HF_TOKEN``.
    """
    repo = os.environ.get("WAREHOUSE_DATASET")
    if not repo:
        return False
    _download_db(repo, SERVE_FILENAME, dest, os.environ.get("HF_TOKEN"))
    return True


def publish_serve_db(serve_path: Path, repo: str, token: str | None = None) -> str:
    """Upload the serve DB to the private HF Dataset ``repo`` (created if missing).

    Returns the commit URL. Called by the pipeline after a build; the token defaults
    to ``HF_TOKEN`` / the caller's cached HF login.
    """
    serve_path = Path(serve_path)
    if not serve_path.exists():
        raise FileNotFoundError(f"{serve_path} not found — run `warehouse serve-export` first")
    return _upload_db(
        serve_path, repo, SERVE_FILENAME, token or os.environ.get("HF_TOKEN"), "Publish serve DB"
    )


# --------------------------------------------------------------- operational DB (Wave 2)


def _rev_sidecar(db_path: Path) -> Path:
    """Sidecar recording the dataset revision a fetched DB came from (CAS base)."""
    db_path = Path(db_path)
    return db_path.with_name(db_path.name + ".fetched-rev")


def fetch_operational_db(dest: Path, repo: str | None = None, token: str | None = None) -> bool:
    """Fetch the operational store (``scprs.db``) from its dataset into ``dest``.

    ``repo`` defaults to the ``SCPRS_DATASET`` env var; ``token`` to the dedicated
    operational token / ``HF_TOKEN``. Returns True if a fetch happened, False when no
    dataset is configured (local dev keeps its own ``data/scprs.db``). This is the
    "download" half of CI's download → mutate → upload-on-success contract.

    The dataset HEAD sha is resolved FIRST, the file is downloaded at exactly that
    revision, and the sha is recorded in a sidecar — ``publish_operational_db``
    passes it as ``parent_commit`` so two concurrent writers (e.g. the CI cron and
    a laptop run) become a hard conflict instead of a silent lost update (#48).
    """
    repo = repo or os.environ.get("SCPRS_DATASET")
    if not repo:
        return False
    tok = token or _operational_token()

    sha: str | None = None
    try:
        from huggingface_hub import HfApi

        sha = _with_retries(
            lambda: HfApi().dataset_info(repo, token=tok).sha, what=f"HEAD lookup of {repo}"
        )
    except Exception as exc:  # noqa: BLE001 - CAS is protection, not a fetch prerequisite
        print(
            f"WARN: could not resolve {repo} HEAD ({type(exc).__name__}) — publishing "
            "without the lost-update guard this run"
        )

    _download_db(repo, OPERATIONAL_FILENAME, dest, tok, revision=sha or "main")
    sidecar = _rev_sidecar(dest)
    if sha:
        sidecar.write_text(sha, encoding="utf-8")
    else:
        sidecar.unlink(missing_ok=True)  # never CAS against a stale base
    return True


def publish_operational_db(db_path: Path, repo: str, token: str | None = None) -> str:
    """Upload the operational store (``scprs.db``) to the private HF Dataset ``repo``.

    Returns the commit URL. Called by CI **only on a successful, gated run** so a
    failed/partial enrichment leaves the dataset untouched and the day unrecorded,
    letting the next run safely retry (the incremental design guarantees this).

    If the fetch recorded a revision sidecar, the upload compare-and-swaps against
    it: a 412 means another writer published since this run fetched — the upload
    is refused (publishing would discard their work) and the run must retry from
    a fresh fetch.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found — nothing to publish")
    sidecar = _rev_sidecar(db_path)
    parent = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else None
    try:
        url = _upload_db(
            db_path,
            repo,
            OPERATIONAL_FILENAME,
            token or _operational_token(),
            "Publish operational DB (CI enrichment)",
            parent_commit=parent,
        )
    except Exception as exc:
        if parent and "412" in str(exc):
            raise WarehouseFetchError(
                f"lost-update guard: {repo} advanced past the revision this run fetched "
                f"({parent[:8]}) — another writer published meanwhile. NOT overwriting "
                "their work; re-run so it fetches fresh and re-applies."
            ) from exc
        raise
    sidecar.unlink(missing_ok=True)  # spent — never reuse a CAS base across runs
    return url


def fetch_supplier_db(dest: Path, repo: str | None = None, token: str | None = None) -> bool:
    """Best-effort fetch of the supplier-enrichment side input from the operational
    dataset. Returns False when no dataset is configured or the file isn't there —
    the warehouse build skips a missing ``supplier_enrichment.db`` gracefully, so a
    CI build must fetch it or gold silently loses the web-researched firmographics."""
    repo = repo or os.environ.get("SCPRS_DATASET")
    if not repo:
        return False
    try:
        _download_db(repo, SUPPLIER_FILENAME, dest, token or _operational_token())
    except WarehouseFetchError:
        return False  # optional side input — absent until first published
    return True


def publish_supplier_db(db_path: Path, repo: str, token: str | None = None) -> str:
    """Upload the supplier-enrichment side input alongside ``scprs.db`` in the
    operational dataset. Run after local web-research sessions update it; CI only
    reads it. Returns the commit URL."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found — nothing to publish")
    return _upload_db(
        db_path,
        repo,
        SUPPLIER_FILENAME,
        token or _operational_token(),
        "Publish supplier enrichment side input",
    )


def fetch_cmas_db(dest: Path, repo: str | None = None, token: str | None = None) -> bool:
    """Best-effort fetch of the CMAS side input from the operational dataset.

    Like ``fetch_supplier_db``: returns False when no dataset is configured or the
    file isn't published yet, so a CI build that lacks it just produces empty CMAS
    marts (the warehouse skips an absent ``cmas.db`` gracefully)."""
    repo = repo or os.environ.get("SCPRS_DATASET")
    if not repo:
        return False
    try:
        _download_db(repo, CMAS_FILENAME, dest, token or _operational_token())
    except WarehouseFetchError:
        return False  # optional side input — absent until first published
    return True


def publish_cmas_db(db_path: Path, repo: str, token: str | None = None) -> str:
    """Upload the CMAS store (``cmas.db``) alongside ``scprs.db`` in the operational
    dataset. Written by the scheduled ``cmas-refresh`` workflow (which runs
    ``src.cmas extract`` first), never by a person. Returns the commit URL."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found — run `python -m src.cmas extract` first")
    return _upload_db(
        db_path,
        repo,
        CMAS_FILENAME,
        token or _operational_token(),
        "Publish CMAS side input (CI refresh)",
    )


def rollback_serve_db(repo: str, token: str | None = None) -> str:
    """Revert the serve dataset to its previous revision. Returns the commit URL.

    Wave 3 auto-rollback: if a refresh publishes a serve DB and go-live
    verification then fails, this re-publishes the **prior** revision of
    ``warehouse-serve.db`` as a new commit, so the Spaces fetch known-good data on
    their next boot instead of being stuck on a bad/unverified snapshot. The serve
    dataset is git-based, so every ``publish`` is a versioned commit and the
    last-good copy is always one revision back. Raises if there is nothing to roll
    back to (a single commit)."""
    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi()
    commits = api.list_repo_commits(repo, repo_type="dataset", token=token)
    if len(commits) < 2:
        raise WarehouseFetchError(
            f"cannot roll back {repo!r}: only {len(commits)} commit(s) — no prior revision"
        )
    prev = commits[1].commit_id  # commits[0] is the just-published (bad) revision
    cached = hf_hub_download(  # nosec B615 — our own private dataset, explicit prior rev
        repo_id=repo,
        filename=SERVE_FILENAME,
        repo_type="dataset",
        revision=prev,
        token=token,
    )
    return _upload_db(Path(cached), repo, SERVE_FILENAME, token, f"Rollback serve DB to {prev[:8]}")


def restart_spaces(
    spaces: tuple[str, ...] = DEFAULT_SPACES, token: str | None = None
) -> list[tuple[str, str]]:
    """Best-effort restart of the always-on Spaces so they re-fetch the serve DB at
    boot and the just-published data goes live. Returns ``(space, outcome)`` pairs
    and never raises — a failed restart only means the Space keeps serving the
    previous snapshot until restarted by hand (warn, never fail: a refresh whose
    publish succeeded must not be reported as failed)."""
    from huggingface_hub import HfApi

    api = HfApi()
    results: list[tuple[str, str]] = []
    for space in spaces:
        try:
            # factory_reboot, not a plain restart: a plain restart reports success
            # but the Space keeps serving its boot-time snapshot (observed
            # 2026-07-20 — restarted at 18:22, still serving the 07-17 serve DB).
            # Only a factory reboot reliably re-runs the boot fetch.
            api.restart_space(repo_id=space, token=token or _deploy_token(), factory_reboot=True)
            results.append((space, "restarted"))
        except Exception as exc:  # noqa: BLE001 — best-effort by design, see docstring
            results.append((space, f"FAILED: {type(exc).__name__}: {exc}"))
    return results


def _deploy_token() -> str | None:
    """Token for restarting the Spaces: a token with write access to both Space
    repos (``HF_DEPLOY_TOKEN``), falling back to ``HF_TOKEN`` / the cached login."""
    return os.environ.get("HF_DEPLOY_TOKEN") or os.environ.get("HF_TOKEN")


def _publish_token() -> str | None:
    """Token for publishing the serve DB: prefer the dedicated warehouse-data write
    token, fall back to ``HF_TOKEN`` / the cached HF login. Read from the git-ignored
    ``.env`` (loaded via the ``config`` import) so unattended runs need no cached login."""
    return os.environ.get("HF_WAREHOUSE_TOKEN") or os.environ.get("HF_TOKEN")


def _operational_token() -> str | None:
    """Token for fetching/publishing the operational DB: prefer the dedicated
    scprs-operational-db read+write token, fall back to ``HF_TOKEN``. In Actions this
    is the ``HF_SCPRS_TOKEN`` secret; the single writer of ``scprs.db``."""
    return os.environ.get("HF_SCPRS_TOKEN") or os.environ.get("HF_TOKEN")


def _cli() -> None:
    import argparse

    from . import model, warehouse

    ap = argparse.ArgumentParser(description="Sync pipeline SQLite stores with private HF Datasets")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pub = sub.add_parser("publish", help="Upload data/warehouse-serve.db to the dataset")
    pub.add_argument("--dataset", default=os.environ.get("WAREHOUSE_DATASET"))

    fop = sub.add_parser(
        "fetch-operational",
        help="Download scprs.db (and the supplier side input, if published) from its dataset",
    )
    fop.add_argument("--dataset", default=os.environ.get("SCPRS_DATASET"))
    fop.add_argument("--dest", default=str(model.DB_PATH))

    pop = sub.add_parser("publish-operational", help="Upload scprs.db to its dataset (CI writer)")
    pop.add_argument("--dataset", default=os.environ.get("SCPRS_DATASET"))
    pop.add_argument("--path", default=str(model.DB_PATH))

    psu = sub.add_parser(
        "publish-supplier",
        help="Upload supplier_enrichment.db to the operational dataset (after local research)",
    )
    psu.add_argument("--dataset", default=os.environ.get("SCPRS_DATASET"))
    psu.add_argument("--path", default=str(warehouse.ENRICHMENT_DB))

    pcm = sub.add_parser(
        "publish-cmas",
        help="Upload cmas.db to the operational dataset (scheduled cmas-refresh workflow)",
    )
    pcm.add_argument("--dataset", default=os.environ.get("SCPRS_DATASET"))
    pcm.add_argument("--path", default=str(warehouse.CMAS_DB))

    rsp = sub.add_parser(
        "restart-spaces",
        help="Best-effort restart of the always-on Spaces so a published serve DB goes live",
    )
    rsp.add_argument(
        "--space",
        action="append",
        dest="spaces",
        help=f"Space id to restart (repeatable; default: {', '.join(DEFAULT_SPACES)})",
    )
    rsp.add_argument(
        "--require",
        default=None,
        metavar="SPACE",
        help="Exit 1 if THIS space's restart failed (best-effort for the rest). Lets the "
        "workflow distinguish 'restart never happened' from 'restarted but serving stale' "
        "— the difference between a token problem and a rollback-worthy deploy (#45).",
    )

    rbk = sub.add_parser(
        "rollback-serve",
        help="Revert the serve dataset to its previous revision and restart the Spaces",
    )
    rbk.add_argument("--dataset", default=os.environ.get("WAREHOUSE_DATASET"))

    args = ap.parse_args()

    if args.cmd == "publish":
        if not args.dataset:
            raise SystemExit("set --dataset or the WAREHOUSE_DATASET env var")
        url = publish_serve_db(warehouse.SERVE_DB, args.dataset, token=_publish_token())
        print(f"Published {warehouse.SERVE_DB.name} -> {args.dataset}: {url}")
    elif args.cmd == "fetch-operational":
        if not args.dataset:
            raise SystemExit("set --dataset or the SCPRS_DATASET env var")
        fetch_operational_db(Path(args.dest), repo=args.dataset)
        print(f"Fetched {OPERATIONAL_FILENAME} <- {args.dataset} into {args.dest}")
        # Optional side inputs — the warehouse build skips either if absent.
        for label, path, fetch in (
            (SUPPLIER_FILENAME, warehouse.ENRICHMENT_DB, fetch_supplier_db),
            (CMAS_FILENAME, warehouse.CMAS_DB, fetch_cmas_db),
        ):
            if fetch(path, repo=args.dataset):
                print(f"Fetched {label} <- {args.dataset}")
            else:
                print(f"Note: {label} not in {args.dataset}; build will skip it")
    elif args.cmd == "publish-operational":
        if not args.dataset:
            raise SystemExit("set --dataset or the SCPRS_DATASET env var")
        url = publish_operational_db(Path(args.path), args.dataset)
        print(f"Published {OPERATIONAL_FILENAME} -> {args.dataset}: {url}")
    elif args.cmd == "publish-supplier":
        if not args.dataset:
            raise SystemExit("set --dataset or the SCPRS_DATASET env var")
        url = publish_supplier_db(Path(args.path), args.dataset)
        print(f"Published {SUPPLIER_FILENAME} -> {args.dataset}: {url}")
    elif args.cmd == "publish-cmas":
        if not args.dataset:
            raise SystemExit("set --dataset or the SCPRS_DATASET env var")
        url = publish_cmas_db(Path(args.path), args.dataset)
        print(f"Published {CMAS_FILENAME} -> {args.dataset}: {url}")
    elif args.cmd == "restart-spaces":
        results = restart_spaces(tuple(args.spaces or DEFAULT_SPACES))
        for space, outcome in results:
            print(f"{space}: {outcome}")
        # Best-effort by contract — a failed restart only delays go-live until a
        # manual reboot; the publish itself already succeeded. But the caller may
        # --require one space, turning ITS failure into a machine-readable exit
        # so the workflow can skip go-live/rollback and fail with the real cause.
        if args.require:
            failed = dict(results).get(args.require, "").startswith("FAILED")
            missing = args.require not in dict(results)
            if failed or missing:
                print(f"required space {args.require} did not restart")
                raise SystemExit(1)
    elif args.cmd == "rollback-serve":
        if not args.dataset:
            raise SystemExit("set --dataset or the WAREHOUSE_DATASET env var")
        url = rollback_serve_db(args.dataset, token=_publish_token())
        print(f"Rolled back {SERVE_FILENAME} in {args.dataset} to last-good: {url}")
        for space, outcome in restart_spaces():  # re-fetch the rolled-back data
            print(f"{space}: {outcome}")


if __name__ == "__main__":
    _cli()
