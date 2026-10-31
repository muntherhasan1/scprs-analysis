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

import datetime
import os
import shutil
import time
from pathlib import Path

from . import config  # noqa: F401 — imported for its load_dotenv() side-effect (.env in local dev)

SERVE_FILENAME = "warehouse-serve.db"
# dw_document_history alone, beside the serve DB: CI rebuilds warehouse.db from
# nothing, so the append-only contract history is carried run to run in this file.
HISTORY_FILENAME = "warehouse-history.db"
# The BI feed under marts/ (published AND mirrored -- see publish_serve_db).
_MART_PATTERNS = ["*.csv", "star/*.parquet"]
OPERATIONAL_FILENAME = "scprs.db"
SUPPLIER_FILENAME = "supplier_enrichment.db"
CMAS_FILENAME = "cmas.db"  # CMAS side input, refreshed device-free by cmas-refresh.yml
EPROCURE_FILENAME = "eprocure.db"  # eProcure SB/DVBE registry, refreshed by eprocure-refresh.yml

# The always-on front ends that fetch the serve DB at boot. Restarting them is how
# a freshly published serve DB goes live; mirrors refresh_pipeline.ps1's defaults.
# The NL chat Space (scprs-warehouse-chat) was retired 2026-10-31 (paused, not
# deleted; see docs/WEB_APP.md) — add it back here if it is revived.
DEFAULT_SPACES = ("munther-hasan/scprs-warehouse-mcp",)


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


def _history_blob(repo: str, token: str | None, revision: str = "main") -> str | None:
    """Blob id of the persisted history file at ``revision``; None when absent.
    Raises (never returns None) when the dataset itself can't be read."""
    from huggingface_hub import HfApi

    info = _with_retries(
        lambda: HfApi().get_paths_info(
            repo, [HISTORY_FILENAME], revision=revision, repo_type="dataset", token=token
        ),
        what=f"history lookup in {repo}",
    )
    return info[0].blob_id if info else None


def _history_rows(path: Path) -> int:
    """Rows in ``dw_document_history`` of the SQLite file ``path`` (0 when absent).
    Stdlib-only on purpose: this module ships to the Spaces, warehouse.py doesn't."""
    import sqlite3

    path = Path(path)
    if not path.exists():
        return 0
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM dw_document_history").fetchone()[0]
    finally:
        con.close()


def _read_history_sidecar(history_path: Path) -> tuple[str, int]:
    """(blob id or ``absent``, rows fetched) recorded by ``fetch_history_db``."""
    sidecar = _rev_sidecar(history_path)
    if not sidecar.exists():
        raise WarehouseFetchError(
            f"{Path(history_path).name} was not fetched this run (no {sidecar.name}) -- "
            "refusing to publish a history that may not include the dataset's"
        )
    parts = sidecar.read_text(encoding="utf-8").split()
    return parts[0], int(parts[1]) if len(parts) > 1 else 0


def fetch_history_db(dest: Path, repo: str, token: str | None = None) -> bool:
    """Fetch the persisted contract history (``warehouse-history.db``) into ``dest``.

    FAIL-CLOSED: if the dataset can't be read, or it HAS the history but the
    download fails, this raises -- the run must stop rather than rebuild an empty
    history and publish it over the real one. Only a confirmed absence (the first
    run) returns False and leaves ``dest`` absent, so the build starts fresh.

    The blob id seen here (or ``absent``) and the row count fetched are recorded in
    a sidecar: the history publish refuses if the dataset's copy changed since (a
    lost-update guard scoped to this one file), and skips the upload entirely when
    the build appended nothing (the file is ~0.6 GB; most refreshes add no rows).
    """
    from huggingface_hub import HfApi

    dest = Path(dest)
    tok = token or _publish_token()
    try:
        sha = _with_retries(
            lambda: HfApi().dataset_info(repo, token=tok).sha, what=f"HEAD lookup of {repo}"
        )
        blob = _history_blob(repo, tok, revision=sha)
    except Exception as exc:  # noqa: BLE001 - re-raised as an actionable, fail-closed error
        raise WarehouseFetchError(
            f"cannot tell whether {repo!r} holds {HISTORY_FILENAME}: "
            f"{type(exc).__name__}: {exc}. Refusing to start history fresh -- check the "
            "token has READ access to the warehouse dataset."
        ) from exc
    sidecar = _rev_sidecar(dest)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    if blob is None:
        dest.unlink(missing_ok=True)
        sidecar.write_text("absent 0", encoding="utf-8")
        return False
    _download_db(repo, HISTORY_FILENAME, dest, tok, revision=sha)  # raises on failure
    sidecar.write_text(f"{blob} {_history_rows(dest)}", encoding="utf-8")
    return True


