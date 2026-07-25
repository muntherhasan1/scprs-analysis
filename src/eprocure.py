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


# ------------------------------------------------------------- CSCR events


# The CSCR event search — the second eProcure surface (recon 2026-07-24, gate
# probe for the events archive). Award/result data was verified ABSENT from
# event details (8/8 sampled Historical pages carry only solicitation
# metadata; no award search surface or status filter exists anywhere), so this
# extracts solicitation metadata only, and only the lean slices: currently
# Posted events plus recent Historical years. The deep 2015+ archive is a
# deliberate non-goal until something needs it.
#
# Mechanism differs from the registry: the suppliers.fiscal.ca.gov component
# (`AUC_MANAGE_BIDS.AUC_RESP_INQ_AUC.GBL`) TLS-black-holes cold direct hits,
# so this drives the caleprocure.ca.gov overlay page instead, which proxies
# the same component through its /nlx3 relay. Overlay quirks, all load-bearing:
# - Headless Chromium's default "HeadlessChrome" UA gets a WAF 403; a normal
#   Chrome UA string must be set on the context.
# - The overlay clones the hidden PeopleSoft DOM into a visible table, so
#   every id exists twice — only :visible elements are interactable.
# - The result banner is a THIRD live format, "Showing Results 1-50 of 5702"
#   (registry: "1-10 of 21450"; SCPRS: "1 to 200 of 206") — see #49 for why
#   formats are read from live text, never assumed.
# - The grid's Download button never produces a file (verified twice, 240s);
#   pagination via the pager's Next button is the working mechanism. Posted
#   renders in one page; Historical chunks at 50 rows.
EVENTS_URL = "https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
EVENTS_BANNER = r"Showing Results (\d[\d,]*)-(\d[\d,]*) of (\d[\d,]*)"
_EVENT_STATUS = '[id="RESP_INQA_WK_ZZ_EVENT_STATUS"]'
_EVENT_YEAR = '[id="RESP_INQA_WK_YEAR"]'  # only rendered in Historical mode
_EVENT_SEARCH = '[id="RESP_INQA_WK_INQ_AUC_GO_PB"]'
_EVENT_NEXT = 'button[aria-label="Next"]:visible'
# Overlay grid cells carry stable data-if-label attributes; ids are cloned.
_EVENT_CELLS = {
    "event_id": "tdEventId",
    "event_name": "tdEventName",
    "department_name": "tdDepName",
    "published_raw": "tdPubDate",
    "end_raw": "tdEndDate",
    "status": "tdStatus",
}
_EVENT_DT = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def default_event_years(today: datetime | None = None) -> tuple[int, ...]:
    """The lean Historical window: the current calendar year and the two prior."""
    year = (today or datetime.now(timezone.utc)).year
    return (year - 2, year - 1, year)


def _tidy_event(raw: dict, search_status: str, search_year: int | None) -> dict:
    """Normalize one scraped grid row: collapse whitespace, derive ISO dates and
    the SB/DVBE set-aside flags departments encode in event names
    (e.g. "**SB Only**", "*DVBE Only*")."""
    row = {k: " ".join((raw.get(k) or "").split()) for k in _EVENT_CELLS}
    for src, dest in (("published_raw", "published_date"), ("end_raw", "end_date")):
        m = _EVENT_DT.search(row[src])
        row[dest] = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None
    name = row["event_name"].upper()
    row["sb_only"] = 1 if "SB ONLY" in name else 0
    row["dvbe_only"] = 1 if "DVBE ONLY" in name else 0
    row["search_status"] = search_status
    row["search_year"] = search_year
    return row


def _page_signature(page) -> str:
    """First visible event id + visible row count — cheap identity of the
    currently rendered grid page, used to reject stale reads mid re-render."""
    return page.evaluate(
        """() => {
          const tds = [...document.querySelectorAll("td[data-if-label='tdEventId']")]
                        .filter(td => td.offsetParent !== null);
          return tds.length ? tds[0].innerText.trim() + '|' + tds.length : '';
        }"""
    )


