"""Reconcile SCPRS contracts against Open FI$Cal vendor payments (external check).

Every other check in this repo compares the pipeline with itself; this one compares it with
the state's own payment ledger. Inputs:

- Open FI$Cal's per-department vendor transaction files (open.fiscal.ca.gov ->
  "Download expenditures"; one CSV per department and fiscal year, named
  ``Vendor_<BU>_<Dept>_FY<yy>.csv`` where ``FY<yy>`` is the fiscal year's START year).
  Download sequentially, only for the departments being checked, re-fetching a file only
  when its UploadDate changes (the terms agreed for this source, 2026-10-29).
- The operational store (``scprs.db``), current version per document.

Tests (thresholds fixed 2026-10-31, before any result; see the "proving the numbers" doc):

- R1 payee coverage: share of in-scope payments made to a vendor that holds an SCPRS
  contract. In scope = state-operations operating expenses, minus card programs, government
  payees, redacted payees and real-property rent (DGS Real Estate Services leases; SCM code
  039, not a department purchase). Reported two ways: the vendor's contract is with THIS
  department, or with ANY department (statewide CALNET, leases held by a co-tenant, a
  consultant engaged through another department).
- R2 paid within awarded: of the 50 largest in-scope payees, the share of dollars paid within
  110% of the value of their SCPRS contracts that started in the award window.
- R3 inputs: contract value per fiscal year spread by day over each contract's term.

Fiscal years: FI$Cal labels a year by its start (2023 = July 2023-June 2024); this module
reports by END year like the warehouse (2024 = the same year).

    python -m src.reconcile run --bu 3540 --fiscal-dir data/fiscal [--award-from 2021-07-01]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

from .supplier_master import normalize_name

FISCAL_COLUMNS = [
    "business_unit",
    "fiscal_year_begin",
    "VENDOR_NAME",
    "account",
    "account_type",
    "account_description",
    "budget_reference_category",
    "monetary_amount",
]
OPERATING = "Operating Expense & Equipment"
STATE_OPERATIONS = "State Operations"
# Real-property rent: Rents and Leases, Rent - Bldgs & Grounds (State), (Non State). Equipment
# rentals, rental cars and IT leases are purchases and stay in scope.
RENT_ACCOUNTS = frozenset({"5324350", "5324400", "5324450"})
# Payee-name rules, matched on the normalized name. Government payees match anywhere in the
# name with word boundaries: a start-anchored rule missed "CALIFORNIA CONSERVATION CORPS".
PAYEE_EXCLUSIONS = {
    "card programs": re.compile(
        r"^(US BANK|U S BANK|WEX|WRIGHT EXPRESS|CITIBANK|CAL ?CARD|VOYAGER)\b"
    ),
    "government payees": re.compile(
        r"\b(STATE OF CALIFORNIA|CALIFORNIA STATE|STATE CONTROLLER|DEPARTMENT OF|DEPT OF|"
        r"CA DEPT|CALIFORNIA DEPARTMENT|BOARD OF EQUALIZATION|FRANCHISE TAX|"
        r"EMPLOYMENT DEVELOPMENT|UNITED STATES|US DEPARTMENT|INTERNAL REVENUE|USDA|"
        r"FOREST SERVICE|OFFICE OF EMERGENCY SERVICES|CONSERVATION CORPS|DEPARTMENT OF TECHNOLOGY|"
        r"UNIVERSITY OF CALIFORNIA|CALIFORNIA STATE UNIVERSITY|SECRETARY OF STATE|"
        r"STATE TREASURER|HIGHWAY PATROL|MILITARY DEPARTMENT|PRISON INDUSTRY)\b"
        # State agencies abbreviated "DEPT <name>": CAL FIRE paid "DEPT FORESTRY FIRE PROTECT"
        # (itself) $5.7M, which only "DEPT OF" missed.
        r"|^DEPT\b"
    ),
    "redacted payees": re.compile(r"^CONFIDENTIAL\b"),
}
R2_TOP = 50
R2_TOLERANCE = 1.10

REFERENCES = Path(__file__).resolve().parent.parent / "references"
# Deferred FI$Cal agencies (State Auditor Report 2025-039): their payments run through their own
# systems and SCO's legacy system, which reach Open FI$Cal without vendor names, so R1/R2 cannot
# run. The label switches off by itself once a unit's vendor files appear.
FISCAL_DEFERRED_CSV = REFERENCES / "fiscal_deferred.csv"
# Budget Act actual expenditures (all funds) by unit and fiscal year (end-year label), each row
# with its source; the R3 envelope.
BUDGET_ACTUALS_CSV = REFERENCES / "budget_actuals.csv"
# One entity under two names (a renamed company, a county paid as "COUNTY OF X" on a contract
# registered to its fire department): each row maps `name` onto `same_as`, applied to BOTH the
# contract suppliers and the FI$Cal payees, each with its evidence. Mapping both sides keeps
# the other name's own contracts and payments where they were.
NAME_ALIASES_CSV = REFERENCES / "name_aliases.csv"
NOT_ON_FISCAL = "not on FI$Cal: vendor payments not published"
NO_FILES = "no FI$Cal vendor files found"

# Exception mechanisms: why a payee may be paid above its recorded award (or with no award)
# for reasons that are NOT a procurement anomaly. They LABEL exceptions; they never remove a
# payment from R1/R2 scope or change a test's result (an outside counsel under contract also
# bills to the legal accounts). Keyed on the payee's dominant FI$Cal account, checked in order.
LOCAL_GOVERNMENT = re.compile(
    r"\b(CITY|COUNTY|CNTY|TOWN OF|FIRE (DEPT|DEPARTMENT|DIST|DISTRICT|AUTHORITY|PROTECTION)|"
    r"FIRE PROT|SPECIAL DISTRICT)\b"
)
INTERDEPARTMENTAL_CONSULTING = "5340330"  # Consult & Prof Svcs-Interdept
LEGAL_ACCOUNTS = frozenset({"5340300", "5340510", "5340520", "5340530", "5340540"})
OTHER_SERVICES = "5390890"  # Other Items of Expense - Svcs
MECHANISMS = (
    "local agency reimbursement",  # e.g. wildfire mutual aid: paid by incident, not contract value
    "legal award or settlement",
    "postage",
    "telecom or utilities under statewide agreements",
    "statutory award (CPUC intervenor compensation)",
)
EXPLAINED = (*MECHANISMS, "contract in another department")


def mechanism(vendor: str, account: str, business_unit: str) -> str | None:
    """The likely non-procurement mechanism behind an exception, from the payee's dominant
    account, or None (the exception stays unexplained)."""
    account = str(account)
    if account == INTERDEPARTMENTAL_CONSULTING and LOCAL_GOVERNMENT.search(vendor):
        return MECHANISMS[0]
    if account in LEGAL_ACCOUNTS:
        return MECHANISMS[1]
    if account.startswith("5306"):  # Postage
        return MECHANISMS[2]
    if account.startswith(("5304", "5326")):  # Communications, Utilities
        return MECHANISMS[3]
    is_cpuc = business_unit == "8660"
    if is_cpuc and account == OTHER_SERVICES and not LOCAL_GOVERNMENT.search(vendor):
        # Public Utilities Code 1801-1812: awards to consumer advocates (e.g. The Utility
        # Reform Network), set by commission decision, not a contract.
        return MECHANISMS[4]
    return None


def classify_payees(
    totals,
    dominant_account: dict[str, str],
    awarded: dict[str, float],
    mine: set[str],
    anywhere: set[str],
    business_unit: str,
) -> dict[str, str]:
    """Every in-scope payee -> 'within award', a MECHANISMS label, 'contract in another
    department', or one of the two unexplained buckets."""
    out = {}
    for vendor, paid in totals.items():
        if paid <= 0:
            continue
        if awarded.get(vendor, 0) > 0 and paid <= R2_TOLERANCE * awarded[vendor]:
            out[vendor] = "within award"
            continue
        label = mechanism(vendor, dominant_account.get(vendor, ""), business_unit)
        if label:
            out[vendor] = label
        elif vendor in mine:
            out[vendor] = "unexplained: paid above recorded award"
        elif vendor in anywhere:
            out[vendor] = "contract in another department"
        else:
            out[vendor] = "unexplained: no contract anywhere"
    return out