def _commit_with_history(
    repo: str,
    token: str | None,
    history_path: Path,
    extra_ops: list,
    message: str,
) -> str:
    """Commit ``history_path`` (plus ``extra_ops``) as ONE compare-and-swap commit.

    Refuses unless this run fetched the history (sidecar) and the dataset's copy is
    still the one it fetched. The check pins the HEAD it read and passes it as
    ``parent_commit``, so a commit landing between the check and this one makes the
    Hub reject ours (412) instead of silently overwriting it."""
    from huggingface_hub import CommitOperationAdd, HfApi

    expected, _ = _read_history_sidecar(history_path)
    head = _with_retries(
        lambda: HfApi().dataset_info(repo, token=token).sha, what=f"HEAD lookup of {repo}"
    )
    current = _history_blob(repo, token, revision=head) or "absent"
    if current != expected:
        raise WarehouseFetchError(
            f"lost-update guard: {repo}'s {HISTORY_FILENAME} changed since this run "
            f"fetched it ({expected[:8]} -> {current[:8]}) -- NOT overwriting; re-run"
        )
    ops = [
        *extra_ops,
        CommitOperationAdd(path_in_repo=HISTORY_FILENAME, path_or_fileobj=str(history_path)),
    ]
    info = _with_retries(
        lambda: HfApi(token=token).create_commit(
            repo_id=repo,
            repo_type="dataset",
            operations=ops,
            commit_message=message,
            parent_commit=head,
        ),
        what=f"history publish to {repo}",
    )
    _rev_sidecar(history_path).unlink(missing_ok=True)  # spent -- never reuse across runs
    return getattr(info, "commit_url", str(info))


def _commit_serve_and_history(
    serve_path: Path, history_path: Path, repo: str, token: str | None, log=print
) -> str:
    """Publish the serve DB, with the history in the SAME commit when it grew.

    A run never leaves the dataset with one updated and not the other. When the
    build appended no history rows (exported rows == rows fetched -- history is
    append-only and the export refuses to shrink), the ~0.6 GB history upload is
    skipped and only the serve DB is published: re-uploading an identical file
    four times a day is pure storage growth (every revision is kept)."""
    from huggingface_hub import CommitOperationAdd

    history_path = Path(history_path)
    if not history_path.exists():
        raise FileNotFoundError(f"{history_path} not found -- run `warehouse export-history`")
    _, fetched_rows = _read_history_sidecar(history_path)
    rows = _history_rows(history_path)
    if rows <= fetched_rows:
        log(f"history unchanged ({rows:,} rows) -- publishing the serve DB alone")
        _rev_sidecar(history_path).unlink(missing_ok=True)
        return _upload_db(serve_path, repo, SERVE_FILENAME, token, "Publish serve DB")
    log(f"history grew {fetched_rows:,} -> {rows:,} rows -- publishing it with the serve DB")
    return _commit_with_history(
        repo,
        token,
        history_path,
        [CommitOperationAdd(path_in_repo=SERVE_FILENAME, path_or_fileobj=str(serve_path))],
        "Publish serve DB + contract history",
    )


