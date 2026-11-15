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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from src.failure_evidence import PageWatch
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
        watch = PageWatch(page, "registry")
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
            watch.capture(e)
            raise EprocureError(f"Timed out driving the registry search: {e}") from e
        except Exception as e:
            watch.capture(e)
            raise
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

    from src.scprs import read_export_html  # same UTF-8, no-charset export format

    df = read_export_html(path)
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


# Set-aside clause = the text before an "ONLY", back to the nearest
# punctuation delimiter (departments wrap it in asterisks or quotes:
# "**Certified SB, including MB, SB-PW, or a DVBE Only**", "*SB/DVBE ONLY*",
# "**SMALL BUSINESS BIDDERS ONLY**"). "-" is not a delimiter (SB-PW).
_SET_ASIDE_CLAUSE = re.compile(r"([^*()\"\[\]:;]{0,80}?)\bONLY")
_SB_GROUP = re.compile(r"\b(SB|SBS|SB-PW|MB|SMALL BUSINESS(ES)?|MICRO ?BUSINESS(ES)?)\b")
_DVBE_GROUP = re.compile(r"\b(DVBE|DISABLED VETERAN)")


def set_aside_flags(event_name: str | None) -> tuple[int, int, int]:
    """(sb_only, dvbe_only, sb_or_dvbe) from an event title's "... ONLY" clauses.

    A clause naming only small-business groups (SB, MB, SB-PW) is SB-only; only
    DVBE is DVBE-only; both ("SB/DVBE ONLY", "SB ... or a DVBE Only") is a set
    aside open to either, which is neither SB-only nor DVBE-only. A floor: an
    untitled set-aside is invisible here.
    """
    name = " ".join((event_name or "").upper().split())
    sb = dvbe = False
    for clause in _SET_ASIDE_CLAUSE.findall(name):
        sb = sb or bool(_SB_GROUP.search(clause))
        dvbe = dvbe or bool(_DVBE_GROUP.search(clause))
    return int(sb and not dvbe), int(dvbe and not sb), int(sb and dvbe)


def _tidy_event(raw: dict, search_status: str, search_year: int | None) -> dict:
    """Normalize one scraped grid row: collapse whitespace, derive ISO dates and
    the SB/DVBE set-aside flags departments encode in event names
    (e.g. "**SB Only**", "*DVBE Only*")."""
    row = {k: " ".join((raw.get(k) or "").split()) for k in _EVENT_CELLS}
    for src, dest in (("published_raw", "published_date"), ("end_raw", "end_date")):
        m = _EVENT_DT.search(row[src])
        row[dest] = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None
    row["sb_only"], row["dvbe_only"], _ = set_aside_flags(row["event_name"])
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


def _event_slice_queue(
    years: tuple[int, ...], include_posted: bool
) -> list[tuple[str, int | None]]:
    """The run's slices in extraction order, as ``(slice_key, year)``."""
    queue: list[tuple[str, int | None]] = [("posted", None)] if include_posted else []
    queue.extend((f"historical:{y}", y) for y in years)
    return queue


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
    queue = _event_slice_queue(years, include_posted)
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
                watch = None
                try:
                    page = ctx.new_page()
                    watch = PageWatch(page, f"events-{key}-attempt{attempts[key]}")
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
                    if watch:
                        watch.capture(e)
                    if attempts[key] < 2:
                        print(f"events {key}: attempt failed ({e}); retrying once at the end")
                        queue.append((key, year))
                    else:
                        failed.append(f"{key}: {e}")
                except Exception as e:
                    # Anything else (e.g. goto's net::ERR_EMPTY_RESPONSE) ends the
                    # run: keep what the page showed before the context closes.
                    if watch:
                        watch.capture(e)
                    raise
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


