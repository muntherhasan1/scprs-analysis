"""Static JSON export for the vendor web app (MVP data contract, schema v1).

The app is a static site: every number it shows is precomputed here, so the
front end never sums, dedupes or joins. Reads the slim serve DB (run
``warehouse serve-export`` first) and writes, under ``data/app/``:

  meta.json                      freshness + coverage the UI shows beside totals
  opportunities.json             open solicitations, soonest close first
  departments/index.json         one row per business unit (loaded or not)
  departments/{bu}.json          one page's data per business unit
  suppliers/index.json           one row per canonical supplier (search)
  suppliers/shard-{NN}.json      supplier pages keyed by canonical_id; NN = last 2 digits
  insights/overview.json         the dashboard home: headline, trends, mixes, closing soon
  insights/{vector}/{key}.json   a page per acquisition type, size bucket, product category
  search.json                    small index over departments (+ aliases) and the vectors
  cmas/agreements.json           every CMAS agreement + filter facets (the CMAS tab)

Grain rules (the contract's): documents are current-version (``gold_document``);
every supplier figure is per CANONICAL supplier computed here from
``gold_document`` (the share / HHI marts are canonical too since 2026-10, but
the export keeps its own computation so it doesn't depend on them). Unloaded
departments get ``loaded: false`` and null spend blocks, never zeros.

    python -m src.app_export [--out DIR]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import scprs
from .warehouse import CMAS_DB, EPROCURE_DB, SERVE_DB, WAREHOUSE_DB

APP_DIR = scprs.DATA_DIR / "app"
SCHEMA_VERSION = 1
TOP_N = 25
RECENT_CONTRACTS = 10
SHARDS = 100


# Not every "supplier" is a company a vendor competes with: interagency
# agreements name a state department/campus (DEPT518000, UNIV678000), and
# redacted or pooled payees get placeholder ids (0820CFD "DOJ CONFIDENTIAL",
# 4500SGP single-pay, UNKNOWN). The app labels these instead of hiding them,
# so totals still reconcile.
_GOVERNMENT_ID = re.compile(r"^(DEPT|UNIV)\d+$")
_PLACEHOLDER_ID = re.compile(r"^(\d+(CFD|SGP)|UNKNOWN)$")


def _supplier_kind(canonical_id: str) -> str:
    if _GOVERNMENT_ID.match(canonical_id):
        return "government"
    if _PLACEHOLDER_ID.match(canonical_id):
        return "placeholder"
    return "company"


def _shard(canonical_id: str) -> str:
    return f"{int(canonical_id[-2:]) % SHARDS:02d}" if canonical_id[-2:].isdigit() else "xx"


def _money(v) -> float | None:
    return None if v is None else round(float(v), 2)


def _rows(con: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur]


def _write(path: Path, payload: dict, generated_at: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": SCHEMA_VERSION, "generated_at": generated_at, **payload}
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _optional_scalar(db: Path, sql: str):
    """One value from an optional side DB; None if the DB/table is absent."""
    if not Path(db).exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(sql).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def _hhi(values: list[float]) -> tuple[int, float, float]:
    """(supplier_count, HHI 0-10000, top supplier % 0-100) over positive values."""
    vals = [v for v in values if v and v > 0]
    total = sum(vals)
    if not total:
        return 0, 0.0, 0.0
    shares = [v / total for v in vals]
    return len(vals), round(sum(s * s for s in shares) * 10000, 0), round(100 * max(shares), 1)


def _opportunities(con, today: str, loaded: set[str]) -> list[dict]:
    rows = _rows(
        con,
        "SELECT event_id, event_name, department_name, business_unit, bid_close_date, "
        "sb_only, dvbe_only, sb_or_dvbe FROM gold_eprocure_posted_opportunity "
        "WHERE bid_close_date >= ? ORDER BY bid_close_date, event_name",
        (today,),
    )
    return [
        {
            "event_id": r["event_id"],
            "event_name": r["event_name"],
            "department_name": r["department_name"],
            "business_unit": r["business_unit"],
            "department_loaded": r["business_unit"] in loaded,
            "bid_close_date": r["bid_close_date"],
            "sb_only": bool(r["sb_only"]),
            "dvbe_only": bool(r["dvbe_only"]),
            # Set aside for SB *or* DVBE bidders ("SB/DVBE ONLY") — neither flag above.
            "sb_or_dvbe": bool(r["sb_or_dvbe"]),
            "eprocure_url": None,  # no stable per-event link known yet (contract open question)
        }
        for r in rows
    ]


def _fy_partial(fiscal_year: int, coverage_from: str, coverage_to: str, today: str) -> bool:
    """True when the data window doesn't span the whole Jul-Jun fiscal year.

    Coverage ends at today at the latest: future-dated start dates (contracts
    entered ahead of their term) must not make a year still in progress -- or
    one that hasn't started -- look complete."""
    return coverage_from > f"{fiscal_year - 1}-07-01" or min(coverage_to, today) < (
        f"{fiscal_year}-06-30"
    )


