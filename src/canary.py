"""Source canary: prove the live site still parses to a known-good value.

`src/health.py` watches that collection *doesn't stop*. This watches that
collection *stays correct*. The two silent-failure modes are different:

  * the scraper stops running        -> caught by health.py (freshness, offline)
  * the site changes under the scraper -> caught HERE (parse drift, live drill)

The second is the dangerous one: if FI$Cal changes a field id or grid layout,
`collect_po_details` keeps running and keeps writing rows, but the parsed
line-item dollars are now wrong — and offline checks can't see it, because the
corrupted data is still internally consistent. So this canary drills one known,
finalized document live and asserts its parsed signature still equals a golden
value captured while the pipeline was healthy.

Two drawbacks of a live check, and how they are handled:

  * Flakiness. A canary that pages on every transient blip is worse than none.
    The outcome is four-state, not pass/fail:
        PASS         drilled cleanly, signature matches golden   -> exit 0
        FAIL         drilled cleanly, signature DRIFTED          -> exit 1  (alert + gate)
        UNAVAILABLE  could not complete the drill after retries  -> exit 2  (retry, no alert)
        NOT_FOUND    drilled cleanly, fixture doc not in results -> exit 3  (alert, DON'T gate)
    Only FAIL means "the parser is writing wrong data" — the one case worth
    blocking a publish over. NOT_FOUND means the fixture document itself became
    unavailable (archive purge, availability change): the parser is unproven but
    not indicted, and a run's banked enrichment must not be forfeited over it —
    the scheduler alerts and a human recaptures the fixture (see #47). Transient
    network/timeout errors retry with backoff and, if still stuck, report
    UNAVAILABLE — a soft signal treated as "try again next run", not an incident.

  * Not unit-testable offline. The scoring is a *pure* function (`signature` +
    `compare`) tested offline; the orchestration (`run`) is tested by faking the
    drill. Only the real Playwright call needs the network, so it lives on a
    schedule, not in the commit-time test suite.

    python -m src.canary            # run the canary (live drill)
    python -m src.canary --json     # machine-readable, for a workflow to parse
    python -m src.canary --capture  # (re)build the golden fixture from scprs.db
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .model import DB_PATH, _money, document
from .scprs import DATA_DIR

# Committed alongside the other reference datasets so the golden value is versioned.
FIXTURE_PATH = DATA_DIR.parent / "references" / "canary_fixture.json"

PASS, FAIL, UNAVAILABLE, NOT_FOUND = "PASS", "FAIL", "UNAVAILABLE", "NOT_FOUND"
_EXIT = {PASS: 0, FAIL: 1, UNAVAILABLE: 2, NOT_FOUND: 3}


# --- pure scoring core (fully offline-testable) ------------------------------


def signature(grand_total: float | None, line_pairs: list[tuple]) -> dict:
    """Reduce a parsed document to the few invariants that break if parsing does.

    `line_pairs` is a list of (unit_price, quantity). We deliberately keep the
    signature small and structural — a shifted column or dropped grid row changes
    the line count or the summed line amount; a stable document does not."""
    total = 0.0
    for up, qty in line_pairs:
        total += (up or 0) * (qty or 0)
    return {
        "line_count": len(line_pairs),
        "grand_total": round(grand_total, 2) if grand_total is not None else None,
        "line_amount_sum": round(total, 2),
    }


def compare(golden: dict, observed: dict, *, money_tol: float = 0.01) -> list[str]:
    """Return human-readable mismatch descriptions; empty list means it matches.

    Money fields compare within `money_tol` (a cent) to absorb float noise;
    line_count is exact."""
    diffs: list[str] = []
    if golden.get("line_count") != observed.get("line_count"):
        diffs.append(
            f"line_count {observed.get('line_count')} != golden {golden.get('line_count')}"
        )
    for key in ("grand_total", "line_amount_sum"):
        g, o = golden.get(key), observed.get(key)
        if g is None or o is None:
            if g != o:
                diffs.append(f"{key} {o} != golden {g}")
        elif abs(o - g) > money_tol:
            diffs.append(f"{key} {o} != golden {g} (> {money_tol})")
    return diffs


def _sig_from_drill(drilled: dict) -> dict:
    """Signature from a live `collect_po_details` result (raw parsed strings)."""
    header = drilled["header"]
    pairs = [(_money(ln.get("unit_price")), _money(ln.get("quantity"))) for ln in drilled["lines"]]
    return signature(_money(header.get("grand_total")), pairs)


def _sig_from_db(doc: dict) -> dict:
    """Signature from a `model.document` result (DB floats) — used at capture."""
    header, lines = doc["header"], doc["lines"]
    pairs = list(zip(lines["unit_price"], lines["quantity"], strict=False))
    return signature(header.get("grand_total"), pairs)


# --- fixture (golden value) --------------------------------------------------


def load_fixture(path: Path = FIXTURE_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def capture(
    business_unit: str, doc_id: str, *, db_path: Path = DB_PATH, money_tol: float = 0.01
) -> dict:
    """Build the golden fixture from the DB's trusted stored parse of one document.

    Capturing from the DB (not a fresh live drill) avoids baking in a site that
    might already be subtly broken: the DB holds the parse from prior healthy
    enrich runs, so it is the reference we trust."""
    doc = document(doc_id, db_path=db_path)
    if doc is None:
        raise ValueError(
            f"{doc_id} is not enriched in {db_path}; pick an already-drilled document."
        )
    header = doc["header"]
    day_iso = header.get("start_date")
    search_date = datetime.strptime(day_iso, "%Y-%m-%d").strftime("%m/%d/%Y")
    return {
        "business_unit": business_unit,
        "document": doc_id,
        "search_date": search_date,
        "version": header.get("version"),
        "supplier_name": header.get("supplier_name"),
        "signature": _sig_from_db(doc),
        "money_tol": money_tol,
        "captured_from": "scprs.db stored parse",
        "note": "Old finalized single-document day; a live drill must reproduce this signature.",
    }


# --- live run (orchestration; tested with a faked drill) ---------------------


@dataclass
class Outcome:
    status: str  # PASS | FAIL | UNAVAILABLE
    detail: str
    observed: dict | None = None


def run(
    fixture: dict,
    *,
    retries: int = 2,
    backoff: float = 5.0,
    max_docs: int = 5,
    headless: bool = True,
    log=None,
) -> Outcome:
    """Drill the fixture document live and classify the result (four-state).

    Retries transient failures (exceptions, empty results, target-not-found) with
    linear backoff; a *clean* drill whose signature diverges is a FAIL and is
    never retried, because site drift is deterministic. Target-not-found that
    survives every retry is NOT_FOUND — alert-worthy but not publish-gating."""
    from .scprs import collect_po_details  # lazy: keeps import light for pure tests

    bu, day, target = fixture["business_unit"], fixture["search_date"], fixture["document"]
    golden, tol = fixture["signature"], fixture.get("money_tol", 0.01)
    sink = log or (lambda *a: None)
    last: str | None = None

    for attempt in range(retries + 1):
        last_attempt = attempt == retries
        try:
            drilled = collect_po_details(
                bu, day, day, max_docs=max_docs, headless=headless, log=sink
            )
        except Exception as e:  # noqa: BLE001 - any drill error is treated as transient
            last = f"{type(e).__name__}: {e}"
            if not last_attempt:
                time.sleep(backoff * (attempt + 1))
                continue
            return Outcome(UNAVAILABLE, f"drill failed after {retries + 1} attempt(s): {last}")

        match = [d for d in drilled if (d.get("document") or "").strip().lower() == target.lower()]
        if not drilled or not match:
            # A stable 2016 document should always be found; a transient search
            # glitch usually clears on retry. After exhausting retries this is
            # NOT_FOUND, not FAIL: the parser produced clean output — there is
            # just nothing to score it against. FAIL (which gates the publish)
            # is reserved for a drilled document whose signature drifted.
            last = (
                f"search returned no records for {target} on {day}"
                if not drilled
                else f"{target} not among {len(drilled)} drilled document(s)"
            )
            if not last_attempt:
                time.sleep(backoff * (attempt + 1))
                continue
            return Outcome(
                NOT_FOUND,
                f"{last}; the fixture document is no longer findable — recapture the "
                "fixture (python -m src.canary --capture --document <id>)",
            )

        observed = _sig_from_drill(match[0])
        diffs = compare(golden, observed, money_tol=tol)
        if diffs:
            return Outcome(FAIL, "parse drift vs golden: " + "; ".join(diffs), observed=observed)
        return Outcome(PASS, "live drill matches golden", observed=observed)

    return Outcome(UNAVAILABLE, last or "no attempts made")  # unreachable; for the type checker


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SCPRS source canary (live parse-drift check).")
    ap.add_argument("--capture", action="store_true", help="(Re)build the golden fixture from DB")
    ap.add_argument("--business-unit", default="8660")
    ap.add_argument("--document", default="15TG6140", help="Fixture document id (for --capture)")
    ap.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--headful", action="store_true", help="Show the browser (debugging)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.capture:
        fixture = capture(args.business_unit, args.document, db_path=args.db)
        args.fixture.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps(fixture, indent=2))
        else:
            sig = fixture["signature"]
            print(
                f"Captured golden for {fixture['document']} "
                f"({fixture['business_unit']}, {fixture['search_date']}):"
            )
            print(f"  {sig}")
            print(f"  -> {args.fixture}")
        return 0

    fixture = load_fixture(args.fixture)
    outcome = run(fixture, retries=args.retries, headless=not args.headful, log=None)
    if args.json:
        print(
            json.dumps(
                {"status": outcome.status, "detail": outcome.detail, "observed": outcome.observed},
                indent=2,
            )
        )
    else:
        print(f"[{outcome.status}] {fixture['document']}: {outcome.detail}")
        if outcome.observed:
            print(f"  observed: {outcome.observed}")
            print(f"  golden:   {fixture['signature']}")
    return _EXIT[outcome.status]


if __name__ == "__main__":
    raise SystemExit(main())
