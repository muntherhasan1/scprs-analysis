"""Starter scraper for SCPRS CSV downloads.

Security-conscious defaults:
- Every network call has a timeout (no hanging on a slow/hostile host).
- TLS verification is left ON (never pass verify=False).
- Downloaded filenames are sanitized so a crafted URL cannot write outside
  the intended output directory (path-traversal guard).
- A descriptive User-Agent is sent; be a polite scraper and check the site's
  robots.txt / terms before pointing this at a target.

This is a labeled starting point — reshape the parsing to match the page you
are actually scraping.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "scprs-analysis/0.1 (data analysis; contact: muntherhasan1@gmail.com)"
DEFAULT_TIMEOUT = 30  # seconds

# Where downloads land. Git-ignored (see .gitignore), so data stays out of git.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_html(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch a page and return its HTML, raising on any HTTP error."""
    with _session() as session:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text


def find_csv_links(html: str, base_url: str) -> list[str]:
    """Return absolute URLs of every .csv link found on the page."""
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        absolute = urljoin(base_url, href)
        if urlparse(absolute).path.lower().endswith(".csv"):
            links.append(absolute)
    return links


def safe_filename(url: str) -> str:
    """Derive a safe local filename from a URL, preventing path traversal.

    Strips any directory components and keeps only a conservative character
    set, so a URL like '../../etc/passwd' cannot escape the output directory.
    """
    name = Path(urlparse(url).path).name or "download.csv"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name.lower().endswith(".csv"):
        name += ".csv"
    return name


def download_csv(url: str, *, dest_dir: Path = DATA_DIR, timeout: int = DEFAULT_TIMEOUT) -> Path:
    """Stream a CSV to dest_dir under a sanitized name; return the saved path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / safe_filename(url)
    with _session() as session:
        with session.get(url, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            with target.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)
    return target


def scrape_page(url: str) -> list[Path]:
    """End-to-end: fetch a page, find CSV links, download each. Returns paths."""
    html = fetch_html(url)
    return [download_csv(link) for link in find_csv_links(html, url)]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m src.scraper <page-url>", file=sys.stderr)
        raise SystemExit(2)
    saved = scrape_page(sys.argv[1])
    print(f"Downloaded {len(saved)} file(s):")
    for path in saved:
        print(f"  {path}")
