"""Extract Cal eProcure's SB/DVBE certified-supplier registry.

The public registry search on caleprocure.ca.gov is an InFlight NLX overlay on
the same PeopleSoft instance SCPRS runs on (``suppliers.fiscal.ca.gov``). Recon
(2026-07-23) showed the overlay's JSON relay is not directly drivable — GETs
return empty page shells and the WAF black-holes non-browser POSTs at the TLS
layer (curl reset, httpx read-timeout) — but the underlying PeopleSoft component
``ZZ_PO.ZZ_PUBSRCH.GBL`` is **anonymously reachable with a headless browser**,
the mechanism this repo already uses for SCPRS. So this module drives that
component directly with Playwright.

The flow, all anonymous — no login, no secret:

1. ``GET`` the component → the search form (six certification-type checkboxes:
   MB, SB, SB-PW, DVBE, NVSA, NP).
2. Check every certification type, run the search, and read the result grid's
   row-count banner. Its literal format is ``"1-10 of 21450"`` (class
   ``PSGRIDCOUNTER``) — note this differs from the SCPRS grid's
   ``"1 to 200 of 206"``; both were verified against live text (see #49 for why
   the format is never assumed).
3. Click the grid's **Download-to-file** export and load the result. Unlike
   SCPRS's detail export (which silently drops line-item dollars and is
   deliberately unused), this export was reconciled field-by-field against the
   site's own result grid during recon and is trustworthy. It is the classic
   PeopleSoft ".xls" HTML table with the usual quirks (leading apostrophes on
   list-valued columns, MM/DD/YYYY dates), cleaned at load time.

Grain: one row per **certification track** — a firm holding e.g. both SB(Micro)
and DVBE appears once per track, same Certification ID, different type/dates.
The grid banner counts distinct firms (unique Certification IDs); the export
has more rows than the banner total. Completeness is therefore judged on
unique Certification IDs vs the banner, with a small tolerance for live churn
between the search and the download.

Output is a standalone store, deliberately separate from the SCPRS pipeline:
``data/eprocure.db`` (table ``registry`` + ``extract_meta``) and a CSV. Run:

    python -m src.eprocure extract-registry            # -> data/eprocure.db + CSV
    python -m src.eprocure extract-registry --show     # headed browser (debug)
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from src.supplier_master import normalize_name

# Chromium needs --no-sandbox when running as non-root in a container; set the
# PLAYWRIGHT_NO_SANDBOX env var there (the Dockerfile does). No effect locally.
_CHROMIUM_ARGS = ["--no-sandbox"] if os.environ.get("PLAYWRIGHT_NO_SANDBOX") else []

REGISTRY_URL = "https://suppliers.fiscal.ca.gov/psc/psfpd1/SUPPLIER/ERP/c/ZZ_PO.ZZ_PUBSRCH.GBL"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "eprocure.db"

# The six certification-type checkboxes, in on-page order (verified 2026-07-23):
# 0 Micro Business (MB), 1 Small Business (SB), 2 Small Business for the Purpose
# of Public Works (SB-PW), 3 Disabled Veteran Business Enterprise (DVBE),
# 4 Non-Profit Veteran Service Agency (NVSA), 5 Non-Profit Recognition (NP).
_CERT_FLAG = "ZZ_PUBSRCH1_WRK_FLAG1${}"
_N_CERT_TYPES = 6
_SEARCH = '[id="ZZ_PUBSRCH1_WRK_BUTTON"]'
_DOWNLOAD = '[id="ZZ_PUBSRCH1_WRK_DOWNLOAD_TO_FILE"]'
# Result-grid row-count banner, e.g. "1-10 of 21450" — the *live* literal format
# for this component; do not "fix" it to the SCPRS format (see #49).
REGISTRY_BANNER = r"(\d[\d,]*)-(\d[\d,]*) of (\d[\d,]*)"

# The export puts a leading apostrophe on every element of these list-valued
# columns ("'22101700,'22101900"); other text columns only get one on the first
# character (and names may contain legitimate apostrophes, so they are only
# lstripped, never rewritten).
_LIST_COLUMNS = (
    "UNSPSC",
    "NAICS",
    "Service Areas",
    "License",
    "Industry Type",
    "Supplier Diversity Certs",
)
_DATE_COLUMNS = ("Start Date", "End Date")

# Completeness tolerance: the banner counts distinct firms at search time, the
# export is generated moments later — recon saw them differ by 4 in 21,450
# (live churn). Anything below this ratio means a truncated export.
_COMPLETE_RATIO = 0.99


class EprocureError(RuntimeError):
    """An eProcure extraction step failed in a way worth surfacing clearly."""


# ------------------------------------------------------------------ extraction


def download_registry(
    *,
    out_dir: Path = DATA_DIR,
    headless: bool = True,
    timeout_ms: int = 180_000,
) -> tuple[Path, int]:
    """Search all certification types and download the registry export.

    Returns ``(xls_path, banner_total)`` where ``banner_total`` is the distinct
    firm count the site's own result banner reported for this search.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_CHROMIUM_ARGS)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(REGISTRY_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_selector(f'[id="{_CERT_FLAG.format(0)}"]', timeout=timeout_ms)

            # Each check can trigger a server round trip; pace them, then verify
            # every box actually took before searching (silently-unchecked boxes
            # would narrow the search — same failure family as the SCPRS date
            # fields that ignore fill()).
            for i in range(_N_CERT_TYPES):
                box = page.locator(f'[id="{_CERT_FLAG.format(i)}"]')
                if not box.is_checked():
                    box.check()
                    page.wait_for_timeout(1200)
            unchecked = [
                i
                for i in range(_N_CERT_TYPES)
                if not page.locator(f'[id="{_CERT_FLAG.format(i)}"]').is_checked()
            ]
            if unchecked:
                raise EprocureError(
                    f"Certification checkboxes {unchecked} did not commit; the "
                    "search would silently cover a subset of the registry."
                )

            page.click(_SEARCH)
            banner_total = _poll_banner_total(page, timeout_ms)

            with page.expect_download(timeout=timeout_ms) as dl_info:
                page.click(_DOWNLOAD)
            dest = out_dir / "eprocure_registry.xls"
            dl_info.value.save_as(str(dest))
            return dest, banner_total
        except PWTimeout as e:
            raise EprocureError(f"Timed out driving the registry search: {e}") from e
        finally:
            browser.close()