def exception_summary(totals, labels: dict[str, str]) -> dict:
    """Dollars and payee count per label, plus the unexplained share of in-scope dollars."""
    by: dict[str, dict] = defaultdict(lambda: {"value": 0.0, "payees": 0})
    for vendor, label in labels.items():
        by[label]["value"] += float(totals[vendor])
        by[label]["payees"] += 1
    total = sum(b["value"] for b in by.values())
    unexplained = sum(b["value"] for k, b in by.items() if k.startswith("unexplained"))
    return {"by_label": dict(by), "unexplained_share": unexplained / total if total else None}


def end_year(start_year: int) -> int:
    """FI$Cal's start-year label -> the warehouse's end-year label (2023 -> 2024)."""
    return start_year + 1


def fy_of(d: date) -> int:
    """July-June fiscal year of a date, labeled by its end year (2023-08-01 -> 2024)."""
    return d.year + 1 if d.month >= 7 else d.year


def exclusion(vendor: str, account: str) -> str | None:
    """Why a payment is out of R1/R2 scope, or None when it is in scope."""
    if str(account) in RENT_ACCOUNTS:
        return "real-property rent"
    for name, rx in PAYEE_EXCLUSIONS.items():
        if rx.search(vendor):
            return name
    return None


def load_name_aliases(path: Path = NAME_ALIASES_CSV) -> dict[str, str]:
    """normalized name -> normalized name it is the same entity as."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        return {normalize_name(r["name"]): normalize_name(r["same_as"]) for r in csv.DictReader(f)}


def match_name(name: str, aliases: dict[str, str]) -> str:
    """The key a contract supplier or payee is matched on: its normalized name, aliased."""
    n = normalize_name(name or "")
    return aliases.get(n, n)


def load_payments(
    paths: list[Path], start_years: set[int] | None = None, aliases: dict[str, str] | None = None
):
    """Concatenate FI$Cal vendor files into one frame with `amount`, `fy` (end-year label)
    and `vendor` (normalized, aliased name). `start_years` keeps only those FI$Cal years."""
    import pandas as pd

    frames = [pd.read_csv(p, usecols=FISCAL_COLUMNS, dtype=str) for p in paths]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=FISCAL_COLUMNS)
    df["amount"] = pd.to_numeric(df["monetary_amount"], errors="coerce").fillna(0.0)
    df["fy"] = df["fiscal_year_begin"].astype(int).map(end_year)
    if start_years is not None:
        df = df[df["fiscal_year_begin"].astype(int).isin(start_years)]
    aliases = load_name_aliases() if aliases is None else aliases
    df["vendor"] = df["VENDOR_NAME"].fillna("").map(lambda n: match_name(n, aliases))
    return df


def contracts(
    con: sqlite3.Connection,
    business_unit: str | None = None,
    aliases: dict[str, str] | None = None,
) -> list[dict]:
    """Current version of each document (max version), optionally for one unit; `vendor` is
    the supplier's normalized, aliased name (the key payees are matched on)."""
    aliases = load_name_aliases() if aliases is None else aliases
    sql = (
        "SELECT business_unit, purchase_document, CAST(version AS INTEGER), start_date, "
        "end_date, grand_total, supplier_name FROM purchases"
    )
    rows = con.execute(
        sql + (" WHERE business_unit = ?" if business_unit else ""),
        (business_unit,) if business_unit else (),
    ).fetchall()
    best: dict[tuple, tuple] = {}
    for bu, doc, ver, sd, ed, tot, sup in rows:
        if (bu, doc) not in best or (ver or 0) >= best[(bu, doc)][0]:
            best[(bu, doc)] = (ver or 0, sd, ed, tot or 0.0, sup or "")
    return [
        {
            "business_unit": bu,
            "document": doc,
            "start": sd,
            "end": ed,
            "value": tot,
            "vendor": match_name(sup, aliases),
        }
        for (bu, doc), (_, sd, ed, tot, sup) in best.items()
    ]


