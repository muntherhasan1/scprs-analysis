"""Offline tests for the SCPRS extract parser (no network/browser)."""

import re
from pathlib import Path

import pytest

from src import scprs


def test_grid_banner_matches_live_format():
    """The site's row-count banner is 'X to Y of Z' — an earlier 'X-Y of Z'
    pattern never matched, which silently hid multi-page days (#49)."""
    m = re.search(scprs.GRID_BANNER, "stuff\n1 to 200 of 1,135\nmore")
    assert m and int(m.group(3).replace(",", "")) == 1135
    m2 = re.search(scprs.GRID_BANNER, "201 to 206 of 206")
    assert m2 and m2.group(1) == "201"


# A miniature version of the HTML-table ".xls" the site produces.
SAMPLE_XLS = (
    "<table border='1'>"
    "<tr><th>Department</th><th>Purchase Document #</th><th>Grand Total</th>"
    "<th>Start Date</th><th>Supplier Name</th></tr>"
    "<tr><td>250</td><td>'0000000000000000000128625</td><td>$6000</td>"
    "<td>09/30/2026</td><td>C MURPHY CONSULTING LLC</td></tr>"
    "<tr><td>250</td><td>'0000000000000000000129533</td><td>$3,840,000</td>"
    "<td>07/01/2026</td><td>EPI-USE AMERICA INC</td></tr>"
    "</table>"
)


def test_load_extract_cleans_quirks(tmp_path: Path):
    f = tmp_path / "s.xls"
    f.write_text(SAMPLE_XLS, encoding="utf-8")
    df = scprs.load_extract(f)

    assert df.shape == (2, 5)
    # leading apostrophe stripped from id column
    assert df["Purchase Document #"].iloc[0] == "0000000000000000000128625"
    # money parsed to float (commas + $ removed)
    assert df["Grand Total"].iloc[1] == 3840000.0
    # dates parsed
    assert str(df["Start Date"].iloc[0].date()) == "2026-09-30"


def test_load_extract_decodes_utf8_without_charset(tmp_path: Path):
    # The live export is UTF-8 with no charset declared; parsed as Latin-1 the
    # NBSP after an id survived strip() as 'Â' (prod 4265 '22-10799Â') and '¿'
    # in titles became 'Â¿' (11,585 titles). Bytes copied from a real export.
    html = (
        "<table><tr><th>Purchase Document #</th><th>First Item Title</th></tr>"
        "<tr><td>'22-10799 </td><td>What¿s Happening</td></tr></table>"
    )
    f = tmp_path / "s.xls"
    f.write_bytes(html.encode("utf-8"))
    df = scprs.load_extract(f)
    assert df["Purchase Document #"].iloc[0] == "22-10799"
    assert df["First Item Title"].iloc[0] == "What¿s Happening"


def test_read_export_html_falls_back_to_cp1252(tmp_path: Path):
    f = tmp_path / "s.xls"
    f.write_bytes("<table><tr><th>A</th></tr><tr><td>café</td></tr></table>".encode("cp1252"))
    assert scprs.read_export_html(f)["A"].iloc[0] == "café"


def test_to_csv_roundtrip(tmp_path: Path):
    f = tmp_path / "s.xls"
    f.write_text(SAMPLE_XLS, encoding="utf-8")
    csv = scprs.to_csv(f)
    assert csv.exists()
    assert "C MURPHY CONSULTING LLC" in csv.read_text(encoding="utf-8")


def test_download_range_bisects_on_export_timeout(monkeypatch):
    """A range too large to export in time must be split, not fail (regression:
    huge BU/FY cells like CAL FIRE timed out before the row-cap bisection ran)."""
    import pandas as pd

    calls = []

    def fake_extract(bu, frm, to, *, out_dir=None):
        a, b = scprs._parse_date(frm), scprs._parse_date(to)
        calls.append((frm, to))
        if (b - a).days > 20:  # "too big" -> the site times out generating it
            raise scprs.ExtractTimeout("simulated slow export")
        return scprs.Extract(bu, frm, to, None, False, True)  # small enough: no records

    monkeypatch.setattr(scprs, "download_extract", fake_extract)
    df, warnings = scprs.download_range("3540", "07/01/2025", "06/30/2026")
    assert isinstance(df, pd.DataFrame) and df.empty and warnings == []
    assert len(calls) > 1  # it bisected instead of raising
    # It reached small-enough slices (the leaves) rather than dying on the full range.
    assert any((scprs._parse_date(t) - scprs._parse_date(f)).days <= 20 for f, t in calls)