def _poll_banner_total(page, timeout_ms: int) -> int:
    """Wait for the result grid's banner and return its total (distinct firms).

    The search is a slow server round-trip behind a glass pane (~15-30s); poll
    for the banner text rather than sleeping a fixed time.
    """
    deadline = timeout_ms
    waited = 0
    while waited < deadline:
        page.wait_for_timeout(1000)
        waited += 1000
        banner = page.locator(".PSGRIDCOUNTER").first
        if banner.count():
            text = banner.inner_text()
            m = re.search(REGISTRY_BANNER, text)
            if m:
                return int(m.group(3).replace(",", ""))
        if "no matching values" in page.inner_text("body").lower():
            raise EprocureError("Registry search returned no results — not overwriting.")
    raise EprocureError("Result banner never appeared; the search did not complete.")


# ---------------------------------------------------------------------- loading


def load_registry(path: Path):
    """Parse the downloaded export (.xls = HTML table) into a tidy DataFrame.

    Cleans the PeopleSoft quirks: leading apostrophes (every element of the
    list-valued columns carries one), MM/DD/YYYY dates -> ISO, and adds a
    ``normalized_name`` join key matching the convention gold uses for
    supplier-side inputs (join is by normalized name, so certifications attach
    to the canonical vendor entity).
    """
    import pandas as pd

    df = pd.read_html(path)[0]
    for col in df.columns:
        if pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].astype("string").str.strip()
            if col in _LIST_COLUMNS:
                df[col] = df[col].map(_clean_list_value, na_action="ignore")
            else:
                df[col] = df[col].str.lstrip("'")
    for col in _DATE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format="%m/%d/%Y", errors="coerce").dt.strftime(
                "%Y-%m-%d"
            )
    df["normalized_name"] = df["Legal Business Name"].map(normalize_name, na_action="ignore")
    return df


