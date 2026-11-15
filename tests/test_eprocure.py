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
    """Slices the run did not bank come back from the published store unchanged
    (their old extracted_at keeps their true age); fresh slices are untouched."""
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
    published = tmp_path / "published.db"
    published.write_bytes(db.read_bytes())

    assert (
        eprocure.carry_forward_events(
            db, tmp_path / "prev.db", _fetch_from(published, []), years=_YEARS
        )
        == []
    )


def test_carry_forward_without_published_store_when_nothing_is_missing(tmp_path):
    """Nothing to carry and no published copy to compare against: leave the store
    alone (the shrink gate's own fetch decides, as before)."""
    db = tmp_path / "eprocure.db"
    eprocure.write_events_slice(db, "posted", [], 0)
    for year in _YEARS:
        eprocure.write_events_slice(db, f"historical:{year}", _hist(year, 2), 2)
    before = db.read_bytes()

    assert (
        eprocure.carry_forward_events(db, tmp_path / "prev.db", lambda _d: False, years=_YEARS)
        == []
    )
    assert db.read_bytes() == before


def test_carry_forward_replays_the_2026_11_10_run(tmp_path):
    """2026-11-10, slice by slice: Posted refreshed; 2024 refreshed on retry; 2025
    "refreshed" on retry with 385 rows against a banner of 385 (it held 5,616 a
    week earlier; 385 was that run's Posted count, so the retry read the wrong
    result set and the banner check could not tell); 2026 failed twice. The 2025
    slice must be treated as unrefreshed, or the whole-table shrink gate refuses
    the publish and the fresh registry is lost again."""
    published = tmp_path / "published.db"
    _published_store(published)  # posted 1, 2024: 4, 2025: 5, 2026: 6
    db = tmp_path / "eprocure.db"
    eprocure.write_events_slice(
        db, "posted", [eprocure._tidy_event(_raw_event(), "posted", None)], 1
    )
    eprocure.write_events_slice(db, "historical:2024", _hist(2024, 4), 4)
    eprocure.write_events_slice(db, "historical:2025", _hist(2025, 1), 1)  # wrong result set

    carried = eprocure.carry_forward_events(
        db, tmp_path / "prev.db", _fetch_from(published, []), years=_YEARS
    )

    by_slice = {c["slice"]: c for c in carried}
    assert list(by_slice) == ["historical:2025", "historical:2026"]
    assert by_slice["historical:2025"]["rows"] == 5
    assert "shrank" in by_slice["historical:2025"]["reason"]
    assert by_slice["historical:2026"]["reason"] == "not refreshed"
    rows, meta = _slices(db)
    assert meta == {"posted": 1, "historical:2024": 4, "historical:2025": 5, "historical:2026": 6}
    assert not rows["historical:2024"].endswith("2026-11-03T12:00:00+00:00")  # kept fresh


@pytest.mark.parametrize(("new_posted", "replaced"), [(4, True), (6, False)])
def test_carry_forward_posted_floor_allows_daily_churn(tmp_path, new_posted, replaced):
    """Posted churns day to day, so its floor is 50% (the daily job's); 6 of a
    published 10 is churn, 4 of 10 is a broken search."""

    def posted(n, tag):
        return [
            eprocure._tidy_event(_raw_event(event_id=f"{tag}{i}"), "posted", None) for i in range(n)
        ]

    published = tmp_path / "published.db"
    eprocure.write_events_slice(published, "posted", posted(10, "old"), 10)
    db = tmp_path / "eprocure.db"
    eprocure.write_events_slice(db, "posted", posted(new_posted, "new"), new_posted)

    carried = eprocure.carry_forward_events(
        db, tmp_path / "prev.db", _fetch_from(published, []), years=()
    )

    assert [c["slice"] for c in carried] == (["posted"] if replaced else [])
    assert _slices(db)[1] == {"posted": 10 if replaced else new_posted}


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
    for year, n in ((2025, 5), (2026, 6)):  # refreshed at their published sizes
        eprocure.write_events_slice(db, f"historical:{year}", _hist(year, n), n)

    carried = eprocure.carry_forward_events(
        db, tmp_path / "prev.db", _fetch_from(published, []), years=(2025, 2026, 2027)
    )

    assert carried == [
        {"slice": "historical:2027", "rows": 0, "extracted_at": None, "reason": "not refreshed"}
    ]
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