def payee_totals(frame):
    """Net paid per payee, floored at 0: a payee whose refunds exceed its payments must not
    shrink a denominator (it pushed PUC coverage past 100% before the floor)."""
    return frame.groupby("vendor")["amount"].sum().clip(lower=0)


def coverage(totals, covered: set[str]) -> float | None:
    total = float(totals.sum())
    return float(totals[totals.index.isin(covered)].sum()) / total if total else None


def paid_within_awarded(
    totals, awarded: dict[str, float], top: int = R2_TOP, tolerance: float = R2_TOLERANCE
) -> tuple[float | None, list[dict]]:
    """R2: share of the top payees' dollars paid within `tolerance` x their awarded value."""
    head = totals.sort_values(ascending=False).head(top)
    rows = [
        {"vendor": v, "paid": float(p), "awarded": awarded.get(v, 0.0)} for v, p in head.items()
    ]
    total = sum(r["paid"] for r in rows)
    ok = sum(r["paid"] for r in rows if r["awarded"] > 0 and r["paid"] <= tolerance * r["awarded"])
    return (ok / total if total else None), rows


def annualized(rows: list[dict]) -> dict[int, float]:
    """Contract value spread evenly by day over [start, end], per end-year-labeled fiscal year.
    A missing (or earlier-than-start) end date puts the whole value on the start day."""
    out: dict[int, float] = defaultdict(float)
    for r in rows:
        if not r["start"] or (r["value"] or 0) <= 0:
            continue
        s = date.fromisoformat(r["start"][:10])
        e = date.fromisoformat(r["end"][:10]) if r.get("end") else s
        e = max(e, s)
        days = (e - s).days + 1
        y = fy_of(s)
        while True:
            fs, fe = date(y - 1, 7, 1), date(y, 6, 30)
            overlap = (min(e, fe) - max(s, fs)).days + 1
            if overlap > 0:
                out[y] += r["value"] * overlap / days
            if fe >= e:
                break
            y += 1
    return dict(out)


