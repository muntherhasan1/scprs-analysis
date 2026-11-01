"""R4 outlier review (src/outliers.py): both flag rules, evidence gathering, verdict logic, and
the recorded-review status that decides whether R4 passes."""

import math

import pandas as pd
import pytest

from src import outliers


def _doc(doc, value, bu="3540", acq="NON-IT Services", fy=2024, flag=None, start="2023-08-01"):
    return {
        "business_unit": bu,
        "purchase_document": doc,
        "supplier_name": f"VENDOR {doc}",
        "canonical_name": f"VENDOR {doc}",
        "acquisition_type": acq,
        "award_class": "C",
        "contract_flag": flag,
        "first_item_title": "t",
        "start_date": start,
        "fiscal_year": fy,
        "grand_total": value,
    }


def test_flag_outliers_by_robust_z_and_budget_share():
    # 40 ordinary contracts spread over two orders of magnitude, plus three candidates.
    docs = [_doc(f"N{i}", 1000 * (1 + i % 10) * (10 if i % 2 else 1)) for i in range(40)]
    docs += [
        _doc("HUGE", 5e9),  # far above its group -> robust z
        _doc("BIG", 200e6, acq="IT Goods"),  # small group (1): no z, but 20% of a $1B budget
        _doc("SEEN", 9e9, flag="payment agent pass-through"),  # already reviewed: skipped
    ]
    actuals = {"3540": {2024: {"total_k": "1000000"}}}  # $1B
    flags = {f["purchase_document"]: f for f in outliers.flag_outliers(docs, actuals)}
    assert set(flags) == {"HUGE", "BIG"}
    assert flags["HUGE"]["z"] > outliers.ROBUST_Z_LIMIT and "robust z" in flags["HUGE"]["rule"]
    assert flags["BIG"]["z"] is None and flags["BIG"]["rule"] == "20% of annual budget"
    assert "500% of annual budget" in flags["HUGE"]["rule"]  # both rules can fire together


def test_budget_share_uses_the_start_year_else_the_latest_year():
    actuals = {"5180": {2023: {"total_k": "100"}, 2025: {"total_k": "1000"}}}
    a = outliers.flag_outliers([_doc("A", 20_000, bu="5180", fy=2023)], actuals)
    b = outliers.flag_outliers([_doc("B", 20_000, bu="5180", fy=2030)], actuals)
    assert a and a[0]["budget_share"] == pytest.approx(0.2)  # vs its own year ($100K)
    assert not b  # vs the latest year ($1M): 2%, not flagged


@pytest.mark.parametrize(
    "live, paid, on_fiscal, expected",
    [
        (None, 1.0, True, "needs review: not checked on SCPRS"),
        (math.nan, 1.0, True, "needs review: not found on SCPRS"),
        (150.0, 1.0, True, "needs review: value differs from SCPRS"),
        (100.2, 1.0, False, "needs review: no payment source"),  # within 0.5%
        (100.0, 0.0, True, "needs review: no payments found"),
        (100.0, 50.0, True, "confirmed"),
    ],
)
def test_propose_verdict(live, paid, on_fiscal, expected):
    verdict, evidence = outliers.propose_verdict(
        {"grand_total": 100.0, "live_value": live, "paid_to_vendor": paid}, on_fiscal
    )
    assert verdict == expected and evidence


def test_live_value_takes_the_current_version_and_reports_misses():
    calls = []

    def fetch(bu, mdy):
        calls.append((bu, mdy))
        if mdy == "09/01/2023":
            raise RuntimeError("site down")
        return [
            {"purchase_document": "A", "version": "1", "grand_total": 10.0},
            {"purchase_document": "A", "version": "3", "grand_total": 30.0},
        ]

    flags = [_doc("A", 30.0), _doc("Z", 1.0), _doc("Q", 1.0, start="2023-09-01")]
    outliers.live_value(flags, fetch=fetch, log=lambda *a: None)
    assert flags[0]["live_value"] == 30.0  # highest version
    assert math.isnan(flags[1]["live_value"])  # not in the day's export
    assert flags[2]["live_value"] is None  # fetch failed: unknown, never a pass
    assert calls == [("3540", "08/01/2023"), ("3540", "09/01/2023")]  # one search per day


def test_paid_to_vendor_matches_normalized_names_and_marks_missing_data():
    pay = pd.DataFrame(
        {"vendor": [outliers.normalize_name("VENDOR A, INC."), "OTHER"], "amount": [70.0, 5.0]}
    )
    flags = [_doc("A", 1.0), _doc("A", 1.0, bu="2660")]
    flags[0]["supplier_name"] = "Vendor A Inc"
    outliers.paid_to_vendor(flags, {"3540": pay, "2660": None})
    assert flags[0]["paid_to_vendor"] == 70.0
    assert flags[1]["paid_to_vendor"] is None  # department has no FI$Cal payments


def test_record_reviews_and_r4_status(tmp_path):
    path = tmp_path / "reviews.csv"
    rows = [
        {
            "business_unit": "3540",
            "purchase_document": "A",
            "verdict": "confirmed",
            "evidence": "e",
        },
        {
            "business_unit": "3540",
            "purchase_document": "B",
            "verdict": "needs review: x",
            "evidence": "e",
        },
    ]
    assert outliers.record_reviews(rows, path, today="2026-10-31") == 1
    assert outliers.record_reviews(rows, path, today="2026-10-31") == 0  # idempotent
    reviews = outliers.load_reviews(path)
    status = outliers.r4_status([_doc("A", 1.0), _doc("B", 1.0)], reviews)["3540"]
    assert (status["flagged"], status["closed"], status["open"]) == (2, 1, ["B"])
    assert status["R4_pass"] is False
    # A human verdict closes the last one.
    with open(path, "a", encoding="utf-8") as f:
        f.write("3540,B,corrected,value fixed in SCPRS,2026-11-01\n")
    status = outliers.r4_status([_doc("A", 1.0), _doc("B", 1.0)], outliers.load_reviews(path))
    assert status["3540"]["R4_pass"] is True