# ------------------------------------------------------------ failure evidence


def _fake_playwright(monkeypatch, tmp_path, goto_error, calls):
    from pathlib import Path
    from types import SimpleNamespace

    from src import failure_evidence

    monkeypatch.setattr(failure_evidence, "EVIDENCE_DIR", tmp_path / "evidence")
    failure_evidence.PageWatch.reset_limit()

    class Page:
        url = eprocure.EVENTS_URL

        def on(self, *_a):
            pass

        def goto(self, *_a, **_k):
            raise goto_error

        def screenshot(self, *, path, **_k):
            calls.append("capture")
            Path(path).write_bytes(b"png")
            return b"png"

        def title(self):
            return "Event Search"

        def evaluate(self, *_a):
            return ""

        def content(self):
            return "<html></html>"

    class Ctx:
        def new_page(self):
            return Page()

        def close(self):
            calls.append("close")

    class Browser:
        def new_context(self, **_k):
            return Ctx()

        def close(self):
            calls.append("browser-close")

    class Playwright:
        chromium = SimpleNamespace(launch=lambda **_k: Browser())

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(eprocure, "sync_playwright", Playwright)
    return tmp_path / "evidence"


def _captured(evidence_dir):
    return sorted(d.name.split("Z-", 1)[1] for d in evidence_dir.iterdir())


def test_events_capture_evidence_on_each_attempt_before_the_session_closes(monkeypatch, tmp_path):
    calls = []
    evidence = _fake_playwright(
        monkeypatch, tmp_path, eprocure.PWTimeout("Timeout 120000ms exceeded."), calls
    )
    with pytest.raises(eprocure.EprocureError, match="posted"):
        eprocure.download_events(lambda *_a: None, years=())
    assert calls == ["capture", "close", "capture", "close", "browser-close"]
    assert _captured(evidence) == ["events-posted-attempt1", "events-posted-attempt2"]


def test_events_capture_evidence_on_an_unexpected_error_too(monkeypatch, tmp_path):
    """The 2026-10 registry failures were net::ERR_EMPTY_RESPONSE from goto — a
    Playwright Error, not a timeout — which ends the run without a retry."""
    calls = []
    evidence = _fake_playwright(
        monkeypatch, tmp_path, RuntimeError("net::ERR_EMPTY_RESPONSE"), calls
    )
    with pytest.raises(RuntimeError, match="ERR_EMPTY_RESPONSE"):
        eprocure.download_events(lambda *_a: None, years=())
    assert calls[:2] == ["capture", "close"]
    assert _captured(evidence) == ["events-posted-attempt1"]


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (eprocure.PWTimeout("Timeout 180000ms exceeded."), eprocure.EprocureError),
        (RuntimeError("net::ERR_EMPTY_RESPONSE"), RuntimeError),
    ],
)
def test_registry_captures_evidence_before_the_browser_closes(monkeypatch, tmp_path, error, raised):
    calls = []
    evidence = _fake_playwright(monkeypatch, tmp_path, error, calls)
    with pytest.raises(raised):
        eprocure.download_registry(out_dir=tmp_path)
    assert calls == ["capture", "browser-close"]
    assert _captured(evidence) == ["registry"]


def test_extract_events_logs_the_site_status_mix(monkeypatch, tmp_path, capsys):
    def fake_download(on_slice, **_kw):
        rows = [
            eprocure._tidy_event(_raw_event(event_id=str(i), status=st), "historical", 2025)
            for i, st in enumerate(["Event Completed", "Event Completed", "Canceled"])
        ]
        on_slice("historical:2025", rows, 3)
        return []

    monkeypatch.setattr(eprocure, "download_events", fake_download)
    eprocure.extract_events(tmp_path / "e.db", years=(2025,), include_posted=False)
    assert "events historical:2025: site status mix Event Completed 2, Canceled 1" in (
        capsys.readouterr().out
    )