def _clean_list_value(value: str) -> str:
    """Normalize a list-valued export cell: "'A,'B,'C," -> "A, B, C"."""
    parts = [p.lstrip("'").strip() for p in value.split(",")]
    return ", ".join(p for p in parts if p)


# ------------------------------------------------------------------ persistence


def write_registry(df, banner_total: int, db_path: Path = DB_PATH) -> dict:
    """Full idempotent refresh of the registry store; returns a run summary.

    The whole search is one bulk pull, so the refresh is a drop-and-reload (the
    scoped-delete degenerate case: the scope is the entire table). ``complete``
    is judged on unique Certification IDs vs the site's own banner total and is
    raised as an error rather than recorded quietly — a partial export must
    never look like a finished run to callers (CI publishes only on success).
    """
    rows = len(df)
    unique_ids = int(df["Certification ID"].nunique())
    if rows == 0:
        raise EprocureError("Export parsed to 0 rows — not writing.")
    if unique_ids < banner_total * _COMPLETE_RATIO:
        raise EprocureError(
            f"Export looks truncated: {unique_ids} unique certification ids vs "
            f"the site's banner total of {banner_total} — not writing."
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(df.columns)
    quoted = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    con = sqlite3.connect(db_path)
    try:
        con.execute("DROP TABLE IF EXISTS registry")
        con.execute(f"CREATE TABLE registry ({quoted})")  # noqa: S608 — cols from the export header, internal
        con.executemany(
            f"INSERT INTO registry ({quoted}) VALUES ({placeholders})",  # noqa: S608 — same
            # astype(object) first: string-dtype columns keep pd.NA (which
            # sqlite3 can't bind) through a plain .where(); object columns get
            # real None.
            df.astype(object).where(df.notna(), None).itertuples(index=False, name=None),
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS extract_meta ("
            "extracted_at TEXT, banner_total INTEGER, rows INTEGER, unique_cert_ids INTEGER)"
        )
        con.execute("DELETE FROM extract_meta")
        con.execute(
            "INSERT INTO extract_meta VALUES (?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                banner_total,
                rows,
                unique_ids,
            ),
        )
        con.commit()
    finally:
        con.close()
    return {"rows": rows, "unique_cert_ids": unique_ids, "banner_total": banner_total}


# ------------------------------------------------------------------------- CLI


def extract_registry(
    db_path: Path = DB_PATH,
    out_dir: Path = DATA_DIR,
    *,
    headless: bool = True,
) -> dict:
    """Download + load + store the full registry. Returns the run summary."""
    xls, banner_total = download_registry(out_dir=out_dir, headless=headless)
    df = load_registry(xls)
    summary = write_registry(df, banner_total, db_path)
    csv_path = out_dir / "eprocure_registry.csv"
    df.to_csv(csv_path, index=False)
    print(
        f"registry: {summary['rows']} rows ({summary['unique_cert_ids']} firms, "
        f"site banner {banner_total}) -> {db_path.name}, {csv_path.name}"
    )
    return summary


def _cli() -> None:
    ap = argparse.ArgumentParser(
        description="Extract Cal eProcure's SB/DVBE certified-supplier registry."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract-registry", help="Full registry -> data/eprocure.db + CSV")
    ex.add_argument("--db", type=Path, default=DB_PATH)
    ex.add_argument("--out", type=Path, default=DATA_DIR)
    ex.add_argument("--show", action="store_true", help="Run browser headed (visible)")

    args = ap.parse_args()
    if args.cmd == "extract-registry":
        extract_registry(args.db, args.out, headless=not args.show)


if __name__ == "__main__":
    _cli()
