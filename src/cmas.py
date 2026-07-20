"""Extract California's CMAS (California Multiple Award Schedules) contract data.

The public CMAS search site (``cmassearch-prod.apps.dgs.ca.gov``) is not a normal
web app — it is an **embedded Power BI report** (US Gov cloud). There is no
official public API, but the report is served through Power BI's anonymous embed
flow, and the underlying data model is fully queryable through the same protocol
the report's own visuals use. This module talks that protocol directly, so it
pulls whole tables (every record, with all the per-record "drill-down" fields the
UI shows one row at a time) instead of scraping the rendered page.

The flow, all anonymous — no login, no secret:

1. ``GET`` the app page → a short-lived **EmbedToken** + the ReportId + the
   Power BI cluster URL (baked into the page as an ``ReportEmbedData`` JS object).
2. ``GET .../explore/reports/{id}/modelsAndExploration`` with that token → an
   **MWCToken**, the **capacity query endpoint**, and the model id. (This is the
   read-only session the report establishes on load.)
3. ``GET .../explore/reports/{id}/conceptualschema`` → the model's entities and
   columns, so the extraction is driven by the live schema, not a frozen list.
4. ``POST`` a Power BI *semantic query* (select all columns of an entity, large
   page window) to the capacity **Query Execution Service** with the MWCToken →
   rows, in Power BI's compressed **DSR** wire format. ``_decode_dsr`` expands
   that (value-dictionary columns, the row-to-row repeat bitmask ``R`` and null
   bitmask ``Ø``) back into plain records, paging via ``RestartTokens`` until the
   entity is exhausted.

Transport note: the Power BI Gov front end drops some HTTP clients' POSTs at the
TLS layer (``httpx`` is refused; ``requests``/urllib3 is accepted). GETs use
``httpx``; the query POST uses ``requests`` — both already project dependencies.

Output is a standalone store, deliberately separate from the SCPRS pipeline: a
SQLite DB (``data/cmas.db``, one table per entity) plus a CSV per entity. Run:

    python -m src.cmas extract              # all entities -> data/cmas.db + CSVs
    python -m src.cmas schema               # print the live model schema
    python -m src.cmas extract --entity Approved_Applications
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import requests

APP_URL = "https://cmassearch-prod.apps.dgs.ca.gov/"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "cmas.db"

# A browser-like UA; the Power BI Gov front end is picky about non-browser clients.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Model entities that are report scaffolding, not real data (date tables etc.).
_SKIP_PREFIXES = ("DateTableTemplate", "LocalDateTable")
# Power BI page window; QES caps a single page, RestartTokens carry the rest.
_PAGE = 30000


class CmasError(RuntimeError):
    """A CMAS extraction step failed in a way worth surfacing clearly."""


# --------------------------------------------------------------------- session


class _Session:
    """A live anonymous Power BI embed session for the CMAS report.

    Bundles the short-lived tokens and endpoints discovered from the app page +
    exploration. Tokens expire (~10 min); re-create for a long run if needed.
    """

    def __init__(self) -> None:
        page = httpx.get(APP_URL, follow_redirects=True, timeout=30, headers={"User-Agent": _UA})
        page.raise_for_status()
        body = page.text
        try:
            self.report_id = re.search(r'ReportId:\s*"([^"]+)"', body).group(1)
            embed_token = re.search(r'EmbedToken:\s*"([^"]+)"', body).group(1)
            embed_url = re.search(r'EmbedUrl:\s*"([^"]+)"', body).group(1).replace("&amp;", "&")
        except AttributeError as exc:  # a page redesign would break these anchors
            raise CmasError(
                "Could not find the Power BI embed parameters on the CMAS page; "
                "the site markup may have changed."
            ) from exc
        cfg = re.search(r"config=([A-Za-z0-9_-]+)", embed_url).group(1)
        conf = json.loads(base64.urlsafe_b64decode(cfg + "=" * (-len(cfg) % 4)))
        self.cluster = conf["clusterUrl"].rstrip("/")

        # Open the read-only session: yields the MWCToken + capacity query endpoint.
        me = self._get(
            f"/explore/reports/{self.report_id}/modelsAndExploration" "?preferReadOnlySession=true",
            embed_token,
        ).json()
        expl = me["exploration"]
        self.mwc_token = expl["mwcToken"]
        self.query_url = expl["capacityUri"].rstrip("/") + "/query"
        self.model_id = me["models"][0]["id"]
        self._embed_token = embed_token

    def _get(self, path: str, token: str) -> httpx.Response:
        r = httpx.get(
            self.cluster + path,
            headers={
                "User-Agent": _UA,
                "Authorization": "EmbedToken " + token,
                "X-PowerBI-ResourceKey": self.report_id,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise CmasError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
        return r

    def entities(self) -> list[dict]:
        """Real data entities (name + ordered column list) from the live schema."""
        schema = self._get(
            f"/explore/reports/{self.report_id}/conceptualschema", self._embed_token
        ).json()
        out = []
        for ent in schema["schema"]["Entities"]:
            name = ent["Name"]
            if ent.get("Private") or name.startswith(_SKIP_PREFIXES):
                continue
            # Only real columns are row-queryable; a Measure (e.g. "Last Date
            # Refreshed") is computed and makes QES reject the whole query.
            cols = [p["Name"] for p in ent.get("Properties", []) if "Column" in p]
            if cols:
                out.append({"name": name, "columns": cols})
        return out


# --------------------------------------------------------------------- querying


def _build_query(sess: _Session, entity: str, columns: list[str], restart=None) -> dict:
    """A Power BI semantic query selecting every column of ``entity``.

    ``restart`` carries a prior page's RestartTokens to fetch the next page."""
    select = [
        {
            "Column": {"Expression": {"SourceRef": {"Source": "a"}}, "Property": c},
            "Name": f"{entity}.{c}",
        }
        for c in columns
    ]
    query = {
        "Version": 2,
        "From": [{"Name": "a", "Entity": entity, "Type": 0}],
        "Select": select,
        # Deterministic paging needs a stable sort; order by the first column.
        "OrderBy": [
            {
                "Direction": 1,
                "Expression": {
                    "Column": {"Expression": {"SourceRef": {"Source": "a"}}, "Property": columns[0]}
                },
            }
        ],
    }
    command = {
        "SemanticQueryDataShapeCommand": {
            "Query": query,
            "Binding": {
                "Primary": {"Groupings": [{"Projections": list(range(len(columns)))}]},
                "DataReduction": {
                    "DataVolume": 3,
                    "Primary": {"Window": {"Count": _PAGE}},
                },
                "Version": 1,
            },
        }
    }
    inner = {
        "Query": {"Commands": [command]},
        "QueryId": "",
        "ApplicationContext": {
            "DatasetId": sess.report_id,
            "Sources": [{"ReportId": sess.report_id}],
        },
    }
    if restart is not None:
        inner["RestartTokens"] = restart
    return {
        "version": "1.0.0",
        "queries": [inner],
        "cancelQueries": [],
        "modelId": sess.model_id,
    }


