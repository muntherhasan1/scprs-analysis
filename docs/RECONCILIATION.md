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
Social Services' own agreement 22-3100 is unregistered). Renamed vendors read as unexplained until
aliased (DynCorp → Amentum, $45.6M at CAL FIRE).

First run (2026-10-31, CAL FIRE / Social Services / PUC, FY2022-23..2024-25, $4.14B in scope):
91.6% of dollars within award, 4.0% local agency reimbursement, 1.6% other labeled mechanisms,
2.7% unexplained (about 1.6% once DynCorp is aliased).
