"""Superseded registrations (src/registrations.py): one agreement stored under a legacy number
and again under its department-prefixed re-registration is counted once."""

from src import registrations


def _row(doc, supplier="KERN COUNTY FIRE DEPT", start="2017-07-01", bu="3540"):
    return {
        "business_unit": bu,
        "purchase_document": doc,
        "supplier_name": supplier,
        "start_date": start,
    }


def test_prefixed_re_registration_supersedes_the_legacy_record():
    rows = [
        _row("7CA03777"),
        _row("35407CA03777", supplier="Kern County Fire Dept."),  # normalized names match
        _row("7CA03780", supplier="ORANGE COUNTY FIRE AUTHORITY"),
        _row("35407CA03780", supplier="COUNTY OF ORANGE HEALTH"),  # same number, other supplier
        _row("35407CA03780FY2018", supplier="ORANGE COUNTY FIRE AUTHORITY"),  # FY suffix
        _row("7CA04121", supplier="10 TANKER"),  # its re-registration's start moved:
        _row("35407CA04121", supplier="10 TANKER", start="2018-07-17"),  # pragma: allowlist secret
        _row("123", supplier="A"),
        _row("3540123", supplier="A"),  # legacy number too short to pair on
        _row("7CA09999", bu="8660"),
        _row("35407CA09999", bu="8660"),  # prefix must be the document's own unit
    ]
    pairs = {
        p["purchase_document"]: p["superseded_by"] for p in registrations.superseded_pairs(rows, [])
    }
    assert pairs == {"7CA03777": "35407CA03777", "7CA03780": "35407CA03780FY2018"}


def test_reviewed_pairs_apply_only_when_both_documents_exist():
    rows = [
        _row("7CA03779", supplier="CNTY OF SANTA BARBARA FIRE DEP"),
        _row("35407CA03779", supplier="COUNTY OF SANTA BARBARA"),
    ]
    reviewed = [
        {
            "business_unit": "3540",
            "purchase_document": "7CA03779",
            "superseded_by": "35407CA03779",
            "evidence": "same agreement",
        },
        {
            "business_unit": "3540",
            "purchase_document": "GONE",
            "superseded_by": "35407CA03779",
            "evidence": "x",
        },
    ]
    assert registrations.superseded_pairs(rows, []) == []  # names differ: the rule alone misses it
    (pair,) = registrations.superseded_pairs(rows, reviewed)
    assert pair["purchase_document"] == "7CA03779" and pair["rule"] == "reviewed: same agreement"


def test_a_superseding_document_is_never_itself_dropped():
    """A chain (X1 -> 7CA1000 by review, 7CA1000 -> 35407CA1000 by rule) resolves to its last
    link, so only 35407CA1000 is counted; a cycle drops nothing rather than everything."""
    rows = [_row("7CA1000"), _row("35407CA1000"), _row("X1")]
    reviewed = [
        {
            "business_unit": "3540",
            "purchase_document": "X1",
            "superseded_by": "7CA1000",
            "evidence": "e",
        }
    ]
    pairs = {
        p["purchase_document"]: p["superseded_by"]
        for p in registrations.superseded_pairs(rows, reviewed)
    }
    assert pairs == {"7CA1000": "35407CA1000", "X1": "35407CA1000"}
    cycle = [
        {"business_unit": "3540", "purchase_document": a, "superseded_by": b, "evidence": "e"}
        for a, b in (("P", "Q"), ("Q", "P"))
    ]
    assert registrations.superseded_pairs([_row("P"), _row("Q", supplier="other")], cycle) == []


def test_governed_review_file_loads():
    assert isinstance(registrations.load_reviewed(), list)
