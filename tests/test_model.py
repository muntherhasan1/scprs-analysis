"""Offline tests for the SQLite data model (download_range is monkeypatched)."""

import pandas as pd

from src import model


def _fixture_df() -> pd.DataFrame:
    """Two rows shaped like the summary extract after load_extract()."""
    return pd.DataFrame(
        {
            "Department": [8660, 8660],
            "Department Name": ["Public Utilities Comm", "Public Utilities Comm"],
            "Purchase Document #": ["A1", "A2"],
            "Associated POs": [None, None],
            "First Item Title": ["x", "y"],
            "Start Date": pd.to_datetime(["2025-01-05", "2025-02-10"]),
            "End Date": pd.to_datetime(["2025-06-01", "2025-07-01"]),
            "Grand Total": [1000.0, 2000.0],
            "Supplier ID": ["S1", "S2"],
            "Supplier Name": ["ACME", "BETA"],
            "Certification Type": [None, None],
            "Acquisition Type_ Sub-Type": ["t", "t"],
            "Acquisition Method": ["Formal - COMPETITIVE", "CMAS"],
            "LPA Contract ID": [None, None],
            "Buyer Name": ["b", "b"],
            "Buyer Email": ["b@x", "b@x"],
            "Status": ["Active", "Active"],
            "Version": [1, 1],
        }
    )


def test_snake():
    assert model._snake("Purchase Document #") == "purchase_document"
    assert model._snake("Acquisition Type_ Sub-Type") == "acquisition_type_sub_type"


