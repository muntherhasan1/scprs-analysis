"""Store web-researched supplier profiles for competitive intelligence.

Profiles are produced by web research (search + fetch authoritative pages) and
land here with **provenance and a confidence score**, kept in a separate store
(data/supplier_enrichment.db) so external research data stays apart from the
SCPRS operational scrape. The warehouse folds this into gold_supplier_enriched.

Because research is name-based, `supplier_name` is the key; attach `supplier_id`
when a confident match to a SCPRS supplier exists.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import scprs

ENRICHMENT_DB = scprs.DATA_DIR / "supplier_enrichment.db"

PROFILE_COLUMNS = [
    "supplier_name",
    "supplier_id",
    "description",
    "org_type",
    "hq_city",
    "hq_state",
    "website",
    "parent_affiliation",
    "sb_dvbe",
    "confidence",
    "confidence_reason",
    "sources",
    "researched_at",
]


def _ensure_schema(con: sqlite3.Connection) -> None:
    cols = ",\n  ".join(
        f"{c} {'REAL' if c == 'confidence' else 'TEXT'}"
        + (" PRIMARY KEY" if c == "supplier_name" else "")
        for c in PROFILE_COLUMNS
    )
    con.execute(f"CREATE TABLE IF NOT EXISTS supplier_web_profile (\n  {cols}\n)")


def save_profiles(profiles: list[dict], *, db_path: Path = ENRICHMENT_DB) -> int:
    """Upsert researched profiles (keyed by supplier_name). `sources` may be a list."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        _ensure_schema(con)
        rows = []
        for p in profiles:
            r = {c: p.get(c) for c in PROFILE_COLUMNS}
            if isinstance(r["sources"], (list, dict)):
                r["sources"] = json.dumps(r["sources"])
            rows.append([r[c] for c in PROFILE_COLUMNS])
        placeholders = ", ".join("?" * len(PROFILE_COLUMNS))
        con.executemany(
            f"INSERT OR REPLACE INTO supplier_web_profile VALUES ({placeholders})",  # noqa: S608
            rows,
        )
        con.commit()
        return len(rows)
    finally:
        con.close()
