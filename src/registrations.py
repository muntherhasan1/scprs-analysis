"""Superseded registrations: one agreement stored in SCPRS under two document numbers.

CAL FIRE re-registered its 2016-2018 agreements under department-prefixed numbers when it
converted to FI$Cal: legacy ``7CA03777`` (version 0, never updated) and ``35407CA03777`` (the
same agreement, amended and extended), both still live in SCPRS. Counting both double-counts
the agreement (2026-10-31: 219 CAL FIRE pairs, $317.7M of legacy value).

The rule (`superseded_pairs`): a document ``L`` is superseded by ``P`` in the same business
unit when ``P`` is the unit code followed by ``L`` (optionally with a trailing ``FY...``
suffix, e.g. ``35407CA03780FY2018``), both start on the same day, and both name the same
supplier (normalized). The prefixed record is kept: it is the one carrying the amendments.
Near-pairs that fail the rule (a supplier spelled two ways, a start day that moved) are added
by hand to ``references/superseded_registrations.csv`` with their evidence.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .supplier_master import normalize_name

REVIEWED_CSV = (
    Path(__file__).resolve().parent.parent / "references" / "superseded_registrations.csv"
)
RULE = "prefixed re-registration: same supplier and start day"
MIN_LEGACY_LENGTH = 4  # a legacy number shorter than this is too generic to pair on
_FY_SUFFIX = re.compile(r"FY\d*$")


def load_reviewed(path: Path | None = None) -> list[dict]:
    """Hand-reviewed pairs: business_unit, purchase_document, superseded_by, evidence, reviewed."""
    path = REVIEWED_CSV if path is None else path
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def superseded_pairs(rows: list[dict], reviewed: list[dict] | None = None) -> list[dict]:
    """Legacy documents superseded by a prefixed re-registration.

    `rows` are current-version documents with business_unit, purchase_document,
    supplier_name and start_date. Returns one dict per superseded document: business_unit,
    purchase_document, superseded_by, rule. A reviewed pair applies only when both of its
    documents are present."""
    idx = {(r["business_unit"], r["purchase_document"]): r for r in rows}
    out: dict[tuple, dict] = {}
    for (bu, doc), p in sorted(idx.items()):
        if not doc.startswith(bu) or len(doc) - len(bu) < MIN_LEGACY_LENGTH:
            continue
        base = doc[len(bu) :]
        for cand in dict.fromkeys([base, _FY_SUFFIX.sub("", base)]):
            legacy = idx.get((bu, cand))
            if (
                legacy is None
                or (bu, cand) in out
                or not p["start_date"]
                or legacy["start_date"] != p["start_date"]
                or normalize_name(legacy["supplier_name"] or "")
                != normalize_name(p["supplier_name"] or "")
            ):
                continue
            out[(bu, cand)] = {
                "business_unit": bu,
                "purchase_document": cand,
                "superseded_by": doc,
                "rule": RULE,
            }
    for r in load_reviewed() if reviewed is None else reviewed:
        key = (r["business_unit"], r["purchase_document"])
        if key in idx and (r["business_unit"], r["superseded_by"]) in idx and key not in out:
            out[key] = {
                "business_unit": r["business_unit"],
                "purchase_document": r["purchase_document"],
                "superseded_by": r["superseded_by"],
                "rule": f"reviewed: {r['evidence']}",
            }
    # A superseding document must itself survive: follow a chain (A -> B by review, B -> C by
    # rule) to its last link, so A and B both point at C and the agreement is counted once.
    for v in out.values():
        seen = {v["purchase_document"]}
        while (v["business_unit"], v["superseded_by"]) in out and v["superseded_by"] not in seen:
            seen.add(v["superseded_by"])
            v["superseded_by"] = out[(v["business_unit"], v["superseded_by"])]["superseded_by"]
    return [
        v for k, v in sorted(out.items()) if (v["business_unit"], v["superseded_by"]) not in out
    ]
