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