def _post_query(sess: _Session, payload: dict) -> dict:
    """POST a semantic query to the capacity QES; return the raw result envelope."""
    aid = str(uuid.uuid4())
    r = requests.post(
        sess.query_url,
        headers={
            "User-Agent": _UA,
            "Authorization": "MWCToken " + sess.mwc_token,
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "ActivityId": aid,
            "RequestId": str(uuid.uuid4()),
            "x-ms-root-activity-id": aid,
        },
        json=payload,
        timeout=120,
    )
    if r.status_code != 200:
        raise CmasError(f"query POST -> {r.status_code}: {r.text[:300]}")
    return r.json()


# ----------------------------------------------------------------- DSR decoding


def _decode_dsr(dsr: dict, columns: list[str]) -> tuple[list[dict], list | None]:
    """Expand one DSR data-set into row dicts; return (rows, restart_tokens).

    Power BI compresses the wire format, all handled here:
      * **value dictionaries** — a string column sends an integer index into
        ``ValueDicts[Dn]`` instead of repeating the string.
      * **repeat bitmask ``R``** — bit *i* set means column *i* equals the
        previous row's value and is omitted from this row's ``C`` array.
      * **null bitmask ``Ø``** — bit *i* set means column *i* is null (omitted).
      * **inline rows** — small/uncompressed results skip the ``C`` array and put
        each value directly under its column key (``{"G0": v, "G1": v2}``).
    The first row also carries the schema ``S`` (column order + which dict each
    column uses). Datetime columns (type 7) arrive as epoch-ms integers.
    """
    ds = dsr["DS"][0]
    dicts = ds.get("ValueDicts", {})
    rows: list[dict] = []
    schema: list[dict] | None = None
    prev: list = []

    for item in ds.get("PH", [{}])[0].get("DM0", []):
        if "S" in item:
            schema = item["S"]
        if schema is None:
            continue
        ncols = len(schema)
        cvals = item.get("C")
        inline = cvals is None  # no C array → values live under the Gn keys
        repeat = item.get("R", 0)
        null = item.get("Ø", 0)  # the "Ø" (null) bitmask key
        values: list = []
        ci = 0
        for idx in range(ncols):
            col = schema[idx]
            bit = 1 << idx
            if repeat & bit:
                values.append(prev[idx] if idx < len(prev) else None)
                continue
            if null & bit:
                values.append(None)
                continue
            if inline:
                raw = item.get(col["N"])
            else:
                raw = cvals[ci]
                ci += 1
            dn = col.get("DN")
            if dn is not None and isinstance(raw, int):
                raw = dicts[dn][raw]
            elif col.get("T") == 7 and isinstance(raw, (int, float)):
                raw = _epoch_ms_to_date(raw)
            values.append(raw)
        prev = values
        rows.append(dict(zip(columns, values, strict=False)))

    restart = ds.get("RT")
    return rows, restart


