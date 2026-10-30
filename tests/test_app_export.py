"""The vendor web app's static JSON export (src/app_export.py): the contract's
grain rules, null-not-zero for unloaded departments, and the public-slice
exclusions."""

import json
import sqlite3

from src import app_export, warehouse
from tests.test_warehouse import _seed_eprocure, _seed_source


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _build(tmp_path, extra_purchases=()):
    src, wh = tmp_path / "scprs.db", tmp_path / "warehouse.db"
    serve, epr = tmp_path / "warehouse-serve.db", tmp_path / "eprocure.db"
    _seed_source(src)  # BU 8660: doc A (v1 100 -> v2 150, Acme/S1), doc B (5000, Beta/S2)
    if extra_purchases:  # (purchase_document, start_date, grand_total)
        con = sqlite3.connect(src)
        con.executemany(
            "INSERT INTO purchases (business_unit, purchase_document, version, grand_total, "
            "start_date, acquisition_type_sub_type, acquisition_method, supplier_id, "
            "supplier_name, status, department_name) VALUES ('8660', ?, '1', ?, ?, "
            "'IT Goods', 'Formal - COMPETITIVE', 'S1', 'Acme', 'Active', 'PUC')",
            [(doc, total, start) for doc, start, total in extra_purchases],
        )
        con.commit()
        con.close()
    _seed_eprocure(
        epr,
        [("100", "Acme, Inc.", "SB", "2025-01-01", "2027-01-31")],
        events=[
            # open, SB-only, loaded department
            ("26-001", "Rate Case Support **SB Only**", "Public Utilities Commission", None,
             "2026-08-01", "Posted", 1, 0, "posted", None),
            # already closed on the export date -> dropped
            ("26-000", "Old Notice", "Public Utilities Commission", None,
             "2026-07-01", "Posted", 0, 0, "posted", None),
            # department name matches no business unit
            ("26-002", "Fair Services *DVBE Only*", "22nd DAA", None,
             "2026-07-30", "Posted", 0, 1, "posted", None),
            # history for an UNLOADED department
            ("25-009", "Audit", "Department of Justice", "2025-01-05",
             "2025-02-01", "Event Completed", 0, 0, "historical", 2025),
        ],
    )  # fmt: skip
    con = sqlite3.connect(epr)
    con.execute("CREATE TABLE events_meta (slice_key, banner_total, rows, extracted_at)")
    con.execute("INSERT INTO events_meta VALUES ('posted', 3, 3, '2026-07-25T05:22:01+00:00')")
    con.commit()
    con.close()
    warehouse.build_all(
        wh_path=wh,
        source_path=src,
        enrichment_db=tmp_path / "no_enrich.db",
        cmas_db=tmp_path / "missing_cmas.db",
        eprocure_db=epr,
        log=lambda *a: None,
    )
    warehouse.export_serve_db(wh_path=wh, serve_path=serve, log=lambda *a: None)
    out = tmp_path / "app"
    counts = app_export.export_app(
        serve_path=serve,
        app_dir=out,
        wh_path=wh,
        eprocure_path=epr,
        today="2026-07-25",
        log=lambda *a: None,
    )
    return out, counts