def _ensure_events_tables(con: sqlite3.Connection) -> None:
    cols = ", ".join(_EVENT_COLUMNS)
    con.execute(
        f"CREATE TABLE IF NOT EXISTS events ({cols}, slice_key TEXT, extracted_at TEXT)"  # noqa: S608 - internal constants
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS events_meta ("
        "slice_key TEXT PRIMARY KEY, banner_total INTEGER, rows INTEGER, extracted_at TEXT)"
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
        _ensure_events_tables(con)
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
    semantics, documented in docs/EPROCURE.md. ``years=()`` extracts Posted
    only (the daily solicitation refresh); ``None`` means the default years.
    Returns per-slice summaries.
    """
    summaries: list[dict] = []

    def on_slice(key: str, rows: list[dict], total: int) -> None:
        s = write_events_slice(db_path, key, rows, total)
        summaries.append(s)
        print(f"events {key}: {s['rows']} rows (site banner {total}) -> {db_path.name}")
        # The site's own status column, so a slice holding another search's
        # results is visible in the log (2026-11-10: Historical 2025's retry held
        # 385 rows, that run's Posted count; nothing logged could show why).
        mix = Counter(r.get("status") or "?" for r in rows).most_common(4)
        print(f"events {key}: site status mix " + (", ".join(f"{st} {n}" for st, n in mix) or "-"))

    skipped = download_events(
        on_slice,
        years=default_event_years() if years is None else years,
        include_posted=include_posted,
        headless=headless,
        budget_minutes=budget_minutes,
    )
    for key in skipped:
        print(f"events {key}: SKIPPED (budget spent before slice started)")
    if not summaries:
        raise EprocureError("No event slice completed — nothing extracted.")
    return summaries


def _has_table(con: sqlite3.Connection, schema: str, table: str) -> bool:
    return (
        con.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",  # noqa: S608 - schema is an internal constant
            (table,),
        ).fetchone()
        is not None
    )


# A refreshed slice below this share of its published copy is treated as not
# refreshed. Historical years barely move week to week (2024: 5,703 on every run
# since 2026-10-20), so the whole-table gate's 90%; Posted churns daily, so the
# daily solicitations job's 50%. Catches a retry that reads the wrong result set:
# 2026-11-10's Historical 2025 retry wrote 385 rows against a banner of 385 (it
# held 5,616 a week earlier; 385 was that run's Posted count).
_SLICE_FLOOR = {"posted": 0.5}
_HISTORICAL_SLICE_FLOOR = 0.9


def carry_forward_events(
    db_path: Path,
    prev_path: Path,
    fetch_prev,
    *,
    years: tuple[int, ...] | None = None,
    include_posted: bool = True,
) -> list[dict]:
    """Replace each event slice this run did not really refresh with its published copy.

    The weekly refresh starts from an empty store, so a slice that timed out (or
    that ``--budget-minutes`` skipped) is simply absent; a slice that "completed"
    can still be wrong when the overlay served another search's results, because
    the banner it is checked against comes from the same wrong page. Publishing
    either would drop good published rows, and refusing to publish throws away
    the run's fresh registry (2026-11-10: registry fine; Historical 2026 failed
    twice and Historical 2025's retry held a Posted-sized result set; nothing
    published, and the registry went two weeks without a publish). Replacing the
    slice keeps both: its published rows and ``events_meta`` row are copied
    unchanged, so its ``extracted_at`` still shows its true age.

    A slice is replaced when it is absent, or when it holds fewer rows than its
    floor (``_SLICE_FLOOR``) of the published copy. ``fetch_prev(path) -> bool``
    downloads the published store to ``prev_path``. False with a slice absent
    fails closed (no store is safe to publish); False with every slice present
    skips the comparison and leaves the store alone, so the shrink gate's own
    fetch decides, as before. Returns one ``{"slice", "rows", "extracted_at",
    "reason"}`` per replaced slice (empty when the run refreshed everything). A
    slice the published store never had (a new year after a rollover) has
    ``rows`` 0 and ``extracted_at`` None and stays as the run left it. Years
    outside the window still age out, as in ``extract_events``.
    """
    want = [
        key
        for key, _ in _event_slice_queue(
            default_event_years() if years is None else years, include_posted
        )
    ]
    db_path, prev_path = Path(db_path), Path(prev_path)
    if not db_path.exists():
        raise EprocureError(f"{db_path} not found — the registry extract writes it first")
    con = sqlite3.connect(db_path)
    try:
        banked = (
            dict(con.execute("SELECT slice_key, rows FROM events_meta"))
            if _has_table(con, "main", "events_meta")
            else {}
        )
    finally:
        con.close()
    missing = [k for k in want if k not in banked]
    if not fetch_prev(prev_path):
        if missing:
            raise EprocureError(
                f"Event slice(s) {', '.join(missing)} did not refresh and the published "
                "eprocure.db could not be fetched to carry them forward; NOT publishing "
                "(the published copy keeps them)."
            )
        print(
            "WARN: could not fetch the published eprocure.db; per-slice shrink check "
            "skipped (the shrink gate still compares whole tables)"
        )
        return []

    cols = ", ".join((*_EVENT_COLUMNS, "slice_key", "extracted_at"))
    carried: list[dict] = []
    con = sqlite3.connect(db_path, uri=True)
    try:
        con.execute("ATTACH DATABASE ? AS prev", (f"{prev_path.resolve().as_uri()}?mode=ro",))
        has_prev = _has_table(con, "prev", "events") and _has_table(con, "prev", "events_meta")
        _ensure_events_tables(con)
        for key in want:
            meta = (
                con.execute(
                    "SELECT banner_total, rows, extracted_at FROM prev.events_meta "
                    "WHERE slice_key = ?",
                    (key,),
                ).fetchone()
                if has_prev
                else None
            )
            if key not in banked:
                reason = "not refreshed"
            elif meta is not None and banked[key] < int(
                meta[1] * _SLICE_FLOOR.get(key, _HISTORICAL_SLICE_FLOOR)
            ):
                floor = _SLICE_FLOOR.get(key, _HISTORICAL_SLICE_FLOOR)
                reason = (
                    f"shrank to {banked[key]} rows, below {floor:.0%} of the published {meta[1]}"
                )
            else:
                continue  # refreshed, and plausibly complete
            if meta is None:
                carried.append({"slice": key, "rows": 0, "extracted_at": None, "reason": reason})
                continue
            con.execute("DELETE FROM events WHERE slice_key = ?", (key,))
            n = con.execute(
                f"INSERT INTO events ({cols}) SELECT {cols} FROM prev.events WHERE slice_key = ?",  # noqa: S608 - internal constants
                (key,),
            ).rowcount
            if n != meta[1]:
                raise EprocureError(
                    f"Published slice {key!r} holds {n} rows but its events_meta records "
                    f"{meta[1]} — not carrying a slice that disagrees with itself; NOT publishing."
                )
            con.execute(
                "INSERT OR REPLACE INTO events_meta (slice_key, banner_total, rows, extracted_at) "
                "VALUES (?, ?, ?, ?)",
                (key, *meta),
            )
            carried.append({"slice": key, "rows": n, "extracted_at": meta[2], "reason": reason})
        con.commit()  # all-or-nothing: an error above rolls back on close
    finally:
        con.close()
    return carried


# ------------------------------------------------------------------------- CLI


# --------------------------------------------------------- CSCR event details
# Each open bid's detail page (recon 2026-11-15) carries what the event grid
# doesn't: UNSPSC codes (type of work), contractor license classes, service-area
# counties, a mandatory/non-mandatory pre-bid conference, the description, the
# published date and the event version. Mechanism, all verified live:
# - https://caleprocure.ca.gov/event/{business unit}/{event id} works headless
#   with the normal Chrome UA (the WAF 403s HeadlessChrome). It 302s to
#   pages/Events-BS3/event-details.aspx?...&BUSINESS_UNIT=...; a wrong business
#   unit or id lands on pages/error.aspx ("Oops!"), so a candidate unit can be
#   tested against the site itself.
# - The overlay renders every field under a stable data-if-label (eventId,
#   unspscClassification, contractorTblType, serviceAreaCounty, conferenceText,
#   ...). Pages render in 2-4 s; one context serves many pages in sequence.
# - The event grid carries no business unit, and a grid row doesn't navigate
#   headless when clicked. So the unit comes from the FI$Cal department list
#   by name (every candidate when a name maps to several, e.g. Franchise Tax
#   Board 1730/7730) plus references/eprocure_department_aliases.csv for
#   eProcure spellings that match no FI$Cal name, each confirmed by the page
#   loading.
# Buyer contact name, phone and email are on the page but never stored.
EVENT_DETAIL_URL = "https://caleprocure.ca.gov/event/{bu}/{event_id}"
REFERENCES = Path(__file__).resolve().parent.parent / "references"
DEPARTMENTS_CSV = REFERENCES / "departments.csv"
ALIASES_CSV = REFERENCES / "eprocure_department_aliases.csv"
# An unresolvable department (no unit loads) is retried after this many days.
_MISS_RETRY_DAYS = 7
# This many page loads in a row that time out or error means the site (or the
# WAF) is refusing us: stop instead of burning the budget on a dead session.
_MAX_CONSECUTIVE_FAILURES = 8
_CONTEXT_PAGES = 100  # recycle the browser context after this many pages

_DETAIL_READ = """() => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const one = (label, root = document) => {
    const e = root.querySelector(`[data-if-label='${label}']`);
    return e ? clean(e.innerText) : null;
  };
  const all = label => [...document.querySelectorAll(`[data-if-label='${label}']`)]
    .map(e => clean(e.innerText));
  const conf = document.querySelector("[data-if-label='conferenceRow']") || document;
  const desc = document.querySelector("[data-if-label='descriptiondetails']");
  return {
    event_id: one('eventId'), event_name: one('eventName'), dept: one('dept'),
    format1: one('format1'), format2: one('format2'),
    published: one('eventStartDate'), version: one('eventVersion'), end: one('eventEndDate'),
    description: desc ? desc.innerText.trim() : null,
    conference: one('conferenceText', conf), conf_date: one('dateText', conf),
    conf_time: one('timeText', conf), conf_location: one('locationText', conf),
    conf_comments: one('commentsText', conf),
    unspsc: all('unspscClassification'), unspsc_desc: all('unspscDescription'),
    license: all('contractorTblType'), license_desc: all('contractorTblDescription'),
    area_id: all('serviceAreaID'), county: all('serviceAreaCounty'),
  };
}"""
_DETAIL_READY = (
    "() => location.href.includes('error.aspx') || "
    "!!(document.querySelector(\"[data-if-label='eventId']\") || {}).innerText"
)


def _iso_mdy(text: str | None) -> str | None:
    """'10/02/2026  3:30PM PDT' or '10/21/2026' -> '2026-10-02'; None if no date."""
    m = _EVENT_DT.search(text or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None


def parse_event_detail(raw: dict) -> dict:
    """Normalize one detail-page read (``_DETAIL_READ``) into stored fields.

    Pure, so the parsing is unit-tested without a browser. Blank strings become
    None; a pre-bid conference is mandatory True/False only when the page says
    "Mandatory" / "Non Mandatory" (None when the section is N/A)."""

    def blank(v):
        v = (v or "").strip()
        return v or None

    conf = (blank(raw.get("conference")) or "").upper()
    mandatory = {"MANDATORY": 1, "NON MANDATORY": 0, "NON-MANDATORY": 0}.get(conf)
    version = blank(raw.get("version"))
    fmt = " / ".join(f for f in (blank(raw.get("format1")), blank(raw.get("format2"))) if f)
    return {
        "format_type": fmt or None,
        "published_date": _iso_mdy(raw.get("published")),
        "event_version": int(version) if version and version.isdigit() else None,
        "end_text": blank(raw.get("end")),
        "description": blank(raw.get("description")),
        "prebid_mandatory": mandatory,
        "prebid_date": _iso_mdy(raw.get("conf_date")),
        "prebid_time": blank(raw.get("conf_time")),
        "prebid_location": blank(raw.get("conf_location")),
        "prebid_comments": blank(raw.get("conf_comments")),
        "unspsc": [
            (c.strip(), blank(d))
            for c, d in zip(raw.get("unspsc") or [], raw.get("unspsc_desc") or [], strict=False)
            if c and c.strip()
        ],
        "licenses": [
            (t.strip(), blank(d))
            for t, d in zip(raw.get("license") or [], raw.get("license_desc") or [], strict=False)
            if t and t.strip()
        ],
        "service_areas": [
            (blank(a), c.strip())
            for a, c in zip(raw.get("area_id") or [], raw.get("county") or [], strict=False)
            if c and c.strip()
        ],
    }


_DETAIL_COLUMNS = (
    "format_type",
    "published_date",
    "event_version",
    "end_text",
    "description",
    "prebid_mandatory",
    "prebid_date",
    "prebid_time",
    "prebid_location",
    "prebid_comments",
)


def _ensure_detail_tables(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS event_details (department_name TEXT, event_id TEXT, "
        "business_unit TEXT, "
        + ", ".join(
            f"{c} {'INTEGER' if c in ('event_version', 'prebid_mandatory') else 'TEXT'}"
            for c in _DETAIL_COLUMNS
        )
        + ", fetched_at TEXT, PRIMARY KEY (department_name, event_id))"
    )
    for table, cols in (
        ("event_detail_unspsc", "unspsc TEXT, unspsc_description TEXT"),
        ("event_detail_licenses", "license_type TEXT, license_description TEXT"),
        ("event_detail_service_areas", "area_id TEXT, county TEXT"),
    ):
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (department_name TEXT, event_id TEXT, "  # noqa: S608 - internal constants
            f"ord INTEGER, {cols})"
        )
    # Events no candidate business unit would load for (e.g. a DAA with no FI$Cal
    # unit), so the daily run doesn't retry them every time.
    con.execute(
        "CREATE TABLE IF NOT EXISTS event_detail_misses (department_name TEXT, event_id TEXT, "
        "tried_units TEXT, checked_at TEXT, PRIMARY KEY (department_name, event_id))"
    )


def write_event_detail(
    con: sqlite3.Connection,
    department_name: str,
    event_id: str,
    business_unit: str,
    detail: dict,
    fetched_at: str,
) -> None:
    """Replace one event's detail rows in a single transaction (idempotent)."""
    key = (department_name, event_id)
    with con:
        for table in (
            "event_details",
            "event_detail_unspsc",
            "event_detail_licenses",
            "event_detail_service_areas",
            "event_detail_misses",
        ):
            con.execute(f"DELETE FROM {table} WHERE department_name = ? AND event_id = ?", key)  # noqa: S608 - internal constants
        con.execute(
            f"INSERT INTO event_details (department_name, event_id, business_unit, "  # noqa: S608 - internal constants
            f"{', '.join(_DETAIL_COLUMNS)}, fetched_at) "
            f"VALUES ({', '.join('?' * (len(_DETAIL_COLUMNS) + 4))})",
            (*key, business_unit, *(detail[c] for c in _DETAIL_COLUMNS), fetched_at),
        )
        for table, rows in (
            ("event_detail_unspsc", detail["unspsc"]),
            ("event_detail_licenses", detail["licenses"]),
            ("event_detail_service_areas", detail["service_areas"]),
        ):
            con.executemany(
                f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)",  # noqa: S608 - internal constants
                [(*key, i, *row) for i, row in enumerate(rows)],
            )