def _epoch_ms_to_date(ms: float) -> str:
    """Power BI datetimes are epoch-ms; render as an ISO date (UTC)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def fetch_entity(sess: _Session, entity: str, columns: list[str]) -> list[dict]:
    """Every row of ``entity``, paging through RestartTokens until exhausted."""
    all_rows: list[dict] = []
    restart = None
    while True:
        payload = _build_query(sess, entity, columns, restart)
        env = _post_query(sess, payload)
        dsr = env["results"][0]["result"]["data"]["dsr"]
        # QES reports semantic errors (bad column, renamed entity) inside the dsr.
        if "DataShapes" in dsr:
            err = dsr["DataShapes"][0].get("odata.error", {})
            msg = err.get("message", {}).get("value", json.dumps(err)[:200])
            raise CmasError(f"query for {entity!r} failed: {msg}")
        rows, restart = _decode_dsr(dsr, columns)
        all_rows.extend(rows)
        # No restart tokens, or a short page, means we've reached the end.
        if not restart or len(rows) < _PAGE:
            break
    return all_rows


# ------------------------------------------------------------------- persistence


def _write_sqlite(con: sqlite3.Connection, entity: str, columns: list[str], rows: list[dict]):
    """Full idempotent refresh of one entity's table (drop + recreate + insert)."""
    table = _safe_table(entity)
    quoted = ", ".join(f'"{c}"' for c in columns)
    con.execute(f'DROP TABLE IF EXISTS "{table}"')  # noqa: S608 — table from internal schema
    con.execute(f'CREATE TABLE "{table}" ({quoted})')  # noqa: S608 — cols from internal schema
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',  # noqa: S608
        [[_scalar(r.get(c)) for c in columns] for r in rows],
    )
    con.commit()


def _write_csv(out_dir: Path, entity: str, columns: list[str], rows: list[dict]) -> Path:
    path = out_dir / f"cmas_{_safe_table(entity)}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: _scalar(r.get(c)) for c in columns})
    return path


def _safe_table(entity: str) -> str:
    """A SQLite-safe table name from an entity name (spaces/slashes → _)."""
    return re.sub(r"\W+", "_", entity).strip("_")


def _scalar(v):
    """Coerce a decoded value to something SQLite/CSV can store."""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


# -------------------------------------------------------------------------- CLI


def extract_all(db_path: Path = DB_PATH, out_dir: Path = DATA_DIR, only: str | None = None):
    """Extract every entity (or just ``only``) to SQLite + CSV. Returns a summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = _Session()
    entities = sess.entities()
    if only:
        entities = [e for e in entities if e["name"] == only]
        if not entities:
            raise CmasError(f"entity {only!r} not found in the model schema")

    summary = []
    con = sqlite3.connect(db_path)
    try:
        for ent in entities:
            name, cols = ent["name"], ent["columns"]
            rows = fetch_entity(sess, name, cols)
            _write_sqlite(con, name, cols, rows)
            csv_path = _write_csv(out_dir, name, cols, rows)
            summary.append({"entity": name, "rows": len(rows), "csv": csv_path.name})
            print(f"  {name}: {len(rows)} rows -> {csv_path.name}")
    finally:
        con.close()
    print(f"\nWrote {len(summary)} tables to {db_path}")
    return summary


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Extract California CMAS contract data (Power BI).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract", help="Pull all entities to data/cmas.db + CSVs")
    ex.add_argument("--entity", default=None, help="Only this entity (default: all)")
    ex.add_argument("--db", type=Path, default=DB_PATH)
    ex.add_argument("--out", type=Path, default=DATA_DIR)

    sub.add_parser("schema", help="Print the live model schema (entities + columns)")

    args = ap.parse_args()
    if args.cmd == "extract":
        extract_all(args.db, args.out, only=args.entity)
    elif args.cmd == "schema":
        for ent in _Session().entities():
            print(f"\n{ent['name']} ({len(ent['columns'])} cols)")
            print("  " + ", ".join(ent["columns"]))


if __name__ == "__main__":
    _cli()
