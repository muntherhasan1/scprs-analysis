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


# --------------------------------------------------------------- CSCR events


def _raw_event(**over):
    raw = {
        "event_id": "0000033835",
        "event_name": "25YS0006 - * DVBE Only* - Portable Toilet Rental",
        "department_name": "Dept of Veterans Affairs",
        "published_raw": "01/02/2025 10:15AM PST",
        "end_raw": "01/15/2025\n5:00PM PST",
        "status": "Event Completed",
    }
    raw.update(over)
    return raw


def test_events_banner_regex_matches_live_format():
    """The events banner is a THIRD live format: "Showing Results 1-50 of 5702"
    — not the registry's "1-10 of 21450" nor SCPRS's "1 to 200 of 206" (#49)."""
    m = re.search(eprocure.EVENTS_BANNER, "Showing Results 1-50 of 5,702")
    assert m and m.group(3) == "5,702"


def test_tidy_event_derives_dates_and_set_aside_flags():
    row = eprocure._tidy_event(_raw_event(), "historical", 2025)
    assert row["published_date"] == "2025-01-02"
    assert row["end_date"] == "2025-01-15"  # newline in the raw cell collapsed
    assert row["end_raw"] == "01/15/2025 5:00PM PST"
    assert row["dvbe_only"] == 1 and row["sb_only"] == 0
    assert row["search_status"] == "historical" and row["search_year"] == 2025

    sb = eprocure._tidy_event(
        _raw_event(event_name="26VS0017-**SB Only** Water Management"), "posted", None
    )
    assert sb["sb_only"] == 1 and sb["dvbe_only"] == 0
    assert sb["search_year"] is None

    plain = eprocure._tidy_event(_raw_event(event_name="Janitorial Services"), "posted", None)
    assert plain["sb_only"] == 0 and plain["dvbe_only"] == 0


def test_write_events_slice_is_idempotent_and_scoped(tmp_path):
    """Rewriting a slice replaces it; other slices are untouched."""
    db = tmp_path / "e.db"
    hist = [eprocure._tidy_event(_raw_event(event_id=str(i)), "historical", 2025) for i in range(3)]
    posted = [eprocure._tidy_event(_raw_event(event_id="P1"), "posted", None)]
    eprocure.write_events_slice(db, "historical:2025", hist, banner_total=3)
    eprocure.write_events_slice(db, "posted", posted, banner_total=1)
    eprocure.write_events_slice(db, "historical:2025", hist, banner_total=3)  # rerun
    con = sqlite3.connect(db)
    try:
        counts = dict(
            con.execute("SELECT slice_key, COUNT(*) FROM events GROUP BY slice_key").fetchall()
        )
        assert counts == {"historical:2025": 3, "posted": 1}
        meta = dict(con.execute("SELECT slice_key, rows FROM events_meta").fetchall())
        assert meta == {"historical:2025": 3, "posted": 1}
    finally:
        con.close()


def test_write_events_slice_rejects_truncated_pagination(tmp_path):
    """A slice short of the site's own banner total must raise, not write."""
    rows = [
        eprocure._tidy_event(_raw_event(event_id=str(i)), "historical", 2024) for i in range(50)
    ]
    with pytest.raises(eprocure.EprocureError, match="truncated"):
        eprocure.write_events_slice(tmp_path / "e.db", "historical:2024", rows, banner_total=5702)


def test_default_event_years_is_current_and_two_prior():
    from datetime import datetime, timezone

    today = datetime(2026, 7, 24, tzinfo=timezone.utc)
    assert eprocure.default_event_years(today) == (2024, 2025, 2026)


@pytest.mark.parametrize(
    ("name", "flags"),
    [
        ("26VS0017-**SB Only** Water Management", (1, 0, 0)),
        ("*DVBE Only* Kitchen Door Replacement", (0, 1, 0)),
        ("24CS0060 *DVBEs only* Kitchen Door Replacement Services", (0, 1, 0)),
        # SB-or-DVBE set-asides: open to either, so neither "only" flag
        ("**Certified SB, including MB, SB-PW, or a DVBE Only** IFPQ 04A7533", (0, 0, 1)),
        ("*SB/DVBE ONLY* IFPQ 07A", (0, 0, 1)),
        (
            "IFPQ - Certified SB, including MB, SB-PW, or a DVBE OnlyOn-Call Weed Abatement",
            (0, 0, 1),
        ),
        # SB phrasings the old "SB ONLY" substring test missed
        ("Flooring Replacement services **SB BIDDERS ONLY**", (1, 0, 0)),
        ("HVAC Units Services **Small Business Bidders ONLY**", (1, 0, 0)),
        ("*Certified Small Business ONLY* IFPQ Elevator Maintenance", (1, 0, 0)),
        ("24YS0056 *SB-MB Only* Johnson Hall Fan Coils", (1, 0, 0)),
        # "only" that is not a set-aside
        ("Emergency repairs, weekends only", (0, 0, 0)),
        ("Janitorial Services", (0, 0, 0)),
        (None, (0, 0, 0)),
    ],
)
def test_set_aside_flags(name, flags):
    assert eprocure.set_aside_flags(name) == flags