def test_build_query_and_refresh(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(model.scprs, "download_range", lambda *a, **k: (_fixture_df(), []))

    n, warnings = model.build_db(
        "8660", "01/01/2025", "12/31/2025", db_path=db, log=lambda *a: None
    )
    assert n == 2 and warnings == []

    df = model.query("SELECT * FROM purchases", db_path=db)
    assert {"business_unit", "purchase_document", "grand_total", "start_date"}.issubset(df.columns)
    # dates stored as ISO strings for lexical filtering
    assert df["start_date"].tolist() == ["2025-01-05", "2025-02-10"]
    assert df["business_unit"].tolist() == ["8660", "8660"]

    # rollup view aggregates correctly
    v = model.query(
        "SELECT total_value FROM v_supplier_totals WHERE business_unit='8660' ORDER BY total_value",
        db_path=db,
    )
    assert v["total_value"].tolist() == [1000.0, 2000.0]

    # re-running a business unit refreshes (no duplicate rows)
    model.build_db("8660", "01/01/2025", "12/31/2025", db_path=db, log=lambda *a: None)
    assert model.query("SELECT COUNT(*) c FROM purchases", db_path=db)["c"][0] == 2


def _seed_purchases(db, rows):
    """rows: (business_unit, purchase_document, start_date, version)."""
    con = model._connect(db)
    model._ensure_schema(con)
    con.executemany(
        "INSERT INTO purchases (business_unit, purchase_document, start_date, version) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _scraped(n, start="2025-01-05", version=1):
    df = _fixture_df().iloc[[0] * n].reset_index(drop=True)
    df["Purchase Document #"] = [f"N{i}" for i in range(n)]
    df["Start Date"] = pd.to_datetime([start] * n)
    df["Version"] = version
    return df


def test_refresh_derives_full_window_and_never_narrows(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
    _seed_purchases(db, [("8660", "OLD", "2016-01-01", 1), ("5180", "X", "2023-03-01", 1)])
    calls = []

    def fake_range(bu, f, t, **k):
        calls.append((bu, f, t))
        return _scraped(2), []

    monkeypatch.setattr(model.scprs, "download_range", fake_range)
    model.refresh_db("8660", db_path=db, log=lambda *a: None)
    model.refresh_db("5180", db_path=db, log=lambda *a: None)
    # 8660's stored history starts before the policy -> kept; 5180's starts
    # after it -> widened to the policy start, never narrowed to 2023.
    assert calls == [
        ("8660", "01/01/2016", model.POLICY_END),
        ("5180", "07/01/2021", model.POLICY_END),
    ]
    stamped = model.query("SELECT * FROM summary_refresh ORDER BY business_unit", db_path=db)
    assert stamped["window_from"].tolist() == ["2021-07-01", "2016-01-01"]
    assert stamped["rows"].tolist() == [2, 2]
    # Stamped in UTC with an explicit offset: health reads a bare timestamp as
    # UTC, so a naive local-time stamp from a laptop run looked hours stale.
    assert all(ts.endswith("+00:00") for ts in stamped["refreshed_at"])


def test_refresh_gate_refuses_without_writing(tmp_path, monkeypatch):
    import pytest

    db = tmp_path / "g.db"
    # Same document ids `_scraped` produces, so a re-scrape is the same documents.
    _seed_purchases(db, [("5180", f"N{i}", "2024-03-05", 1) for i in range(100)])

    def count():
        return model.query("SELECT COUNT(*) c FROM purchases", db_path=db)["c"][0]

    for scraped, warnings in [
        (_scraped(90), []),  # shrink of 10 > max(5, 0.5%)
        (_scraped(0), []),  # empty result for a populated unit
        (_scraped(120), ["01/01/2024..01/31/2024 hit 65,000-row cap; partial"]),
    ]:
        monkeypatch.setattr(
            model.scprs, "download_range", lambda *a, s=scraped, w=warnings, **k: (s, w)
        )
        with pytest.raises(model.RefreshRefused):
            model.refresh_db("5180", db_path=db, log=lambda *a: None)
        assert count() == 100  # untouched

    # A small shrink (site pruned a few documents) is within tolerance.
    monkeypatch.setattr(model.scprs, "download_range", lambda *a, **k: (_scraped(96), []))
    r = model.refresh_db("5180", db_path=db, log=lambda *a: None)
    assert (r["before"], r["after"]) == (100, 96) and count() == 96


def test_reload_reopens_days_with_undrilled_documents(tmp_path, monkeypatch):
    """A late-entered document on an enriched day reopens it; enrich drills it,
    clears the reopen, and details_progress never shrinks (contracts metric)."""
    db = tmp_path / "o.db"
    _seed_purchases(db, [("5180", "N0", "2025-01-05", 1)])
    con = model._connect(db)
    model._ensure_progress_schema(con)
    model._ensure_details_schema(con)
    con.execute(
        "INSERT INTO document_details (business_unit, purchase_document, version) "
        "VALUES ('5180', 'N0', '1')"
    )
    con.execute("INSERT INTO details_progress VALUES ('5180', '2025-01-05', 1, 0, 0, 'x')")
    con.commit()
    con.close()

    # Same doc at the same version only -> nothing to reopen.
    monkeypatch.setattr(model.scprs, "download_range", lambda *a, **k: (_scraped(1), []))
    assert model.refresh_db("5180", db_path=db, log=lambda *a: None)["reopened_days"] == 0

    # A second (late-entered) document appears on that day -> reopened.
    monkeypatch.setattr(model.scprs, "download_range", lambda *a, **k: (_scraped(2), []))
    assert model.refresh_db("5180", db_path=db, log=lambda *a: None)["reopened_days"] == 1

    visited = []
    monkeypatch.setattr(
        model,
        "build_details_db",
        lambda bu, f, t, *, db_path, skip_docs=None, **k: (
            visited.append((t, skip_docs)) or {"documents": 1, "lines": 0, "pos": 0}
        ),
    )
    model.enrich_details("5180", "01/01/2025", "12/31/2025", db_path=db, log=lambda *a: None)
    assert visited == [("01/05/2025", {"N0"})]  # only the new document is drilled
    q = "SELECT (SELECT COUNT(*) FROM details_reopen) r, (SELECT COUNT(*) FROM details_progress) p"
    assert model.query(q, db_path=db).iloc[0].tolist() == [0, 1]

    # An amendment (summary version newer than every drilled version) also reopens.
    monkeypatch.setattr(model.scprs, "download_range", lambda *a, **k: (_scraped(1, version=2), []))
    assert model.refresh_db("5180", db_path=db, log=lambda *a: None)["reopened_days"] == 1


def test_refresh_units_rotation_budget_and_failures(tmp_path, monkeypatch):
    db = tmp_path / "u.db"
    _seed_purchases(db, [(bu, "D", "2024-01-01", 1) for bu in ("0820", "2720", "3540", "8660")])
    con = model._connect(db)
    con.executemany(
        "INSERT INTO summary_refresh VALUES (?, '2021-07-01', 1, ?)",
        [("0820", "2026-10-20T00:00:00"), ("3540", "2026-10-10T00:00:00")],
    )
    con.commit()
    con.close()
    order = []

    def fake_refresh(bu, **k):
        order.append(bu)
        if bu == "2720":
            raise model.RefreshRefused("would shrink")
        return {"business_unit": bu, "before": 1, "after": 1, "reopened_days": 0}

    monkeypatch.setattr(model, "refresh_db", fake_refresh)
    r = model.refresh_units(db_path=db, log=lambda *a: None)
    # never-refreshed first (by code), then oldest refresh first
    assert order == ["2720", "8660", "3540", "0820"]
    assert [f["business_unit"] for f in r["failed"]] == ["2720"]  # others still ran
    assert len(r["refreshed"]) == 3 and r["remaining"] == []

    order.clear()
    r = model.refresh_units(db_path=db, budget_minutes=0, log=lambda *a: None)
    assert order == [] and len(r["remaining"]) == 4


def test_refresh_gate_catches_lost_documents_masked_by_new_ones(tmp_path, monkeypatch):
    """A scrape that loses stored documents while late-entered ones arrive
    elsewhere nets out flat — the row count alone let it through."""
    import pytest

    db = tmp_path / "m.db"
    _seed_purchases(db, [("5180", f"N{i}", "2024-03-05", 1) for i in range(1000)])
    # Same row count, but 20 stored documents replaced by 20 new ones: 2% lost,
    # over the 0.5% tolerance, with a net change of zero.
    swapped = _scraped(1000, start="2024-03-05")
    swapped.loc[:19, "Purchase Document #"] = [f"NEW{i}" for i in range(20)]
    monkeypatch.setattr(model.scprs, "download_range", lambda *a, **k: (swapped, []))
    with pytest.raises(model.RefreshRefused, match="would drop 20 stored document"):
        model.refresh_db("5180", db_path=db, log=lambda *a: None)


def test_refresh_gate_catches_a_lost_month_inside_the_unit_tolerance(tmp_path, monkeypatch):
    """A big unit's 0.5% can hide a whole small month; the per-month check can't."""
    import pytest

    db = tmp_path / "mm.db"
    big = [("3540", f"B{i}", "2024-01-10", 1) for i in range(4000)]
    small = [("3540", f"S{i}", "2024-02-10", 1) for i in range(15)]
    _seed_purchases(db, big + small)
    # Lose all 15 February docs: 15 <= max(5, 0.5% of 4015 = 20) unit-wide.
    jan = _scraped(4000, start="2024-01-10")
    jan["Purchase Document #"] = [f"B{i}" for i in range(4000)]
    monkeypatch.setattr(model.scprs, "download_range", lambda *a, **k: (jan, []))
    with pytest.raises(model.RefreshRefused, match="15 of 15 stored document.*2024-02"):
        model.refresh_db("3540", db_path=db, log=lambda *a: None)

    # A few pruned documents in a month stay within the per-month allowance.
    feb = _scraped(12, start="2024-02-10")
    feb["Purchase Document #"] = [f"S{i}" for i in range(12)]
    jan_feb = pd.concat([jan, feb], ignore_index=True)
    monkeypatch.setattr(model.scprs, "download_range", lambda *a, **k: (jan_feb, []))
    assert model.refresh_db("3540", db_path=db, log=lambda *a: None)["after"] == 4012


def test_refused_unit_rotates_to_the_back_without_counting_as_refreshed(tmp_path, monkeypatch):
    """The 2026-10-27..29 BU 3790 loop: a unit refused every run used to stay
    first in the rotation forever and could spend the whole budget."""
    db = tmp_path / "r.db"
    _seed_purchases(db, [(bu, "D", "2024-01-01", 1) for bu in ("0820", "2720", "3790")])
    con = model._connect(db)
    con.executemany(
        "INSERT INTO summary_refresh VALUES (?, '2021-07-01', 1, ?)",
        [("0820", "2026-10-20T00:00:00"), ("2720", "2026-10-21T00:00:00")],
    )
    con.commit()
    con.close()
    order, refuse = [], {"3790"}

    def fake_refresh(bu, **k):
        order.append(bu)
        if bu in refuse:
            raise model.RefreshRefused("hit the row cap")
        con = model._connect(db)
        try:
            model._replace_unit(con, bu, model._prepare_summary(_scraped(1), bu), "07/01/2021")
        finally:
            con.close()
        return {"business_unit": bu, "before": 1, "after": 1, "reopened_days": 0}

    monkeypatch.setattr(model, "refresh_db", fake_refresh)
    # Run 1: never-refreshed 3790 goes first and is refused.
    model.refresh_units(db_path=db, business_units=["3790"], log=lambda *a: None)
    fail = model.query("SELECT * FROM summary_refresh_failure", db_path=db)
    assert fail["business_unit"].tolist() == ["3790"] and "row cap" in fail["error"][0]
    stamped = model.query("SELECT business_unit FROM summary_refresh", db_path=db)
    assert "3790" not in stamped["business_unit"].tolist()  # not recorded as refreshed

    # Run 2: the others go first now; 3790 waits its turn at the back.
    order.clear()
    model.refresh_units(db_path=db, log=lambda *a: None)
    assert order == ["0820", "2720", "3790"]

    # A later success clears the failure stamp.
    refuse.clear()
    model.refresh_units(db_path=db, business_units=["3790"], log=lambda *a: None)
    assert model.query("SELECT COUNT(*) c FROM summary_refresh_failure", db_path=db)["c"][0] == 0
