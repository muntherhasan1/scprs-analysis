# External reconciliation (Open FI$Cal)

`src/reconcile.py` checks SCPRS contracts against the state's payment ledger — the only check in
the repo that compares the pipeline with an outside source. Results, acceptance criteria and the
first run (2026-10-31, CAL FIRE / Social Services / PUC) are in the "Contract Lens: proving the
numbers" doc.

## Inputs

- **Open FI$Cal vendor transaction files**: `open.fiscal.ca.gov/download-expenditures.html` →
  the file list `DepartmentVendorTransactionPointer.csv` → one CSV per department and fiscal year,
  `Vendor_<BU>_<Dept>_FY<yy>.csv`, where `FY<yy>` is the fiscal year's **start** (FY23 = July
  2023–June 2024). Terms agreed for this source: download sequentially with an identifying user
  agent, only the departments being checked, and re-fetch a file only when its `UploadDate`
  changes. Files are 10–240 MB each; keep them out of git (`data/` is ignored).
- **The operational store** (`scprs.db`), current version per document.

## Run

```bash
python -m src.reconcile run --bu 3540 --bu 5180 --fiscal-dir data/fiscal --out recon.json
```

## Tests and scope

| Test | Definition |
| --- | --- |
| R1 payee coverage | Share of in-scope payments made to a vendor holding an SCPRS contract — with **this** department, and with **any** department |
| R2 paid within awarded | Of the 50 largest payees, the share of dollars paid within 110% of their contracts' value in the award window, which starts by default at the unit's earliest stored contract (`--award-from` overrides) |
| R3 inputs | Contract value spread by day over each term, per fiscal year, for comparison with Budget Act actuals |

In scope = **state-operations operating expenses**, minus card programs (US Bank, WEX, …),
government payees (word-boundary match anywhere in the name), redacted payees, and
**real-property rent** (accounts 5324350, 5324400, 5324450: DGS Real Estate Services leases,
SCM code 039, which a department does not register as its own purchase). Equipment rentals,
rental cars and IT leases stay in scope. Local assistance is out of scope by budget category.

Why "any department": a department's vendors are often contracted elsewhere — statewide CALNET
telecom, a lease held by a co-tenant department, a consultant engaged through another
department. On 2026-10-31 that moved Social Services' R1 from 79.9% (own department) to 99.3%.

## Exception labels

Payees paid above their recorded award (or with none) get a likely mechanism from their dominant
FI$Cal account. Labels **never** remove a payment from scope or change R1/R2; they split the
exceptions into explained and unexplained so a reviewer can focus on the residue.

| Label | Rule |
| --- | --- |
| local agency reimbursement | account 5340330 (Consult & Prof Svcs-Interdept) paid to a city, county or fire agency (e.g. wildfire mutual aid, paid per incident) |
| legal award or settlement | legal accounts 5340300, 5340510–5340540 |
| postage | accounts 5306xxx |
| telecom or utilities under statewide agreements | accounts 5304xxx, 5326xxx |
| statutory award (CPUC intervenor compensation) | PUC only, account 5390890, non-government payee (Pub. Util. Code 1801–1812) |
| contract in another department | the payee holds an SCPRS contract with some other department |
| unexplained: paid above recorded award / no contract anywhere | none of the above |

