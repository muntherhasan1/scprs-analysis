"""The vendor web app's static JSON export (src/app_export.py): the contract's
grain rules, null-not-zero for unloaded departments, and the public-slice
exclusions."""

import json
import sqlite3

import pytest

from src import app_export, warehouse
from tests.test_warehouse import _seed_cmas, _seed_eprocure, _seed_source


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _build(tmp_path, extra_purchases=(), cmas=None, titles=None, source_sql=(), details=()):
    src, wh = tmp_path / "scprs.db", tmp_path / "warehouse.db"
    serve, epr = tmp_path / "warehouse-serve.db", tmp_path / "eprocure.db"
    _seed_source(src)  # BU 8660: doc A (v1 100 -> v2 150, Acme/S1), doc B (5000, Beta/S2)
    if titles:  # {purchase_document: first_item_title}, set on every version
        con = sqlite3.connect(src)
        con.executemany(
            "UPDATE purchases SET first_item_title = ? WHERE purchase_document = ?",
            [(t, doc) for doc, t in titles.items()],
        )
        con.commit()
        con.close()
    if extra_purchases:  # (purchase_document, start_date, grand_total[, business_unit])
        con = sqlite3.connect(src)
        con.executemany(
            "INSERT INTO purchases (business_unit, purchase_document, version, grand_total, "
            "start_date, acquisition_type_sub_type, acquisition_method, supplier_id, "
            "supplier_name, status, department_name) VALUES (?, ?, '1', ?, ?, "
            "'IT Goods', 'Formal - COMPETITIVE', 'S1', 'Acme', 'Active', ?)",
            [
                (bu, doc, total, start, {"8660": "PUC", "2660": "Caltrans"}[bu])
                for doc, start, total, bu in (
                    (*p, "8660") if len(p) == 3 else p for p in extra_purchases
                )
            ],
        )
        con.commit()
        con.close()
    if source_sql:  # [(sql, [params, ...])]: edits to the seeded source before the build
        con = sqlite3.connect(src)
        for sql, rows in source_sql:
            con.executemany(sql, rows or [()])
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
    if details:  # [(department_name, event_id, business_unit, raw detail-page read)]
        from src import eprocure

        eprocure._ensure_detail_tables(con)
        for dept, eid, bu, raw in details:
            eprocure.write_event_detail(
                con, dept, eid, bu, eprocure.parse_event_detail(raw), "2026-07-25T06:00:00+00:00"
            )
    con.commit()
    con.close()
    warehouse.build_all(
        wh_path=wh,
        source_path=src,
        enrichment_db=tmp_path / "no_enrich.db",
        cmas_db=cmas or tmp_path / "missing_cmas.db",
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
        cmas_path=cmas or tmp_path / "missing_cmas.db",
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
    # A direct Cal eProcure link needs the business unit; no match, no link.
    assert puc["eprocure_url"] == "https://caleprocure.ca.gov/event/8660/26-001"
    assert daa["eprocure_url"] is None

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
    assert puc_page["open_opportunities"][0]["eprocure_url"] == puc["eprocure_url"]
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
            ("F3", "2020-08-01", 1.0, "2660"),  # a second department buying in FY2021
            ("F4", "2020-09-01", 1.0),  # ...and a second FY2021 contract for 8660
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
    # Insight pages too (they used to carry neither marker): a category or
    # acquisition trend must not read a year in progress, or a year fewer
    # departments reported, as the market shrinking.
    it_goods = _load(out / "insights" / "acquisition" / "it-goods.json")["spend_by_fiscal_year"]
    assert {y["fiscal_year"]: y["partial"] for y in it_goods} == {
        2021: False,
        2027: True,
        2028: True,
    }
    assert {y["fiscal_year"]: y["department_count"] for y in it_goods} == {
        2021: 2,  # distinct departments (8660 twice, 2660 once), not contracts (3)
        2027: 1,
        2028: 1,
    }
    for page in (out / "insights").glob("*/*.json"):
        for row in _load(page)["spend_by_fiscal_year"]:
            assert {"partial", "department_count"} <= row.keys(), page


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


def test_cmas_tab_lists_every_agreement_with_facets(tmp_path):
    """cmas/agreements.json carries ALL agreements (matched to a SCPRS supplier or
    not), derived status/enterprise flags/code families for client-side filters,
    a supplier-page link when matched, and never the contact columns."""
    cmas = tmp_path / "cmas.db"
    _seed_cmas(
        cmas,
        [
            ("3-01-01-1001", "Acme, Inc.", "SB", "2030-01-01"),  # matches supplier Acme
            ("3-01-01-1002", "Zeta Unrelated Corp.", "NA", "2026-01-01"),  # expired, unmatched
        ],
    )
    con = sqlite3.connect(cmas)
    con.execute(
        'UPDATE "Approved_Applications" SET select_applicable_product_service_codes = '
        "'IT CONSULT-SYSTEM MGMT, IT CONSULT-PROGRAMMING, SECURITY-SMART CARDS, BRAND-CISCO', "
        "contact_email = 'person@example.com', contact_name = 'A Person', "
        "\"State or Country\" = 'California' WHERE \"CMAS Agreement Number\" = '3-01-01-1001'"
    )
    con.execute("CREATE TABLE extract_meta (extracted_at TEXT)")
    con.execute("INSERT INTO extract_meta VALUES ('2026-07-24T06:40:00+00:00')")
    con.commit()
    con.close()
    out, counts = _build(tmp_path, cmas=cmas)

    assert _load(out / "meta.json")["cmas_as_of"] == "2026-07-24"
    page = _load(out / "cmas" / "agreements.json")
    assert counts["cmas_agreements"] == page["total_count"] == 2
    assert page["active_count"] == 1 and page["matched_to_supplier_count"] == 1
    acme, zeta = page["items"]
    assert acme["status"] == "active" and zeta["status"] == "expired"
    assert (acme["enterprise_type"], acme["small_business"], acme["dvbe"]) == ("SB", True, False)
    assert zeta["enterprise_type"] is None  # "NA" = no SB/DVBE certification
    assert acme["categories"] == ["IT CONSULT", "SECURITY"]  # BRAND-* is not a family
    assert acme["brands"] == ["CISCO"]
    assert len(acme["product_service_codes"]) == 4
    assert acme["supplier"]["canonical_name"] and zeta["supplier"] is None
    assert acme["supplier"]["shard"] == app_export._shard(acme["supplier"]["canonical_id"])
    facets = page["facets"]
    assert {"value": "IT CONSULT", "count": 1} in facets["category"]
    assert facets["state"] == [{"value": "California", "count": 1}]
    assert facets["brand"] == [{"value": "CISCO", "count": 1}]
    assert {f["value"] for f in facets["enterprise_type"]} == {"SB", "None"}
    text = (out / "cmas" / "agreements.json").read_text(encoding="utf-8")
    assert "person@example.com" not in text and "A Person" not in text


def test_no_cmas_store_means_no_cmas_file(tmp_path):
    out, counts = _build(tmp_path)
    assert _load(out / "meta.json")["cmas_as_of"] is None
    assert counts["cmas_agreements"] == 0


def test_explore_cube_ties_to_gold_and_lists_large_contracts(tmp_path):
    """explore/cube.json sums to gold_document (FY2021-22 on) to the cent; contracts of
    EXPLORE_LIST_MIN or more are listed per department with their award family and class."""
    out, counts = _build(
        tmp_path,
        extra_purchases=[("BIG", "2023-08-01", 250_000.0), ("SMALL", "2023-09-01", 5_000.0)],
    )
    cube = _load(out / "explore" / "cube.json")
    col = cube["columns"]
    con = sqlite3.connect(tmp_path / "warehouse-serve.db")
    n, v = con.execute(
        "SELECT COUNT(*), SUM(grand_total) FROM gold_document WHERE fiscal_year >= ?",
        (app_export.EXPLORE_FROM_FY,),
    ).fetchone()
    assert (sum(col["count"]), round(sum(col["value"]), 2)) == (n, round(v, 2)) == (2, 255_000.0)
    assert len({len(c) for c in col.values()}) == 1  # columnar arrays line up
    fam = cube["dimensions"]["family"][col["family"][0]]
    assert (fam["label"], fam["award_class"]) == ("Formal competitive", "C")
    items = _load(out / "explore" / "contracts" / "8660.json")["items"]
    assert [i["purchase_document"] for i in items] == ["BIG"]  # SMALL is counted, not listed
    assert items[0]["scm_code"] == "002" and items[0]["flag"] is None
    assert counts["unmapped_award_methods"] == 0
    meta = _load(out / "meta.json")
    assert meta["explore"]["beta"] is True and "not payments" in meta["coverage"]["note"]

    # A reviewed outlier's override moves it to class X under its own family.
    flags = {("8660", "BIG"): {"award_class": "X", "flag": "pass-through", "note": "n"}}
    con.row_factory = None
    dept_index = _load(out / "departments" / "index.json")["items"]
    methods = app_export.load_award_methods()
    cube2, contracts, cov = app_export._explore(con, dept_index, methods, flags)
    big = contracts["8660"][0]
    assert cube2["dimensions"]["family"][big["family"]]["award_class"] == "X"
    assert big["flag"]["flag"] == "pass-through" and cov["non_procurement_value"] == 250_000.0

    # A quarantined value (class Q) is in no Explore total and no listing.
    flags = {("8660", "BIG"): {"award_class": "Q", "flag": "value quarantined", "note": "n"}}
    cube3, contracts3, cov3 = app_export._explore(con, dept_index, methods, flags)
    assert sum(cube3["columns"]["value"]) == 5_000.0 and "8660" not in contracts3
    assert cov3["quarantined_value"] == 250_000.0 and cov3["non_procurement_value"] == 0.0


def test_award_methods_reference_is_well_formed():
    """Every governed mapping row has a known class and a 3-digit SCM code (or none)."""
    rows = app_export.load_award_methods()
    assert rows["Fair and Reasonable - COMPETITIVE"]["award_class"] == "N"  # SCM 15-01 code 012
    assert rows["CMAS (requires offers)"]["award_class"] == "L"
    for method, r in rows.items():
        assert r["award_class"] in app_export.AWARD_CLASSES, method
        assert r["scm_code"] == "" or (len(r["scm_code"]) == 3 and r["scm_code"].isdigit()), method


def test_contract_flags_reference_is_well_formed():
    """Every reviewed override moves a contract out of the totals (X or Q), says why, and cites
    a public source; no contract is flagged twice."""
    flags = app_export.load_contract_flags()
    assert flags
    for key, r in flags.items():
        assert r["award_class"] in {"X", "Q"}, key
        assert r["flag"] and "http" in r["note"], key
        assert r["reviewed"][:2] == "20", key


def test_payments_coverage_splits_checkable_and_deferred_departments():
    deferred = {
        "2660": {
            "department_name": "Department of Transportation",
            "expected_completion": "2028-07",
        }
    }
    cov = app_export.payments_coverage({"3540": 70.0, "2660": 30.0}, deferred)
    assert (cov["departments_checkable"], cov["departments_loaded"]) == (1, 2)
    assert cov["value_share_checkable"] == pytest.approx(0.7)
    assert [d["business_unit"] for d in cov["not_on_fiscal"]] == ["2660"]
    assert app_export.payments_coverage({}, deferred)["value_share_checkable"] is None


def test_department_pages_use_procurement_totals_and_governed_shares(tmp_path, monkeypatch):
    """Totals and shares exclude class X (interagency, assistance, leases), which is reported
    beside them, and class Q (a reviewed implausible value), which is listed with its reason;
    competitive share = C / (C + L + N) with Fair and Reasonable as N."""
    flags = tmp_path / "flags.csv"
    flags.write_text(
        "business_unit,purchase_document,award_class,flag,note,reviewed\n"
        "8660,BAD,Q,value quarantined: implausible registered value,n,2026-10-31\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(warehouse, "CONTRACT_FLAGS_CSV", flags)
    src, wh = tmp_path / "scprs.db", tmp_path / "warehouse.db"
    serve, out = tmp_path / "warehouse-serve.db", tmp_path / "app"
    _seed_source(src)  # 8660: A (Formal -> C, current v2 = 150), B (NCB -> N, 5000)
    con = sqlite3.connect(src)
    con.executemany(
        "INSERT INTO purchases (business_unit, purchase_document, version, grand_total, "
        "start_date, acquisition_type_sub_type, acquisition_method, supplier_id, supplier_name, "
        "status, department_name) VALUES ('8660', ?, '1', ?, '2023-08-01', 'NON-IT Services', ?, "
        "'S1', 'Acme', 'Active', 'PUC')",
        [
            ("FR", 850.0, "Fair and Reasonable - COMPETITIVE"),  # N
            ("CM", 4000.0, "CMAS (requires offers)"),  # L
            ("IA", 1_000_000.0, "Exempt by Law - Interagency Agreement - NON-COMPETITIVELY BID"),
            ("BAD", 9e9, "Exempt by Law - Other (buyer must indicate law) - NON-COMPETITIVELY BID"),
        ],
    )
    con.commit()
    con.close()
    warehouse.build_all(
        wh_path=wh,
        source_path=src,
        enrichment_db=tmp_path / "e.db",
        cmas_db=tmp_path / "c.db",
        log=lambda *a: None,
    )
    warehouse.export_serve_db(wh_path=wh, serve_path=serve, log=lambda *a: None)
    app_export.export_app(
        serve_path=serve,
        app_dir=out,
        wh_path=wh,
        eprocure_path=tmp_path / "none.db",
        cmas_path=tmp_path / "none.db",
        today="2026-07-25",
        log=lambda *a: None,
    )
    page = _load(out / "departments" / "8660.json")
    # C = 150, L = 4000, N = 5000 + 850 -> procurement 10,000; the $1M interagency is X.
    assert page["total_value"] == 10_000
    assert page["competitive_share_pct"] == 1.5
    assert page["leveraged_share_pct"] == 40.0 and page["noncompetitive_share_pct"] == 58.5
    assert page["non_procurement"] == {"document_count": 1, "total_value": 1_000_000.0}
    (bad,) = page["quarantined"]
    assert (bad["purchase_document"], bad["registered_value"]) == ("BAD", 9e9)
    assert bad["reason"].startswith("value quarantined")
    flags = {m["award_class"]: m["competitive_flag"] for m in page["acquisition_mix"]}
    assert flags == {"C": "Competitive", "L": "Leveraged", "N": "Non-Competitive"}
    assert sum(y["total_value"] for y in page["spend_by_fiscal_year"]) == 10_000


def test_top_suppliers_carry_per_year_value_and_share(tmp_path):
    """Who is gaining: each top supplier's value and share of the page per fiscal
    year. Where every supplier is listed, the years tie to the page's trend."""
    out, _ = _build(
        tmp_path,
        extra_purchases=[("F0", "2020-07-01", 1.0), ("F1", "2022-08-01", 3.0)],
    )
    checked = 0
    for path in (out / "insights").glob("*/*.json"):
        page = _load(path)
        if page["top_suppliers_total_count"] > len(page["top_suppliers"]):
            continue  # a cut list can't tie out
        for fy in page["spend_by_fiscal_year"]:
            rows = [
                r
                for s in page["top_suppliers"]
                for r in s["by_fiscal_year"]
                if r["fiscal_year"] == fy["fiscal_year"]
            ]
            assert round(sum(r["total_value"] for r in rows), 2) == fy["total_value"], path
            if fy["total_value"] > 0:  # no share of a net-credit or $0 year
                assert abs(sum(r["share_pct"] for r in rows) - 100) < 0.05, path
            checked += 1
    assert checked
    # Under $10K holds doc A (Acme, 150), doc B (Beta, 5000), F0 (Acme, 1) in FY2021
    # and F1 (Acme, 3) in FY2023: Beta's FY2021 share is 5000 / 5151.
    small = _load(out / "insights" / "size" / "under-10k.json")
    beta = next(s for s in small["top_suppliers"] if s["canonical_name"].upper() == "BETA")
    assert beta["by_fiscal_year"] == [
        {"fiscal_year": 2021, "total_value": 5000.0, "share_pct": round(100 * 5000 / 5151, 2)}
    ]
    acme = next(s for s in small["top_suppliers"] if s["canonical_name"].upper().startswith("ACME"))
    assert [(r["fiscal_year"], r["share_pct"]) for r in acme["by_fiscal_year"]] == [
        (2021, round(100 * 151 / 5151, 2)),
        (2023, 100.0),
    ]


def test_supplier_recent_contracts_say_what_and_for_whom(tmp_path):
    """A competitor's recent contracts carry the SCPRS first-item title and the
    buying department's name, not just a document id and a business-unit code."""
    out, _ = _build(tmp_path, titles={"A": "Rate case analytics software"})
    acme = next(
        p
        for f in (out / "suppliers").glob("shard-*.json")
        for p in _load(f)["suppliers"].values()
        if p["profile"]["canonical_name"].upper().startswith("ACME")
    )
    doc_a = next(c for c in acme["recent_contracts"] if c["purchase_document"] == "A")
    assert doc_a["title"] == "Rate case analytics software"
    # The department dimension's name, not the raw "PUC" on the source row.
    assert doc_a["department_name"] == "Public Utilities Commission"
    assert doc_a["business_unit"] == "8660"


def test_no_supplier_share_of_a_net_credit_year(tmp_path):
    """A year whose page total is net credits (real: Waste & land FY2016,
    -$2,898) gets no per-year share: a value over a negative total would read as
    a share with the wrong sign."""
    out, _ = _build(
        tmp_path,
        extra_purchases=[("C1", "2024-08-01", -50.0), ("C2", "2024-09-01", 10.0)],
    )
    page = _load(out / "insights" / "acquisition" / "it-goods.json")
    assert {y["fiscal_year"]: y["total_value"] for y in page["spend_by_fiscal_year"]} == {
        2025: -40.0
    }
    (acme,) = page["top_suppliers"]
    assert acme["by_fiscal_year"] == [
        {"fiscal_year": 2025, "total_value": -40.0, "share_pct": None}
    ]


@pytest.mark.parametrize(
    ("column", "field"), [("first_item_title", "title"), ("end_date", "end_date")]
)
def test_export_survives_a_serve_db_published_before_a_column(tmp_path, column, field):
    """The refresh exports from whatever serve DB is live. One built before
    first_item_title (2026-10) or end_date (2026-11) existed must still export,
    with that field null and no recompetes list without end dates, not fail the
    run."""
    _build(
        tmp_path,
        titles={"A": "Rate case analytics software"},
        source_sql=[
            ("UPDATE purchases SET end_date = '2027-01-31' WHERE purchase_document = 'A'", [])
        ],
    )
    serve = tmp_path / "warehouse-serve.db"
    con = sqlite3.connect(serve)
    (sql,) = con.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'gold_document'"
    ).fetchone()
    cols = [r[1] for r in con.execute("PRAGMA table_info(gold_document)")]
    assert column in cols
    con.executescript(
        f"CREATE VIEW old_gold_document AS {sql.split(' AS ', 1)[1]};"  # noqa: S608 — the DB's own view SQL
        "DROP VIEW gold_document;"
        "CREATE VIEW gold_document AS SELECT "
        + ", ".join(c for c in cols if c != column)
        + " FROM old_gold_document;"
    )
    con.close()
    out = tmp_path / "app-old-serve"
    app_export.export_app(
        serve_path=serve,
        app_dir=out,
        wh_path=tmp_path / "warehouse.db",
        eprocure_path=tmp_path / "eprocure.db",
        cmas_path=tmp_path / "missing_cmas.db",
        today="2026-07-25",
        log=lambda *a: None,
    )
    acme = next(
        p
        for f in (out / "suppliers").glob("shard-*.json")
        for p in _load(f)["suppliers"].values()
        if p["profile"]["canonical_name"].upper().startswith("ACME")
    )
    doc_a = next(c for c in acme["recent_contracts"] if c["purchase_document"] == "A")
    assert doc_a[field] is None
    assert doc_a["department_name"] == "Public Utilities Commission"
    assert (out / "recompetes.json").exists() == (column != "end_date")


def test_recompetes_list_active_contracts_ending_within_a_year(tmp_path):
    """recompetes.json: active procurement contracts of $100K or more whose term
    ends in the next 365 days, soonest first. Funding records, closed or small
    contracts and ends outside the window stay out; the coverage figure says how
    many of the comparable active contracts list an end date at all."""
    big = 250_000.0
    ends = {  # export date 2026-07-25 -> window to 2027-07-25
        "R1": "2026-09-30",  # in
        "R2": "2026-08-01",  # in, first (soonest)
        "R3": "2028-01-01",  # past the window
        "R4": "2026-06-30",  # already ended, still Active
        "R5": "2026-10-01",  # under the minimum value
        "R6": "2026-11-01",  # Closed
        "R7": "2026-12-01",  # Encumbrance Only: a funding record
    }  # R8: no end date on record
    out, _ = _build(
        tmp_path,
        extra_purchases=[
            ("R1", "2025-01-15", big),
            ("R2", "2025-02-01", 400_000.0),
            ("R3", "2025-03-01", big),
            ("R4", "2024-01-01", big),
            ("R5", "2025-01-01", 50_000.0),
            ("R6", "2025-01-01", big),
            ("R7", "2025-01-01", big),
            ("R8", "2025-01-01", big),
        ],
        source_sql=[
            (
                "UPDATE purchases SET end_date = ? WHERE purchase_document = ?",
                [(end, doc) for doc, end in ends.items()],
            ),
            ("UPDATE purchases SET status = 'Closed' WHERE purchase_document = 'R6'", []),
            (
                "UPDATE purchases SET acquisition_type_sub_type = 'Encumbrance Only' "
                "WHERE purchase_document = 'R7'",
                [],
            ),
        ],
    )
    rc = _load(out / "recompetes.json")
    assert [i["purchase_document"] for i in rc["items"]] == ["R2", "R1"]
    assert (rc["window_from"], rc["window_to"], rc["min_value"]) == (
        "2026-07-25",
        "2027-07-25",
        100_000.0,
    )
    assert (rc["total_count"], rc["total_value"]) == (2, 650_000.0)
    # Active, $100K+, not a funding record: R1-R4 and R8; four list an end date.
    assert (rc["active_contract_count"], rc["end_date_coverage_pct"]) == (5, 80.0)
    first = rc["items"][0]
    assert first["end_date"] == "2026-08-01" and first["grand_total"] == 400_000.0
    assert first["department_name"] == "Public Utilities Commission"
    assert first["canonical_name"].upper().startswith("ACME")
    assert first["supplier_kind"] == "company" and first["award_class"] == "C"
    assert {"sb_certified", "dvbe_certified", "cmas_holder", "title"} <= first.keys()
    # The same end dates ride on the incumbent's recent contracts.
    acme = next(
        p
        for f in (out / "suppliers").glob("shard-*.json")
        for p in _load(f)["suppliers"].values()
        if p["profile"]["canonical_name"].upper().startswith("ACME")
    )
    recent = {c["purchase_document"]: c["end_date"] for c in acme["recent_contracts"]}
    assert recent["R2"] == "2026-08-01" and recent["R8"] is None


def test_eprocure_url_shapes():
    """/event/{bu}/{event id}, confirmed in a browser for every ID shape posted
    on 2026-11-15; a space inside an ID ("IFB 2175") is percent-encoded."""
    url = app_export._eprocure_url
    assert url("8955", "0000040541") == "https://caleprocure.ca.gov/event/8955/0000040541"
    assert url("2660", "11A4467") == "https://caleprocure.ca.gov/event/2660/11A4467"
    assert url("4300", "IFB 2175") == "https://caleprocure.ca.gov/event/4300/IFB%202175"
    assert url("4300", " 26-165 ") == "https://caleprocure.ca.gov/event/4300/26-165"
    assert url(None, "26-001") is None
    assert url("8660", "  ") is None


def test_open_bids_carry_detail_pages_and_filter_counts(tmp_path):
    """With a bid's detail page read: type of work (UNSPSC codes -> product
    categories and spelled-out work types), license classes, counties, the
    pre-bid conference and the published date, plus precomputed filter counts.
    A bid not read yet keeps empty detail fields."""
    raw = {
        "format1": "Sell Event",
        "format2": "RFx",
        "published": "07/01/2026 9:00AM PDT",
        "version": "2",
        "conference": "Mandatory",
        "conf_date": "07/20/2026",
        "conf_time": "10:00AM",
        "conf_location": "505 Van Ness Ave",
        "conf_comments": "Attendance is required to bid.",
        "description": "Rate case support. " + "x" * 800,
        "unspsc": ["81112200", "72102100"],
        "unspsc_desc": [
            "Computer services - Software maintenance & support",
            "Building facility construction & maint srvcs - Pest control",
        ],
        "license": ["C-20"],
        "license_desc": ["Warm-Air Heating Contractor"],
        "area_id": ["038"],
        "county": ["San Francisco"],
    }
    out, _ = _build(tmp_path, details=[("Public Utilities Commission", "26-001", "8660", raw)])
    doc = _load(out / "opportunities.json")
    assert doc["details_read_count"] == 1
    bids = {o["event_id"]: o for o in doc["items"]}
    puc = bids["26-001"]
    assert puc["details_read"] is True and puc["published_date"] == "2026-07-01"
    assert puc["amended"] is True  # event version 2
    assert puc["categories"] == ["it-services", "construction"]
    assert puc["work_types"] == ["Software maintenance & support", "Pest control"]
    assert puc["unspsc"][1] == {
        "code": "72102100",
        "description": "Building facility construction & maint srvcs - Pest control",
    }
    assert puc["licenses"] == [{"code": "C-20", "description": "Warm-Air Heating Contractor"}]
    assert puc["service_area"] == ["San Francisco"]
    assert puc["prebid_conference"] == {
        "mandatory": True,
        "date": "2026-07-20",
        "time": "10:00AM",
        "location": "505 Van Ness Ave",
        "comments": "Attendance is required to bid.",
    }
    assert len(puc["description"]) == app_export.BID_DESCRIPTION_MAX + 1  # cut, with "…"
    # Not read yet: empty, never guessed.
    daa = bids["26-002"]
    assert daa["details_read"] is False and daa["prebid_conference"] is None
    assert daa["categories"] == daa["work_types"] == daa["licenses"] == daa["service_area"] == []
    assert daa["amended"] is None
    # The department page and dashboard reuse the same bid rows.
    page = _load(out / "departments" / "8660.json")
    assert page["open_opportunities"][0]["work_types"] == puc["work_types"]
    f = doc["facets"]
    assert f["categories"][0]["bid_count"] == 1 and {c["key"] for c in f["categories"]} == {
        "it-services",
        "construction",
    }
    assert {"label": "Pest control", "bid_count": 1} in f["work_types"]
    assert f["licenses"] == [
        {"code": "C-20", "description": "Warm-Air Heating Contractor", "bid_count": 1}
    ]
    assert f["counties"] == [{"county": "San Francisco", "bid_count": 1}]
    assert f["mandatory_prebid_count"] == 1


def test_bid_type_and_work_type_wording():
    assert app_export._bid_type("RFQ #2026061 - SB Option- 3DATX Mini PEMS", 0) == "RFQ"
    assert app_export._bid_type("IFB 2175", 0) == "IFB"
    assert app_export._bid_type("RFP-IT-2026-214-RB-LAN WAN MSA", 0) == "RFP"
    assert app_export._bid_type("CSU Preferred Lender RFI", 0) == "RFI"
    assert app_export._bid_type("Renewable Diesel", 1) == "RFI"  # Format/Type said so
    assert app_export._bid_type("Janitorial Services", 0) is None  # never guessed
    assert app_export._bid_type("Sprifq sprinklers", 0) is None  # whole tokens only
    assert (
        app_export._work_type(
            "Special trade construction & maint srvcs - Floor laying - Resilient floor laying"
        )
        == "Floor laying"
    )
    assert (
        app_export._work_type("Building facility maintenance & repair services")
        == "Building facility maintenance & repair services"
    )
    assert app_export._work_type("Heavy equip maint srvcs - Repair") == "Repair"
    assert app_export._work_type("Facility maint srvcs") == "Facility maintenance services"
    assert app_export._work_type(None) is None