def _supplier_flags(con, today: str) -> dict[str, dict]:
    flags: dict[str, dict] = defaultdict(
        lambda: {"sb_certified": False, "dvbe_certified": False, "cmas_holder": False}
    )
    for r in _rows(
        con,
        "SELECT canonical_id, cert_small_business, cert_micro_business, cert_dvbe "
        "FROM gold_supplier_certification WHERE cert_record_count > 0",
    ):
        f = flags[r["canonical_id"]]
        f["sb_certified"] = bool(r["cert_small_business"] or r["cert_micro_business"])
        f["dvbe_certified"] = bool(r["cert_dvbe"])
    # A CMAS holder needs an agreement still in term: 21 of 558 matched
    # suppliers held only expired ones (2026-10) and read as current holders.
    for r in _rows(
        con,
        "SELECT canonical_id FROM gold_supplier_cmas "
        "WHERE cmas_agreement_count > 0 AND latest_term_end >= ?",
        (today,),
    ):
        flags[r["canonical_id"]]["cmas_holder"] = True
    return flags


def _departments(
    con,
    opps: list[dict],
    flags: dict[str, dict],
    aliases: dict[str, list[str]] | None = None,
    today: str | None = None,
) -> tuple[list[dict], dict]:
    """(index rows, {bu: page payload}) for every business unit in dim_department."""
    names = {
        r["business_unit"]: r["department_name"]
        for r in _rows(con, "SELECT business_unit, department_name FROM lv_dim_department")
    }
    head = {
        r["business_unit"]: r
        for r in _rows(
            con,
            "SELECT business_unit, MIN(start_date) AS coverage_from, "
            "MAX(start_date) AS coverage_to, COUNT(*) AS document_count, "
            "SUM(grand_total) AS total_value, COUNT(DISTINCT canonical_id) AS supplier_count "
            "FROM gold_document GROUP BY business_unit",
        )
    }
    by_fy = defaultdict(list)
    for r in _rows(
        con,
        "SELECT business_unit, fiscal_year, MIN(start_date) AS lo, MAX(start_date) AS hi, "
        "COUNT(*) AS document_count, SUM(grand_total) AS total_value FROM gold_document "
        "WHERE fiscal_year IS NOT NULL GROUP BY business_unit, fiscal_year ORDER BY fiscal_year",
    ):
        by_fy[r["business_unit"]].append(r)
    mix = defaultdict(list)
    for r in _rows(
        con,
        "SELECT d.business_unit, d.acquisition_type, "
        "COALESCE(a.competitive_flag, 'Other') AS competitive_flag, "
        "COUNT(*) AS document_count, SUM(d.grand_total) AS total_value "
        "FROM gold_document d LEFT JOIN lv_dim_acquisition a "
        "  ON a.acquisition_type IS d.acquisition_type "
        " AND a.acquisition_sub_type IS d.acquisition_sub_type "
        " AND a.acquisition_method IS d.acquisition_method "
        "GROUP BY 1, 2, 3 ORDER BY total_value DESC",
    ):
        mix[r["business_unit"]].append(r)
    sup = defaultdict(list)
    for r in _rows(
        con,
        "SELECT business_unit, canonical_id, MAX(canonical_name) AS canonical_name, "
        "COUNT(*) AS document_count, SUM(grand_total) AS total_value FROM gold_document "
        "GROUP BY business_unit, canonical_id ORDER BY total_value DESC",
    ):
        sup[r["business_unit"]].append(r)
    market = defaultdict(lambda: defaultdict(list))
    for r in _rows(
        con,
        "SELECT business_unit, acquisition_type, canonical_id, SUM(grand_total) AS v "
        "FROM gold_document GROUP BY 1, 2, 3",
    ):
        market[r["business_unit"]][r["acquisition_type"]].append(r["v"])
    demand = defaultdict(list)
    for r in _rows(
        con,
        "SELECT business_unit, event_year, event_count, sb_only_count, dvbe_only_count, "
        "set_aside_pct, sb_or_dvbe_count FROM gold_eprocure_event_demand "
        "WHERE business_unit IS NOT NULL ORDER BY event_year",
    ):
        demand[r.pop("business_unit")].append(r)
    opps_by_bu = defaultdict(list)
    for o in opps:
        if o["business_unit"]:
            opps_by_bu[o["business_unit"]].append(o)

    today = today or date.today().isoformat()
    index, pages = [], {}
    for bu, name in names.items():
        h = head.get(bu)
        loaded = h is not None
        row = {
            "business_unit": bu,
            "department_name": name,
            "aliases": (aliases or {}).get(bu, []),  # curated: Caltrans, CHP, DMV, ...
            "loaded": loaded,
            "coverage_from": h["coverage_from"] if loaded else None,
            "coverage_to": h["coverage_to"] if loaded else None,
            "document_count": h["document_count"] if loaded else None,
            "total_value": _money(h["total_value"]) if loaded else None,
            "open_opportunity_count": len(opps_by_bu[bu]),
        }
        index.append(row)
        page = dict(row)
        if loaded:
            total = h["total_value"] or 0
            competitive = sum(
                m["total_value"] or 0 for m in mix[bu] if m["competitive_flag"] == "Competitive"
            )
            page["supplier_count"] = h["supplier_count"]
            page["competitive_share_pct"] = round(100 * competitive / total, 1) if total else None
            page["spend_by_fiscal_year"] = [
                {
                    "fiscal_year": r["fiscal_year"],
                    "document_count": r["document_count"],
                    "total_value": _money(r["total_value"]),
                    "partial": _fy_partial(
                        r["fiscal_year"], h["coverage_from"], h["coverage_to"], today
                    ),
                }
                for r in by_fy[bu]
            ]
            page["acquisition_mix"] = [
                {
                    **{k: m[k] for k in ("acquisition_type", "competitive_flag", "document_count")},
                    "total_value": _money(m["total_value"]),
                }
                for m in mix[bu]
            ]
            page["top_suppliers"] = [
                {
                    "canonical_id": s["canonical_id"],
                    "canonical_name": s["canonical_name"],
                    "supplier_kind": _supplier_kind(s["canonical_id"]),
                    "document_count": s["document_count"],
                    "total_value": _money(s["total_value"]),
                    "share_pct": round(100 * (s["total_value"] or 0) / total, 2) if total else None,
                    **flags[s["canonical_id"]],
                }
                for s in sup[bu][:TOP_N]
            ]
            page["top_suppliers_total_count"] = len(sup[bu])
            conc = []
            for acq, vals in market[bu].items():
                n, hhi, top = _hhi(vals)
                if n:
                    conc.append(
                        {
                            "acquisition_type": acq,
                            "supplier_count": n,
                            "hhi": hhi,
                            "top_supplier_pct": top,
                            "market_value": _money(sum(v for v in vals if v and v > 0)),
                        }
                    )
            page["market_concentration"] = sorted(conc, key=lambda c: -c["market_value"])
        else:
            for k in (
                "supplier_count",
                "competitive_share_pct",
                "spend_by_fiscal_year",
                "acquisition_mix",
                "top_suppliers",
                "top_suppliers_total_count",
                "market_concentration",
            ):
                page[k] = None
        page["solicitation_history"] = demand.get(bu, [])
        page["open_opportunities"] = opps_by_bu[bu]
        pages[bu] = page
    index.sort(key=lambda r: (not r["loaded"], -(r["total_value"] or 0), r["department_name"]))
    return index, pages


