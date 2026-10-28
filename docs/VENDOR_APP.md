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
