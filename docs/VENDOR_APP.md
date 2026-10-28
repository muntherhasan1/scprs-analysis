# Vendor web app — static JSON export

`src/app_export.py` writes the data behind the planned vendor/small-business web
app: a static site that reads precomputed JSON, so the front end never sums,
dedupes or joins. The field-level spec (schema v1) is the "Data contract" tab of
the product brief (Claude Doc, linked from the PR that added this module).

```bash
python -m src.warehouse serve-export          # the export reads the slim serve DB
python -m src.app_export                      # -> data/app/
python -m src.app_export --today 2026-07-25   # pin "open" solicitations to a snapshot date
```

| File | Contents |
| --- | --- |
| `meta.json` | freshness dates (`contracts_as_of`, `opportunities_as_of`, …) and coverage counts the UI shows beside totals |
| `opportunities.json` | posted solicitations closing on/after the export date, soonest first |
| `departments/index.json` | one row per business unit, loaded first by value |
| `departments/{bu}.json` | spend by fiscal year, acquisition mix, top canonical suppliers, canonical HHI, solicitation history, open solicitations |
| `suppliers/index.json` | one row per canonical supplier (search) |
| `suppliers/shard-{NN}.json` | supplier pages keyed by `canonical_id`; `NN` = its last two digits, `xx` for non-numeric ids |
| `insights/overview.json` | the dashboard home: headline counts, spend by fiscal year (with `department_count` reporting each year), top departments and companies, acquisition / size / category mixes, solicitations closing soon |
| `insights/{acquisition,size,category}/{key}.json` | one page per vector value: totals, trend, top departments and suppliers, concentration, plus a cross-mix (size mix, acquisition mix, or top UNSPSC commodities) |
| `search.json` | small index (~60 KB) over departments (with aliases) and every vector value; suppliers stay in the lazy `suppliers/index.json` |

Insight vectors:

- **Acquisition type** rolls glued sub-types (`NON-IT Services_Expert Witnesses`, a
  silver parsing gap on ~1% of documents) up to the base type before `_`.
- **Contract size** buckets `grand_total` (`SIZE_BUCKETS`); `$0` and credits get
  their own `adjustment` bucket so they don't read as small buys.
- **Product category** maps line-item UNSPSC codes to ~15 plain-language
  categories via `references/product_categories.csv` (longest prefix wins, so
  `8111` IT services beats `81` engineering). It covers only itemized contracts:
  `basis: "line_items"`, and the UI must say so.
- **Department aliases** (Caltrans, CHP, DMV, …) come from
  `references/department_aliases.csv`; add a row when a search misses.

Rules the export enforces (don't move them into the front end):

- **Canonical suppliers only.** Shares and HHI are recomputed from `gold_document`
  by `canonical_id`; `gold_supplier_share` / `gold_market_concentration` group by
  registration and split re-registered companies.
- **Null, not zero.** Unloaded departments still get a page (eProcure history and
  open solicitations cover every department) with `loaded: false` and null spend blocks.
- **`supplier_kind`** labels interagency counterparties (`DEPT…`/`UNIV…` →
  `government`) and redacted/pooled payees (`…CFD`, `…SGP`, `UNKNOWN` →
  `placeholder`) so the UI doesn't present them as competitors; totals still include them.
- **Public slice.** CMAS contact name/email/website are never selected.
- The output directory is rebuilt via a staging dir and swapped in, so a department
  or supplier that leaves the warehouse can't leave a stale page behind.

## Publishing

`warehouse-refresh.yml` runs the export and `python -m src.data_sync publish-app`
after go-live verifies the build, so only a verified build reaches the public
slice. One commit per publish; files the export no longer writes are deleted.
The step is dormant until set up:

1. Create the **public** dataset in the HF UI (no pipeline token can create repos,
   and `publish-app` deliberately never creates one).
2. Mint a fine-grained token with write on that dataset only; add it as the
   `HF_APP_TOKEN` Actions secret.
3. Set the `APP_DATASET` Actions variable to the dataset id.

Not wired yet: the static Space that serves the site.