def _coverage(con) -> tuple[str, str]:
    """The whole warehouse's start-date window (first, last)."""
    return con.execute("SELECT MIN(start_date), MAX(start_date) FROM gold_document").fetchone()


def _suppliers(con, flags: dict[str, dict], today: str) -> tuple[list[dict], dict[str, dict]]:
    """(index rows, {shard: {canonical_id: page payload}})."""
    cov_from, cov_to = _coverage(con)
    master = _rows(
        con,
        "SELECT canonical_id, canonical_name, parent_name, registration_count, "
        "award_count AS document_count, total_value, department_count, "
        "pct_noncompetitive_value, hq_city, hq_state, website FROM gold_supplier_master "
        "ORDER BY total_value DESC",
    )
    by_fy, depts, acq, recent = (defaultdict(list) for _ in range(4))
    for r in _rows(
        con,
        "SELECT canonical_id, fiscal_year, COUNT(*) AS document_count, "
        "SUM(grand_total) AS total_value FROM gold_document WHERE fiscal_year IS NOT NULL "
        "GROUP BY 1, 2 ORDER BY 2",
    ):
        by_fy[r.pop("canonical_id")].append(
            {
                **r,
                "total_value": _money(r["total_value"]),
                # Against the warehouse window (a supplier has no window of its
                # own): flags the in-progress and not-yet-started years.
                "partial": _fy_partial(r["fiscal_year"], cov_from, cov_to, today),
            }
        )
    for r in _rows(
        con,
        "SELECT canonical_id, business_unit, MAX(department_name) AS department_name, "
        "COUNT(*) AS document_count, SUM(grand_total) AS total_value FROM gold_document "
        "GROUP BY 1, 2 ORDER BY total_value DESC",
    ):
        depts[r.pop("canonical_id")].append({**r, "total_value": _money(r["total_value"])})
    for r in _rows(
        con,
        "SELECT canonical_id, acquisition_type, COUNT(*) AS document_count, "
        "SUM(grand_total) AS total_value FROM gold_document GROUP BY 1, 2 "
        "ORDER BY total_value DESC",
    ):
        acq[r.pop("canonical_id")].append({**r, "total_value": _money(r["total_value"])})
    for r in _rows(
        con,
        "SELECT canonical_id, business_unit, purchase_document, start_date, grand_total, "
        "acquisition_type, status FROM ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY canonical_id "
        "    ORDER BY start_date DESC, purchase_document) AS rn FROM gold_document"
        ") WHERE rn <= ?",
        (RECENT_CONTRACTS,),
    ):
        recent[r.pop("canonical_id")].append({**r, "grand_total": _money(r["grand_total"])})
    certs = {
        r["canonical_id"]: r
        for r in _rows(
            con,
            "SELECT canonical_id, cert_small_business, cert_micro_business, cert_dvbe, "
            "latest_cert_end, certification_types FROM gold_supplier_certification "
            "WHERE cert_record_count > 0",
        )
    }
    cmas = {
        r["canonical_id"]: r
        for r in _rows(
            con,
            "SELECT canonical_id, cmas_agreement_count, cmas_small_business, cmas_dvbe, "
            "latest_term_end FROM gold_supplier_cmas WHERE cmas_agreement_count > 0",
        )
    }
    agreements = defaultdict(list)
    # Contact name/email/website columns are deliberately not selected (public slice).
    for r in _rows(
        con,
        "SELECT matched_canonical_id AS canonical_id, agreement_number, term_start_date, "
        "term_end_date, product_service_codes FROM gold_cmas_agreement "
        "WHERE matched_canonical_id IS NOT NULL ORDER BY term_end_date DESC",
    ):
        agreements[r.pop("canonical_id")].append(r)

    index, shards = [], defaultdict(dict)
    for m in master:
        cid = m["canonical_id"]
        index.append(
            {
                "canonical_id": cid,
                "canonical_name": m["canonical_name"],
                "supplier_kind": _supplier_kind(cid),
                "document_count": m["document_count"],
                "total_value": _money(m["total_value"]),
                "department_count": m["department_count"],
                **flags[cid],
            }
        )
        c, k = certs.get(cid), cmas.get(cid)
        shards[_shard(cid)][cid] = {
            "profile": {
                **m,
                "supplier_kind": _supplier_kind(cid),
                "total_value": _money(m["total_value"]),
            },
            "spend_by_fiscal_year": by_fy[cid],
            "departments": depts[cid][:TOP_N],
            "departments_total_count": len(depts[cid]),
            "acquisition_types": acq[cid],
            "certification": {
                "found": True,
                "small_business": bool(c["cert_small_business"]),
                "micro_business": bool(c["cert_micro_business"]),
                "dvbe": bool(c["cert_dvbe"]),
                "latest_cert_end": c["latest_cert_end"],
                "certification_types": c["certification_types"],
            }
            if c
            else {"found": False},
            "cmas": {
                "found": True,
                "agreement_count": k["cmas_agreement_count"],
                "small_business": bool(k["cmas_small_business"]),
                "dvbe": bool(k["cmas_dvbe"]),
                "latest_term_end": k["latest_term_end"],
                "agreements": agreements[cid],
            }
            if k
            else {"found": False},
            "recent_contracts": recent[cid],
        }
    return index, shards


