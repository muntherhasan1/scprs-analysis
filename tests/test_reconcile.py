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
    assert reconcile.reconcile("5180", payments, rows, award_from="2021-07-01")["R2"] == 0.0
    widened = reconcile.reconcile("5180", payments, rows, award_from="2016-07-01")
    assert widened["R2"] == 1.0


def test_award_window_defaults_to_the_units_earliest_stored_contract(tmp_path):
    """No explicit window: R2 counts every award the unit's stored history holds (a 2019
    contract still running pays out in FY2023-24), and nothing from another unit."""
    payments = reconcile.load_payments(
        [
            _fiscal_csv(
                tmp_path / "Vendor_5180_DSS_FY23.csv",
                [
                    ("AVIATION CO", "5340580", OPS, SO, "200"),
                    ("OTHER CO", "5340580", OPS, SO, "100"),
                ],
            )
        ]
    )
    con = _store(
        tmp_path / "s.db",
        [
            ("5180", "OLD", "1", "2019-03-01", "2026-12-31", 900.0, "AVIATION CO"),
            ("5180", "NEW", "1", "2023-08-01", "2024-06-30", 50.0, "OTHER CO"),  # paid 2x award
            ("1111", "ELSE", "1", "2015-01-01", None, 1e9, "OTHER CO"),  # another unit: ignored
        ],
    )
    r = reconcile.reconcile("5180", payments, reconcile.contracts(con))
    assert r["award_window"][0] == "2019-03-01"
    assert r["R2"] == pytest.approx(200 / 300)  # AVIATION within its award; OTHER CO over


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


def test_contracts_count_a_re_registered_agreement_once(tmp_path, monkeypatch):
    """A legacy record superseded by its prefixed re-registration is left out of the awards,
    as in the warehouse facts (src/registrations.py)."""
    from src import registrations

    monkeypatch.setattr(registrations, "REVIEWED_CSV", tmp_path / "none.csv")
    con = _store(
        tmp_path / "s.db",
        [
            ("3540", "7CA03777", "0", "2017-07-01", "2020-06-30", 58.0, "KERN COUNTY FIRE DEPT"),
            (
                "3540",
                "35407CA03777",
                "3",
                "2017-07-01",
                "2020-12-31",
                62.0,
                "KERN COUNTY FIRE DEPT",
            ),
        ],
    )
    (only,) = reconcile.contracts(con, aliases={})
    assert (only["document"], only["value"]) == ("35407CA03777", 62.0)


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


@pytest.mark.parametrize(
    "vendor, account, bu, expected",
    [
        ("CITY OF SAN JOSE FIRE DEPT", "5340330", "3540", "local agency reimbursement"),
        ("ALAMEDA COUNTY FIRE DEPARTMENT", "5340330", "3540", "local agency reimbursement"),
        ("SACRAMENTO METRO FIRE DIST", "5340330", "3540", "local agency reimbursement"),
        ("CITY OF LONG BEACH", "5340580", "3540", None),  # local government, not interdept acct
        ("ACME CONSULTING", "5340330", "3540", None),  # interdept account, not local government
        ("HEINRICH LAW IOLTA TRUST", "5340300", "3540", "legal award or settlement"),
        ("SIERRA PACIFIC INDUSTRIES", "5340510", "3540", "legal award or settlement"),
        ("US POSTAL SERVICE", "5306100", "8660", "postage"),
        ("VERIZON WIRELESS", "5304700", "5180", "telecom or utilities under statewide agreements"),
        (
            "PACIFIC GAS ELECTRIC",
            "5326100",
            "8660",
            "telecom or utilities under statewide agreements",
        ),
        (
            "UTILITY REFORM NETWORK",
            "5390890",
            "8660",
            "statutory award (CPUC intervenor compensation)",
        ),
        ("UTILITY REFORM NETWORK", "5390890", "3540", None),  # intervenor rule is CPUC-only
        ("COUNTY OF MARIN", "5390890", "8660", None),  # a local agency is not an intervenor
        ("AMENTUM SERVICES", "5302000", "3540", None),
    ],
)
def test_exception_mechanisms(vendor, account, bu, expected):
    assert reconcile.mechanism(vendor, account, bu) == expected


def test_dept_abbreviated_state_agency_is_a_government_payee():
    """CAL FIRE paid itself as "DEPT FORESTRY FIRE PROTECT"; "DEPTFORD" is not an agency."""
    assert reconcile.exclusion("DEPT FORESTRY FIRE PROTECT", "5390900") == "government payees"
    assert reconcile.exclusion("DEPTFORD HOLDINGS", "5340580") is None


