"""Outlier review (acceptance test R4): flag extraordinary contracts, gather evidence, record.

R4 (fixed 2026-10-31, before any result): any single contract over 10% of its department's
annual budget, or with a robust z-score over 5 within its department and acquisition type, must
be confirmed or corrected against the SCPRS page AND a third source before it sits in published
totals. Doing that by hand for every flag doesn't scale, so this module:

1. **Flags** contracts by both rules (`flag_outliers`), skipping ones already reviewed with a
   class override in ``references/contract_flags.csv``.
2. **Gathers evidence** for each flag: the live SCPRS summary value for its business unit and
   start day (`live_value`, one search per distinct day) and the department's Open FI$Cal
   payments to the contract's vendor (`paid_to_vendor`, the independent third source).
3. **Proposes a verdict** (`propose_verdict`): ``confirmed`` only when the live value matches
   the stored one AND the vendor was actually paid by that department; anything else goes to a
   human (`needs review: ...`) with the reason.
4. **Records** verdicts in ``references/outlier_reviews.csv``; R4 passes for a department when
   every contract it currently flags has a recorded verdict (`r4_status`).

    python -m src.outliers run --wh data/warehouse.db --fiscal-dir data/fiscal --bu 3540 \
        [--live] --out queue.csv [--record]
"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

from . import reconcile
from .supplier_master import normalize_name

BUDGET_SHARE_LIMIT = 0.10
ROBUST_Z_LIMIT = 5.0
MIN_GROUP = 30  # a department x acquisition-type group needs this many contracts for a z-score
VALUE_TOLERANCE = 0.005  # live vs stored value agree within 0.5%
REVIEWS_CSV = reconcile.REFERENCES / "outlier_reviews.csv"
REVIEW_FIELDS = ["business_unit", "purchase_document", "verdict", "evidence", "reviewed"]
CLOSED = ("confirmed", "corrected", "reclassified")


def documents(con: sqlite3.Connection, business_units: list[str]) -> list[dict]:
    """Current-version documents for the units, from the warehouse's gold_document."""
    marks = ",".join("?" * len(business_units))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT business_unit, purchase_document, supplier_name, canonical_name, "  # noqa: S608
        "acquisition_type, award_class, contract_flag, first_item_title, start_date, "
        f"fiscal_year, grand_total FROM gold_document WHERE business_unit IN ({marks})",
        business_units,
    ).fetchall()
    return [dict(r) for r in rows]


def _budget_for(fy: int | None, actuals: dict[int, dict]) -> float | None:
    """The unit's actual total expenditures for the contract's start year, else its latest."""
    years = {y: float(r["total_k"]) * 1000 for y, r in actuals.items() if r.get("total_k")}
    if not years:
        return None
    return years.get(fy) if fy in years else years[max(years)]


def flag_outliers(docs: list[dict], actuals: dict[str, dict[int, dict]]) -> list[dict]:
    """Contracts breaking either R4 rule. A robust z-score is computed on log10(value) within
    each (department, acquisition type) group of at least MIN_GROUP positive contracts, with
    the median absolute deviation scaled by 1.4826. Already-reviewed overrides are skipped."""
    groups: dict[tuple, list[float]] = defaultdict(list)
    for d in docs:
        if (d["grand_total"] or 0) > 0:
            groups[(d["business_unit"], d["acquisition_type"])].append(math.log10(d["grand_total"]))
    stats = {}
    for key, logs in groups.items():
        if len(logs) >= MIN_GROUP:
            med = statistics.median(logs)
            mad = statistics.median(abs(x - med) for x in logs)
            stats[key] = (med, mad * 1.4826 or 1e-9)
    out = []
    for d in docs:
        value = d["grand_total"] or 0
        if value <= 0 or d.get("contract_flag"):
            continue
        reasons, z, share = [], None, None
        st = stats.get((d["business_unit"], d["acquisition_type"]))
        if st:
            z = (math.log10(value) - st[0]) / st[1]
            if z > ROBUST_Z_LIMIT:
                reasons.append(f"robust z {z:.1f}")
        budget = _budget_for(d["fiscal_year"], actuals.get(d["business_unit"], {}))
        if budget:
            share = value / budget
            if share > BUDGET_SHARE_LIMIT:
                reasons.append(f"{share:.0%} of annual budget")
        if reasons:
            out.append({**d, "z": z, "budget_share": share, "rule": "; ".join(reasons)})
    return sorted(out, key=lambda r: -(r["grand_total"] or 0))


def paid_to_vendor(flags: list[dict], payments_by_bu: dict[str, object]) -> None:
    """Fill `paid_to_vendor`: the department's net FI$Cal payments (all accounts) to the flag's
    supplier, matched on the normalized supplier or canonical name. None = no payment data."""
    for f in flags:
        pay = payments_by_bu.get(f["business_unit"])
        if pay is None:
            f["paid_to_vendor"] = None
            continue
        names = {
            normalize_name(f["supplier_name"] or ""),
            normalize_name(f["canonical_name"] or ""),
        }
        names.discard("")
        f["paid_to_vendor"] = float(pay.loc[pay["vendor"].isin(names), "amount"].sum())