# --------------------------------------------------------------------------- insights
# Three "vectors" a vendor browses by, beside department and supplier. Acquisition
# type and contract size cover every contract; product category comes from
# line-item UNSPSC codes, so it covers only itemized contracts (basis
# "line_items") and the UI must say so.

# (key, label, lower bound inclusive, upper bound exclusive). grand_total <= 0
# (credits, adjustments) gets its own bucket so it doesn't read as a small buy.
SIZE_BUCKETS = (
    ("adjustment", "$0 or adjustment", None, 0.0),
    ("under-10k", "Under $10K", 0.0, 10_000.0),  # exclusive of 0 (see _size_bucket)
    ("10k-100k", "$10K–$100K", 10_000.0, 100_000.0),
    ("100k-250k", "$100K–$250K", 100_000.0, 250_000.0),
    ("250k-500k", "$250K–$500K", 250_000.0, 500_000.0),
    ("500k-1m", "$500K–$1M", 500_000.0, 1_000_000.0),
    ("1m-5m", "$1M–$5M", 1_000_000.0, 5_000_000.0),
    ("5m-10m", "$5M–$10M", 5_000_000.0, 10_000_000.0),
    ("10m-plus", "$10M+", 10_000_000.0, None),
)

# Base acquisition types. Silver splits the combined "Type_SubType" string
# (the drill-down's glued "NON-IT Services_Expert Witnesses" too, since the
# 2026-10 fix, gated by the acquisition_type_split DQ check); rolling up on the
# part before "_" stays as a guard so the vector can never splinter again.
ACQUISITION_LABELS = {
    "NON-IT Goods": "Goods (non-IT)",
    "NON-IT Services": "Services (non-IT)",
    "IT Goods": "IT goods",
    "IT Services": "IT services",
    "Telecom": "Telecom",
    "Encumbrance Only": "Encumbrance only (funding record)",
    "Unknown": "Unknown",
}

REFERENCES = Path(__file__).resolve().parent.parent / "references"
ALIASES_CSV = REFERENCES / "department_aliases.csv"
CATEGORIES_CSV = REFERENCES / "product_categories.csv"
OTHER_CATEGORY = ("other", "Other / unclassified")


# CMAS "Business Enterprise Type" -> (small_business, dvbe). "NA" = neither.
_CMAS_ENTERPRISE = {"SB": (True, False), "DVBE": (False, True), "DVBE and SB": (True, True)}


def _cmas_category(code: str) -> str:
    """A product/service code's family — the text before its first hyphen
    ("IT CONSULT-SYSTEM MGMT" -> "IT CONSULT"); a code without one is its own."""
    return code.split("-", 1)[0].strip()


def _cmas_brand(code: str) -> str | None:
    """ "BRAND-CISCO" -> "CISCO": an authorized-brand code, not a product family
    (700 brands, the single largest "family" if left in the categories)."""
    head, _, tail = code.partition("-")
    return tail.strip() or None if head.strip() == "BRAND" else None


