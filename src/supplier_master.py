"""Supplier master-data management: a canonical vendor crosswalk.

SCPRS assigns a supplier_id per vendor *registration*, so a single real-world
company routinely appears under several ids (e.g. NORTH RIDGE CONSULTING has two;
BETA ALPHA PSI has four). Name-keyed enrichment and per-supplier rollups then
split or double-count that vendor.

This module resolves supplier identities to a **canonical entity**:

- `normalize_name` collapses spelling/legal-suffix noise so variants of the same
  vendor share a key.
- `suggest_merges` scans the operational `purchases` table and proposes groups of
  supplier_ids that look like one entity (same normalized name, >1 id), choosing
  the highest-spend id as the canonical by default.
- The crosswalk itself is curated, version-controlled reference data
  (`references/supplier_master.csv`): one row per supplier_id that needs remapping
  to a `canonical_id` / `canonical_name`, optionally tagged with a `parent_name`
  (e.g. MAXIMUS HUMAN SERVICES INC -> parent "Maximus Inc."). Ids absent from the
  file default to themselves, so the file only carries the exceptions.

The warehouse reads the curated crosswalk to add canonical attributes to
`dim_supplier` and to roll facts up by canonical entity.

CLI:
    python -m src.supplier_master suggest              # print merge candidates
    python -m src.supplier_master suggest --write      # (re)seed the crosswalk CSV
    python -m src.supplier_master info                 # crosswalk coverage summary
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path

from . import scprs

DB_PATH = scprs.DATA_DIR / "scprs.db"
MASTER_CSV = Path(__file__).resolve().parent.parent / "references" / "supplier_master.csv"

MASTER_COLUMNS = ["supplier_id", "canonical_id", "canonical_name", "parent_name", "note"]

# Legal-form / filler tokens dropped when normalizing a vendor name so that
# "SURF DEVELOPMENT COMPANY INC" and "SURF DEVELOPMENT COMPANY" collapse together.
# Deliberately conservative: meaningful words like GROUP/SYSTEMS/SOLUTIONS stay.
_SUFFIX_TOKENS = {
    "INC",
    "INCORPORATED",
    "LLC",
    "LLP",
    "LP",
    "LTD",
    "CORP",
    "CORPORATION",
    "CO",
    "COMPANY",
    "PC",
    "PLLC",
    "THE",
}


def normalize_name(name: str | None) -> str:
    """Return a comparison key for a vendor name (case/punctuation/suffix-insensitive)."""
    if not name:
        return ""
    # Non-alphanumerics -> spaces, uppercase, then drop legal-form/filler tokens.
    words = re.sub(r"[^0-9A-Za-z]+", " ", name).upper().split()
    kept = [w for w in words if w not in _SUFFIX_TOKENS]
    return " ".join(kept or words)  # never normalize to empty if input had content


def load_master(path: Path = MASTER_CSV) -> dict[str, dict]:
    """Load the curated crosswalk as {supplier_id: {canonical_id, canonical_name, parent_name}}.

    Missing file -> empty crosswalk (everything defaults to itself downstream).
    """
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sid = (row.get("supplier_id") or "").strip()
            if not sid:
                continue
            out[sid] = {
                "canonical_id": (row.get("canonical_id") or "").strip() or sid,
                "canonical_name": (row.get("canonical_name") or "").strip(),
                "parent_name": (row.get("parent_name") or "").strip() or None,
            }
    return out


def suggest_merges(db_path: Path = DB_PATH) -> list[dict]:
    """Propose canonical groups from `purchases`: normalized name -> many supplier_ids.

    Each group: {canonical_id, canonical_name, normalized, total_value, members:
    [{supplier_id, supplier_name, total_value}]}. The canonical is the member with
    the greatest total spend (most authoritative registration). Only groups with
    more than one supplier_id are returned. Sorted by total group spend, descending.
    """
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT supplier_id, supplier_name, "
            "       COALESCE(SUM(grand_total), 0) AS total_value, COUNT(*) AS n "
            "FROM purchases WHERE supplier_id IS NOT NULL AND supplier_id != '' "
            "GROUP BY supplier_id, supplier_name"
        ).fetchall()
    finally:
        con.close()

    groups: dict[str, list[dict]] = {}
    for sid, name, total, n in rows:
        groups.setdefault(normalize_name(name), []).append(
            {"supplier_id": sid, "supplier_name": name, "total_value": total or 0.0, "docs": n}
        )

    suggestions = []
    for norm, members in groups.items():
        ids = {m["supplier_id"] for m in members}
        if len(ids) < 2:
            continue
        canonical = max(members, key=lambda m: (m["total_value"], m["docs"]))
        suggestions.append(
            {
                "normalized": norm,
                "canonical_id": canonical["supplier_id"],
                "canonical_name": canonical["supplier_name"],
                "total_value": sum(m["total_value"] for m in members),
                "members": sorted(members, key=lambda m: m["total_value"], reverse=True),
            }
        )
    return sorted(suggestions, key=lambda s: s["total_value"], reverse=True)


def write_crosswalk(
    suggestions: list[dict], path: Path = MASTER_CSV, *, existing: dict[str, dict] | None = None
) -> int:
    """Write a crosswalk CSV from merge suggestions, preserving curated overrides.

    For every non-canonical member of each group, emit a supplier_id -> canonical
    mapping. Rows already present in `existing` (a loaded crosswalk) win, so manual
    edits (a corrected canonical, a parent_name) survive a re-seed. Returns the row
    count written.
    """
    existing = existing or {}
    rows: dict[str, dict] = {}
    for s in suggestions:
        for m in s["members"]:
            if m["supplier_id"] == s["canonical_id"]:
                continue  # the canonical maps to itself; no row needed
            rows[m["supplier_id"]] = {
                "supplier_id": m["supplier_id"],
                "canonical_id": s["canonical_id"],
                "canonical_name": s["canonical_name"],
                "parent_name": "",
                "note": f"auto: same normalized name '{s['normalized']}'",
            }
    # Curated rows (existing file) override auto-generated ones and are always kept.
    for sid, cur in existing.items():
        rows[sid] = {
            "supplier_id": sid,
            "canonical_id": cur.get("canonical_id") or sid,
            "canonical_name": cur.get("canonical_name") or "",
            "parent_name": cur.get("parent_name") or "",
            "note": "curated",
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: (r["canonical_name"], r["supplier_id"]))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MASTER_COLUMNS)
        writer.writeheader()
        writer.writerows(ordered)
    return len(ordered)


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Supplier master-data crosswalk tools.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sg = sub.add_parser("suggest", help="Propose supplier_ids that are one entity")
    sg.add_argument("--write", action="store_true", help="(Re)seed references/supplier_master.csv")
    sg.add_argument("--limit", type=int, default=25, help="How many groups to print")
    sub.add_parser("info", help="Crosswalk coverage summary")
    args = ap.parse_args()

    if args.cmd == "suggest":
        suggestions = suggest_merges()
        print(f"{len(suggestions)} vendor(s) appear under multiple supplier_ids.")
        for s in suggestions[: args.limit]:
            ids = ", ".join(f"{m['supplier_id']}(${m['total_value']:,.0f})" for m in s["members"])
            print(f"  {s['canonical_name']:<38} -> {s['canonical_id']}   [{ids}]")
        if args.write:
            n = write_crosswalk(suggestions, existing=load_master())
            print(f"\nWrote {n} crosswalk row(s) to {MASTER_CSV}")
            print("Review/curate the file (fix canonicals, add parent_name), then commit it.")
    elif args.cmd == "info":
        master = load_master()
        parents = sum(1 for v in master.values() if v["parent_name"])
        canon_ids = {v["canonical_id"] for v in master.values()}
        print(f"crosswalk: {MASTER_CSV}")
        print(f"  {len(master)} remapped supplier_id(s) -> {len(canon_ids)} canonical entit(ies)")
        print(f"  {parents} row(s) tagged with a parent_name")


if __name__ == "__main__":
    _cli()