def test_app_export_follows_the_data_contract(tmp_path):
    out, counts = _build(tmp_path)

    meta = _load(out / "meta.json")
    assert meta["schema_version"] == app_export.SCHEMA_VERSION
    assert meta["opportunities_as_of"] == "2026-07-25"
    assert meta["contracts_as_of"] is not None
    assert meta["coverage"]["documents"] == 2  # doc A's two versions collapse to one
    assert meta["coverage"]["departments_loaded"] == 1
    assert meta["coverage"]["departments_total"] == counts["departments"]

    # Opportunities: closed rows dropped, soonest close first, flags are booleans.
    opps = _load(out / "opportunities.json")["items"]
    assert [o["event_id"] for o in opps] == ["26-002", "26-001"]
    daa, puc = opps
    assert daa["business_unit"] is None and daa["department_loaded"] is False
    assert daa["dvbe_only"] is True and daa["sb_or_dvbe"] is False
    assert puc["business_unit"] == "8660" and puc["department_loaded"] is True
    assert puc["sb_only"] is True and puc["dvbe_only"] is False

    # Loaded department: current-version totals, shares by canonical supplier.
    puc_page = _load(out / "departments" / "8660.json")
    assert puc_page["loaded"] is True
    assert puc_page["total_value"] == 5150.0  # 150 (A v2) + 5000 (B), not 5250
    assert puc_page["document_count"] == 2
    assert round(sum(s["share_pct"] for s in puc_page["top_suppliers"]), 2) == 100.0
    assert {s["supplier_kind"] for s in puc_page["top_suppliers"]} == {"company"}
    fy = puc_page["spend_by_fiscal_year"]
    assert [y["fiscal_year"] for y in fy] == [2021] and fy[0]["partial"] is True
    assert [o["event_id"] for o in puc_page["open_opportunities"]] == ["26-001"]
    assert {m["acquisition_type"] for m in puc_page["market_concentration"]} == {
        "IT Services",
        "NON-IT Services",
    }

    # Unloaded department: still has a page; spend blocks are null, never zero.
    doj = _load(out / "departments" / "0820.json")
    assert doj["loaded"] is False
    for key in ("total_value", "document_count", "spend_by_fiscal_year", "top_suppliers"):
        assert doj[key] is None
    assert [h["event_year"] for h in doj["solicitation_history"]] == [2025]

    index = _load(out / "departments" / "index.json")["items"]
    assert index[0]["business_unit"] == "8660"  # loaded departments sort first
    assert len(index) == counts["departments"]

    # Suppliers: every index row resolves to a page in its shard.
    sup_index = _load(out / "suppliers" / "index.json")["items"]
    assert len(sup_index) == counts["suppliers"] == 2
    for row in sup_index:
        cid = row["canonical_id"]
        shard = _load(out / "suppliers" / f"shard-{app_export._shard(cid)}.json")
        page = shard["suppliers"][cid]
        assert page["profile"]["canonical_name"] == row["canonical_name"]
        assert page["certification"]["found"] in (True, False)
        assert page["cmas"] == {"found": False}  # no CMAS store in this build
    acme = next(r for r in sup_index if r["canonical_name"].upper().startswith("ACME"))
    assert acme["sb_certified"] is True  # matched to the SB certification by name


def test_app_export_replaces_stale_files(tmp_path):
    out, _ = _build(tmp_path)
    stale = out / "departments" / "9999.json"
    stale.write_text("{}", encoding="utf-8")
    serve, wh = tmp_path / "warehouse-serve.db", tmp_path / "warehouse.db"
    app_export.export_app(
        serve_path=serve, app_dir=out, wh_path=wh, today="2026-07-25", log=lambda *a: None
    )
    assert not stale.exists()
    assert not out.with_name("app.tmp").exists()


def test_supplier_kind_labels_non_companies():
    assert app_export._supplier_kind("0000015235") == "company"
    assert app_export._supplier_kind("DEPT518000") == "government"
    assert app_export._supplier_kind("UNIV678000") == "government"
    assert app_export._supplier_kind("0820CFD") == "placeholder"
    assert app_export._supplier_kind("4500SGP") == "placeholder"
    assert app_export._supplier_kind("UNKNOWN") == "placeholder"


def test_hhi_and_shard():
    assert app_export._hhi([50.0, 50.0]) == (2, 5000.0, 50.0)
    assert app_export._hhi([100.0, 0.0, None]) == (1, 10000.0, 100.0)
    assert app_export._hhi([]) == (0, 0.0, 0.0)
    assert app_export._shard("0000015235") == "35"
    assert app_export._shard("0820CFD") == "xx"


def test_insights_vectors_overview_and_search(tmp_path):
    out, counts = _build(tmp_path)

    # Contract-size vector: every document lands in exactly one bucket.
    sizes = {p.stem: _load(p) for p in (out / "insights" / "size").glob("*.json")}
    assert set(sizes) == {"under-10k"}  # docs A (150) and B (5000)
    assert sizes["under-10k"]["document_count"] == 2
    assert sizes["under-10k"]["total_value"] == 5150.0  # current version only
    assert sizes["under-10k"]["basis"] == "contracts"

    # Acquisition vector rolls sub-types up to the base type.
    acq = {p.stem for p in (out / "insights" / "acquisition").glob("*.json")}
    assert acq == {"it-services", "non-it-services"}
    it = _load(out / "insights" / "acquisition" / "it-services.json")
    assert it["acquisition_type"] == "IT Services" and it["label"] == "IT services"
    assert [s["key"] for s in it["size_mix"]] == ["under-10k"]

    # Product category vector: line-item basis, longest UNSPSC prefix wins.
    cats = {p.stem: _load(p) for p in (out / "insights" / "category").glob("*.json")}
    assert set(cats) == {"it-equipment", "it-services"}  # 4323..., 8111... (not 81 engineering)
    assert all(c["basis"] == "line_items" for c in cats.values())
    assert cats["it-services"]["top_commodities"][0]["unspsc"] == "81111508"

    ov = _load(out / "insights" / "overview.json")
    meta = _load(out / "meta.json")
    assert ov["headline"]["documents"] == meta["coverage"]["documents"]
    assert ov["headline"]["total_value"] == meta["coverage"]["total_value"]
    assert ov["headline"]["open_opportunities"] == 2
    assert [m["key"] for m in ov["size_mix"]] == ["under-10k"]
    assert round(sum(m["share_pct"] for m in ov["acquisition_mix"]), 1) == 100.0

    # Search: departments carry curated aliases; vectors carry keywords.
    search = _load(out / "search.json")["items"]
    puc = next(i for i in search if i["type"] == "department" and i["key"] == "8660")
    assert "CPUC" in puc["terms"]
    assert any(i["type"] == "category" and "laptop" in i["terms"] for i in search)
    index = _load(out / "departments" / "index.json")["items"]
    assert "CPUC" in next(d for d in index if d["business_unit"] == "8660")["aliases"]
    assert counts["insight_pages"] == len(sizes) + len(acq) + len(cats)