def _cmas(con, today: str) -> dict | None:
    """cmas/agreements.json: every CMAS agreement (not just the ones matched to a
    SCPRS supplier — 70% aren't) plus filter facets, for a searchable tab the
    front end filters client-side. None when the build had no CMAS side input.
    Contact name/email and company website are deliberately not selected
    (public slice, same rule as the supplier shards)."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'gold_cmas_agreement'").fetchone():
        return None
    items = []
    facets = {
        k: defaultdict(int) for k in ("status", "enterprise_type", "state", "category", "brand")
    }
    for r in _rows(
        con,
        "SELECT agreement_number, supplier_name, base_schedule_number, base_schedule_holder, "
        "business_enterprise_type, term_start_date, term_end_date, product_service_codes, "
        "city, state_or_country, matched_canonical_id, matched_canonical_name "
        "FROM gold_cmas_agreement ORDER BY supplier_name, agreement_number",
    ):
        enterprise = (r["business_enterprise_type"] or "").strip()
        sb, dvbe = _CMAS_ENTERPRISE.get(enterprise, (False, False))
        codes = [c.strip() for c in (r["product_service_codes"] or "").split(",") if c.strip()]
        brands = sorted({b for c in codes if (b := _cmas_brand(c))})
        categories = sorted({_cmas_category(c) for c in codes if not _cmas_brand(c)})
        end = r["term_end_date"]
        status = "active" if end and end >= today else "expired"
        cid = r["matched_canonical_id"]
        items.append(
            {
                "agreement_number": r["agreement_number"],
                "supplier_name": r["supplier_name"],
                "base_schedule_number": r["base_schedule_number"],
                "base_schedule_holder": r["base_schedule_holder"],
                "enterprise_type": enterprise if enterprise in _CMAS_ENTERPRISE else None,
                "small_business": sb,
                "dvbe": dvbe,
                "term_start_date": r["term_start_date"],
                "term_end_date": end,
                "status": status,
                "city": r["city"] or None,
                "state": r["state_or_country"] or None,
                "product_service_codes": codes,
                "categories": categories,
                "brands": brands,
                # Link to the supplier page (suppliers/shard-{NN}.json) when the
                # agreement holder also has SCPRS contracts.
                "supplier": {
                    "canonical_id": cid,
                    "canonical_name": r["matched_canonical_name"],
                    "shard": _shard(cid),
                }
                if cid
                else None,
            }
        )
        facets["status"][status] += 1
        facets["enterprise_type"][items[-1]["enterprise_type"] or "None"] += 1
        if items[-1]["state"]:
            facets["state"][items[-1]["state"]] += 1
        for c in categories:
            facets["category"][c] += 1
        for b in brands:
            facets["brand"][b] += 1
    return {
        "total_count": len(items),
        "active_count": facets["status"]["active"],
        "matched_to_supplier_count": sum(1 for i in items if i["supplier"]),
        # Facet values with counts, most common first, for the filter controls.
        "facets": {
            k: [{"value": v, "count": n} for v, n in sorted(f.items(), key=lambda x: (-x[1], x[0]))]
            for k, f in facets.items()
        },
        "items": items,
    }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _size_bucket(v) -> str | None:
    if v is None:
        return None
    if v <= 0:
        return SIZE_BUCKETS[0][0]  # $0 contracts and credits, not "small buys"
    for key, _label, lo, hi in SIZE_BUCKETS:
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return key
    return None


def _base_acquisition(t) -> str:
    return (t or "Unknown").split("_", 1)[0].strip() or "Unknown"


def load_aliases(path: Path = ALIASES_CSV) -> dict[str, list[str]]:
    """{business_unit: [alias, ...]} from the curated CSV (missing file = none)."""
    out: dict[str, list[str]] = defaultdict(list)
    if Path(path).exists():
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                out[r["business_unit"].strip()].append(r["alias"].strip())
    return out


def load_categories(path: Path = CATEGORIES_CSV) -> list[dict]:
    """Curated UNSPSC-prefix → plain-language category rows, file order."""
    with open(path, encoding="utf-8", newline="") as f:
        return [
            {
                "key": r["key"],
                "label": r["label"],
                "prefixes": r["unspsc_prefixes"].split(),
                "keywords": [k.strip() for k in r["keywords"].split(";") if k.strip()],
            }
            for r in csv.DictReader(f)
        ]


def _category_matcher(categories: list[dict]):
    """unspsc -> category key; the LONGEST matching prefix wins (8111 beats 81)."""
    prefixes = sorted(
        ((p, c["key"]) for c in categories for p in c["prefixes"]), key=lambda t: -len(t[0])
    )

    def match(unspsc) -> str:
        code = str(unspsc or "")
        return next((key for p, key in prefixes if code.startswith(p)), OTHER_CATEGORY[0])

    return match


# The slice sources: one temp table per vector with the same columns, so one
# aggregation routine serves all three. Each SELECT is an internal constant.
_DOC_SLICE = (
    "SELECT {key} AS k, business_unit, purchase_document, canonical_id, canonical_name, "
    "fiscal_year, grand_total AS v, {cross} AS x, NULL AS x_desc FROM gold_document"
)
_LINE_SLICE = (
    "SELECT app_category(l.unspsc) AS k, l.business_unit, l.purchase_document, "
    "d.canonical_id, d.canonical_name, l.fiscal_year, l.line_amount AS v, "
    "l.unspsc AS x, l.category AS x_desc FROM gold_line_item l "
    "JOIN gold_document d ON d.business_unit = l.business_unit "
    "AND d.purchase_document = l.purchase_document"
)
_VECTORS = (
    (
        "acquisition",
        _DOC_SLICE.format(key="app_acq(acquisition_type)", cross="app_size(grand_total)"),
    ),
    ("size", _DOC_SLICE.format(key="app_size(grand_total)", cross="app_acq(acquisition_type)")),
    ("category", _LINE_SLICE),
)


_DOCS = "COUNT(DISTINCT business_unit || '|' || purchase_document) AS document_count"
_Q_HEAD = (
    "SELECT k, {docs}, COUNT(*) AS row_count, SUM(v) AS total_value "
    "FROM temp.{t} WHERE k IS NOT NULL GROUP BY k"
)
_Q_BY_FY = (
    "SELECT k, fiscal_year, {docs}, SUM(v) AS total_value FROM temp.{t} "
    "WHERE k IS NOT NULL AND fiscal_year IS NOT NULL GROUP BY 1, 2 ORDER BY 2"
)
_Q_DEPTS = (
    "SELECT k, business_unit, {docs}, SUM(v) AS total_value FROM temp.{t} "
    "WHERE k IS NOT NULL GROUP BY 1, 2 ORDER BY total_value DESC"
)
_Q_SUPPLIERS = (
    "SELECT k, canonical_id, MAX(canonical_name) AS canonical_name, {docs}, "
    "SUM(v) AS total_value FROM temp.{t} WHERE k IS NOT NULL "
    "GROUP BY 1, 2 ORDER BY total_value DESC"
)
_Q_CROSS = (
    "SELECT k, x, MAX(x_desc) AS x_desc, {docs}, COUNT(*) AS row_count, "
    "SUM(v) AS total_value FROM temp.{t} WHERE k IS NOT NULL AND x IS NOT NULL "
    "GROUP BY 1, 2 ORDER BY total_value DESC"
)


def _slices(con, table: str, flags: dict[str, dict], dept_names: dict[str, str]) -> dict:
    """{key: page payload} for one vector's temp table (internal constant name)."""

    # {t} = the vector's temp table (internal constant); DOCS counts distinct contracts.
    def q(sql: str) -> list[dict]:
        return _rows(con, sql.replace("{docs}", _DOCS).format(t=table))

    head = {r["k"]: r for r in q(_Q_HEAD)}
    grand = sum(h["total_value"] or 0 for h in head.values())
    by_fy, depts, sups, cross = (defaultdict(list) for _ in range(4))
    for r in q(_Q_BY_FY):
        by_fy[r.pop("k")].append({**r, "total_value": _money(r["total_value"])})
    for r in q(_Q_DEPTS):
        depts[r.pop("k")].append(r)
    for r in q(_Q_SUPPLIERS):
        sups[r.pop("k")].append(r)
    for r in q(_Q_CROSS):
        cross[r.pop("k")].append(r)

    pages = {}
    for k, h in head.items():
        total = h["total_value"] or 0
        n, hhi, top = _hhi([s["total_value"] for s in sups[k]])
        pages[k] = {
            "key": k,
            "document_count": h["document_count"],
            "line_count": h["row_count"],
            "total_value": _money(total),
            "share_of_total_pct": round(100 * total / grand, 1) if grand else None,
            "spend_by_fiscal_year": by_fy[k],
            "top_departments": [
                {
                    "business_unit": d["business_unit"],
                    "department_name": dept_names.get(d["business_unit"], d["business_unit"]),
                    "document_count": d["document_count"],
                    "total_value": _money(d["total_value"]),
                    "share_pct": round(100 * (d["total_value"] or 0) / total, 2) if total else None,
                }
                for d in depts[k][:TOP_N]
            ],
            "top_departments_total_count": len(depts[k]),
            "top_suppliers": [
                {
                    "canonical_id": s["canonical_id"],
                    "canonical_name": s["canonical_name"],
                    "supplier_kind": _supplier_kind(s["canonical_id"]),
                    "document_count": s["document_count"],
                    "total_value": _money(s["total_value"]),
                    "share_pct": round(100 * (s["total_value"] or 0) / total, 2) if total else None,
                    **flags[s["canonical_id"]],
                }
                for s in sups[k][:TOP_N]
            ],
            "top_suppliers_total_count": len(sups[k]),
            "concentration": {"supplier_count": n, "hhi": hhi, "top_supplier_pct": top},
            "_cross": cross[k],
        }
    return pages