def test_extract_events_posted_only_queues_no_historical_years(monkeypatch, tmp_path):
    """years=() is the daily job's Posted-only run: it must NOT fall back to the
    default Historical years (the old `years or default` did), or the daily job
    would re-extract ~16K Historical rows it is gated to leave untouched."""
    seen = {}

    def fake_download(on_slice, *, years, include_posted, **_kw):
        seen["years"], seen["posted"] = years, include_posted
        on_slice("posted", [], 0)
        return []

    monkeypatch.setattr(eprocure, "download_events", fake_download)
    monkeypatch.setattr(
        eprocure, "write_events_slice", lambda _db, key, rows, total: {"slice": key, "rows": 0}
    )
    eprocure.extract_events(tmp_path / "e.db", years=())
    assert seen == {"years": (), "posted": True}
    eprocure.extract_events(tmp_path / "e.db", years=None)
    assert seen["years"] == eprocure.default_event_years()


# ------------------------------------------- carry-forward of unrefreshed slices

_YEARS = (2024, 2025, 2026)


def _hist(year, n):
    return [
        eprocure._tidy_event(_raw_event(event_id=f"{year}-{i}"), "historical", year)
        for i in range(n)
    ]


def _published_store(path):
    """A previously published store: Posted + three Historical years."""
    eprocure.write_events_slice(
        path, "posted", [eprocure._tidy_event(_raw_event(), "posted", None)], 1
    )
    for year, n in zip(_YEARS, (4, 5, 6), strict=True):
        eprocure.write_events_slice(path, f"historical:{year}", _hist(year, n), n)
    con = sqlite3.connect(path)
    try:  # age the published slices so a carried copy is distinguishable from a fresh one
        con.execute("UPDATE events SET extracted_at = '2026-11-03T12:00:00+00:00'")
        con.execute("UPDATE events_meta SET extracted_at = '2026-11-03T12:00:00+00:00'")
        con.commit()
    finally:
        con.close()


def _fetch_from(published, calls):
    def fetch(dest):
        calls.append(dest)
        dest.write_bytes(published.read_bytes())
        return True

    return fetch


def _slices(path):
    con = sqlite3.connect(path)
    try:
        rows = dict(
            con.execute(
                "SELECT slice_key, COUNT(*) || ' @ ' || MAX(extracted_at) FROM events "
                "GROUP BY slice_key"
            ).fetchall()
        )
        meta = dict(con.execute("SELECT slice_key, rows FROM events_meta").fetchall())
        return rows, meta
    finally:
        con.close()


def test_carry_forward_fills_only_the_slices_this_run_did_not_bank(tmp_path):
    """The 2026-11-10 failure: the registry and Posted refreshed, every Historical
    year timed out. The Historical slices come back from the published store
    unchanged (their old extracted_at keeps their true age); fresh slices are untouched."""
    published = tmp_path / "published.db"
    _published_store(published)
    db = tmp_path / "eprocure.db"
    eprocure.write_events_slice(
        db, "posted", [eprocure._tidy_event(_raw_event(event_id="NEW"), "posted", None)], 1
    )
    calls = []

    carried = eprocure.carry_forward_events(
        db, tmp_path / "prev.db", _fetch_from(published, calls), years=_YEARS
    )

    assert [c["slice"] for c in carried] == [
        "historical:2024",
        "historical:2025",
        "historical:2026",
    ]
    assert [c["rows"] for c in carried] == [4, 5, 6]
    assert {c["extracted_at"] for c in carried} == {"2026-11-03T12:00:00+00:00"}
    rows, meta = _slices(db)
    assert rows["historical:2025"] == "5 @ 2026-11-03T12:00:00+00:00"
    assert not rows["posted"].endswith("2026-11-03T12:00:00+00:00")  # the fresh slice stayed fresh
    assert meta == {"posted": 1, "historical:2024": 4, "historical:2025": 5, "historical:2026": 6}
    assert len(calls) == 1


