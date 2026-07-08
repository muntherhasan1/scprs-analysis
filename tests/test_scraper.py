"""Tests for scraper helpers (no network access required)."""

from src import scraper


def test_find_csv_links_resolves_relative_urls():
    html = """
    <a href="reports/2024.csv">2024</a>
    <a href="https://example.gov/data/2023.csv">2023</a>
    <a href="index.html">not a csv</a>
    """
    links = scraper.find_csv_links(html, "https://example.gov/scprs/")
    assert "https://example.gov/scprs/reports/2024.csv" in links
    assert "https://example.gov/data/2023.csv" in links
    assert len(links) == 2


def test_safe_filename_blocks_path_traversal():
    assert scraper.safe_filename("https://x.gov/a/../../etc/passwd") == "passwd.csv"
    assert scraper.safe_filename("https://x.gov/reports/2024.csv") == "2024.csv"
    # No slashes or traversal survive.
    assert "/" not in scraper.safe_filename("https://x.gov/../secret.csv")


def test_safe_filename_forces_csv_extension():
    assert scraper.safe_filename("https://x.gov/dump").endswith(".csv")