def reconcile(
    business_unit: str,
    payments,
    all_contracts: list[dict],
    award_from: str | None = None,
    award_to: str = "2025-06-30",
) -> dict:
    """R1 (this department / any department), R2 and the payment bridge for one unit.

    The R2 award window starts, by default, at the unit's EARLIEST stored contract: a payment
    in the period can draw on any award still running, however old. A fixed July-2021 start
    read long contracts as unawarded (2026-10-31: PUC's R2 was 60.0% that way and 95.4% with
    its 2016+ history; CAL FIRE's $485M January-2021 aviation contract looked missing)."""
    ops = payments[
        (payments["account_type"] == OPERATING)
        & (payments["budget_reference_category"] == STATE_OPERATIONS)
    ].copy()
    ops["excluded"] = [exclusion(v, a) for v, a in zip(ops["vendor"], ops["account"], strict=True)]
    bridge = {
        "all payments": float(payments["amount"].sum()),
        "state-operations operating payments": float(ops["amount"].sum()),
    }
    for reason in ["real-property rent", *PAYEE_EXCLUSIONS]:
        bridge[f"less {reason}"] = -float(ops.loc[ops["excluded"] == reason, "amount"].sum())
    scope = ops[ops["excluded"].isna()]
    totals = payee_totals(scope)
    mine = {c["vendor"] for c in all_contracts if c["business_unit"] == business_unit}
    anywhere = {c["vendor"] for c in all_contracts}
    if award_from is None:
        starts = [
            c["start"][:10]
            for c in all_contracts
            if c["business_unit"] == business_unit and c["start"]
        ]
        award_from = min(starts, default="0000-00-00")
    awarded: dict[str, float] = defaultdict(float)
    for c in all_contracts:
        if (
            c["business_unit"] == business_unit
            and c["start"]
            and award_from <= c["start"][:10] <= award_to
        ):
            awarded[c["vendor"]] += max(c["value"] or 0, 0)
    r2, r2_rows = paid_within_awarded(totals, awarded)
    dominant = (
        scope.groupby(["vendor", "account"])["amount"]
        .sum()
        .reset_index()
        .sort_values("amount", ascending=False)
        .drop_duplicates("vendor")
        .set_index("vendor")["account"]
        .to_dict()
    )
    labels = classify_payees(totals, dominant, awarded, mine, anywhere, business_unit)
    bridge["in-scope payments"] = float(totals.sum())
    bridge["paid to vendors with a contract in this department"] = float(
        totals[totals.index.isin(mine)].sum()
    )
    return {
        "business_unit": business_unit,
        "bridge": bridge,
        "R1_department": coverage(totals, mine),
        "R1_any_department": coverage(totals, anywhere),
        "R2": r2,
        "R2_rows": r2_rows,
        "exceptions": exception_summary(totals, labels),
        "top_unexplained": sorted(
            (
                [v, float(totals[v]), lab]
                for v, lab in labels.items()
                if lab.startswith("unexplained")
            ),
            key=lambda x: -x[1],
        )[:15],
        "award_window": [award_from, award_to],
        "annualized_by_fy": annualized(
            [c for c in all_contracts if c["business_unit"] == business_unit]
        ),
        "uncovered_anywhere": [
            [v, float(p)]
            for v, p in totals[~totals.index.isin(anywhere)]
            .sort_values(ascending=False)
            .head(15)
            .items()
        ],
    }


def load_fiscal_deferred(path: Path = FISCAL_DEFERRED_CSV) -> dict[str, dict]:
    """business_unit -> {department_name, status, expected_completion, source}."""
    with open(path, encoding="utf-8", newline="") as f:
        return {r["business_unit"]: r for r in csv.DictReader(f)}


