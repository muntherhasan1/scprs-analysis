"""Offline tests for the eProcure registry extractor.

The browser-facing search/download is not exercised here (no live calls in CI);
these lock down the load-bearing parts — cleaning the PeopleSoft export quirks,
the banner format, and the completeness gate — using a fixture built from real
captured export rows (recon 2026-07-23).
"""

import re
import sqlite3

import pytest

from src import eprocure

# Two firms; DRAVES-like row exercises the list-column apostrophe quirk, the
# WELLS-like pair exercises the per-certification-track grain (same cert id).
_EXPORT_HTML = """
<table border='1'>
<tr><th>Certification ID</th><th>Legal Business Name</th><th>Certification Type</th>
<th>Start Date</th><th>End Date</th><th>UNSPSC</th><th>Service Areas</th>
<th>Industry Type</th><th>Telephone</th></tr>
<tr><td>45</td><td>O'BRIEN PIPELINE INC</td><td>DVBE,SB(Micro)</td>
<td>10/08/2025</td><td>10/31/2027</td><td>'22101700,'22101900</td>
<td>'Alpine,'Butte</td><td>'Non-Manufacturer,'Service,</td><td>760/728-7094</td></tr>
<tr><td>333</td><td>WELLS SWEEPING COMPANY</td><td>SB(Micro)</td>
<td>01/17/2026</td><td>01/31/2028</td><td>'47131824</td>
<td>'Alameda</td><td>'Service,</td><td>916/718-8345</td></tr>
<tr><td>333</td><td>WELLS SWEEPING COMPANY</td><td>DVBE</td>
<td>01/20/2026</td><td>01/31/2028</td><td></td>
<td>'Alameda</td><td>'Service,</td><td></td></tr>
</table>
"""


@pytest.fixture
def export_file(tmp_path):
    path = tmp_path / "eprocure_registry.xls"
    path.write_text(_EXPORT_HTML, encoding="utf-8")
    return path


def test_banner_regex_matches_live_format():
    """The literal live banner is "1-10 of 21450" — NOT the SCPRS format (#49)."""
    m = re.search(eprocure.REGISTRY_BANNER, "1-10 of 21450")
    assert m and m.group(3) == "21450"
    assert re.search(eprocure.REGISTRY_BANNER, "1 to 200 of 206") is None


def test_load_cleans_list_columns_and_dates(export_file):
    df = eprocure.load_registry(export_file)
    row = df.iloc[0]
    assert row["UNSPSC"] == "22101700, 22101900"
    assert row["Service Areas"] == "Alpine, Butte"
    # Trailing empty element (the export's dangling comma) is dropped.
    assert row["Industry Type"] == "Non-Manufacturer, Service"
    assert row["Start Date"] == "2025-10-08"
    assert row["End Date"] == "2027-10-31"


def test_load_preserves_interior_apostrophes(export_file):
    """Names may legitimately contain apostrophes; only a leading one is a quirk."""
    df = eprocure.load_registry(export_file)
    assert df.iloc[0]["Legal Business Name"] == "O'BRIEN PIPELINE INC"


def test_load_adds_normalized_name_join_key(export_file):
    df = eprocure.load_registry(export_file)
    # Same normalization gold uses for supplier side inputs (suffixes dropped).
    assert df.iloc[1]["normalized_name"] == "WELLS SWEEPING"


def test_write_registry_stores_rows_and_meta(export_file, tmp_path):
    df = eprocure.load_registry(export_file)
    db = tmp_path / "eprocure.db"
    summary = eprocure.write_registry(df, banner_total=2, db_path=db)
    assert summary == {"rows": 3, "unique_cert_ids": 2, "banner_total": 2}

    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM registry").fetchone()[0] == 3
    meta = con.execute("SELECT banner_total, rows, unique_cert_ids FROM extract_meta").fetchone()
    assert meta == (2, 3, 2)
    con.close()


def test_write_registry_is_idempotent(export_file, tmp_path):
    df = eprocure.load_registry(export_file)
    db = tmp_path / "eprocure.db"
    eprocure.write_registry(df, banner_total=2, db_path=db)
    eprocure.write_registry(df, banner_total=2, db_path=db)  # full replace, no dupes
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM registry").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM extract_meta").fetchone()[0] == 1
    con.close()


def test_write_registry_rejects_truncated_export(export_file, tmp_path):
    """A partial export must fail loudly, never look like a finished run."""
    df = eprocure.load_registry(export_file)
    with pytest.raises(eprocure.EprocureError, match="truncated"):
        eprocure.write_registry(df, banner_total=1000, db_path=tmp_path / "e.db")
    assert not (tmp_path / "e.db").exists()


def test_write_registry_rejects_empty(export_file, tmp_path):
    df = eprocure.load_registry(export_file).iloc[0:0]
    with pytest.raises(eprocure.EprocureError, match="0 rows"):
        eprocure.write_registry(df, banner_total=0, db_path=tmp_path / "e.db")
