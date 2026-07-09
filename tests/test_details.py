"""Offline tests for the PO Details drill-down parser and loader."""

from src import model, scprs

# Minimal HTML with the drill-down span ids: header + 1 line + 1 associated PO.
SAMPLE = (
    "<html><body>"
    "<span id='ZZ_SCPR_SBP_WRK_BUSINESS_UNIT'>8660</span>"
    "<span id='ZZ_SCPR_SBP_WRK_CRDMEM_ACCT_NBR'>0000000000000000000063626</span>"
    "<span id='ZZ_SCPR_SBP_WRK_ZZ_DGS_BILL_CD'>059000</span>"
    "<span id='ZZ_SCPR_SBP_WRK_AWARDED_AMT'>$482,500.00</span>"
    "<span id='ZZ_SCPR_PDL_DVW_CRDMEM_ACCT_NBR$0'>1</span>"
    "<span id='ZZ_SCPR_PDL_DVW_DESCR254_MIXED$0'>Item\xa0A</span>"
    "<span id='ZZ_SCPR_PDL_DVW_PV_UNSPSC_CODE$0'>43230000</span>"
    "<span id='ZZ_SCPR_PDL_DVW_UNIT_PRICE$0'>$2,500.00</span>"
    "<span id='PO_DETAIL$span$0'>0000003978</span>"
    "<span id='ZZ_SCPR_PHD_DVW_DESCR60$0'>Rose Miramontes</span>"
    "<span id='ZZ_SCPR_PHD_DVW_PO_TOTAL$0'>$245,180.00</span>"
    "</body></html>"
)


def test_parse_po_details():
    header, lines, pos = scprs.parse_po_details(SAMPLE)
    assert header["bill_code"] == "059000"  # absent from both CSV exports
    assert header["business_unit"] == "8660"
    assert len(lines) == 1
    assert lines[0]["item_description"] == "Item A"  # nbsp normalized
    assert lines[0]["unit_price"] == "$2,500.00"
    assert len(pos) == 1
    assert pos[0]["po_id"] == "0000003978"


def test_money_and_iso():
    assert model._money("$245,180.00") == 245180.0
    assert model._money("N/A") is None
    assert model._money(None) is None
    assert model._iso("02/18/2021") == "2021-02-18"
    assert model._iso(None) is None


def test_build_details_db(tmp_path, monkeypatch):
    db = tmp_path / "d.db"
    fixture = [
        {
            "document": "0000000000000000000063626",
            "header": {
                "purchase_document": "0000000000000000000063626",
                "business_unit": "8660",
                "bill_code": "059000",
                "status": "Expired",
                "version": "3",
                "grand_total": "$482,500.00",
                "start_date": "02/18/2021",
            },
            "lines": [{"line_number": "1", "unit_price": "$2,500.00", "unspsc": "43230000"}],
            "pos": [{"po_id": "0000003978", "po_total": "$245,180.00", "start_date": "02/19/2021"}],
        }
    ]
    monkeypatch.setattr(model.scprs, "collect_po_details", lambda *a, **k: fixture)

    counts = model.build_details_db(
        "8660", "02/18/2021", "02/18/2021", db_path=db, log=lambda *a: None
    )
    assert counts == {"documents": 1, "lines": 1, "pos": 1}

    det = model.query("SELECT bill_code, grand_total, start_date FROM document_details", db_path=db)
    assert det["bill_code"][0] == "059000"
    assert det["grand_total"][0] == 482500.0  # parsed to REAL
    assert det["start_date"][0] == "2021-02-18"  # ISO
    po = model.query("SELECT po_total FROM document_pos", db_path=db)
    assert po["po_total"][0] == 245180.0

    # reload replaces (idempotent), no duplicate rows
    model.build_details_db("8660", "02/18/2021", "02/18/2021", db_path=db, log=lambda *a: None)
    assert model.query("SELECT COUNT(*) c FROM document_lines", db_path=db)["c"][0] == 1


def test_document_view(tmp_path):
    db = tmp_path / "doc.db"
    doc_id = "0000000000000000000063626"
    con = model._connect(db)
    model._ensure_details_schema(con)
    con.execute(
        "INSERT INTO document_details (business_unit, purchase_document, bill_code, "
        "merchandise_amount, grand_total) VALUES ('8660', ?, '059000', 482500.0, 482500.0)",
        (doc_id,),
    )
    con.executemany(
        "INSERT INTO document_lines (business_unit, purchase_document, line_number, "
        "unit_price, quantity, item_description) VALUES (?, ?, ?, ?, ?, ?)",
        [("8660", doc_id, "1", 2500.0, 1.0, "A"), ("8660", doc_id, "2", 480000.0, 1.0, "B")],
    )
    con.execute(
        "INSERT INTO document_pos (business_unit, purchase_document, po_id, po_total) "
        "VALUES ('8660', ?, 'P1', 245180.0)",
        (doc_id,),
    )
    con.commit()
    con.close()

    doc = model.document("63626", db_path=db)  # suffix match
    assert doc is not None
    assert doc["header"]["bill_code"] == "059000"
    assert len(doc["lines"]) == 2
    assert len(doc["pos"]) == 1
    # line items reconcile to merchandise amount
    assert (doc["lines"]["unit_price"] * doc["lines"]["quantity"]).sum() == 482500.0
    model._print_document(doc)  # must not raise
    assert model.document("99999", db_path=db) is None


def test_enrich_resume(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    con = model._connect(db)
    model._ensure_schema(con)
    con.executemany(
        "INSERT INTO purchases (business_unit, purchase_document, start_date) VALUES (?, ?, ?)",
        [("8660", "A", "2021-02-18"), ("8660", "B", "2021-02-18"), ("8660", "C", "2021-05-20")],
    )
    con.commit()
    con.close()

    calls = []

    def fake_build(bu, f, t, *, db_path, log=print, **k):
        calls.append((bu, f, t))
        return {"documents": 1, "lines": 2, "pos": 0}

    monkeypatch.setattr(model, "build_details_db", fake_build)

    # first run: two distinct active days (dupe start_date collapses to one)
    r1 = model.enrich_details("8660", "01/01/2021", "12/31/2021", db_path=db, log=lambda *a: None)
    assert r1["days_processed"] == 2
    assert ("8660", "02/18/2021", "02/18/2021") in calls  # ISO -> MM/DD/YYYY
    assert ("8660", "05/20/2021", "05/20/2021") in calls

    # resume: nothing left to do
    r2 = model.enrich_details("8660", "01/01/2021", "12/31/2021", db_path=db, log=lambda *a: None)
    assert r2["days_processed"] == 0

    # force reprocesses
    r3 = model.enrich_details(
        "8660", "01/01/2021", "12/31/2021", db_path=db, force=True, log=lambda *a: None
    )
    assert r3["days_processed"] == 2
    assert model.query("SELECT COUNT(*) c FROM details_progress", db_path=db)["c"][0] == 2
