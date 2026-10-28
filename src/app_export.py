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

Grain rules (the contract's): documents are current-version (``gold_document``);
every supplier figure is per CANONICAL supplier computed from ``gold_document``
-- the per-registration marts (``gold_supplier_share``,
``gold_market_concentration``, ``gold_supplier_acquisition_profile``) split a
re-registered company into several smaller vendors and are not used. Unloaded
departments get ``loaded: false`` and null spend blocks, never zeros.

    python -m src.app_export [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from . import scprs
from .warehouse import EPROCURE_DB, SERVE_DB, WAREHOUSE_DB

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
        "sb_only, dvbe_only FROM gold_eprocure_posted_opportunity "
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
            "eprocure_url": None,  # no stable per-event link known yet (contract open question)
        }
        for r in rows
    ]


def _supplier_flags(con) -> dict[str, dict]:
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
    for r in _rows(
        con, "SELECT canonical_id FROM gold_supplier_cmas WHERE cmas_agreement_count > 0"
    ):
        flags[r["canonical_id"]]["cmas_holder"] = True
    return flags


def _departments(con, opps: list[dict], flags: dict[str, dict]) -> tuple[list[dict], dict]:
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
        "set_aside_pct FROM gold_eprocure_event_demand WHERE business_unit IS NOT NULL "
        "ORDER BY event_year",
    ):
        demand[r.pop("business_unit")].append(r)
    opps_by_bu = defaultdict(list)
    for o in opps:
        if o["business_unit"]:
            opps_by_bu[o["business_unit"]].append(o)

    index, pages = [], {}
    for bu, name in names.items():
        h = head.get(bu)
        loaded = h is not None
        row = {
            "business_unit": bu,
            "department_name": name,
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
                    # The window doesn't span the whole Jul-Jun year.
                    "partial": h["coverage_from"] > f"{r['fiscal_year'] - 1}-07-01"
                    or h["coverage_to"] < f"{r['fiscal_year']}-06-30",
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


def _suppliers(con, flags: dict[str, dict]) -> tuple[list[dict], dict[str, dict]]:
    """(index rows, {shard: {canonical_id: page payload}})."""
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
        by_fy[r.pop("canonical_id")].append({**r, "total_value": _money(r["total_value"])})
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


def export_app(
    serve_path: Path = SERVE_DB,
    app_dir: Path = APP_DIR,
    wh_path: Path = WAREHOUSE_DB,
    eprocure_path: Path = EPROCURE_DB,
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
        flags = _supplier_flags(con)
        dept_index, dept_pages = _departments(con, opps, flags)
        sup_index, sup_shards = _suppliers(con, flags)
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
        "cmas_as_of": None,  # cmas.db records no extraction timestamp
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
