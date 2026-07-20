"""Offline tests for the CMAS Power BI extractor.

The network-facing session/query is not exercised here (no live calls in CI);
these lock down the load-bearing, tricky part — decoding Power BI's compressed
DSR wire format — using fixtures built from real captured response shapes.
"""

import sqlite3

from src import cmas


def _dsr(schema, dm0, value_dicts=None, restart=None):
    ds = {"PH": [{"DM0": dm0}]}
    if value_dicts:
        ds["ValueDicts"] = value_dicts
    if restart is not None:
        ds["RT"] = restart
    return {"DS": [ds]}


def test_decode_inline_rows():
    """Small results skip the C array and key values under Gn (SB/DVBE shape)."""
    dsr = _dsr(
        None,
        [{"S": [{"N": "G0", "T": 1}], "G0": "Small Business (SB)"}],
    )
    rows, restart = cmas._decode_dsr(dsr, ["SB"])
    assert rows == [{"SB": "Small Business (SB)"}]
    assert restart is None


def test_decode_value_dictionaries():
    """Dictionary columns send an integer index into ValueDicts[Dn]."""
    schema = [{"N": "G0", "T": 1, "DN": "D0"}, {"N": "G1", "T": 1, "DN": "D1"}]
    dm0 = [
        {"S": schema, "C": [0, 0]},
        {"C": [1, 1]},
    ]
    dsr = _dsr(schema, dm0, value_dicts={"D0": ["ACME", "BETA"], "D1": ["X", "Y"]})
    rows, _ = cmas._decode_dsr(dsr, ["name", "code"])
    assert rows == [{"name": "ACME", "code": "X"}, {"name": "BETA", "code": "Y"}]


def test_decode_repeat_bitmask():
    """R bit set => that column repeats the previous row and is omitted from C."""
    schema = [{"N": "G0", "T": 1}, {"N": "G1", "T": 1}, {"N": "G2", "T": 1}]
    dm0 = [
        {"S": schema, "C": ["a", "b", "c"]},
        # R=2 (binary 010) => column 1 repeats "b"; C carries only cols 0 and 2.
        {"C": ["a2", "c2"], "R": 2},
    ]
    rows, _ = cmas._decode_dsr(_dsr(schema, dm0), ["x", "y", "z"])
    assert rows[1] == {"x": "a2", "y": "b", "z": "c2"}


def test_decode_null_bitmask():
    """Ø bit set => that column is null and omitted from C."""
    schema = [{"N": "G0", "T": 1}, {"N": "G1", "T": 1}]
    # Ø=2 (binary 10) => column 1 is null; C carries only column 0.
    dm0 = [{"S": schema, "C": ["only"], "Ø": 2}]
    rows, _ = cmas._decode_dsr(_dsr(schema, dm0), ["a", "b"])
    assert rows == [{"a": "only", "b": None}]


def test_decode_datetime_column():
    """Type-7 columns arrive as epoch-ms and render as ISO dates."""
    schema = [{"N": "G0", "T": 7}]
    dm0 = [{"S": schema, "C": [1753833600000]}]
    rows, _ = cmas._decode_dsr(_dsr(schema, dm0), ["when"])
    assert rows == [{"when": "2025-07-30"}]


def test_decode_returns_restart_tokens():
    schema = [{"N": "G0", "T": 1}]
    dsr = _dsr(schema, [{"S": schema, "C": ["v"]}], restart=[["tok"]])
    _, restart = cmas._decode_dsr(dsr, ["c"])
    assert restart == [["tok"]]


def test_safe_table_name():
    assert cmas._safe_table("CMAS Product and Service Codes") == "CMAS_Product_and_Service_Codes"
    assert cmas._safe_table("Approved_Applications") == "Approved_Applications"


def test_scalar_serializes_complex():
    assert cmas._scalar(["a", "b"]) == '["a", "b"]'
    assert cmas._scalar("plain") == "plain"
    assert cmas._scalar(None) is None


def test_write_sqlite_full_refresh(tmp_path):
    """Writing is an idempotent drop+recreate; odd column names survive."""
    con = sqlite3.connect(tmp_path / "cmas.db")
    cols = ["CMAS Agreement Number", "Contractor Name"]
    rows = [{"CMAS Agreement Number": "3-26-04-1048", "Contractor Name": "HCI Systems, Inc."}]
    cmas._write_sqlite(con, "Approved_Applications", cols, rows)
    cmas._write_sqlite(con, "Approved_Applications", cols, rows)  # again -> no dup
    n = con.execute('SELECT COUNT(*) FROM "Approved_Applications"').fetchone()[0]
    assert n == 1
    got = con.execute('SELECT "Contractor Name" FROM "Approved_Applications"').fetchone()[0]
    assert got == "HCI Systems, Inc."
    con.close()


def test_write_csv(tmp_path):
    cols = ["a", "b"]
    rows = [{"a": "1", "b": "2"}, {"a": "3", "b": None}]
    path = cmas._write_csv(tmp_path, "Some Entity", cols, rows)
    assert path.name == "cmas_Some_Entity.csv"
    text = path.read_text(encoding="utf-8")
    assert "a,b" in text and "1,2" in text
