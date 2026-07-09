"""Offline tests for the SCPRS extract parser (no network/browser)."""

from pathlib import Path

from src import scprs

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


def test_to_csv_roundtrip(tmp_path: Path):
    f = tmp_path / "s.xls"
    f.write_text(SAMPLE_XLS, encoding="utf-8")
    csv = scprs.to_csv(f)
    assert csv.exists()
    assert "C MURPHY CONSULTING LLC" in csv.read_text(encoding="utf-8")