def test_download_range_reraises_when_single_day_times_out(monkeypatch):
    import pytest

    def always_timeout(bu, frm, to, *, out_dir=None):
        raise scprs.ExtractTimeout("simulated")

    monkeypatch.setattr(scprs, "download_extract", always_timeout)
    with pytest.raises(scprs.ExtractTimeout):
        scprs.download_range("3540", "07/01/2025", "07/01/2025")  # unsplittable single day


def test_download_range_splits_on_row_cap_without_dialog(monkeypatch):
    """If the truncation dialog text ever changes, a cap-hit slice must still be
    split (detected by row count) rather than silently kept as complete."""
    import pandas as pd

    monkeypatch.setattr(scprs, "ROW_CAP", 10)  # keep test frames tiny
    calls = []

    def fake_extract(bu, frm, to, *, out_dir=None):
        a, b = scprs._parse_date(frm), scprs._parse_date(to)
        calls.append((frm, to))
        # truncated flag deliberately False (simulates a reworded dialog); path
        # encodes the day span so fake_load can size the frame.
        return scprs.Extract(bu, frm, to, str((b - a).days), False, False)

    def fake_load(path):
        days = int(path)
        n = scprs.ROW_CAP if days > 15 else 3  # wide span hits the cap
        return pd.DataFrame({"x": range(n)})

    monkeypatch.setattr(scprs, "download_extract", fake_extract)
    monkeypatch.setattr(scprs, "load_extract", fake_load)
    df, warnings = scprs.download_range("3540", "07/01/2025", "06/30/2026")
    assert len(calls) > 1  # bisected on the row count despite no dialog flag
    assert warnings == []  # leaves are under the cap, so nothing kept partial


def test_truncation_comes_from_the_grid_banner_not_page_text():
    """BU 3790, 2026-10: grid text containing "65000"/"65,000" (a contract
    title, a dollar amount) was read as capped, so every refresh of the unit
    was refused. Only the banner total may flag truncation."""
    grid = "1 to 22 of 22\nS24650003 - BULK FUEL\n$65,000.00"
    assert scprs._grid_total(grid) == 22
    assert scprs._grid_total("1 to 200 of 70,123") == 70123
    assert scprs._grid_total("No banner here") is None


class _SlowGrid:
    """Fake page whose results render only after `ready_after` polls."""

    def __init__(self, ready_after: int, text: str):
        self.polls, self.ready_after, self.text = 0, ready_after, text

    def wait_for_timeout(self, _ms):
        self.polls += 1

    def inner_text(self, _sel):
        # The pre-result page: no banner, but a stray "65,000" must not matter.
        return self.text if self.polls >= self.ready_after else "Search Criteria 65,000"


def test_wait_for_results_polls_until_the_banner_renders():
    """CI runners render the grid after the old fixed 6s sleep; the wait must
    poll past the pre-result page instead of snapshotting it."""
    page = _SlowGrid(9, "1 to 22 of 22\n$65,000.00")
    assert scprs._grid_total(scprs._wait_for_results(page, 60_000)) == 22
    assert page.polls == 9
    assert "No Records Found" in scprs._wait_for_results(_SlowGrid(2, "No Records Found"), 60_000)


def test_wait_for_results_times_out_instead_of_guessing():
    """No banner ever: raise ExtractTimeout (download_range bisects on it)
    rather than guess the size from page text."""
    with pytest.raises(scprs.ExtractTimeout):
        scprs._wait_for_results(_SlowGrid(10**9, ""), 5_000)
