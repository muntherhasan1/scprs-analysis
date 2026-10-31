"""External reconciliation against Open FI$Cal (src/reconcile.py): scope rules, both coverage
definitions, the award window, and the fiscal-year conventions. Fixtures are synthetic FI$Cal
vendor files in the real column layout plus a minimal SCPRS store."""

import csv
import sqlite3
from datetime import date

import pytest

from src import model, reconcile

OPS, LA, SO = "Operating Expense & Equipment", "Local Assistance", "State Operations"


def _fiscal_csv(path, rows):
    """rows: (vendor, account, account_type, budget_category, amount); FI$Cal start year 2023."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(reconcile.FISCAL_COLUMNS)
        for vendor, account, atype, bcat, amount in rows:
            w.writerow(["5180", "2023", vendor, account, atype, "desc", bcat, amount])
    return path


def _store(path, rows):
    """rows: (business_unit, document, version, start, end, value, supplier)."""
    con = model._connect(path)
    model._ensure_schema(con)
    con.executemany(
        "INSERT INTO purchases (business_unit, purchase_document, version, start_date, end_date, "
        "grand_total, supplier_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    return con


@pytest.mark.parametrize(
    "vendor, account, expected",
    [
        ("ACME LANDLORD", "5324450", "real-property rent"),  # Rent - Bldgs & Grounds (Non State)
        ("ACME LANDLORD", "5324400", "real-property rent"),  # (State)
        ("ACME LANDLORD", "5324350", "real-property rent"),  # Rents and Leases
        ("HERTZ", "5320470", None),  # rental car: a purchase, stays in scope
        ("XEROX", "5302400", None),  # copier rental: a purchase
        ("CALIFORNIA CONSERVATION CORPS", "5340580", "government payees"),  # mid-name match
        ("DEPT OF GENERAL SERVICES", "5340580", "government payees"),
        ("HOLT OF CALIFORNIA", "5340580", None),
        ("STATE FARM INSURANCE", "5340580", None),
        ("US BANK", "5301800", "card programs"),
        ("WEXFORD CONSULTING", "5340580", None),  # "WEX" only as a whole word
        ("CONFIDENTIAL", "5340580", "redacted payees"),
    ],
)
def test_exclusion_rules(vendor, account, expected):
    assert reconcile.exclusion(vendor, account) == expected


def test_reconcile_counts_rent_out_and_reports_both_coverage_definitions(tmp_path):
    """Rent is out of scope by account code; a vendor whose contract sits with ANOTHER
    department counts only under the any-department definition; a payee netting refunds is
    floored at $0 so coverage can never exceed 100%."""
    payments = reconcile.load_payments(
        [
            _fiscal_csv(
                tmp_path / "Vendor_5180_DSS_FY23.csv",
                [
                    ("ACME CONSULTING", "5340580", OPS, SO, "100"),  # contract with 5180
                    ("HORNE", "5340580", OPS, SO, "50"),  # contract only with 1111
                    ("LANDLORD PARTNERS", "5324450", OPS, SO, "40"),  # office rent
                    ("US BANK", "5301800", OPS, SO, "30"),  # card program
                    ("REFUND CO", "5340580", OPS, SO, "-20"),  # net refund -> 0
                    ("UNKNOWN VENDOR", "5340580", OPS, SO, "10"),  # no contract anywhere
                    (
                        "CHILD CARE AGENCY",
                        "5340580",
                        OPS,
                        LA,
                        "1000",
                    ),  # local assistance: out of scope
                ],
            )
        ]
    )
    con = _store(
        tmp_path / "s.db",
        [
            ("5180", "A1", "1", "2023-08-01", "2024-06-30", 95.0, "ACME CONSULTING"),
            ("1111", "H1", "1", "2023-08-01", "2024-06-30", 500.0, "HORNE"),
        ],
    )
    r = reconcile.reconcile("5180", payments, reconcile.contracts(con))
    assert r["bridge"]["less real-property rent"] == -40
    assert r["bridge"]["less card programs"] == -30
    assert r["bridge"]["in-scope payments"] == 160  # 100 + 50 + 0 (floored) + 10
    assert r["R1_department"] == pytest.approx(100 / 160)
    assert r["R1_any_department"] == pytest.approx(150 / 160)
    # R2: only ACME has an award here (95; 100 paid is within 110%); HORNE's award is 1111's.
    assert r["R2"] == pytest.approx(100 / 160)
    names = [reconcile.normalize_name(n) for n in ("UNKNOWN VENDOR", "REFUND CO")]
    assert [v for v, _ in r["uncovered_anywhere"]] == names


def test_award_window_picks_up_contracts_older_than_the_store_policy(tmp_path):
    """The backfill hypothesis: payments against a contract that started before July 2021 read
    as unawarded until the award window (and the store) reach back to its start."""
    payments = reconcile.load_payments(
        [
            _fiscal_csv(
                tmp_path / "Vendor_5180_DSS_FY23.csv",
                [
                    ("AVIATION CO", "5340580", OPS, SO, "200"),
                ],
            )
        ]
    )
    con = _store(
        tmp_path / "s.db",
        [
            ("5180", "OLD", "1", "2019-03-01", "2026-12-31", 900.0, "AVIATION CO"),
        ],
    )
    rows = reconcile.contracts(con)
    assert reconcile.reconcile("5180", payments, rows)["R2"] == 0.0
    widened = reconcile.reconcile("5180", payments, rows, award_from="2016-07-01")
    assert widened["R2"] == 1.0


def test_contracts_resolve_the_current_version(tmp_path):
    con = _store(
        tmp_path / "s.db",
        [
            ("5180", "D", "1", "2023-08-01", None, 50_000_000.0, "ACME"),
            ("5180", "D", "2", "2023-08-01", None, 10.0, "ACME"),  # current version
        ],
    )
    (only,) = reconcile.contracts(con)
    assert only["value"] == 10.0


def test_annualized_spreads_by_day_across_fiscal_years():
    rows = [
        {"start": "2023-01-01", "end": "2024-12-31", "value": 731.0},  # 731 days
        {"start": "2024-08-15", "end": None, "value": 5.0},  # no end: all on the start day
        {"start": "2024-08-15", "end": "2024-01-01", "value": 7.0},  # end before start: start day
        {"start": "2024-08-15", "end": None, "value": -3.0},  # credits ignored
    ]
    out = reconcile.annualized(rows)
    # FY2023 = Jan-Jun 2023 (181 days), FY2024 = Jul 2023-Jun 2024 (366, leap), FY2025 = rest.
    assert out[2023] == pytest.approx(181)
    assert out[2024] == pytest.approx(366)
    assert out[2025] == pytest.approx(184 + 5 + 7)
    assert sum(out.values()) == pytest.approx(731 + 12)


def test_fiscal_year_labels_convert_start_year_to_end_year(tmp_path):
    assert reconcile.end_year(2023) == 2024
    assert reconcile.fy_of(date(2023, 8, 1)) == 2024
    assert reconcile.fy_of(date(2024, 6, 30)) == 2024
    assert reconcile.fy_of(date(2024, 7, 1)) == 2025
    df = reconcile.load_payments([_fiscal_csv(tmp_path / "f.csv", [("X", "1", OPS, SO, "1")])])
    assert set(df["fy"]) == {2024}
    assert reconcile.load_payments(
        [_fiscal_csv(tmp_path / "g.csv", [("X", "1", OPS, SO, "1")])], start_years={2022}
    ).empty


def test_store_query_is_read_only_safe(tmp_path):
    """contracts() needs nothing beyond `purchases`; a store opened read-only works."""
    path = tmp_path / "s.db"
    _store(path, [("5180", "A", "1", "2023-08-01", None, 1.0, "ACME")]).close()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert len(reconcile.contracts(con, "5180")) == 1