def test_carry_forward_is_a_no_op_when_every_slice_refreshed(tmp_path):
    db = tmp_path / "eprocure.db"
    eprocure.write_events_slice(db, "posted", [], 0)
    for year in _YEARS:
        eprocure.write_events_slice(db, f"historical:{year}", _hist(year, 2), 2)
    calls = []

    assert (
        eprocure.carry_forward_events(
            db, tmp_path / "prev.db", _fetch_from(db, calls), years=_YEARS
        )
        == []
    )
    assert calls == []  # nothing missing -> the published store is never fetched


def test_carry_forward_fails_closed_without_the_published_store(tmp_path):
    """A slice is missing and the published copy can't be fetched: publishing
    would wipe that slice's good rows, so refuse and leave the store as it was."""
    db = tmp_path / "eprocure.db"
    eprocure.write_events_slice(db, "posted", [], 0)
    before = db.read_bytes()

    with pytest.raises(eprocure.EprocureError, match="NOT publishing"):
        eprocure.carry_forward_events(db, tmp_path / "prev.db", lambda _dest: False, years=_YEARS)
    assert db.read_bytes() == before


def test_carry_forward_when_no_event_slice_completed(tmp_path):
    """Only the registry wrote the store (no events tables yet): every slice is carried."""
    published = tmp_path / "published.db"
    _published_store(published)
    db = tmp_path / "eprocure.db"
    sqlite3.connect(db).close()

    carried = eprocure.carry_forward_events(
        db, tmp_path / "prev.db", _fetch_from(published, []), years=_YEARS
    )

    assert [c["slice"] for c in carried] == [
        "posted",
        "historical:2024",
        "historical:2025",
        "historical:2026",
    ]
    assert _slices(db)[1] == {
        "posted": 1,
        "historical:2024": 4,
        "historical:2025": 5,
        "historical:2026": 6,
    }


def test_carry_forward_reports_a_slice_the_published_store_never_had(tmp_path):
    """After a year rollover the new year's slice may fail before it was ever
    published: nothing to carry, so it is reported (rows 0, no age) and left absent."""
    published = tmp_path / "published.db"
    _published_store(published)
    db = tmp_path / "eprocure.db"
    eprocure.write_events_slice(db, "posted", [], 0)
    for year in (2025, 2026):
        eprocure.write_events_slice(db, f"historical:{year}", _hist(year, 2), 2)

    carried = eprocure.carry_forward_events(
        db, tmp_path / "prev.db", _fetch_from(published, []), years=(2025, 2026, 2027)
    )

    assert carried == [{"slice": "historical:2027", "rows": 0, "extracted_at": None}]
    assert "historical:2027" not in _slices(db)[1]
    assert "historical:2024" not in _slices(db)[1]  # out-of-window years still age out


def test_carry_forward_refuses_a_published_slice_that_disagrees_with_its_meta(tmp_path):
    published = tmp_path / "published.db"
    _published_store(published)
    con = sqlite3.connect(published)
    con.execute("DELETE FROM events WHERE slice_key = 'historical:2025' AND event_id = '2025-0'")
    con.commit()
    con.close()
    db = tmp_path / "eprocure.db"
    eprocure.write_events_slice(db, "posted", [], 0)
    before = db.read_bytes()

    with pytest.raises(eprocure.EprocureError, match="historical:2025"):
        eprocure.carry_forward_events(
            db, tmp_path / "prev.db", _fetch_from(published, []), years=_YEARS
        )
    assert db.read_bytes() == before


def _refresh_steps():
    from pathlib import Path

    import yaml

    wf = yaml.safe_load(Path(".github/workflows/eprocure-refresh.yml").read_text(encoding="utf-8"))
    return wf["jobs"]["refresh"]["steps"]


def test_refresh_workflow_publishes_the_registry_when_event_slices_fail():
    """An event-search timeout must not throw away a good registry extract
    (2026-11-10: registry extracted, events timed out, publish skipped, registry
    stale for two weeks) — but the run must still end red so triage sees it."""
    steps = _refresh_steps()
    names = [s["name"] for s in steps]
    by_id = {s.get("id"): s for s in steps if s.get("id")}

    events, carry = by_id["events"], by_id["carry"]
    assert events.get("continue-on-error") is True
    assert "carry_forward_events" in carry["run"]
    assert "continue-on-error" not in carry  # can't carry -> fail closed, no publish
    gate = next(n for n in names if n.startswith("Sanity-check"))
    publish = next(n for n in names if n.startswith("Publish"))
    fail = next(s for s in steps if s["name"].startswith("Fail the run"))
    assert names.index(events["name"]) < names.index(carry["name"]) < names.index(gate)
    assert names.index(gate) < names.index(publish) < names.index(fail["name"])
    assert "steps.events.outcome == 'failure'" in fail["if"]
    assert "steps.carry.outputs.carried != ''" in fail["if"]
    assert "${{" not in fail["run"]  # inputs via env:, never inline in bash
