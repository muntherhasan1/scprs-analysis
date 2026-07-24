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

- **triage.yml**: failed/cancelled main-branch runs → idempotent issue,
  auto-closes on recovery.
- **pipeline-monitor.yml** (6h): watches from OUTSIDE — last successful enrich
  age + serve-dataset commit age (14h thresholds) + cron keep-alive (GitHub
  disables schedules after 60 idle days).
- **healthchecks.io dead-man's switch** (`HEALTHCHECK_PING_URL`): the one alarm
  outside GitHub — pinged by healthy enrich runs and clean monitor passes;
  silence ≈14h pages by email even if GitHub's schedulers are the thing that died.

## Tokens (least privilege, 4-token model)

`HF_SCPRS_TOKEN` (operational RW — enrich/cmas only) · `HF_SCPRS_READ_TOKEN`
(operational RO — PR-executed warehouse-diff) · `HF_WAREHOUSE_TOKEN` (serve
dataset RW) · `HF_DEPLOY_TOKEN` (Space restarts/deploys). A missing deploy
token must FAIL a deploy, never green-skip. Wrong scopes are the #1 cause of
RUNTIME_ERROR Spaces and false go-live verdicts.

## Local dev notes

WDAC blocks unsigned binaries intermittently (WinError 4551): pre-commit hooks
run as local `python -m` hooks, and pre-commit needs the `.venv` ACTIVATED.
