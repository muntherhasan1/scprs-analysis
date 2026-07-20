# Wave 2 scope — cloud orchestration (device-free pipeline)

> Part of the platform-hardening roadmap. Wave 1 (observability: `health`/`canary`/
> `contracts`) was authored in PR #13 but never merged; it lands on `main` as the
> prerequisite for this wave (the enrichment canary gates on those checks).

## Goal

Move the operational store (`data/scprs.db`) and the recurring work — enrichment,
the Wave-1 checks, and the serve-DB refresh — off the intermittent collection
laptop into **GitHub Actions**, so the pipeline advances 24/7 with no local machine
running. Solves the "only progresses when my laptop is on" problem.

Non-goal (for this wave): moving the heavy full-range summary scrape (`model.build`)
into CI. That stays a manual/occasional local step; Wave 2 automates the *incremental*
work (`model.enrich`) and the downstream refresh.

## Why now

Enrichment only advances when someone runs the daily job locally, and the refresh
cycle (build → export → publish → reboot) is laptop-bound. PR #20 already made the
refresh **publish** unattended; Wave 2 removes the laptop from the loop entirely.

## Enabling facts (verified 2026-07-17)

- The repo is **public** → GitHub Actions minutes are effectively free (no 2000-min
  private-repo cap).
- `data/scprs.db` is **167 MB** → fits a private HF dataset via LFS; each write is a
  versioned commit, giving free data versioning.
- `model.enrich` is already **resumable/incremental**: it visits distinct
  `start_date`s in `purchases`, records each finished day in `details_progress`,
  skips done days, and supports `--limit` / `--newest-first`. Purpose-built for
  short, repeatable CI slices — a failed run just retries the unrecorded day.
- CI already runs Python 3.12 on `ubuntu-latest` (`.github/workflows/ci.yml`). The
  scraper is headless Playwright/Chromium, installable in Actions.

## Progress

- **2a — done** (this PR). `fetch_operational_db` / `publish_operational_db` added to
  `src/data_sync.py` with `fetch-operational` / `publish-operational` CLI subcommands,
  a dedicated `HF_SCPRS_TOKEN` (falls back to `HF_TOKEN`), and tests.
- **Canary workflow — done** (this PR). `.github/workflows/enrich-canary.yml`,
  `workflow_dispatch`-only, implements the full 2b loop at `--limit 3` so the round-trip
  and Playwright-in-CI can be proven by hand before a schedule is added.
- **Remaining:** promote the canary to a cron schedule (2b), chain the refresh (2c),
  and the operational-DB seed + single-writer cutover (below).

### Before the first run — seed + secret
1. Seed the dataset once from a local checkout (CI fetches it, so it must exist):
   `python -m src.data_sync publish-operational --dataset munther-hasan/scprs-operational-db`
   (needs `HF_SCPRS_TOKEN` or a cached HF login with write on that dataset).
2. Add the `HF_SCPRS_TOKEN` **Actions secret** (read+write on the dataset). Optionally
   set a `SCPRS_DATASET` **repo variable** to override the default dataset id.
3. Run **Enrich canary (Wave 2)** from the Actions tab. Green = Playwright + round-trip work.

## Sub-phases (in order)

### 2a — `scprs.db` as HF source of truth  ✅
Add fetch/publish for `scprs.db` to `src/data_sync.py`, mirroring the serve-DB
pattern (`ensure_local_db` / `publish_serve_db`), targeting a **new private dataset**
(`munther-hasan/scprs-operational-db`). Establish the
**download → mutate → upload-on-success** contract, with CI as the sole writer.

### 2b — Enrichment in Actions (the core)
A scheduled workflow (cron): fetch `scprs.db` → `model enrich --limit N
--newest-first` → run the Wave-1 checks (`health`/`canary`/`contracts`, gate the run
on any `error` finding) → publish `scprs.db` back. Guardrails:
- **Concurrency group** serializes runs — never two writers mutating `scprs.db` at
  once.
- **Upload only on success** — a failed/partial run leaves the dataset untouched and
  the day unrecorded, so the next run safely retries (the incremental design already
  guarantees this).
- Secret: an HF token with **read+write** on the `scprs.db` dataset (Actions secret).

### 2c — Refresh in Actions
Chain `warehouse build → serve-export → data_sync publish` (all reused from PR #20)
after enrichment. Restarting the Spaces to go live is the **auto-restart backlog
item** (`docs/BACKLOG.md`) — needed here to close the loop fully device-free.

### 2d — (later) Dagster + lineage
Pipeline-as-assets and richer data versioning. Deferred; the Actions workflows are
the MVP.

## Decisions to make

1. **CI scraping scope** — enrichment only (incremental, bounded) vs also the heavy
   summary `build`. *Recommend: enrichment in CI; keep summary builds manual.*
2. **Single-writer cutover** — once CI owns `scprs.db`, local enrichment must stop or
   the two diverge. *Recommend: clean cutover after a final local sync.*
3. **Dataset** — a dedicated private `scprs-operational-db` vs reusing an existing
   namespace.
4. **Cadence / `--limit`** — cron frequency and slice size (browser scraping is slow).

## Risks / unknowns

- **Playwright reliability in CI** — the biggest one. The stateful PeopleSoft quirks
  (typed dates, the 65k-row bisection) work headless locally, but cloud IPs and the
  runner environment differ, and CA's site could rate-limit/block cloud IPs. Prove it
  with an early canary before trusting a schedule.
- **167 MB round-trip per run** — minor time/bandwidth; acceptable.
- **Mid-run failure integrity** — mitigated by upload-on-success + incremental
  progress.
- **Secret scope creep** — CI ends up needing `scprs.db`-write, `warehouse-data`-write,
  and (for auto-restart) Spaces-manage tokens, all as Actions secrets.

## Recommended first step

Build **2a + a single canary enrichment workflow**: fetch `scprs.db`, run *one* small
`--limit` slice in Actions, run the Wave-1 checks, and publish back. This proves
Playwright + the round-trip work in CI before building the full schedule —
de-risking the largest unknown cheaply.