def load_budget_actuals(path: Path = BUDGET_ACTUALS_CSV) -> dict[str, dict[int, dict]]:
    """business_unit -> {fiscal year (end-year label) -> row}; amounts in thousands."""
    out: dict[str, dict[int, dict]] = defaultdict(dict)
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            out[r["business_unit"]][int(r["fiscal_year"])] = r
    return dict(out)


def budget_envelope(annualized_by_fy: dict[int, float], actuals: dict[int, dict]) -> list[dict]:
    """R3: annualized contract value vs Budget Act actual total expenditures, per fiscal year
    with an actual. A year passes when contract value fits inside the whole budget."""
    rows = []
    for fy, row in sorted(actuals.items()):
        if not row.get("total_k"):
            continue
        budget = float(row["total_k"]) * 1000
        contracts_value = annualized_by_fy.get(fy, 0.0)
        rows.append(
            {
                "fiscal_year": fy,
                "annualized_contract_value": contracts_value,
                "budget_actual_total": budget,
                "ratio": contracts_value / budget if budget else None,
                "within_budget": contracts_value <= budget,
                "source": row.get("source_url"),
            }
        )
    return rows


def reconcile_unit(
    business_unit: str,
    fiscal_paths: list[Path],
    all_contracts: list[dict],
    deferred: dict[str, dict],
    actuals: dict[str, dict[int, dict]],
    award_from: str | None = None,
    award_to: str = "2025-06-30",
) -> dict:
    """Everything this module can say about one unit: the payment tests when its FI$Cal vendor
    files exist, otherwise a status that says why not; R3 whenever budget actuals exist."""
    mine = [c for c in all_contracts if c["business_unit"] == business_unit]
    envelope = budget_envelope(annualized(mine), actuals.get(business_unit, {}))
    if fiscal_paths:
        out = reconcile(
            business_unit, load_payments(fiscal_paths), all_contracts, award_from, award_to
        )
        out["status"] = "reconciled"
    elif business_unit in deferred:
        d = deferred[business_unit]
        out = {
            "business_unit": business_unit,
            "status": NOT_ON_FISCAL,
            "fiscal_onboarding": {k: d[k] for k in ("status", "expected_completion", "source")},
        }
    else:
        out = {"business_unit": business_unit, "status": NO_FILES}
    out["R3"] = envelope
    out["R3_pass"] = all(r["within_budget"] for r in envelope) if envelope else None
    return out


def _cli() -> None:
    ap = argparse.ArgumentParser(
        description="Reconcile SCPRS contracts against Open FI$Cal payments."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="R1/R2/R3 inputs for one or more business units")
    run.add_argument("--bu", action="append", required=True)
    run.add_argument(
        "--fiscal-dir",
        type=Path,
        required=True,
        help="folder holding Vendor_<BU>_*_FY<yy>.csv files",
    )
    run.add_argument("--db", type=Path, default=None, help="scprs.db (default: data/scprs.db)")
    run.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=[2022, 2023, 2024],
        help="FI$Cal start years to read (2022 = FY2022-23)",
    )
    run.add_argument(
        "--award-from", default=None, help="default: the unit's earliest stored contract"
    )
    run.add_argument("--award-to", default="2025-06-30")
    run.add_argument("--out", type=Path, default=None, help="write full results as JSON")
    args = ap.parse_args()
    from .model import DB_PATH

    con = sqlite3.connect(f"file:{args.db or DB_PATH}?mode=ro", uri=True)
    try:
        everything = contracts(con)
    finally:
        con.close()
    deferred, actuals = load_fiscal_deferred(), load_budget_actuals()
    out = {}
    for bu in args.bu:
        paths = sorted(
            p
            for p in args.fiscal_dir.glob(f"Vendor_{bu}_*_FY*.csv")
            if int(p.stem.rsplit("FY", 1)[1]) + 2000 in args.years
        )
        out[bu] = r = reconcile_unit(
            bu, paths, everything, deferred, actuals, args.award_from, args.award_to
        )
        r3 = {True: "pass", False: "FAIL", None: "no budget actuals"}[r["R3_pass"]]
        if r["status"] != "reconciled":
            print(f"{bu}: {r['status']}  |  R3 {r3}")
            continue
        print(
            f"{bu}: R1 dept {r['R1_department']:.1%}  any dept {r['R1_any_department']:.1%}  "
            f"R2 {r['R2']:.1%}  unexplained {r['exceptions']['unexplained_share']:.1%} of $  "
            f"R3 {r3}  (award window {r['award_window'][0]}..{r['award_window'][1]})"
        )
    if args.out:
        args.out.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    _cli()