def test_size_buckets_and_category_matcher():
    assert app_export._size_bucket(None) is None
    assert app_export._size_bucket(0) == "adjustment"
    assert app_export._size_bucket(-50) == "adjustment"
    assert app_export._size_bucket(9_999.99) == "under-10k"
    assert app_export._size_bucket(10_000) == "10k-100k"
    assert app_export._size_bucket(250_000) == "250k-500k"
    assert app_export._size_bucket(10_000_000) == "10m-plus"
    assert app_export._base_acquisition("NON-IT Services_Expert Witnesses") == "NON-IT Services"
    assert app_export._base_acquisition(None) == "Unknown"
    match = app_export._category_matcher(app_export.load_categories())
    assert match("81111508") == "it-services"  # 8111 beats 81
    assert match("81101500") == "engineering"
    assert match("72141100") == "construction"
    assert match("99999999") == match(None) == "other"


def test_reference_files_are_consistent():
    cats = app_export.load_categories()
    keys = [c["key"] for c in cats]
    assert len(keys) == len(set(keys)) and app_export.OTHER_CATEGORY[0] not in keys
    prefixes = [p for c in cats for p in c["prefixes"]]
    assert len(prefixes) == len(set(prefixes)), "a UNSPSC prefix maps to two categories"
    valid = {r.split(",")[0] for r in warehouse.DEPARTMENTS_CSV.read_text().splitlines()[1:]}
    aliases = app_export.load_aliases()
    assert aliases and set(aliases) <= valid, set(aliases) - valid


def test_partial_fiscal_year_ignores_future_start_dates(tmp_path):
    """Contracts entered ahead of their term carry future start dates; they must
    not make a fiscal year still in progress (or not begun) look complete."""
    out, _ = _build(
        tmp_path,
        extra_purchases=[
            ("F0", "2020-07-01", 1.0),  # FY2021 fully covered from its first day
            ("F1", "2027-06-30", 1.0),  # last day of FY2027 -- a year still running
            ("F2", "2028-06-30", 1.0),  # FY2028 has not started yet
        ],
    )
    fy = {y["fiscal_year"]: y["partial"] for y in _load(out / "departments" / "8660.json")[
        "spend_by_fiscal_year"
    ]}  # fmt: skip
    assert fy == {2021: False, 2027: True, 2028: True}
    # Same rule on the overview trend and every supplier's series (both used
    # to emit FY rows with no partial marker at all).
    ov = {y["fiscal_year"]: y["partial"] for y in _load(out / "insights" / "overview.json")[
        "spend_by_fiscal_year"
    ]}  # fmt: skip
    assert ov == {2021: False, 2027: True, 2028: True}
    acme = next(
        p
        for f in (out / "suppliers").glob("shard-*.json")
        for p in _load(f)["suppliers"].values()
        if p["profile"]["canonical_name"].upper().startswith("ACME")
    )
    assert {y["fiscal_year"]: y["partial"] for y in acme["spend_by_fiscal_year"]} == {
        2021: False,
        2027: True,
        2028: True,
    }


def test_cmas_holder_requires_an_agreement_in_term(tmp_path):
    serve = tmp_path / "s.db"
    con = sqlite3.connect(serve)
    for t in ("gold_supplier_certification", "gold_supplier_cmas"):
        cols = (
            "canonical_id, cert_small_business, cert_micro_business, cert_dvbe, cert_record_count"
            if t == "gold_supplier_certification"
            else "canonical_id, cmas_agreement_count, latest_term_end"
        )
        con.execute(f"CREATE TABLE {t} ({cols})")
    con.executemany(
        "INSERT INTO gold_supplier_cmas VALUES (?, 1, ?)",
        [("LIVE", "2027-01-01"), ("EXPIRED", "2026-01-01")],
    )
    flags = app_export._supplier_flags(con, "2026-07-25")
    assert flags["LIVE"]["cmas_holder"] is True
    assert flags["EXPIRED"]["cmas_holder"] is False