def _poll_events_page(
    page, timeout_ms: int, expect_start: int | None = None, prev_sig: str | None = None
) -> tuple[int, int, int]:
    """Wait until the grid shows the expected window *and* its rows are rendered.

    Three conditions, all from live page state (never a fixed sleep): the banner's
    window starts at ``expect_start`` (the relay round trip finished), at least
    the window's row count is visible (the overlay clones PeopleSoft rows into
    its table a few seconds *after* the banner text lands — scraping at banner
    time reads an empty grid), and the page signature moved past ``prev_sig``
    (not a stale render of the previous page). Returns ``(start, end, total)``.
    """
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(1500)
        waited += 1500
        m = re.search(EVENTS_BANNER, page.inner_text("body"))
        if not m:
            continue
        start, end, total = (int(g.replace(",", "")) for g in m.groups())
        if expect_start is not None and start != expect_start:
            continue
        visible_sig = _page_signature(page)
        if not visible_sig or int(visible_sig.split("|")[1]) < end - start + 1:
            continue
        if prev_sig is not None and visible_sig == prev_sig:
            continue
        return start, end, total
    what = "appeared" if expect_start is None else f"reached row {expect_start}"
    raise EprocureError(f"Events banner/rows never {what}; the search/page turn did not complete.")


def _scrape_events_page(page) -> list[dict]:
    """Read the visible grid rows via their data-if-label cells."""
    return page.evaluate(
        """(cells) => [...document.querySelectorAll("tr[data-if-cloned-from='tblBodyTr']")]
             .filter(tr => tr.offsetParent !== null)
             .map(tr => Object.fromEntries(Object.entries(cells).map(([k, label]) => {
                const td = tr.querySelector(`td[data-if-label='${label}']`);
                return [k, td ? td.innerText : ""];
             })))""",
        _EVENT_CELLS,
    )


def _collect_slice(
    page, timeout_ms: int, prev_sig: str | None = None
) -> tuple[list[dict], int, str]:
    """After a search: paginate the whole result grid.

    Returns ``(rows, banner_total, last_page_signature)``. ``prev_sig`` is the
    previous slice's final page signature: right after a new search the OLD
    slice's banner and rows are still rendered, so without it the first poll
    could return (and scrape!) the previous slice's grid as this slice's page 1.
    The expected next window comes from the banner's own ``end + 1`` (server
    truth), never from our local row count — so one odd render can't drift the
    expectation and stall the walk. A page turn that doesn't land within its
    poll window gets ONE re-click (the relay occasionally swallows a click)
    before the slice fails.
    """
    _, end, total = _poll_events_page(page, timeout_ms, prev_sig=prev_sig)
    rows = _scrape_events_page(page)
    sig = _page_signature(page)
    retried = False
    while end < total:
        try:
            page.wait_for_timeout(750)  # pace the relay between turns (WAF etiquette)
            page.wait_for_selector(_EVENT_NEXT, timeout=60_000)
            page.locator(_EVENT_NEXT).first.click()
            _, end, total = _poll_events_page(
                page, min(timeout_ms, 60_000), expect_start=end + 1, prev_sig=sig
            )
        except (EprocureError, PWTimeout):
            if not retried:
                retried = True  # the relay occasionally swallows a click/render
                continue
            if len(rows) >= total * _COMPLETE_RATIO:
                # A stuck turn this close to the end (seen live on a final
                # 1-row page) is within the same churn tolerance the write
                # gate applies — keep the slice rather than losing it.
                print(f"events: page turn stuck at {len(rows)}/{total}; within tolerance")
                break
            raise
        retried = False
        sig = _page_signature(page)
        rows.extend(_scrape_events_page(page))
    return rows, total, sig


def _verify_select(page, selector: str, want: str) -> None:
    """Raise unless the select actually committed (silently-uncommitted filters
    would extract the wrong slice — same failure family as the SCPRS dates)."""
    got = page.locator(f"{selector}:visible").first.evaluate(
        "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : ''"
    )
    if got != want:
        raise EprocureError(f"Filter {selector} shows {got!r}, wanted {want!r} — not searching.")