def live_value(flags: list[dict], fetch=None, log=print) -> None:
    """Fill `live_value` from the live SCPRS summary for each flag's unit and start day (one
    search per distinct day; the current version's grand total). `fetch(bu, mdy)` returns the
    day's rows; the default drives the scraper."""
    if fetch is None:
        from . import model, scprs

        def fetch(bu, mdy):
            res = scprs.download_extract(bu, mdy, mdy)
            if res.no_records:
                return []
            return model._prepare_summary(scprs.load_extract(res.path), bu).to_dict("records")

    days: dict[tuple, list] = {}
    for i, f in enumerate(flags):
        sd = f["start_date"]
        key = (f["business_unit"], sd)
        if key not in days:
            try:
                days[key] = fetch(f["business_unit"], f"{sd[5:7]}/{sd[8:10]}/{sd[:4]}")
            except Exception as e:  # noqa: BLE001 - recorded as a verdict reason, never a pass
                log(f"[{i + 1}/{len(flags)}] {key}: live check failed: {e!r}"[:200])
                days[key] = None
        rows = days[key]
        if rows is None:
            f["live_value"] = None
            continue
        mine = [r for r in rows if str(r["purchase_document"]) == f["purchase_document"]]
        best = max(mine, key=lambda r: int(r.get("version") or 0)) if mine else None
        f["live_value"] = float(best["grand_total"]) if best else float("nan")


def propose_verdict(flag: dict, on_fiscal: bool) -> tuple[str, str]:
    """('confirmed', evidence) only when the live SCPRS value matches AND the department paid
    the vendor; otherwise ('needs review: ...', what is missing)."""
    value, live = flag["grand_total"], flag.get("live_value")
    paid = flag.get("paid_to_vendor")
    if live is None:
        return "needs review: not checked on SCPRS", "live SCPRS value not fetched"
    if isinstance(live, float) and math.isnan(live):
        return "needs review: not found on SCPRS", "document absent from the live day's export"
    if abs(live - value) > VALUE_TOLERANCE * max(abs(value), 1):
        return "needs review: value differs from SCPRS", f"stored ${value:,.0f}, live ${live:,.0f}"
    if not on_fiscal:
        return "needs review: no payment source", "department not on FI$Cal; SCPRS value matches"
    if not paid or paid <= 0:
        return (
            "needs review: no payments found",
            f"SCPRS value matches; FI$Cal paid ${paid or 0:,.0f}",
        )
    return "confirmed", f"SCPRS value matches (${live:,.0f}); FI$Cal paid the vendor ${paid:,.0f}"


def load_reviews(path: Path = REVIEWS_CSV) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        return {(r["business_unit"], r["purchase_document"]): r for r in csv.DictReader(f)}


def record_reviews(rows: list[dict], path: Path = REVIEWS_CSV, today: str | None = None) -> int:
    """Append confirmed verdicts not yet recorded (human verdicts are added by hand)."""
    existing = load_reviews(path)
    new = [
        r
        for r in rows
        if r["verdict"] == "confirmed"
        and (r["business_unit"], r["purchase_document"]) not in existing
    ]
    if not new:
        return 0
    write_header = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS, lineterminator="\n")
        if write_header:
            w.writeheader()
        for r in new:
            w.writerow(
                {
                    "business_unit": r["business_unit"],
                    "purchase_document": r["purchase_document"],
                    "verdict": "confirmed",
                    "evidence": r["evidence"],
                    "reviewed": today or date.today().isoformat(),
                }
            )
    return len(new)


def r4_status(flags: list[dict], reviews: dict[tuple[str, str], dict]) -> dict[str, dict]:
    """Per department: flagged, closed (confirmed / corrected / reclassified) and open; R4
    passes when nothing is open."""
    out: dict[str, dict] = defaultdict(lambda: {"flagged": 0, "closed": 0, "open": []})
    for f in flags:
        s = out[f["business_unit"]]
        s["flagged"] += 1
        rev = reviews.get((f["business_unit"], f["purchase_document"]))
        if rev and rev["verdict"].startswith(CLOSED):
            s["closed"] += 1
        else:
            s["open"].append(f["purchase_document"])
    for s in out.values():
        s["R4_pass"] = not s["open"]
    return dict(out)


def _cli() -> None:
    ap = argparse.ArgumentParser(description="R4 outlier review: flag, gather evidence, record.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--wh", type=Path, required=True, help="warehouse.db (gold_document)")
    run.add_argument("--fiscal-dir", type=Path, required=True)
    run.add_argument("--bu", action="append", required=True)
    run.add_argument("--live", action="store_true", help="check each flag on live SCPRS")
    run.add_argument("--out", type=Path, required=True, help="review queue CSV")
    run.add_argument("--record", action="store_true", help="append confirmed verdicts")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.wh}?mode=ro", uri=True)
    try:
        docs = documents(con, args.bu)
    finally:
        con.close()
    flags = flag_outliers(docs, reconcile.load_budget_actuals())
    payments = {}
    for bu in args.bu:
        paths = sorted(args.fiscal_dir.glob(f"Vendor_{bu}_*_FY*.csv"))
        payments[bu] = reconcile.load_payments(paths) if paths else None
    paid_to_vendor(flags, payments)
    if args.live:
        live_value(flags)
    for f in flags:
        f["verdict"], f["evidence"] = propose_verdict(
            f, payments.get(f["business_unit"]) is not None
        )
    cols = [
        "business_unit",
        "purchase_document",
        "canonical_name",
        "acquisition_type",
        "award_class",
        "grand_total",
        "start_date",
        "rule",
        "live_value",
        "paid_to_vendor",
        "verdict",
        "evidence",
        "first_item_title",
    ]
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(flags)
    if args.record:
        print(f"recorded {record_reviews(flags)} confirmed verdict(s) in {REVIEWS_CSV.name}")
    for bu, s in sorted(r4_status(flags, load_reviews()).items()):
        print(
            f"{bu}: flagged {s['flagged']}, closed {s['closed']}, open {len(s['open'])} "
            f"-> R4 {'pass' if s['R4_pass'] else 'open'}"
        )


if __name__ == "__main__":
    _cli()