def merge_history(
    local_db: Path,
    repo: str,
    token: str | None = None,
    work_dir: Path | None = None,
    apply: bool = False,
    log=print,
) -> dict:
    """One-time merge of a LOCAL ``dw_document_history`` (e.g. the laptop's
    ``data/warehouse.db``, which holds the July 2026 snapshots CI never saw) into
    the dataset's persisted history.

    Fetches the dataset copy (fail-closed), builds a merged file -- local rows
    first so their earlier observations keep the lower keys, then the dataset's
    rows not already present by (document, version, signature) -- and refuses if
    the result has fewer rows than the dataset copy. Dry run unless ``apply``;
    with ``apply`` it publishes the history ALONE as one compare-and-swap commit
    (the next refresh seeds from it). Returns the counts."""
    import sqlite3

    from . import warehouse

    work = Path(work_dir or warehouse.HISTORY_DB.parent / "history-merge")
    work.mkdir(parents=True, exist_ok=True)
    fetched, merged_wh, out = (
        work / "fetched-history.db",
        work / "merge.db",
        work / HISTORY_FILENAME,
    )
    tok = token or _publish_token()
    fetch_history_db(fetched, repo, token=tok)
    for p in (merged_wh, out):
        p.unlink(missing_ok=True)
    con = sqlite3.connect(merged_wh)
    try:
        added_local = warehouse.seed_history(con, Path(local_db), log=log)
        added_remote = warehouse.seed_history(con, fetched, log=log)
    finally:
        con.close()
    rows = warehouse.export_history(merged_wh, out, log=log)
    _rev_sidecar(out).write_text(_rev_sidecar(fetched).read_text(encoding="utf-8"), "utf-8")
    remote_rows = _history_rows(fetched)
    counts = {
        "dataset_rows": remote_rows,
        "local_rows": added_local,
        "merged_rows": rows,
        "added_to_dataset": rows - remote_rows,
    }
    if rows < remote_rows:
        raise WarehouseFetchError(
            f"merged history has {rows:,} rows, fewer than the dataset's {remote_rows:,} -- "
            "refusing (history is append-only)"
        )
    log(
        f"merge: dataset {remote_rows:,} + local {added_local:,} -> {rows:,} rows "
        f"(+{rows - remote_rows:,} new to the dataset; {added_remote:,} dataset rows kept)"
    )
    if not apply:
        log("dry run -- nothing published; re-run with --apply to publish")
        return counts
    if rows == remote_rows:
        log("nothing new to publish")
        return counts
    counts["commit"] = _commit_with_history(
        repo, tok, out, [], "Merge local contract history (one-time seed)"
    )
    log(f"published merged history -> {repo}: {counts['commit']}")
    return counts


def publish_serve_db(
    serve_path: Path, repo: str, token: str | None = None, history_path: Path | None = None
) -> str:
    """Upload the serve DB to the private HF Dataset ``repo`` (created if missing).

    Returns the commit URL. Called by the pipeline after a build; the token defaults
    to ``HF_TOKEN`` / the caller's cached HF login. When the sibling BI mart-CSV
    directory exists (``warehouse serve-export`` writes it), it is published too,
    under ``marts/`` — the Power BI Web-connector feed (docs/POWER_BI.md). The feed
    is mirrored, not just added to: a mart file no longer exported locally is
    deleted from ``marts/`` in the same commit, so Power BI never keeps reading a
    removed or renamed mart's last (frozen) file without an error.

    ``history_path`` (CI only) publishes the persisted contract history in the SAME
    commit as the serve DB; ``_commit_serve_and_history`` holds its guards.
    """
    serve_path = Path(serve_path)
    if not serve_path.exists():
        raise FileNotFoundError(f"{serve_path} not found — run `warehouse serve-export` first")
    tok = token or os.environ.get("HF_TOKEN")
    if history_path is not None:
        url = _commit_serve_and_history(serve_path, history_path, repo, tok)
    else:
        url = _upload_db(serve_path, repo, SERVE_FILENAME, tok, "Publish serve DB")
    marts_dir = serve_path.parent / "marts"
    has_feed = marts_dir.is_dir() and (
        any(marts_dir.glob("*.csv")) or any(marts_dir.glob("star/*.parquet"))
    )
    if has_feed:
        from huggingface_hub import HfApi

        _with_retries(
            lambda: HfApi(token=token or os.environ.get("HF_TOKEN")).upload_folder(
                folder_path=str(marts_dir),
                repo_id=repo,
                repo_type="dataset",
                path_in_repo="marts",
                allow_patterns=_MART_PATTERNS,
                # relative to path_in_repo; files being uploaded are never deleted
                delete_patterns=_MART_PATTERNS,
                commit_message="Publish BI mart CSVs + star parquet",
            ),
            what=f"BI feed publish to {repo}",
        )
    return url


