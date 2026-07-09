# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pipeline that scrapes California's FI$Cal **SCPRS** procurement portal (public,
no login) and turns it into an analytical data warehouse. Data flows in one
direction through four stages, each a module under `src/` with its own `python -m`
CLI:

```
scprs.py         →  model.py          →  warehouse.py       →  (marts / queries)
browser scraping    SQLite operational    medallion analytical
                    store (scprs.db)      warehouse (warehouse.db)
```

`supplier_research.py` is a side input: web-researched supplier firmographics
stored separately (`supplier_enrichment.db`) and folded into gold during the
warehouse build.

## Commands

```bash
# Environment (Windows PowerShell)
.venv\Scripts\Activate.ps1          # puts ruff/pytest/bandit/pre-commit on PATH

# Checks — all must pass; CI and pre-commit run these
ruff check .
bandit -c pyproject.toml -r src
pip-audit -r requirements.txt
pytest
pytest tests/test_warehouse.py::test_name   # single test
pre-commit run --all-files                   # everything at once

# Pipeline (each stage builds on the previous store)
python -m src.model build 8660 01/01/2016 07/08/2026   # scrape summary -> scprs.db
python -m src.model enrich 8660 07/01/2021 06/30/2028 --limit 200 --newest-first  # drill PO Details
python -m src.warehouse build                          # scprs.db -> warehouse.db (all layers + DQ)

# Inspection
python -m src.model info            # loaded business units in scprs.db
python -m src.model document 63626  # reproduce one doc's PO Details page from the DB
python -m src.warehouse info        # layer row counts + last batch
python -m src.warehouse dq          # re-run data-quality checks only
```

Runs also work through Docker (`docker run <image> <module> <args…>` maps to
`python -m <module> <args…>`; SQLite files live in the `/app/data` volume).

## Architecture notes that span files

**The scraper (`scprs.py`) encodes hard-won site quirks — do not "simplify" them away.**
The SCPRS search is a stateful PeopleSoft component driven by a headless browser
(Playwright/Chromium). Key constraints, all load-bearing:
- Date fields reject `fill()`; they must be typed with real keystrokes and
  committed with Tab (`_type()`), then verified — otherwise the filter is
  silently ignored and you get unfiltered data. There is an explicit guard that
  raises if the dates didn't commit.
- A single export is capped at **65,000 rows**. `download_range()` bisects the
  date range recursively until every slice is under the cap. Only `model.build`
  goes through this; `download_extract` just flags `Extract.truncated`.
- **The site's "Download Detail Information" Excel export is deliberately unused** —
  it silently drops line-item dollars on multi-line documents (verified: exported
  $22,680 of a $482,500 contract). Authoritative line items and associated POs
  come from the **PO Details drill-down** (`collect_po_details`), which clicks
  each document and parses the nested PeopleSoft grids span-by-span (the field
  ids are the `_PODET_*` maps; `pandas.read_html` can't handle these tables).
- Downloaded `.xls` files are actually HTML tables; `load_extract()` cleans the
  PeopleSoft quirks (leading apostrophes on id columns, `$1,234` money).

**Grain / versioning is the central data-modeling concern.** SCPRS documents exist
at `(document, version)` grain — a document can be re-drilled at several versions.
`model.document()` and the silver layer both resolve to the **current version**
(max version present) before showing/aggregating. When touching any query over
`document_details`/`document_lines`/`document_pos` or the silver tables, preserve
current-version resolution or you'll double-count.

**`model.py` enrichment is resumable and incremental.** `enrich` only visits
distinct `start_date`s that already exist in `purchases` (so build the summary
first), records each finished day in `details_progress`, and skips done days on
re-run. A day that errors is left unrecorded so it retries next run. This is why
the daily job (`scripts/enrich_daily.ps1`) can run a small `--limit` slice each
day and make progress. `--newest-first` prioritizes recent fiscal years.

**`warehouse.py` is a medallion warehouse built by SQL-generating Python.**
`build_all` runs bronze → silver → gold in one batch, logged in `dw_batch` with
severity-tiered checks in `dw_dq_results` (`error` checks gate the build; `warn`
checks are informational). Each layer is a full idempotent refresh from the layer
below. Operational (`scprs.db`) and analytical (`warehouse.db`) stores are kept
separate; the warehouse ATTACHes the source read-only. Gold is a Kimball star
schema (surrogate-keyed `dim_*`, `fact_document`/`fact_line`/`fact_associated_po`,
plus `gold_*` mart views). See `docs/WAREHOUSE.md` for the full layer/grain spec.

## Security & conventions

- Secrets load only through `src/config.py` (`require()` fails loudly on a missing
  var) from a git-ignored `.env` or the platform secret store — never hard-coded.
- `data/` is git-ignored and never committed; `references/departments.csv` (the
  300 valid business-unit codes) is the one committed dataset.
- Ruff lint selects `E,F,I,B,S` (incl. flake8-bandit). `warehouse.py` is exempt
  from `S608`/`E501` because it generates DDL from **internal constants only**
  with parameterized values — keep that invariant true (never interpolate user
  input into SQL). Elsewhere, dynamic SQL over internal-constant table names is
  annotated with `# noqa: S608` and a justifying comment.
- Bandit runs as a local `python -m` pre-commit hook (not the isolated shim)
  because Windows WDAC blocks pre-commit's unsigned `.exe` shims (WinError 4551).