def _insights(con, flags, dept_index, sup_index, opps, today, categories):
    """(overview payload, {vector: {key: page}}, search items) — all precomputed."""
    match = _category_matcher(categories)
    con.create_function("app_size", 1, _size_bucket, deterministic=True)
    con.create_function("app_acq", 1, _base_acquisition, deterministic=True)
    con.create_function("app_category", 1, match, deterministic=True)
    dept_names = {d["business_unit"]: d["department_name"] for d in dept_index}
    size_labels = {k: label for k, label, _lo, _hi in SIZE_BUCKETS}
    cat_meta = {c["key"]: c for c in categories}
    cat_meta[OTHER_CATEGORY[0]] = {"label": OTHER_CATEGORY[1], "keywords": []}

    vectors = {}
    for vector, select in _VECTORS:
        table = f"app_{vector}"
        con.execute(f"DROP TABLE IF EXISTS temp.{table}")  # noqa: S608 — internal name
        con.execute(f"CREATE TEMP TABLE {table} AS {select}")  # noqa: S608 — internal constants
        vectors[vector] = _slices(con, table, flags, dept_names)

    def mix(pages, labels, order=None):
        keys = order or sorted(pages, key=lambda k: -(pages[k]["total_value"] or 0))
        return [
            {
                "key": k,
                "label": labels(k),
                "document_count": pages[k]["document_count"],
                "total_value": pages[k]["total_value"],
                "share_pct": pages[k]["share_of_total_pct"],
            }
            for k in keys
            if k in pages
        ]

    acq_label = lambda k: ACQUISITION_LABELS.get(k, k)  # noqa: E731
    size_order = [k for k, *_ in SIZE_BUCKETS]
    out: dict[str, dict] = {}
    for vector, pages in vectors.items():
        out[vector] = {}
        for k, page in pages.items():
            cross = page.pop("_cross")
            if vector == "acquisition":
                slug, label = _slug(k), acq_label(k)
                page.update(kind="acquisition", key=slug, label=label, acquisition_type=k,
                            basis="contracts")  # fmt: skip
                by_size = {c["x"]: c for c in cross}
                page["size_mix"] = [
                    {"key": s, "label": size_labels[s],
                     "document_count": by_size[s]["document_count"],
                     "total_value": _money(by_size[s]["total_value"])}
                    for s in size_order if s in by_size
                ]  # fmt: skip
            elif vector == "size":
                slug, label = k, size_labels[k]
                page.update(kind="size", label=label, basis="contracts")
                page["acquisition_mix"] = [
                    {"key": _slug(c["x"]), "label": acq_label(c["x"]),
                     "document_count": c["document_count"], "total_value": _money(c["total_value"])}
                    for c in cross
                ]  # fmt: skip
            else:
                slug, label = k, cat_meta[k]["label"]
                page.update(kind="category", label=label, basis="line_items",
                            keywords=cat_meta[k]["keywords"])  # fmt: skip
                page["top_commodities"] = [
                    {"unspsc": c["x"], "description": c["x_desc"], "line_count": c["row_count"],
                     "total_value": _money(c["total_value"])}
                    for c in cross[:TOP_N]
                ]  # fmt: skip
                page["top_commodities_total_count"] = len(cross)
            if vector != "category":
                page.pop("line_count")
            out[vector][slug] = page

    fy = _rows(
        con,
        "SELECT fiscal_year, COUNT(*) AS document_count, SUM(grand_total) AS total_value, "
        "COUNT(DISTINCT business_unit) AS department_count FROM gold_document "
        "WHERE fiscal_year IS NOT NULL GROUP BY 1 ORDER BY 1",
    )
    cov = _coverage(con)
    week = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    companies = [s for s in sup_index if s["supplier_kind"] == "company"]
    overview = {
        "headline": {
            "documents": sum(d["document_count"] or 0 for d in dept_index),
            "total_value": _money(sum(d["total_value"] or 0 for d in dept_index)),
            "departments_loaded": sum(d["loaded"] for d in dept_index),
            "departments_total": len(dept_index),
            "company_count": len(companies),
            "open_opportunities": len(opps),
            "closing_within_7_days": sum(o["bid_close_date"] <= week for o in opps),
            "open_sb_only": sum(o["sb_only"] for o in opps),
            "open_dvbe_only": sum(o["dvbe_only"] for o in opps),
            "open_sb_or_dvbe": sum(o["sb_or_dvbe"] for o in opps),
        },
        # department_count: how many departments report in that year — coverage
        # varies by year, so a dip can be fewer departments, not less buying.
        # partial: the year isn't fully inside the data window -- the current
        # year is in progress and later ones carry only contracts entered ahead
        # of their start date (FY2027 at $53.2B four months in, 2026-10).
        "spend_by_fiscal_year": [
            {
                **r,
                "total_value": _money(r["total_value"]),
                "partial": _fy_partial(r["fiscal_year"], *cov, today),
            }
            for r in fy
        ],
        "top_departments": [
            {k: d[k] for k in ("business_unit", "department_name", "document_count", "total_value")}
            for d in dept_index[:10]
            if d["loaded"]
        ],
        "top_companies": [
            {
                k: s[k]
                for k in (
                    "canonical_id",
                    "canonical_name",
                    "document_count",
                    "total_value",
                    "department_count",
                    "sb_certified",
                    "dvbe_certified",
                )
            }
            for s in companies[:10]
        ],  # fmt: skip
        "acquisition_mix": mix(out["acquisition"], lambda k: out["acquisition"][k]["label"]),
        "size_mix": mix(out["size"], lambda k: size_labels[k], size_order),
        "category_mix": mix(out["category"], lambda k: out["category"][k]["label"]),
        "category_basis": "line_items",
        "closing_soon": opps[:10],
    }

    aliases = {d["business_unit"]: d.get("aliases", []) for d in dept_index}
    search = [
        {"type": "department", "key": d["business_unit"], "label": d["department_name"],
         "terms": [d["business_unit"], *aliases[d["business_unit"]]],
         "total_value": d["total_value"], "loaded": d["loaded"]}
        for d in dept_index
    ]  # fmt: skip
    for vector, pages in out.items():
        for slug, p in pages.items():
            terms = [p.get("acquisition_type", "")] if vector == "acquisition" else []
            search.append(
                {"type": vector, "key": slug, "label": p["label"],
                 "terms": [t for t in (*terms, *p.get("keywords", [])) if t],
                 "total_value": p["total_value"], "loaded": True}
            )  # fmt: skip
    return overview, out, search