Caveats: "contract in another department" can mask a real gap when the other department's
contract is unrelated (2026-10-31: Horne — Consumer Affairs' license-processing contract, while
Social Services' own agreement 22-3100 is unregistered).

First run (2026-10-31, CAL FIRE / Social Services / PUC, FY2022-23..2024-25, $4.14B in scope):
91.6% of dollars within award, 4.0% local agency reimbursement, 1.6% other labeled mechanisms,
2.7% unexplained.

### Name aliases (`references/name_aliases.csv`)

One entity under two names — a renamed company, or a county paid as `COUNTY OF X` on a contract
registered to its fire department — reads as an unpaid contract plus an unexplained payee. Each
row maps `name` onto `same_as` with its evidence, and `reconcile.match_name` applies it to **both**
the contract suppliers and the FI$Cal payees, so the other name's own contracts and payments stay
where they were. `outliers.paid_to_vendor` uses the same key. Rows so far: DynCorp International →
Amentum Services (renamed; $45.6M paid to DynCorp in FY2022-23 on the aviation contract),
Kern County Fire Dept → County of Kern. Effect (2026-10-31, same files): CAL FIRE R2 97.2% →
99.5%, its unexplained share 3.0% → 1.7%; Social Services and PUC unchanged.

## Departments not on FI$Cal, and the budget envelope (R3)

Deferred FI$Cal agencies (`references/fiscal_deferred.csv`, from State Auditor Report 2025-039)
process payments in their own systems and SCO's legacy system, which reach Open FI$Cal without
vendor names — and SCO publishes no state-agency vendor payments. For them `reconcile_unit`
reports status `not on FI$Cal: vendor payments not published` instead of R1/R2; the label stops
applying by itself once their vendor files appear. A unit with no files that is **not** on that
list reports `no FI$Cal vendor files found` — a download problem, never excused.

R3 runs for every unit with Budget Act actuals (`references/budget_actuals.csv`: the actual
column of the Enacted Budget Detail two editions later, all funds, $ thousands, each row with its
ebudget.ca.gov source; the PDF folder is the agency area, e.g. `2500` for Caltrans and DMV,
`5210` for Corrections, `4000` for Social Services). A year passes when contract value spread by
day over each term fits inside total expenditures. Caltrans' total includes $5.7–7.2B a year of
capital outlay and Water Resources' OE&E is not a procurement proxy (2023-24 printed negative),
so R3 is an envelope, not a tie-out.

`app_export` reports the same split in `meta.coverage.payments_reconcilable` and the coverage note.

## Outlier review (R4) — `src/outliers.py`

R4: any contract over 10% of its department's annual budget, or with a robust z-score over 5
within its department × acquisition type (log10 value, median/MAD × 1.4826, groups of ≥ 30), must
be confirmed or corrected against the SCPRS page **and** a third source before it sits in
published totals. Contracts already reviewed with a class override (`contract_flags.csv`) are
skipped.

```bash
python -m src.outliers run --wh data/warehouse.db --fiscal-dir data/fiscal \
    --bu 3540 --bu 5180 --bu 8660 --live --out outlier_queue.csv --record
```

For each flag it fetches the live SCPRS summary value (one search per unit and start day) and the
department's Open FI$Cal payments to that vendor. A verdict is proposed as `confirmed` only when
the live value matches the stored one (±0.5%) **and** the department paid the vendor; every other
case is `needs review: …` with the reason (not found, value differs, no payments, department not
on FI$Cal, live check failed). `--record` appends confirmed verdicts to
`references/outlier_reviews.csv`; human verdicts (`corrected`, `reclassified`, or `confirmed` with
other evidence) are added by hand with their evidence. R4 passes for a department when every
contract it currently flags has a closed verdict.

First run (2026-10-31, CAL FIRE / Social Services / PUC; FI$Cal FY2018-19..2025-26 for CAL FIRE
and PUC): 184 flagged, all by robust z; **175 confirmed, 3 reclassified, 6 open**. The
reclassified are PUC Federal Funding Account broadband grants already in class X (not in
procurement totals, not yet disbursed). Open, for a human verdict: Rosenbauer fire apparatus
($32.8M, Sept 2025) and Advanced Fire Control ($3.0M, June 2026) with no payments yet; two BME Fire
Trucks orders ($12.2M, $2.7M) absent from the live day's export though BME was paid $6.3M; two
2016 PUC DDTP phone purchases paid through the program administrator.

The review also surfaced CAL FIRE **legacy duplicate registrations**: 219 agreements from
2016–2018 stored both under the legacy number (`7CA03777`, version 0, end 2020-06-30) and again
under the department-prefixed number (`35407CA03777`, amended and extended) with the same supplier
and start day — $317.7M of legacy value counted twice (the five 2017 contract-county fire
agreements are $220.8M of it). Both records are live in SCPRS. Since PR #187 the warehouse and
`reconcile.contracts` count each such agreement once (`src/registrations.py`; see
docs/WAREHOUSE.md, "Superseded registrations"), with 70 hand-reviewed near-pairs in
`references/superseded_registrations.csv`.
