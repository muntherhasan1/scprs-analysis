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

`cmas.py` is a **separate, standalone** data source (not a SCPRS stage): it
extracts California's CMAS master-agreement contractor data — an anonymous
**Power BI** embed, queried directly over the model's DSR protocol, not scraped —
into its own `data/cmas.db` + CSVs. `python -m src.cmas extract`. The warehouse
folds it in as an **optional side input** (like `supplier_enrichment.db`): skipped
if `data/cmas.db` is absent. See `docs/CMAS.md`.

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
`--budget-minutes` bounds the run's wall clock so it exits 0 ahead of any outer
job timeout: a day cut mid-drill keeps its drilled documents (idempotent per
doc) and resumes doc-by-doc next run via a skip set (`_drilled_docs`), and is
only recorded once its grid is fully covered. The CI cron relies on this —
without it, a business unit whose single day exceeds the job timeout livelocks
the scheduler (killed → unrecorded → re-picked; see the 2026-07 BU 3540 incident).

**`warehouse.py` is a medallion warehouse built by SQL-generating Python.**
`build_all` runs bronze → silver → gold in one batch, logged in `dw_batch` with
severity-tiered checks in `dw_dq_results` (`error` checks gate the build; `warn`
checks are informational). Each layer is a full idempotent refresh from the layer
below. Operational (`scprs.db`) and analytical (`warehouse.db`) stores are kept
separate; the warehouse ATTACHes the source read-only. Gold is a Kimball star
schema (surrogate-keyed `dim_*`, `fact_document`/`fact_line`/`fact_associated_po`,
plus `gold_*` mart views). See `docs/WAREHOUSE.md` for the full layer/grain spec.

**Contract change over time lives in `dw_document_history`.** The bronze/silver/gold
layers are a full-refresh snapshot of *current* state, so they can't show how a
contract changed. `capture_document_history` (run each build, after bronze) appends
to the **append-only** `dw_document_history` (never dropped) a snapshot of each
document/version's tracked attributes — but only when its signature changed, so
rebuilds are idempotent. The first build backfills the versions present now; later
builds accumulate real history. `gold_contract_change_log` and
`gold_contract_amendments` derive amendments/value-growth from it. When you need
"how did this contract change," query the history / change-log, not silver (which
keeps only the current version).

**Gold physical columns are abbreviated; marts stay friendly.** A post-build pass
(`_abbreviate_gold`) renames `dim_*`/`fact_*` physical columns to a governed
standard from `references/abbreviations.csv` (`grand_total`→`grand_tot`,
`supplier_id`→`sup_id`, `business_unit`→`bu`). Marts and DQ **don't** reference the
tables directly — a build-time transform (`_to_logical_views`) points their SQL at
per-table `lv_<table>` views that alias the abbreviated columns back to logical
names, so mart output names and existing queries are unchanged. `gold_data_dictionary`
records the full logical↔physical mapping. When adding a gold column, just rebuild —
the pass abbreviates it automatically; add the term to the CSV if it's a new word.

**Supplier identity is many-to-one.** SCPRS issues a `supplier_id` per vendor
*registration*, so one company appears under several ids (NORTH RIDGE CONSULTING
has two, BETA ALPHA PSI four). `src/supplier_master.py` + the curated crosswalk
`references/supplier_master.csv` resolve these to a `canonical_id`/`canonical_name`
(+ optional `parent_name`), which `build` stamps onto `dim_supplier`. Prefer the
canonical marts (`gold_canonical_supplier_spend`, `gold_supplier_master`) for
vendor rollups; the per-`supplier_id` marts double-count split vendors. Web
enrichment joins to gold **by name**, so it attaches to the canonical entity.

**Two read-only query front ends share one hardened guard.** `src/warehouse_query.py`
is the single source of truth for querying gold: connection opened `?mode=ro`
(writes impossible), `run_select` accepts one `SELECT`/`WITH` only, object names
checked against a live `gold_*`/`lv_*`/`dim_*`/`fact_*` allowlist. Both front ends
are thin layers over it — the **remote MCP server** (`src/mcp_server.py`, stdio +
token-gated HTTP; see `docs/REMOTE_MCP.md`) for MCP clients, and the **public NL
web app** (`src/nl_query.py` + `src/web_app.py`, a Gradio chat that turns plain
English into guarded SQL via free-tier Gemini; see `docs/WEB_APP.md`) for anyone
with a browser. Never duplicate the query guard into a front end — extend
`warehouse_query`. Each deploys as its own Hugging Face Docker Space via
`deploy/hf-space/deploy.py` and `deploy/hf-chat/deploy.py`. The MCP server also
has `generate_chart` / `generate_report` tools (matplotlib via `src/charting.py`;
reports served at unauthenticated `/files/` capability URLs) so a **Microsoft 365
/ Copilot Studio agent** can produce query results *and* executive reports — see
`docs/COPILOT_STUDIO.md`.

## Security & conventions

- Secrets load only through `src/config.py` (`require()` fails loudly on a missing
  var) from a git-ignored `.env` or the platform secret store — never hard-coded.
- `data/` is git-ignored and never committed; the committed datasets live in
  `references/` (`departments.csv`, the 300 valid business-unit codes;
  `supplier_master.csv`, the curated canonical-vendor crosswalk; and
  `abbreviations.csv`, the gold column-naming dictionary).
- Ruff lint selects `E,F,I,B,S` (incl. flake8-bandit). `warehouse.py` is exempt
  from `S608`/`E501` because it generates DDL from **internal constants only**
  with parameterized values — keep that invariant true (never interpolate user
  input into SQL). Elsewhere, dynamic SQL over internal-constant table names is
  annotated with `# noqa: S608` and a justifying comment.
- Bandit runs as a local `python -m` pre-commit hook (not the isolated shim)
  because Windows WDAC blocks pre-commit's unsigned `.exe` shims (WinError 4551).
