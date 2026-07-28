---
name: pipeline-ops
description: Operate, debug, and harden the CI/CD pipeline — incident triage, workflow changes, deploy/rollback, tokens, alerting. Use when a pipeline run fails/goes quiet, when adding or editing any GitHub Actions workflow, or when touching deploy/rollback/publish logic. Encodes the 2026-07 audit protocols.
---

# Pipeline ops: the protocols that keep this device-free pipeline honest

Born from the 2026-07 BU-3540 livelock (12 silent timeout kills, 3 days of
stale serving, zero alerts) and the audit that followed (issues #43–#56).

## Incident drill (a run failed or something looks stale)

1. `gh issue list --label pipeline-failure` and `--label pipeline-monitor-alert`
   — triage usually already names the failing step and likely cause.
2. `gh run list` — check conclusions **including `cancelled`** (a
   `timeout-minutes` kill concludes cancelled, not failure).
3. Read the run log tail; every pipeline workflow sets `PYTHONUNBUFFERED` so a
   killed run still shows how far it got.
4. Data safety reasoning: publishes are upload-on-success and atomic — a killed
   run cannot half-publish. Rollback fires ONLY on verified marker mismatch.
5. Verify serving with `python -m src.golive_check` (never the local stdio MCP
   tools — those hit the local DB, not the Space).

## Change-impact checklist (run BEFORE any change to data volume, cadence, or surfaces)

Born 2026-07-28: a 5x data expansion doubled build times and nobody's plan
mentioned it. Answer these five in the plan, in writing:

1. **Capacity** — what happens to build/scrape/publish DURATION and does it
   still fit inside every `timeout-minutes` it runs under (check the build
   timings trend in recent enrich step summaries)?
2. **Storage/cost** — what grows (HF LFS revisions, dataset size, runner
   minutes) and at what rate?
3. **Consumers** — which downstream surfaces bind to what this changes
   (MCP/NL schema, mart CSV columns, star parquet keys, Copilot, dashboards)?
   Column names in the BI feed are a public contract.
4. **Alarms** — which existing monitor covers the new failure/erosion mode;
   if none, add one IN THE SAME CHANGE (report-only is fine).
5. **Rollback** — how does this un-ship if wrong (and does the shrink-gate /
   upload-on-success contract already cover it)?

## Invariants for ANY workflow change

- **Upload-on-success**: gates run before publishes; a blocked publish leaves
  state so the next run retries safely.
- **Work forfeit only for integrity**: availability-shaped failures (canary
  NOT_FOUND, site unreachable) alert but never discard a run's banked work.
- **Rollback only on positive evidence** the served data is wrong (go-live
  rc=1 mismatch). Timeouts/unreachable = fail loudly, touch nothing (rc=2).
- **Best-effort steps must emit machine-readable outcomes** (step outputs,
  exit codes like `restart-spaces --require`) — soft-fail that looks like
  success caused the worst audit findings.
- **Budgets under backstops**: long work self-budgets (`--budget-minutes`) and
  exits 0 with banked progress; `timeout-minutes` sits above it as backstop.
- **Timeouts need a measured basis** (state it in a comment) and results go to
  `GITHUB_STEP_SUMMARY` so erosion is a visible trend.
- **Single writer or CAS**: operational-dataset writers share the
  `scprs-operational-writer` group; fetched-then-mutated publishes pass
  `parent_commit`.
- New cron workflow? Wire ALL THREE: a `_HINTS` entry in `src/triage.py`,
  pipeline-monitor coverage (it keep-alives every workflow automatically), and
  ask whether it should ping the dead-man's switch.
- Workflow inputs pass via `env:`, never inline `${{ }}` in bash.

## Alerting stack (who watches what)

- **recon.yml** (weekly, Wed): ground truth — samples (BU, month) windows and
  compares the store against the LIVE site; the only check that catches
  silently-lost data (all others are internal-consistency).
- **backup.yml** (weekly, Sun): cold-copies scprs.db to the backup dataset;
  fails loudly until `HF_BACKUP_TOKEN` exists.
- **query-review.yml** (weekly, Mon): replays logged user questions; content
  regressions become issues.

- **triage.yml**: failed/cancelled main-branch runs → idempotent issue,
  auto-closes on recovery.
- **pipeline-monitor.yml** (6h): watches from OUTSIDE — last successful enrich
  age + serve-dataset commit age (8h thresholds; enrich runs every 3h) + cron
  keep-alive (GitHub disables schedules after 60 idle days).
- **healthchecks.io dead-man's switch** (`HEALTHCHECK_PING_URL`): the one alarm
  outside GitHub — pinged by healthy enrich runs and clean monitor passes;
  grace ≈14h pages by email even if GitHub's schedulers are the thing that died
  (can be tightened to ~8h in the healthchecks.io UI now that pings are 3-hourly).

## Tokens (least privilege, 4-token model)

`HF_SCPRS_TOKEN` (operational RW — enrich/cmas only) · `HF_SCPRS_READ_TOKEN`
(operational RO — PR-executed warehouse-diff) · `HF_WAREHOUSE_TOKEN` (serve
dataset RW) · `HF_DEPLOY_TOKEN` (Space restarts/deploys). A missing deploy
token must FAIL a deploy, never green-skip. Wrong scopes are the #1 cause of
RUNTIME_ERROR Spaces and false go-live verdicts.

## Local dev notes

WDAC blocks unsigned binaries intermittently (WinError 4551): pre-commit hooks
run as local `python -m` hooks, and pre-commit needs the `.venv` ACTIVATED.