def download_events(
    on_slice,
    *,
    years: tuple[int, ...],
    include_posted: bool = True,
    headless: bool = True,
    timeout_ms: int = 120_000,
    budget_minutes: float | None = None,
) -> list[str]:
    """Drive the overlay search across the requested slices in one browser session.

    Calls ``on_slice(slice_key, rows, banner_total)`` as each slice completes, so
    finished slices are banked even if a later one fails. A slice that fails is
    retried ONCE, at the back of the queue in a brand-new session (the overlay
    stalls a pagination now and then; one 20-minute run must not die on a single
    flaky slice) — only a twice-failed slice fails the run, and only after every
    other slice has been given its chance to bank. With ``budget_minutes``, stops
    cleanly *between* slices when the budget is spent (each written slice is
    complete on its own; skipped ones are reported, never half-written). Returns
    the list of skipped slice keys.
    """
    from time import monotonic

    t0 = monotonic()
    queue: list[tuple[str, int | None]] = []
    if include_posted:
        queue.append(("posted", None))
    queue.extend((f"historical:{y}", y) for y in years)
    skipped: list[str] = []
    attempts: dict[str, int] = {}
    failed: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_CHROMIUM_ARGS)
        try:
            i = 0
            while i < len(queue):
                key, year = queue[i]
                i += 1
                if budget_minutes is not None and (monotonic() - t0) / 60 > budget_minutes:
                    skipped.append(key)
                    continue
                attempts[key] = attempts.get(key, 0) + 1
                # Fresh CONTEXT (new PeopleSoft session) per slice. Reusing a
                # session across searches breaks the overlay in ways observed
                # live twice: the old slice's banner/rows linger while the new
                # search is in flight, and — cookie-carried server state — a
                # second search in one PS session can render its grid with the
                # pager permanently hidden, stalling pagination. Every clean
                # recon run was a session's first search; mirror that.
                ctx = browser.new_context(user_agent=_BROWSER_UA)
                try:
                    page = ctx.new_page()
                    page.goto(EVENTS_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_selector(f"{_EVENT_STATUS}:visible", timeout=timeout_ms)
                    page.wait_for_timeout(5000)
                    label = "Historical" if year else "Posted"
                    page.locator(f"{_EVENT_STATUS}:visible").first.select_option(label=label)
                    page.wait_for_timeout(4000)  # round trip may add/remove the year select
                    _verify_select(page, _EVENT_STATUS, label)
                    if year:
                        page.wait_for_selector(f"{_EVENT_YEAR}:visible", timeout=timeout_ms)
                        page.locator(f"{_EVENT_YEAR}:visible").first.select_option(label=str(year))
                        page.wait_for_timeout(2000)
                        _verify_select(page, _EVENT_YEAR, str(year))
                    page.locator(f"{_EVENT_SEARCH}:visible").first.click()
                    rows, total, _ = _collect_slice(page, timeout_ms)
                    status = "historical" if year else "posted"
                    on_slice(key, [_tidy_event(r, status, year) for r in rows], total)
                except (EprocureError, PWTimeout) as e:
                    if attempts[key] < 2:
                        print(f"events {key}: attempt failed ({e}); retrying once at the end")
                        queue.append((key, year))
                    else:
                        failed.append(f"{key}: {e}")
                finally:
                    ctx.close()
        finally:
            browser.close()
    if failed:
        raise EprocureError(
            "Event slice(s) failed after retry (completed slices are banked): " + "; ".join(failed)
        )
    return skipped


_EVENT_COLUMNS = (
    "event_id",
    "event_name",
    "department_name",
    "published_raw",
    "published_date",
    "end_raw",
    "end_date",
    "status",
    "sb_only",
    "dvbe_only",
    "search_status",
    "search_year",
)


def write_events_slice(db_path: Path, slice_key: str, rows: list[dict], banner_total: int) -> dict:
    """Idempotent per-slice replace of the ``events`` table + its meta row.

    Completeness gates the write: the paginated row count must reach >=99% of the
    site's own banner total (tolerance for live churn while paging, same policy
    as the registry) — a partial slice raises instead of looking finished.
    """
    if banner_total > 0 and len(rows) < banner_total * _COMPLETE_RATIO:
        raise EprocureError(
            f"Events slice {slice_key!r} looks truncated: {len(rows)} rows vs "
            f"the site's banner total of {banner_total} — not writing."
        )
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        cols = ", ".join(_EVENT_COLUMNS)
        con.execute(
            f"CREATE TABLE IF NOT EXISTS events ({cols}, slice_key TEXT, extracted_at TEXT)"  # noqa: S608 - internal constants
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS events_meta ("
            "slice_key TEXT PRIMARY KEY, banner_total INTEGER, rows INTEGER, extracted_at TEXT)"
        )
        con.execute("DELETE FROM events WHERE slice_key = ?", (slice_key,))
        ph = ", ".join("?" * (len(_EVENT_COLUMNS) + 2))
        con.executemany(
            f"INSERT INTO events VALUES ({ph})",  # noqa: S608 - fixed arity
            [[r[c] for c in _EVENT_COLUMNS] + [slice_key, ts] for r in rows],
        )
        con.execute(
            "INSERT OR REPLACE INTO events_meta VALUES (?, ?, ?, ?)",
            (slice_key, banner_total, len(rows), ts),
        )
        con.commit()
    finally:
        con.close()
    return {"slice": slice_key, "rows": len(rows), "banner_total": banner_total}


def extract_events(
    db_path: Path = DB_PATH,
    *,
    years: tuple[int, ...] | None = None,
    include_posted: bool = True,
    headless: bool = True,
    budget_minutes: float | None = None,
) -> list[dict]:
    """Extract the lean CSCR event slices into ``events`` in data/eprocure.db.

    The store holds exactly the slices of the latest run (per-slice replace; a
    year outside ``--years`` ages out on the next run) — deliberate lean
    semantics, documented in docs/EPROCURE.md. Returns per-slice summaries.
    """
    summaries: list[dict] = []

    def on_slice(key: str, rows: list[dict], total: int) -> None:
        s = write_events_slice(db_path, key, rows, total)
        summaries.append(s)
        print(f"events {key}: {s['rows']} rows (site banner {total}) -> {db_path.name}")

    skipped = download_events(
        on_slice,
        years=years or default_event_years(),
        include_posted=include_posted,
        headless=headless,
        budget_minutes=budget_minutes,
    )
    for key in skipped:
        print(f"events {key}: SKIPPED (budget spent before slice started)")
    if not summaries:
        raise EprocureError("No event slice completed — nothing extracted.")
    return summaries


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

    ev = sub.add_parser(
        "extract-events",
        help="Lean CSCR event slices (Posted + recent Historical years) -> data/eprocure.db",
    )
    ev.add_argument("--db", type=Path, default=DB_PATH)
    ev.add_argument(
        "--years",
        type=int,
        nargs="*",
        default=None,
        help="Historical years to extract (default: current year and the two prior)",
    )
    ev.add_argument("--no-posted", action="store_true", help="Skip the live Posted slice")
    ev.add_argument(
        "--budget-minutes",
        type=float,
        default=None,
        help="Stop cleanly between slices once this wall-clock budget is spent",
    )
    ev.add_argument("--show", action="store_true", help="Run browser headed (visible)")

    args = ap.parse_args()
    if args.cmd == "extract-registry":
        extract_registry(args.db, args.out, headless=not args.show)
    elif args.cmd == "extract-events":
        extract_events(
            args.db,
            years=tuple(args.years) if args.years else None,
            include_posted=not args.no_posted,
            headless=not args.show,
            budget_minutes=args.budget_minutes,
        )


if __name__ == "__main__":
    _cli()
