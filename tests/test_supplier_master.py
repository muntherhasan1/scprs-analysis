"""Offline tests for supplier master-data: normalization, crosswalk, canonical roll-up."""

import sqlite3

from src import model, supplier_master, warehouse


def test_normalize_name():
    # legal suffixes + punctuation collapse so variants of one vendor match
    assert supplier_master.normalize_name("Acme, Inc.") == "ACME"
    assert supplier_master.normalize_name("SURF DEVELOPMENT COMPANY INC") == "SURF DEVELOPMENT"
    assert supplier_master.normalize_name("Politico LLC") == "POLITICO"
    assert supplier_master.normalize_name("T-Mobile USA, Inc.") == "T MOBILE USA"
    assert supplier_master.normalize_name("") == ""
    # guard: an all-suffix name never normalizes to empty
    assert supplier_master.normalize_name("The Co") == "THE CO"


def test_load_master(tmp_path):
    csv_path = tmp_path / "master.csv"
    csv_path.write_text(
        "supplier_id,canonical_id,canonical_name,parent_name,note\n"
        "ID_A,ID_B,NORTH RIDGE CONSULTING,,merge\n"
        "ID_C,,MAXIMUS,Maximus Inc.,parent\n",  # blank canonical_id -> defaults to self
        encoding="utf-8",
    )
    master = supplier_master.load_master(csv_path)
    assert master["ID_A"]["canonical_id"] == "ID_B"
    assert master["ID_C"]["canonical_id"] == "ID_C"  # defaulted to itself
    assert master["ID_C"]["parent_name"] == "Maximus Inc."
    assert master["ID_A"]["parent_name"] is None
    assert supplier_master.load_master(tmp_path / "missing.csv") == {}  # no file -> empty


def test_suggest_merges(tmp_path):
    db = tmp_path / "scprs.db"
    con = model._connect(db)
    model._ensure_schema(con)
    con.executemany(
        "INSERT INTO purchases (business_unit, purchase_document, supplier_id, supplier_name, "
        "grand_total) VALUES ('8660', ?, ?, ?, ?)",
        [
            ("D1", "0000001", "ACME INC", 1000.0),  # same vendor, two ids
            ("D2", "0000002", "ACME", 9000.0),  # higher spend -> canonical
            ("D3", "0000003", "UNIQUE VENDOR LLC", 500.0),  # single id -> not suggested
        ],
    )
    con.commit()
    con.close()

    groups = supplier_master.suggest_merges(db)
    assert len(groups) == 1
    g = groups[0]
    assert g["canonical_id"] == "0000002"  # the higher-spend registration
    assert g["total_value"] == 10000.0
    assert [m["supplier_id"] for m in g["members"]] == ["0000002", "0000001"]  # spend desc


def test_apply_supplier_master_dedup_and_parent():
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE dim_supplier (supplier_key INTEGER, supplier_id TEXT, supplier_name TEXT)"
    )
    con.executemany(
        "INSERT INTO dim_supplier VALUES (?, ?, ?)",
        [
            (1, "ID_A", "NORTH RIDGE CONSULTING"),
            (2, "ID_B", "NORTH RIDGE CONSULTING"),  # canonical (self, no crosswalk row)
            (3, "ID_C", "MAXIMUS HUMAN SERVICES INC"),
            (4, "ID_D", "UNMAPPED VENDOR"),  # absent from crosswalk -> defaults to self
        ],
    )
    master = {
        "ID_A": {
            "canonical_id": "ID_B",
            "canonical_name": "NORTH RIDGE CONSULTING",
            "parent_name": "Parent Co.",
        },
        "ID_C": {
            "canonical_id": "ID_C",
            "canonical_name": "MAXIMUS HUMAN SERVICES INC",
            "parent_name": "Maximus Inc.",
        },
    }
    warehouse._apply_supplier_master(con, master)

    rows = dict(
        (sid, (cid, parent))
        for sid, cid, parent in con.execute(
            "SELECT supplier_id, canonical_id, parent_name FROM dim_supplier"
        )
    )
    # both North Ridge ids collapse to the canonical ID_B
    assert rows["ID_A"][0] == "ID_B"
    assert rows["ID_B"][0] == "ID_B"
    # parent propagates to the sibling registration that had no crosswalk row
    assert rows["ID_A"][1] == "Parent Co."
    assert rows["ID_B"][1] == "Parent Co."
    # tagged single-id vendor keeps its parent; unmapped vendor is its own canonical, no parent
    assert rows["ID_C"] == ("ID_C", "Maximus Inc.")
    assert rows["ID_D"] == ("ID_D", None)
