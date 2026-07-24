---
name: external-research
description: Research and vet external/context data — supplier firmographics, economic indicators, budgets, news — before it touches the pipeline. Use when enriching suppliers via web research, evaluating a candidate data source (eProcure, ebudget/DOF, FRED, GDELT), or adding economic context to analyses.
---

# External research: enrichment and source vetting

## Supplier enrichment (the shipped pattern)

- Web-researched firmographics live in their own store
  (`supplier_enrichment.db` via `src/supplier_research.py`), published as an
  optional side input, folded into gold **by normalized name** — so enrichment
  attaches to the canonical entity, not one registration.
- Multi-source verification before storing: a firmographic claim (size, HQ,
  parent) needs two independent sources or gets stored with a confidence
  qualifier. Prefer primary sources (SoS registrations, company sites) over
  aggregators.
- New enrichment attributes: add the column, rebuild (abbreviation pass is
  automatic), extend the supplier marts.

## Vetting a candidate external source (do this BEFORE extraction work)

Score against this checklist; a weak join key or unstable publisher kills more
integrations than hard scraping does:
1. **Join key** to existing entities — BU/department code (best; via
   `references/departments.csv`), normalized supplier name (proven), or month
   (`dim_date`). No join key → context store, not warehouse.
2. **License/terms** permit automated collection and republication of derived
   aggregates.
3. **Stability**: official publisher, versioned/archived releases, machine-
   readable format. One-off PDFs are a red flag.
4. **Refresh cadence** matched to a cron (annual budgets ≠ daily news).
5. **Volume/shape** small enough for the SQLite side-input pattern.

Current ranked candidates (2026-07): Cal eProcure (CSCR awards/solicitations +
SB/DVBE registry; joins by supplier name + dept), ebudget/DOF appropriations
(joins by dept code; enables spend-vs-budget marts), FRED (CPI/indicators;
joins by month; enables real-dollar deflation), GDELT/RSS news (no reliable
join — context feed for the NL app, NOT warehouse facts).

## Economic context in analyses

- Contract dollars are nominal; for multi-year comparisons fetch CPI (FRED)
  and state whether figures are deflated.
- Budget context: procurement spend is meaningful relative to department
  appropriations — cite the appropriation year and source when comparing.
- News/events: attribute impacts cautiously ("coincides with", not "caused")
  unless the mechanism is documented.