def export_app(
    serve_path: Path = SERVE_DB,
    app_dir: Path = APP_DIR,
    wh_path: Path = WAREHOUSE_DB,
    eprocure_path: Path = EPROCURE_DB,
    cmas_path: Path = CMAS_DB,
    today: str | None = None,
    log=print,
) -> dict[str, int]:
    """Write the app's JSON files from the serve DB; returns {file kind: count}.

    ``app_dir`` is replaced wholesale so a department or supplier that drops out
    of the warehouse can't leave a stale page behind."""
    serve_path, app_dir = Path(serve_path), Path(app_dir)
    if not serve_path.exists():
        raise FileNotFoundError(f"{serve_path} not found — run `warehouse serve-export` first")
    today = today or date.today().isoformat()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    staging = app_dir.with_name(app_dir.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)

    con = sqlite3.connect(f"file:{serve_path}?mode=ro", uri=True)
    try:
        loaded = {r[0] for r in con.execute("SELECT DISTINCT business_unit FROM gold_document")}
        opps = _opportunities(con, today, loaded)
        flags = _supplier_flags(con, today)
        dept_index, dept_pages = _departments(con, opps, flags, load_aliases(), today)
        sup_index, sup_shards = _suppliers(con, flags, today)
        overview, insight_pages, search = _insights(
            con, flags, dept_index, sup_index, opps, today, load_categories()
        )
        cmas = _cmas(con, today)
        cov = con.execute(
            "SELECT COUNT(*), SUM(grand_total), MIN(start_date), MAX(start_date), "
            "SUM(line_count > 0) FROM gold_document"
        ).fetchone()
    finally:
        con.close()

    batch = _optional_scalar(
        wh_path,
        "SELECT batch_id || '|' || finished_at FROM dw_batch WHERE status = 'ok' "
        "ORDER BY finished_at DESC LIMIT 1",
    )
    batch_id, batch_end = batch.split("|", 1) if batch else (None, None)
    meta = {
        "warehouse_batch_id": batch_id,
        "contracts_as_of": batch_end[:10] if batch_end else None,
        "opportunities_as_of": (
            _optional_scalar(
                eprocure_path,
                "SELECT extracted_at FROM events_meta WHERE slice_key = 'posted'",
            )
            or ""
        )[:10]
        or None,
        "certifications_as_of": (
            _optional_scalar(eprocure_path, "SELECT MAX(extracted_at) FROM extract_meta") or ""
        )[:10]
        or None,
        # Stamped by `src.cmas extract` since 2026-10-31; null for an older store.
        "cmas_as_of": (
            _optional_scalar(cmas_path, "SELECT MAX(extracted_at) FROM extract_meta") or ""
        )[:10]
        or None,
        "coverage": {
            "departments_loaded": len(loaded),
            "departments_total": len(dept_index),
            "documents": cov[0],
            "total_value": _money(cov[1]),
            "earliest_start_date": cov[2],
            "latest_start_date": cov[3],
            "documents_with_line_items": cov[4] or 0,
        },
        "ask_url": None,  # the chat Space is private/frozen; the MVP ships without the Ask box
    }

    sizes = {}
    sizes["meta"] = _write(staging / "meta.json", meta, generated_at)
    sizes["opportunities"] = _write(
        staging / "opportunities.json", {"total_count": len(opps), "items": opps}, generated_at
    )
    sizes["departments_index"] = _write(
        staging / "departments" / "index.json", {"items": dept_index}, generated_at
    )
    for bu, page in dept_pages.items():
        _write(staging / "departments" / f"{bu}.json", page, generated_at)
    sizes["suppliers_index"] = _write(
        staging / "suppliers" / "index.json", {"items": sup_index}, generated_at
    )
    shard_bytes = [
        _write(staging / "suppliers" / f"shard-{nn}.json", {"suppliers": body}, generated_at)
        for nn, body in sorted(sup_shards.items())
    ]
    sizes["overview"] = _write(staging / "insights" / "overview.json", overview, generated_at)
    for vector, pages in insight_pages.items():
        for key, page in pages.items():
            _write(staging / "insights" / vector / f"{key}.json", page, generated_at)
    sizes["search"] = _write(staging / "search.json", {"items": search}, generated_at)
    if cmas is not None:
        sizes["cmas"] = _write(staging / "cmas" / "agreements.json", cmas, generated_at)
    shutil.rmtree(app_dir, ignore_errors=True)
    staging.rename(app_dir)

    counts = {
        "opportunities": len(opps),
        "departments": len(dept_pages),
        "departments_loaded": len(loaded),
        "suppliers": len(sup_index),
        "supplier_shards": len(shard_bytes),
        "largest_shard_bytes": max(shard_bytes, default=0),
        "suppliers_index_bytes": sizes["suppliers_index"],
        "insight_pages": sum(len(p) for p in insight_pages.values()),
        "search_bytes": sizes["search"],
        "cmas_agreements": cmas["total_count"] if cmas else 0,
        "cmas_bytes": sizes.get("cmas", 0),
    }
    log(f"app-export: {app_dir} {counts}")
    return counts


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Write the vendor web app's static JSON files.")
    ap.add_argument("--out", type=Path, default=APP_DIR, help="output directory")
    ap.add_argument("--serve-db", type=Path, default=SERVE_DB)
    ap.add_argument(
        "--today",
        help="YYYY-MM-DD cutoff for open solicitations (default: today; "
        "pin it to a snapshot's date when generating design samples)",
    )
    args = ap.parse_args()
    export_app(serve_path=args.serve_db, app_dir=args.out, today=args.today)


if __name__ == "__main__":
    _cli()