# ---- event detail pages (recon 2026-11-15) -------------------------------------
# Real reads of two live detail pages (Veterans Affairs flooring, Caltrans SB
# Minor B), trimmed. Buyer contact fields are on the page but never read.
_FLOORING = {
    "event_id": "0000040541",
    "event_name": "25HS0024 - NCVC Flooring Replacement Service",
    "dept": "Dept of Veterans Affairs",
    "format1": "Sell Event",
    "format2": "RFx",
    "published": "10/02/2026 3:30PM PDT",
    "version": "3",
    "end": "11/16/2026 4:00PM PST",
    "description": "The Northern California Veterans Cemetery requires Flooring Replacement "
    "Services to be done on their campus.\nADDENDUMS ADDED***",
    "conference": "Mandatory",
    "conf_date": "10/21/2026",
    "conf_time": "1:00PM",
    "conf_location": "Northern California Veterans Cemetery Administration Building",
    "conf_comments": "Attendance for the entire inspection is required in order for bidders "
    "to be eligible to submit a bid.",
    "unspsc": ["72152500", "72152506"],
    "unspsc_desc": [
        "Special trade construction & maint srvcs - Floor laying",
        "Special trade construction & maint srvcs - Floor laying - Resilient floor laying",
    ],
    "license": ["B", "C-15"],
    "license_desc": ["General Building Contractor", "Flooring and Floor Covering Contractors"],
    "area_id": ["045", "052", "053"],
    "county": ["Shasta", "Tehama", "Trinity"],
}
_CALTRANS = {
    "event_id": "11A4467",
    "format1": "Sell Event",
    "format2": "RFx",
    "published": "10/30/2026 2:00PM PDT",
    "version": "1",
    "conference": "Non Mandatory",
    "conf_date": "",
    "conf_time": "",
    "conf_location": "",
    "conf_comments": "",
    "unspsc": ["72141000"],
    "unspsc_desc": ["Heavy construction services - Highway & road construction services"],
    "license": ["A"],
    "license_desc": ["General Engineering Contractor"],
    "area_id": ["013"],
    "county": ["Imperial"],
}


def test_parse_event_detail_from_live_reads():
    d = eprocure.parse_event_detail(_FLOORING)
    assert d["format_type"] == "Sell Event / RFx"
    assert (d["published_date"], d["event_version"]) == ("2026-10-02", 3)
    assert d["prebid_mandatory"] == 1 and d["prebid_date"] == "2026-10-21"
    assert d["prebid_comments"].startswith("Attendance for the entire inspection")
    assert d["unspsc"][0] == ("72152500", "Special trade construction & maint srvcs - Floor laying")
    assert [t for t, _ in d["licenses"]] == ["B", "C-15"]
    assert d["service_areas"] == [("045", "Shasta"), ("052", "Tehama"), ("053", "Trinity")]
    assert "\n" in d["description"]  # line breaks kept

    c = eprocure.parse_event_detail(_CALTRANS)
    assert c["prebid_mandatory"] == 0  # "Non Mandatory"
    assert c["prebid_date"] is None and c["prebid_location"] is None  # blanks -> None
    # A "Pre Bid Conference(N/A)" section has no conference text at all.
    na = eprocure.parse_event_detail({**_CALTRANS, "conference": None})
    assert na["prebid_mandatory"] is None


def _posted_db(tmp_path, events):
    db = tmp_path / "e.db"
    rows = [
        eprocure._tidy_event(
            {
                "event_id": eid,
                "event_name": f"Bid {eid}",
                "department_name": dept,
                "published_raw": "",
                "end_raw": end,
                "status": "Posted",
            },
            "posted",
            None,
        )
        for dept, eid, end in events
    ]
    eprocure.write_events_slice(db, "posted", rows, banner_total=len(rows))
    con = sqlite3.connect(db)
    eprocure._ensure_detail_tables(con)
    return db, con


def test_write_event_detail_replaces_one_event_atomically(tmp_path):
    db, con = _posted_db(tmp_path, [("Dept of Veterans Affairs", "0000040541", "11/16/2026")])
    key = ("Dept of Veterans Affairs", "0000040541")
    d = eprocure.parse_event_detail(_FLOORING)
    eprocure.write_event_detail(con, *key, "8955", d, "2026-11-15T22:00:00+00:00")
    eprocure.write_event_detail(
        con, *key, "8955", {**d, "unspsc": d["unspsc"][:1]}, "2026-11-16T22:00:00+00:00"
    )

    def count(t):
        return con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 - test constant

    assert count("event_details") == 1
    assert count("event_detail_unspsc") == 1  # the second read replaced, not appended
    assert count("event_detail_licenses") == 2 and count("event_detail_service_areas") == 3
    row = con.execute(
        "SELECT business_unit, fetched_at, prebid_mandatory FROM event_details"
    ).fetchone()
    assert row == ("8955", "2026-11-16T22:00:00+00:00", 1)
    cols = {r[1] for r in con.execute("PRAGMA table_info(event_details)")}
    assert not cols & {"contact_name", "phone", "email"}  # buyer contact is never stored