def backup_operational(
    backup_repo: str,
    *,
    source_repo: str | None = None,
    read_token: str | None = None,
    write_token: str | None = None,
    keep: int = 4,
    today: str | None = None,
) -> str:
    """Weekly cold backup: copy the operational stores to a SECOND dataset.

    ``scprs.db`` is the one non-rebuildable artifact in the pipeline — months of
    scraping living in a single HF dataset whose revision history protects
    against bad writes but not against repo deletion or token misuse. This
    copies ``scprs.db`` + the side inputs into ``backup_repo`` under a dated
    folder and prunes to the newest ``keep`` snapshots. Same-account residual
    risk (an account-level compromise takes both) is documented in the workflow;
    a quarterly manual download covers that tail.

    ``today`` (YYYY-MM-DD) names the snapshot folder; defaults to the current
    date. Returns the folder name."""
    import tempfile

    from huggingface_hub import CommitOperationDelete, HfApi, hf_hub_download

    source_repo = source_repo or os.environ.get("SCPRS_DATASET")
    if not source_repo:
        raise SystemExit("set --dataset or the SCPRS_DATASET env var")
    read_tok = read_token or _operational_token()
    write_tok = write_token or os.environ.get("HF_BACKUP_TOKEN")
    if not write_tok:
        raise SystemExit("set HF_BACKUP_TOKEN (write on the backup dataset)")
    api = HfApi(token=write_tok)
    api.create_repo(backup_repo, repo_type="dataset", private=True, exist_ok=True)

    folder = today or datetime.date.today().isoformat()
    src_api = HfApi(token=read_tok)
    names = [
        f for f in src_api.list_repo_files(source_repo, repo_type="dataset") if f.endswith(".db")
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for name in names:
            # nosec B615 — backing up the LATEST revision of our own private
            # dataset is the entire point; pinning a commit would freeze it.
            local = hf_hub_download(  # nosec B615
                source_repo,
                name,
                repo_type="dataset",
                token=read_tok,
                local_dir=tmp,
            )
            _with_retries(
                lambda local=local, name=name: api.upload_file(
                    path_or_fileobj=local,
                    path_in_repo=f"{folder}/{name}",
                    repo_id=backup_repo,
                    repo_type="dataset",
                    commit_message=f"Cold backup {folder}: {name}",
                ),
                what=f"backup upload of {name}",
            )

    # Retention: keep the newest `keep` dated snapshot folders.
    backup_files = api.list_repo_files(backup_repo, repo_type="dataset")
    snapshots = sorted({f.split("/", 1)[0] for f in backup_files if "/" in f})
    stale = snapshots[:-keep] if keep and len(snapshots) > keep else []
    if stale:
        ops = [CommitOperationDelete(path_in_repo=f"{s}/", is_folder=True) for s in stale]
        _with_retries(
            lambda: api.create_commit(
                repo_id=backup_repo,
                repo_type="dataset",
                operations=ops,
                commit_message=f"Prune backups beyond newest {keep}: {', '.join(stale)}",
            ),
            what="backup retention prune",
        )
    return folder


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


def fetch_eprocure_db(dest: Path, repo: str | None = None, token: str | None = None) -> bool:
    """Best-effort fetch of the eProcure registry side input from the operational
    dataset. Like ``fetch_cmas_db``: False when unconfigured or not yet published
    (the refresh workflow's shrink gate treats that as a first publish)."""
    repo = repo or os.environ.get("SCPRS_DATASET")
    if not repo:
        return False
    try:
        _download_db(repo, EPROCURE_FILENAME, dest, token or _operational_token())
    except WarehouseFetchError:
        return False  # optional side input — absent until first published
    return True


def publish_eprocure_db(db_path: Path, repo: str, token: str | None = None) -> str:
    """Upload the eProcure registry store (``eprocure.db``) alongside ``scprs.db``
    in the operational dataset. Written by the scheduled ``eprocure-refresh``
    workflow (which runs ``src.eprocure extract-registry`` first), never by a
    person. Returns the commit URL."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"{db_path} not found — run `python -m src.eprocure extract-registry` first"
        )
    return _upload_db(
        db_path,
        repo,
        EPROCURE_FILENAME,
        token or _operational_token(),
        "Publish eProcure registry side input (CI refresh)",
    )


# How many recent commits rollback searches for the previous serve DB.
_ROLLBACK_SEARCH = 20


def rollback_serve_db(repo: str, token: str | None = None) -> str:
    """Revert the serve dataset to its previous revision. Returns the commit URL.

    Wave 3 auto-rollback: if a refresh publishes a serve DB and go-live
    verification then fails, this re-publishes the **prior** revision of
    ``warehouse-serve.db`` as a new commit, so the Spaces fetch known-good data on
    their next boot instead of being stuck on a bad/unverified snapshot. The serve
    dataset is git-based, so every ``publish`` is a versioned commit.

    The last-good copy is the newest revision whose ``warehouse-serve.db`` blob
    DIFFERS from the current one -- not simply one commit back: a publish makes
    two commits (the DB, then ``marts/``), so ``commits[1]`` is usually the bad
    DB itself. Raises if no earlier differing revision exists.

    Only the serve DB is reverted; ``warehouse-history.db`` deliberately stays at
    its newest copy. History is append-only observations of the source, and a
    go-live failure is about what the Space serves, not about those observations
    (the build already passed its DQ gate) -- reverting it would drop changes the
    next build can never re-observe (an amended contract's prior values)."""
    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi()
    commits = api.list_repo_commits(repo, repo_type="dataset", token=token)

    def blob(rev: str) -> str | None:
        info = api.get_paths_info(
            repo, [SERVE_FILENAME], revision=rev, repo_type="dataset", token=token
        )
        return info[0].blob_id if info else None

    current = blob(commits[0].commit_id) if commits else None
    prev = next(
        (
            c.commit_id
            for c in commits[1:_ROLLBACK_SEARCH]
            if blob(c.commit_id) not in (None, current)
        ),
        None,
    )
    if current is None or prev is None:
        raise WarehouseFetchError(
            f"cannot roll back {repo!r}: no earlier revision of {SERVE_FILENAME} "
            f"in the last {_ROLLBACK_SEARCH} commits"
        )
    cached = hf_hub_download(  # nosec B615 — our own private dataset, explicit prior rev
        repo_id=repo,
        filename=SERVE_FILENAME,
        repo_type="dataset",
        revision=prev,
        token=token,
    )
    return _upload_db(Path(cached), repo, SERVE_FILENAME, token, f"Rollback serve DB to {prev[:8]}")


# --------------------------------------------------------------- storage pruning

# Squash only when the history is at least this long (~3 days of publishes on
# either dataset), so a weekly prune always has real growth to reclaim.
PRUNE_MIN_COMMITS = 20


class PruneError(RuntimeError):
    """A history squash was refused, or its result could not be confirmed."""


def _tree(api, repo: str, token: str | None, revision: str) -> dict[str, str]:
    """{path: blob id} of every file at ``revision`` -- the squash must not change it."""
    return {
        f.path: f.blob_id
        for f in api.list_repo_tree(
            repo, recursive=True, revision=revision, repo_type="dataset", token=token
        )
        if getattr(f, "blob_id", None)
    }


def prune_history(
    repo: str,
    token: str | None = None,
    *,
    apply: bool = False,
    min_commits: int = PRUNE_MIN_COMMITS,
    log=print,
) -> dict:
    """Reclaim dataset storage by squashing ``main``'s history to ONE commit.

    Why: every publish keeps a full copy of the DB in the dataset's history and
    nothing ever drops it (2026-10-29: operational 482 GB, warehouse ~208 GB; a PRO
    account includes 1 TB of private storage). ``super_squash_history`` collapses
    the branch to a single commit holding exactly the HEAD files; the Hub frees
    the unreferenced objects (its docs: reflected in the quota within ~36h).

    IRREVERSIBLE -- so this is a dry run (report only) unless ``apply``. Callers
    must hold the dataset's writer lock (CI concurrency group): the squash takes
    no parent revision, so the guard here is a HEAD re-check just before it plus a
    before/after comparison of every file's blob id, which must be identical.

    Rollback: ``rollback_serve_db`` needs an earlier revision holding a different
    serve DB. After a squash the single commit holds the CURRENT (verified-live)
    serve DB, and the next publish adds commits on top of it -- so a go-live
    failure right after still finds that copy. The caller must only squash when
    HEAD is known good (the workflow checks the last refresh succeeded).
    """
    from huggingface_hub import HfApi

    api = HfApi()
    commits = _with_retries(
        lambda: api.list_repo_commits(repo, repo_type="dataset", token=token),
        what=f"commit list of {repo}",
    )
    head = commits[0].commit_id
    info = _with_retries(
        lambda: api.dataset_info(repo, token=token, expand=["usedStorage"]),
        what=f"storage lookup of {repo}",
    )
    used = getattr(info, "used_storage", None)
    refs = api.list_repo_refs(repo, repo_type="dataset", token=token)
    other_refs = [b.ref for b in refs.branches if b.name != "main"] + [
        r.ref for r in (*refs.converts, *refs.tags)
    ]
    report = {
        "repo": repo,
        "commits": len(commits),
        "used_storage_gb": round(used / 1e9, 1) if used is not None else None,
        "head": head,
        "other_refs": other_refs,
    }
    log(
        f"{repo}: {len(commits)} commit(s) on main, "
        f"{report['used_storage_gb']} GB used storage, HEAD {head[:8]}"
    )
    if other_refs:
        # Objects these refs point at stay billed after a squash of main.
        log(f"  note: other refs keep their own objects: {', '.join(other_refs)}")
    if len(commits) < min_commits:
        log(f"  {len(commits)} < {min_commits} commits -- nothing worth squashing")
        return {**report, "action": "skip"}
    if not apply:
        log(f"  DRY RUN: would squash {len(commits)} commits into 1 (re-run with --apply)")
        return {**report, "action": "dry-run"}

    before = _tree(api, repo, token, head)
    now = api.dataset_info(repo, token=token).sha
    if now != head:
        raise PruneError(f"{repo} HEAD moved {head[:8]} -> {now[:8]} -- a writer is active")
    api.super_squash_history(
        repo,
        branch="main",
        repo_type="dataset",
        token=token,
        commit_message=f"Storage prune: squash {len(commits)} commits (HEAD {head[:8]} files)",
    )
    after_commits = api.list_repo_commits(repo, repo_type="dataset", token=token)
    after = _tree(api, repo, token, after_commits[0].commit_id)
    if after != before:
        changed = sorted(set(before.items()) ^ set(after.items()))[:5]
        raise PruneError(f"{repo}: files differ after the squash: {changed}")
    log(
        f"  squashed {len(commits)} -> {len(after_commits)} commit(s); "
        f"{len(after)} file(s) unchanged. Storage drops within ~36h."
    )
    return {**report, "action": "squashed", "commits_after": len(after_commits)}


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


def publish_app(app_dir: Path, repo: str, token: str | None = None) -> str:
    """Upload the vendor web app's static JSON (``app_export``) to the PUBLIC HF
    Dataset ``repo`` as one commit, deleting files the export no longer writes (a
    department or supplier that left the warehouse must not keep a stale page).

    Unlike the private stores, the repo is never created here: it is public by
    design, so it must be created deliberately (HF UI) — a typo'd name fails with a
    404 instead of minting a stray repo. ``meta.json`` must exist, so an empty or
    half-written directory is never published.
    """
    from huggingface_hub import HfApi

    app_dir = Path(app_dir)
    if not (app_dir / "meta.json").exists():
        raise FileNotFoundError(
            f"{app_dir / 'meta.json'} not found — run `python -m src.app_export` first"
        )
    info = _with_retries(
        lambda: HfApi(token=token).upload_folder(
            folder_path=str(app_dir),
            repo_id=repo,
            repo_type="dataset",
            allow_patterns=["*.json"],
            delete_patterns=["*.json"],
            commit_message="Publish vendor app JSON",
        ),
        what=f"app publish to {repo}",
    )
    return getattr(info, "commit_url", str(info))


def _app_token() -> str | None:
    """Token for the public app dataset: prefer the dedicated ``HF_APP_TOKEN``
    (fine-grained, write on that one dataset only), fall back to ``HF_TOKEN``."""
    return os.environ.get("HF_APP_TOKEN") or os.environ.get("HF_TOKEN")


def _cli() -> None:
    import argparse

    from . import model, warehouse

    ap = argparse.ArgumentParser(description="Sync pipeline SQLite stores with private HF Datasets")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pub = sub.add_parser("publish", help="Upload data/warehouse-serve.db to the dataset")
    pub.add_argument("--dataset", default=os.environ.get("WAREHOUSE_DATASET"))
    pub.add_argument(
        "--with-history",
        action="store_true",
        help="also publish data/warehouse-history.db in the same commit (CI only: needs "
        "fetch-history earlier in the same run)",
    )

    fhi = sub.add_parser(
        "fetch-history",
        help="Download warehouse-history.db (persisted contract history); fails closed",
    )
    fhi.add_argument("--dataset", default=os.environ.get("WAREHOUSE_DATASET"))
    fhi.add_argument("--dest", default=str(warehouse.HISTORY_DB))

    mhi = sub.add_parser(
        "merge-history",
        help="One-time: merge a local warehouse.db's dw_document_history into the dataset's "
        "persisted history (dry run unless --apply)",
    )
    mhi.add_argument("--dataset", default=os.environ.get("WAREHOUSE_DATASET"))
    mhi.add_argument("--local", default=str(warehouse.WAREHOUSE_DB))
    mhi.add_argument("--apply", action="store_true", help="publish (default: dry run)")

    fsv = sub.add_parser(
        "fetch-serve",
        help="Download warehouse-serve.db from WAREHOUSE_DATASET (read-only review/replay jobs)",
    )
    fsv.add_argument("--dest", default=str(warehouse.SERVE_DB))

    bck = sub.add_parser(
        "backup-operational",
        help="Cold-copy scprs.db + side inputs to a second dataset (weekly backup.yml)",
    )
    bck.add_argument("--dataset", default=os.environ.get("SCPRS_DATASET"))
    bck.add_argument(
        "--backup-dataset",
        default=os.environ.get("SCPRS_BACKUP_DATASET", "munther-hasan/scprs-operational-backup"),
    )
    bck.add_argument("--keep", type=int, default=4)

    fop = sub.add_parser(
        "fetch-operational",
        help="Download scprs.db (and the supplier/CMAS/eProcure side inputs, if published) "
        "from its dataset",
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

    pep = sub.add_parser(
        "publish-eprocure",
        help="Upload eprocure.db to the operational dataset (scheduled eprocure-refresh workflow)",
    )
    pep.add_argument("--dataset", default=os.environ.get("SCPRS_DATASET"))
    pep.add_argument("--path", default=None)

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
    papp = sub.add_parser(
        "publish-app", help="Upload data/app/ (vendor app JSON) to the PUBLIC app dataset"
    )
    papp.add_argument("--dataset", default=os.environ.get("APP_DATASET"))
    papp.add_argument("--path", default=None, help="app JSON directory (default: data/app)")
    prn = sub.add_parser(
        "prune-history",
        help="Squash a dataset's commit history to reclaim storage (dry run unless --apply; "
        "IRREVERSIBLE with --apply)",
    )
    prn.add_argument("--dataset", required=True)
    prn.add_argument(
        "--kind",
        choices=("operational", "warehouse"),
        required=True,
        help="which token to use: the operational writer's or the warehouse publisher's",
    )
    prn.add_argument("--min-commits", type=int, default=PRUNE_MIN_COMMITS)
    prn.add_argument("--apply", action="store_true", help="really squash (default: report only)")

    args = ap.parse_args()

    if args.cmd == "publish":
        if not args.dataset:
            raise SystemExit("set --dataset or the WAREHOUSE_DATASET env var")
        if args.with_history:
            url = publish_serve_db(
                warehouse.SERVE_DB,
                args.dataset,
                token=_publish_token(),
                history_path=warehouse.HISTORY_DB,
            )
        else:
            url = publish_serve_db(warehouse.SERVE_DB, args.dataset, token=_publish_token())
        print(f"Published {warehouse.SERVE_DB.name} -> {args.dataset}: {url}")
    elif args.cmd == "fetch-history":
        if not args.dataset:
            raise SystemExit("set --dataset or the WAREHOUSE_DATASET env var")
        if fetch_history_db(Path(args.dest), args.dataset, token=_publish_token()):
            print(f"Fetched {HISTORY_FILENAME} <- {args.dataset} into {args.dest}")
        else:
            print(f"Note: {HISTORY_FILENAME} not in {args.dataset} yet -- history starts fresh")
    elif args.cmd == "merge-history":
        if not args.dataset:
            raise SystemExit("set --dataset or the WAREHOUSE_DATASET env var")
        merge_history(Path(args.local), args.dataset, token=_publish_token(), apply=args.apply)
    elif args.cmd == "fetch-serve":
        if not ensure_local_db(Path(args.dest)):
            raise SystemExit("set the WAREHOUSE_DATASET env var")
        print(f"Fetched {SERVE_FILENAME} into {args.dest}")
    elif args.cmd == "backup-operational":
        folder = backup_operational(args.backup_dataset, source_repo=args.dataset, keep=args.keep)
        print(f"Backed up operational stores -> {args.backup_dataset}/{folder} (keep {args.keep})")
    elif args.cmd == "fetch-operational":
        if not args.dataset:
            raise SystemExit("set --dataset or the SCPRS_DATASET env var")
        fetch_operational_db(Path(args.dest), repo=args.dataset)
        print(f"Fetched {OPERATIONAL_FILENAME} <- {args.dataset} into {args.dest}")
        # Optional side inputs — the warehouse build skips either if absent.
        for label, path, fetch in (
            (SUPPLIER_FILENAME, warehouse.ENRICHMENT_DB, fetch_supplier_db),
            (CMAS_FILENAME, warehouse.CMAS_DB, fetch_cmas_db),
            (EPROCURE_FILENAME, warehouse.EPROCURE_DB, fetch_eprocure_db),
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
    elif args.cmd == "publish-eprocure":
        from . import eprocure

        if not args.dataset:
            raise SystemExit("set --dataset or the SCPRS_DATASET env var")
        url = publish_eprocure_db(Path(args.path or eprocure.DB_PATH), args.dataset)
        print(f"Published {EPROCURE_FILENAME} -> {args.dataset}: {url}")
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
    elif args.cmd == "publish-app":
        from .app_export import APP_DIR

        if not args.dataset:
            raise SystemExit("set --dataset or the APP_DATASET env var")
        url = publish_app(Path(args.path or APP_DIR), args.dataset, token=_app_token())
        print(f"Published app JSON -> {args.dataset}: {url}")
    elif args.cmd == "prune-history":
        tok = _operational_token() if args.kind == "operational" else _publish_token()
        prune_history(args.dataset, token=tok, apply=args.apply, min_commits=args.min_commits)


if __name__ == "__main__":
    _cli()
