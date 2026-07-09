"""SCPRS scraper for the California FI$Cal supplier portal.

Drives the PeopleSoft SCPRS search component with a headless browser and
downloads the result extract for a given business unit (Department) and date
range. See docs/SCPRS_NOTES.md for the reverse-engineering findings.

Key facts discovered about the site:
- Public (no login). Search is a stateful PeopleSoft component.
- Date fields reject programmatically-set values; they must be *typed*
  (real keystrokes) then committed with Tab, or the filter is silently ignored.
- The From/To Date filter applies to each record's Start Date.
- "Download Search Results" returns a Summary .xls (actually an HTML table).
  "Download Detail Information" returns line-item Detail.
- A download is capped at 65,000 rows; if the result is larger, a modal warns
  and only the first 65,000 rows are exported (see `Truncated`).

Usage:
    python -m src.scprs 0250 06/01/2025 06/30/2025
    python -m src.scprs 2660 01/01/2025 01/31/2025 --kind detail
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

SEARCH_URL = "https://suppliers.fiscal.ca.gov/psc/psfpd1/SUPPLIER/ERP/c/" "ZZ_PO.ZZ_SCPRS1_CMP.GBL"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ROW_CAP = 65000  # per-download export limit enforced by the site

# Result-page controls
_BU = "#ZZ_SCPRS_SP_WRK_BUSINESS_UNIT"
_FROM = "#ZZ_SCPRS_SP_WRK_FROM_DATE"
_TO = "#ZZ_SCPRS_SP_WRK_TO_DATE"
_SEARCH = "#ZZ_SCPRS_SP_WRK_BUTTON"
_DL_SUMMARY = "#ZZ_SCPRS_SP_WRK_BUTTONS_GB"
_DL_DETAIL = "#ZZ_SCPRS_SP_WRK_BUTTON_BACKWARD"
_MODAL_OK = '[id="#ICOK"]'


@dataclass
class Extract:
    business_unit: str
    from_date: str
    to_date: str
    kind: str
    path: Path | None  # None when no records were found
    truncated: bool  # True if the result hit the 65,000-row cap
    no_records: bool


def _type(page, selector: str, value: str) -> None:
    """Type into a PeopleSoft field with real keystrokes and commit with Tab.

    A plain fill() is silently discarded by the date-field edit mask, which
    is why the date filter appears to be ignored unless we type like a user.
    """
    page.click(selector)
    page.fill(selector, "")
    page.locator(selector).press_sequentially(value, delay=35)
    page.keyboard.press("Tab")
    page.wait_for_timeout(1000)


def download_extract(
    business_unit: str,
    from_date: str,
    to_date: str,
    *,
    kind: str = "summary",
    out_dir: Path = DATA_DIR,
    headless: bool = True,
    timeout_ms: int = 120_000,
) -> Extract:
    """Search SCPRS and download the extract for one business unit + date range.

    Dates are MM/DD/YYYY. `kind` is "summary" or "detail".
    """
    dl_button = _DL_SUMMARY if kind == "summary" else _DL_DETAIL
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(SEARCH_URL, wait_until="networkidle", timeout=timeout_ms)
            _type(page, _BU, business_unit)
            _type(page, _FROM, from_date)
            _type(page, _TO, to_date)
            # Guard: confirm the filter actually took (see module docstring).
            got = {page.input_value(_FROM), page.input_value(_TO)}
            if got != {from_date, to_date}:
                raise RuntimeError(
                    f"Date fields did not commit (got {got}); the site would "
                    "return unfiltered data."
                )
            page.click(_SEARCH)
            page.wait_for_timeout(6000)

            if "No Records Found" in page.inner_text("body"):
                return Extract(business_unit, from_date, to_date, kind, None, False, True)

            truncated = False
            with page.expect_download(timeout=timeout_ms) as dl_info:
                page.click(dl_button)
                page.wait_for_timeout(2500)
                # A confirmation dialog appears for every download; it must be
                # accepted or the file is never generated. When the result set
                # exceeds the row cap the dialog also warns about truncation.
                ok = page.locator(_MODAL_OK)
                if ok.count() and ok.is_visible():
                    body = page.inner_text("body").lower()
                    if any(k in body for k in ("exceeds excel", "row limit", str(ROW_CAP))):
                        truncated = True
                    ok.click()
            dl = dl_info.value
            dest = (
                out_dir / f"scprs_{business_unit}_{from_date.replace('/','')}_"
                f"{to_date.replace('/','')}_{kind}.xls"
            )
            dl.save_as(str(dest))
            return Extract(business_unit, from_date, to_date, kind, dest, truncated, False)
        except PWTimeout as e:
            raise RuntimeError(f"Timed out driving SCPRS search: {e}") from e
        finally:
            browser.close()


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%m/%d/%Y").date()


def _fmt_date(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def download_range(
    business_unit: str,
    from_date: str,
    to_date: str,
    *,
    kind: str = "summary",
    out_dir: Path = DATA_DIR,
    max_depth: int = 12,
    log=print,
):
    """Download a full date range, auto-splitting when the 65k row cap is hit.

    The site caps a single export at 65,000 rows. This bisects the date range
    (recursively, on non-overlapping sub-ranges) until every slice is under the
    cap, then concatenates. Returns (DataFrame, warnings). `warnings` lists any
    slice that still exceeded the cap at a single day (unsplittable).
    """
    import pandas as pd

    frames: list = []
    warnings: list[str] = []

    def rec(a: date, b: date, depth: int) -> None:
        res = download_extract(
            business_unit, _fmt_date(a), _fmt_date(b), kind=kind, out_dir=out_dir
        )
        if res.no_records:
            log(f"  {_fmt_date(a)}..{_fmt_date(b)}: no records")
            return
        if not res.truncated or a == b or depth >= max_depth:
            df = load_extract(res.path)
            if res.truncated:
                warnings.append(f"{_fmt_date(a)}..{_fmt_date(b)} exceeds {ROW_CAP:,} rows; partial")
                log(f"  {_fmt_date(a)}..{_fmt_date(b)}: TRUNCATED, kept first {ROW_CAP:,}")
            else:
                log(f"  {_fmt_date(a)}..{_fmt_date(b)}: {len(df)} rows")
            frames.append(df)
            return
        # Truncated and splittable: bisect on date into two disjoint halves.
        mid = a + (b - a) / 2
        log(f"  {_fmt_date(a)}..{_fmt_date(b)}: >cap, splitting at {_fmt_date(mid)}")
        rec(a, mid, depth + 1)
        rec(mid + timedelta(days=1), b, depth + 1)

    rec(_parse_date(from_date), _parse_date(to_date), 0)
    if not frames:
        return pd.DataFrame(), warnings
    df = pd.concat(frames, ignore_index=True).drop_duplicates()
    return df, warnings


# Fields on the Business Unit lookup (prompt) page.
_LK_CRITERIA = "ZZ_PO_BU_CLSVW_BUSINESS_UNIT"
_LK_OPERATOR = _LK_CRITERIA + "$op"  # "1" = "begins with"


def _form_fields(soup):
    form = soup.find("form", {"name": "win0"})
    return {
        i.get("name"): (i.get("value", "") or "")
        for i in form.find_all(["input", "select", "textarea"])
        if i.get("name")
    }


def _parse_lookup_rows(soup):
    import re

    for a in soup.find_all("a"):
        code = a.get_text(strip=True)
        if re.fullmatch(r"\d{4,5}", code):
            tr = a.find_parent("tr")
            texts = [c.get_text(strip=True) for c in tr.find_all(["td", "a", "span"])] if tr else []
            name = max(
                (t for t in texts if t and not re.fullmatch(r"\d{4,5}", t)), key=len, default=""
            )
            yield code, name


def fetch_departments() -> list[tuple[str, str]]:
    """Return (code, name) for every valid Department via the site's lookup.

    Uses plain HTTP (no browser). The lookup only returns the first ~300 rows
    per search, so we iterate the Business Unit criteria "begins with" each
    digit 0-9 (each bucket is well under the cap) and merge the results.
    """
    import requests
    from bs4 import BeautifulSoup

    s = requests.Session()
    s.headers.update({"User-Agent": "scprs-analysis/0.1 (+muntherhasan1@gmail.com)"})
    # Open the Department lookup (prompt) once.
    data = _form_fields(BeautifulSoup(s.get(SEARCH_URL, timeout=45).text, "lxml"))
    data["ICAction"] = "ZZ_SCPRS_SP_WRK_BUSINESS_UNIT$prompt"
    data["ICStateNum"] = "1"
    soup = BeautifulSoup(s.post(SEARCH_URL, data=data, timeout=90).text, "lxml")

    found: dict[str, str] = {}
    for digit in "0123456789":
        d = _form_fields(soup)  # reuse latest state (ICSID/ICStateNum)
        d[_LK_CRITERIA] = digit
        d[_LK_OPERATOR] = "1"  # begins with
        d["ICAction"] = "#ICSearch"
        soup = BeautifulSoup(s.post(SEARCH_URL, data=d, timeout=90).text, "lxml")
        for code, name in _parse_lookup_rows(soup):
            found.setdefault(code, name)
    return sorted(found.items())


def load_extract(path: Path):
    """Parse a downloaded SCPRS .xls (HTML table) into a tidy DataFrame.

    Cleans the PeopleSoft quirks: identifier columns are prefixed with a
    literal apostrophe, money is "$1234.5", and dates are MM/DD/YYYY.
    Imported lazily so the scraper works without pandas installed.
    """
    import pandas as pd

    df = pd.read_html(path)[0]
    # Strip leading apostrophes the export puts on id-like text columns.
    # (Handle both the legacy object dtype and pandas' newer str dtype.)
    for col in df.columns:
        if pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].astype("string").str.lstrip("'").str.strip()
    if "Grand Total" in df.columns:
        df["Grand Total"] = pd.to_numeric(
            df["Grand Total"].astype("string").str.replace(r"[$,]", "", regex=True),
            errors="coerce",
        )
    for dcol in ("Start Date", "End Date"):
        if dcol in df.columns:
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
    return df


def to_csv(xls_path: Path, csv_path: Path | None = None) -> Path:
    """Convert a downloaded .xls extract to a clean CSV; return the CSV path."""
    csv_path = csv_path or xls_path.with_suffix(".csv")
    load_extract(xls_path).to_csv(csv_path, index=False)
    return csv_path


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Download a SCPRS extract.")
    ap.add_argument("business_unit", help="Department / business-unit code, e.g. 0250")
    ap.add_argument("from_date", help="From date MM/DD/YYYY")
    ap.add_argument("to_date", help="To date MM/DD/YYYY")
    ap.add_argument("--kind", choices=["summary", "detail"], default="summary")
    ap.add_argument("--show", action="store_true", help="Run browser headed (visible)")
    args = ap.parse_args()

    result = download_extract(
        args.business_unit,
        args.from_date,
        args.to_date,
        kind=args.kind,
        headless=not args.show,
    )
    if result.no_records:
        print("No records found for that business unit + date range.")
    else:
        print(f"Saved: {result.path}")
        try:
            csv = to_csv(result.path)
            print(f"CSV:   {csv}")
        except ImportError:
            print("(install pandas to auto-convert to CSV)")
        if result.truncated:
            print(
                f"WARNING: result exceeded {ROW_CAP:,} rows; only the first "
                f"{ROW_CAP:,} were exported. Narrow the date range for full coverage."
            )


if __name__ == "__main__":
    _cli()