def test_detail_queue_reads_new_events_first_and_retries_misses_weekly(tmp_path):
    db, con = _posted_db(
        tmp_path,
        [
            ("A Dept", "1", "12/01/2026"),
            ("A Dept", "2", "11/20/2026"),
            ("B Dept", "3", "11/18/2026"),
            ("C Dept", "4", "11/17/2026"),
        ],
    )
    d = eprocure.parse_event_detail(_CALTRANS)
    eprocure.write_event_detail(con, "A Dept", "1", "0001", d, "2026-11-10T00:00:00+00:00")
    eprocure.write_event_detail(con, "B Dept", "3", "0002", d, "2026-11-01T00:00:00+00:00")
    eprocure._record_detail_miss(con, "C Dept", "4", ["9999"], "2026-11-12")
    queue = eprocure._detail_queue(con, "2026-11-15")
    # never read first (2), then oldest read (3 before 1); 4 was missed 3 days ago
    assert queue == [("A Dept", "2"), ("B Dept", "3"), ("A Dept", "1")]
    assert ("C Dept", "4") in eprocure._detail_queue(con, "2026-11-20")  # 8 days later: retried


def test_unit_candidates_try_aliases_then_every_matching_unit(tmp_path):
    depts = tmp_path / "departments.csv"
    depts.write_text(
        "code,name\n1730,Franchise Tax Board\n7730,Franchise Tax Board\n8940,Military Department\n",
        encoding="utf-8",
    )
    aliases = tmp_path / "aliases.csv"
    aliases.write_text(
        "eprocure_department_name,business_unit,note\nCalifornia Military Department,8940,x\n",
        encoding="utf-8",
    )
    c = eprocure.load_unit_candidates(depts, aliases)
    assert c["FRANCHISE TAX BOARD"] == ["1730", "7730"]  # ambiguous: the page decides
    assert c["CALIFORNIA MILITARY DEPARTMENT"] == ["8940"]
    real = eprocure.load_unit_candidates()  # the committed reference files load
    assert "8940" in real["CALIFORNIA MILITARY DEPARTMENT"]
    assert real["COVERED CALIFORNIA"] == ["4800"]


def test_weekly_rebuild_carries_published_detail_reads(tmp_path):
    """The weekly refresh rebuilds eprocure.db from empty; the daily job's detail
    reads must survive it for every bid still Posted, and only those."""
    prev_dir, new_dir = tmp_path / "prev", tmp_path / "new"
    prev_dir.mkdir()
    new_dir.mkdir()
    prev, pcon = _posted_db(
        prev_dir, [("A Dept", "1", "12/01/2026"), ("A Dept", "2", "11/20/2026")]
    )
    d = eprocure.parse_event_detail(_FLOORING)
    for eid in ("1", "2"):
        eprocure.write_event_detail(pcon, "A Dept", eid, "0001", d, "2026-11-14T00:00:00+00:00")
    pcon.close()
    # This week's Posted slice: bid 1 is still open, bid 2 closed, bid 3 is new.
    new, ncon = _posted_db(new_dir, [("A Dept", "1", "12/01/2026"), ("A Dept", "3", "12/05/2026")])
    ncon.close()
    out = eprocure.carry_forward_event_details(new, prev, lambda _p: False)
    assert out["carried"] == 1
    con = sqlite3.connect(new)
    assert con.execute("SELECT event_id FROM event_details").fetchall() == [("1",)]
    assert con.execute("SELECT COUNT(*) FROM event_detail_unspsc").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM event_detail_service_areas").fetchone()[0] == 3
    con.close()
    # Nothing published yet: a clean no-op, not an error.
    fresh, fcon = _posted_db(tmp_path, [("A Dept", "9", "12/01/2026")])
    fcon.close()
    missing = eprocure.carry_forward_event_details(fresh, tmp_path / "absent.db", lambda _p: False)
    assert missing["carried"] == 0