def test_exceptions_are_labeled_without_changing_r1_or_r2(tmp_path):
    """Every in-scope payee lands in exactly one bucket; mechanism labels explain exceptions but
    never move dollars out of scope, so R1/R2 equal the unlabeled computation."""
    payments = reconcile.load_payments(
        [
            _fiscal_csv(
                tmp_path / "Vendor_3540_CF_FY23.csv",
                [
                    ("ACME CONSULTING", "5340580", OPS, SO, "100"),  # within award (95)
                    ("CITY OF ANAHEIM", "5340330", OPS, SO, "40"),  # over award, mutual aid
                    ("SIERRA PACIFIC INDUSTRIES", "5340510", OPS, SO, "30"),  # no award, legal
                    ("BIG VENDOR", "5340580", OPS, SO, "50"),  # over award, unexplained
                    ("HORNE", "5340580", OPS, SO, "20"),  # contract in another dept
                    ("MYSTERY LLC", "5340580", OPS, SO, "10"),  # no contract anywhere
                ],
            )
        ]
    )
    con = _store(
        tmp_path / "s.db",
        [
            ("3540", "A", "1", "2022-08-01", None, 95.0, "ACME CONSULTING"),
            ("3540", "C", "1", "2022-08-01", None, 10.0, "CITY OF ANAHEIM"),
            ("3540", "B", "1", "2022-08-01", None, 20.0, "BIG VENDOR"),
            ("1111", "H", "1", "2022-08-01", None, 99.0, "HORNE"),
        ],
    )
    r = reconcile.reconcile("3540", payments, reconcile.contracts(con))
    by = {k: v["value"] for k, v in r["exceptions"]["by_label"].items()}
    assert by == {
        "within award": 100,
        "local agency reimbursement": 40,
        "legal award or settlement": 30,
        "unexplained: paid above recorded award": 50,
        "contract in another department": 20,
        "unexplained: no contract anywhere": 10,
    }
    assert sum(by.values()) == r["bridge"]["in-scope payments"] == 250
    assert r["exceptions"]["unexplained_share"] == pytest.approx(60 / 250)
    assert [v for v, _, _ in r["top_unexplained"]] == [
        reconcile.normalize_name("BIG VENDOR"),
        reconcile.normalize_name("MYSTERY LLC"),
    ]
    # Labels don't touch the tests: R2 counts only ACME within award, as before.
    assert r["R2"] == pytest.approx(100 / 250)
    assert r["R1_department"] == pytest.approx(190 / 250)  # ACME, ANAHEIM, BIG


DEFERRED = {
    "2660": {
        "department_name": "Department of Transportation",
        "status": "onboarding in progress",
        "expected_completion": "2028-07",
        "source": "State Auditor Report 2025-039",
    }
}


def test_budget_envelope_passes_only_inside_the_budget():
    actuals = {
        2024: {"total_k": "1000", "source_url": "u"},  # $1.0M
        2025: {"total_k": "500", "source_url": "u"},  # $0.5M
        2026: {"total_k": "", "source_url": "u"},  # no figure: skipped, not failed
    }
    rows = reconcile.budget_envelope({2024: 800_000.0, 2025: 600_000.0}, actuals)
    assert [(r["fiscal_year"], r["within_budget"]) for r in rows] == [(2024, True), (2025, False)]
    assert rows[0]["ratio"] == pytest.approx(0.8)


def test_unit_not_on_fiscal_is_labeled_and_still_gets_r3(tmp_path):
    """A deferred FI$Cal agency has no vendor files: no R1/R2, a status saying why, and the
    budget-envelope check still runs from its Budget Act actuals."""
    con = _store(
        tmp_path / "s.db",
        [
            ("2660", "T1", "1", "2023-07-01", "2024-06-30", 900_000.0, "ROAD CO"),
        ],
    )
    actuals = {"2660": {2024: {"total_k": "1000", "source_url": "https://ebudget.ca.gov/x"}}}
    r = reconcile.reconcile_unit("2660", [], reconcile.contracts(con), DEFERRED, actuals)
    assert r["status"] == reconcile.NOT_ON_FISCAL
    assert r["fiscal_onboarding"]["expected_completion"] == "2028-07"
    assert "R1_department" not in r and "R2" not in r
    assert r["R3_pass"] is True and r["R3"][0]["annualized_contract_value"] == pytest.approx(
        900_000
    )


def test_unit_missing_files_but_not_deferred_is_flagged_not_excused(tmp_path):
    con = _store(tmp_path / "s.db", [("3540", "A", "1", "2023-08-01", None, 1.0, "ACME")])
    r = reconcile.reconcile_unit("3540", [], reconcile.contracts(con), DEFERRED, {})
    assert r["status"] == reconcile.NO_FILES
    assert r["R3"] == [] and r["R3_pass"] is None  # no actuals: unknown, never a pass


def test_reconciled_unit_carries_status_and_r3(tmp_path):
    path = _fiscal_csv(tmp_path / "Vendor_5180_DSS_FY23.csv", [("ACME", "5340580", OPS, SO, "10")])
    con = _store(tmp_path / "s.db", [("5180", "A", "1", "2023-08-01", None, 20.0, "ACME")])
    actuals = {"5180": {2024: {"total_k": "1", "source_url": "u"}}}
    r = reconcile.reconcile_unit("5180", [path], reconcile.contracts(con), DEFERRED, actuals)
    assert r["status"] == "reconciled" and r["R2"] == 1.0 and r["R3_pass"] is True


def test_reference_files_are_well_formed():
    deferred = reconcile.load_fiscal_deferred()
    assert {"2660", "0820", "5225", "3860", "2740"} <= set(deferred)
    assert all(d["source"] and d["expected_completion"] for d in deferred.values())
    for bu, years in reconcile.load_budget_actuals().items():
        for fy, row in years.items():
            assert 2016 <= fy <= 2030, (bu, fy)
            assert row["source_url"].startswith("https://ebudget.ca.gov/"), (bu, fy)
            if row["total_k"]:
                assert int(row["total_k"]) > 0 and int(row["state_operations_k"]) >= 0