def _record_detail_miss(
    con, department_name: str, event_id: str, tried: list[str], at: str
) -> None:
    with con:
        con.execute(
            "INSERT OR REPLACE INTO event_detail_misses VALUES (?, ?, ?, ?)",
            (department_name, event_id, " ".join(tried), at),
        )


def load_unit_candidates(
    departments_csv: Path = DEPARTMENTS_CSV, aliases_csv: Path = ALIASES_CSV
) -> dict[str, list[str]]:
    """Normalized department name -> candidate business units, aliases first.

    Every unit a FI$Cal name maps to is a candidate (the page decides which is
    right), so an ambiguous name such as Franchise Tax Board (1730, 7730) still
    resolves."""
    import csv

    out: dict[str, list[str]] = {}

    def add(name: str, unit: str) -> None:
        units = out.setdefault(" ".join(name.upper().split()), [])
        if unit and unit not in units:
            units.append(unit)

    if aliases_csv.exists():
        with open(aliases_csv, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                add(r["eprocure_department_name"], r["business_unit"].strip())
    with open(departments_csv, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            add(r["name"], r["code"].strip())
    return out


def _detail_queue(con: sqlite3.Connection, today: str) -> list[tuple[str, str]]:
    """Posted events to read: never-read first, then the oldest reads; recently
    missed events (no unit loaded) are skipped until _MISS_RETRY_DAYS pass."""
    return [
        (r[0], r[1])
        for r in con.execute(
            """
            SELECT e.department_name, e.event_id
            FROM events e
            LEFT JOIN event_details d
                   ON d.department_name = e.department_name AND d.event_id = e.event_id
            LEFT JOIN event_detail_misses m
                   ON m.department_name = e.department_name AND m.event_id = e.event_id
            WHERE e.slice_key = 'posted'
              AND (m.checked_at IS NULL OR julianday(?) - julianday(m.checked_at) >= ?)
            GROUP BY e.department_name, e.event_id
            ORDER BY MAX(d.fetched_at IS NOT NULL), MAX(d.fetched_at), MIN(e.end_date), e.event_id
            """,
            (today, _MISS_RETRY_DAYS),
        )
    ]


def extract_event_details(
    db_path: Path = DB_PATH,
    *,
    budget_minutes: float | None = None,
    limit: int | None = None,
    headless: bool = True,
    timeout_ms: int = 60_000,
) -> dict:
    """Read the detail page of every currently Posted event into ``db_path``.

    Resumable: each event is written as soon as it is read, never-read events go
    first, then the oldest reads. ``budget_minutes`` stops cleanly between pages
    and returns with ``complete`` False (exit 0); a run of page failures raises.
    Returns a summary dict."""
    from time import monotonic
    from urllib.parse import quote

    con = sqlite3.connect(db_path)
    try:
        _ensure_detail_tables(con)
        today = datetime.now(timezone.utc).date().isoformat()
        queue = _detail_queue(con, today)
        if limit is not None:
            queue = queue[:limit]
        candidates = load_unit_candidates()
        resolved: dict[str, str] = {
            dept: bu
            for dept, bu in con.execute(
                "SELECT department_name, business_unit FROM event_details "
                "GROUP BY department_name HAVING COUNT(DISTINCT business_unit) = 1"
            )
        }
        stats = Counter()
        failed: list[str] = []
        t0, streak = monotonic(), 0
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=_CHROMIUM_ARGS)
            ctx = page = watch = None
            try:
                for n, (dept, eid) in enumerate(queue):
                    if budget_minutes is not None and (monotonic() - t0) / 60 > budget_minutes:
                        stats["skipped_budget"] = len(queue) - n
                        break
                    if n % _CONTEXT_PAGES == 0:
                        if ctx:
                            ctx.close()
                        ctx = browser.new_context(user_agent=_BROWSER_UA)
                        page = ctx.new_page()
                        watch = PageWatch(page, f"event-details-{n}")
                    norm = " ".join(dept.upper().split())
                    units = [u for u in [resolved.get(dept), *candidates.get(norm, [])] if u]
                    units = list(dict.fromkeys(units))
                    tried, read = [], None
                    for bu in units:
                        tried.append(bu)
                        url = EVENT_DETAIL_URL.format(
                            bu=quote(bu, safe=""), event_id=quote(eid, safe="")
                        )
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                            page.wait_for_function(_DETAIL_READY, timeout=timeout_ms)
                            page.wait_for_timeout(800)  # tables render just after the header
                        except PWTimeout as e:
                            streak += 1
                            watch.capture(e)
                            if streak >= _MAX_CONSECUTIVE_FAILURES:
                                raise EprocureError(
                                    f"{streak} detail pages in a row failed to load — the site "
                                    "or its firewall is refusing us; stopping (events read so "
                                    "far are banked)"
                                ) from e
                            failed.append(f"{dept} {eid} @ {bu}")
                            read = "failed"
                            break
                        streak = 0
                        if "error.aspx" in page.url:
                            continue  # wrong unit for this event: try the next candidate
                        raw = page.evaluate(_DETAIL_READ)
                        if (raw.get("event_id") or "").strip() != eid.strip():
                            continue
                        write_event_detail(
                            con,
                            dept,
                            eid,
                            bu,
                            parse_event_detail(raw),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        )
                        resolved[dept] = bu
                        stats["read"] += 1
                        read = "ok"
                        break
                    if read is None:
                        _record_detail_miss(con, dept, eid, tried, today)
                        stats["missed"] += 1
                    if (n + 1) % 50 == 0:
                        print(f"event details: {n + 1}/{len(queue)} — {dict(stats)}")
            finally:
                if ctx:
                    ctx.close()
                browser.close()
        posted, with_detail = con.execute(
            "SELECT COUNT(DISTINCT e.department_name || '|' || e.event_id), "
            "COUNT(DISTINCT d.department_name || '|' || d.event_id) FROM events e "
            "LEFT JOIN event_details d ON d.department_name = e.department_name "
            "AND d.event_id = e.event_id WHERE e.slice_key = 'posted'"
        ).fetchone()
    finally:
        con.close()
    summary = {
        "queued": len(queue),
        "read": stats["read"],
        "missed": stats["missed"],
        "failed": len(failed),
        "skipped_budget": stats["skipped_budget"],
        "posted_events": posted,
        "posted_with_details": with_detail,
        "complete": not stats["skipped_budget"] and not failed,
        "minutes": round((monotonic() - t0) / 60, 1),
    }
    print(f"event details: {summary}")
    for f in failed[:10]:
        print(f"event details: FAILED {f}")
    return summary


_DETAIL_TABLES = (
    "event_details",
    "event_detail_unspsc",
    "event_detail_licenses",
    "event_detail_service_areas",
    "event_detail_misses",
)


def carry_forward_event_details(db_path: Path, prev_path: Path, fetch_prev) -> dict:
    """Copy the published detail reads into a freshly built store.

    The weekly registry refresh starts from an empty ``eprocure.db``; without
    this its publish would erase every detail read the daily job banked. Only
    events in the new store's Posted slice are carried, and an event the new
    store already holds is left as it is. ``fetch_prev(prev_path)`` is called
    when ``prev_path`` doesn't exist yet."""
    if not prev_path.exists() and not fetch_prev(prev_path):
        return {"carried": 0, "reason": "no published store to carry from"}
    con = sqlite3.connect(db_path)
    try:
        _ensure_detail_tables(con)
        con.execute("ATTACH DATABASE ? AS prev", (f"file:{prev_path}?mode=ro",))
        if not _has_table(con, "prev", "event_details"):
            return {"carried": 0, "reason": "published store has no detail reads yet"}
        # Fix the set of events to carry BEFORE copying: once event_details is
        # copied, the child tables' keys would no longer look missing.
        con.execute("DROP TABLE IF EXISTS temp.carry_keys")
        con.execute(
            "CREATE TEMP TABLE carry_keys AS "
            "SELECT department_name, event_id FROM main.events WHERE slice_key = 'posted' "
            "EXCEPT SELECT department_name, event_id FROM main.event_details"
        )
        carried = 0
        with con:
            for table in _DETAIL_TABLES:
                if not _has_table(con, "prev", table):
                    continue
                cur = con.execute(
                    f"INSERT OR IGNORE INTO main.{table} SELECT p.* FROM prev.{table} p "  # noqa: S608 - internal constants
                    "JOIN temp.carry_keys k ON k.department_name = p.department_name "
                    "AND k.event_id = p.event_id"
                )
                if table == "event_details":
                    carried = cur.rowcount
        return {"carried": carried, "reason": "copied from the published store"}
    finally:
        con.close()


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
        "--posted-only",
        action="store_true",
        help="Only the live Posted slice (daily refresh); Historical slices are left as stored",
    )
    ev.add_argument(
        "--budget-minutes",
        type=float,
        default=None,
        help="Stop cleanly between slices once this wall-clock budget is spent",
    )
    ev.add_argument("--show", action="store_true", help="Run browser headed (visible)")
    dt = sub.add_parser(
        "extract-details",
        help="Posted events' detail pages (UNSPSC, licenses, areas, pre-bid) -> eprocure.db",
    )
    dt.add_argument("--db", type=Path, default=DB_PATH)
    dt.add_argument(
        "--budget-minutes",
        type=float,
        default=None,
        help="Stop cleanly between pages once this wall-clock budget is spent (exit 0)",
    )
    dt.add_argument("--limit", type=int, default=None, help="Read at most this many events")
    dt.add_argument("--show", action="store_true", help="Run browser headed (visible)")

    args = ap.parse_args()
    if args.cmd == "extract-registry":
        extract_registry(args.db, args.out, headless=not args.show)
    elif args.cmd == "extract-events":
        if args.posted_only and (args.no_posted or args.years):
            ap.error("--posted-only cannot be combined with --no-posted or --years")
        extract_events(
            args.db,
            years=() if args.posted_only else (tuple(args.years) if args.years else None),
            include_posted=not args.no_posted,
            headless=not args.show,
            budget_minutes=args.budget_minutes,
        )
    elif args.cmd == "extract-details":
        extract_event_details(
            args.db, budget_minutes=args.budget_minutes, limit=args.limit, headless=not args.show
        )


if __name__ == "__main__":
    _cli()
