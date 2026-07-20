# Retrospective — what made this hard, and how to decide what to cut

_Written 2026-07-19, after ~11 days and 90 commits (2026-07-08 → 2026-07-19)._

## TL;DR

The turbulence in this project did **not** come from a poor core architecture or
bad direction. It came from **scope** (four external delivery surfaces on
free-tier/heterogeneous infra) sitting on top of one costly **foundational
constraint** (an intermittent Windows laptop as the pipeline runner), with a few
ordinary solo-dev **process misses** on top. The fix is **subtraction and
consolidation**, not a rebuild.

## The evidence

| Signal | Value | Reading |
|---|---|---|
| Commits touching the ops/serving surface (MCP, Space, web app, Copilot, auth, refresh, keep-warm, tokens, CI, Docker) | **45 / 90** | Most work — and most rework — was integration/deployment |
| Commits touching the core pipeline (scraper, warehouse, marts, grain, supplier, contracts) | **30 / 90** | The actual product came together fast and stayed stable |
| Remediation commits ("fix / actually work / silent failure / P0 / P1") | **~14**, ~12 in the ops layer | Only 2 core-data fixes, both early and surgical |
| Core landed | days 1–2 | Foundation held |
| Ops churn ran | days 3–12 | The edges frayed |

Per-surface maintenance cost (commits mentioning each theme):

| Surface | Commits | Note |
|---|---|---|
| MCP server | 17 | Highest investment; the primary machine-facing channel |
| auth / tokens | 9 | Cross-cutting cost of the least-privilege multi-token design |
| refresh / serve-DB | 9 | Cross-cutting cost of the boot-snapshot serving model |
| web app (Model B) | 4 | Public NL access; Gradio + free-tier Gemini |
| Copilot Studio | 4 | M365 agent connector |
| charts / reports | 4 | Built mainly for Copilot executive reports |
| observability | 4 | Wave 1 (health/canary/contracts) |
| keep-warm | 2 | Exists only to stop the Copilot connection going stale |

## Category verdict

- **Architecture (core data): strong, not the culprit.** Medallion layering,
  disciplined `(document, version)` grain with current-version resolution, canonical
  supplier identity, append-only contract history, one hardened query guard
  (`warehouse_query.py`) reused by every front end, governed column abbreviation.
  These generated almost no rework.
- **Scope: the real multiplier.** Four delivery channels (remote MCP, public web app,
  M365/Copilot connector, chart/report generation) = 4× the token/boot/schema/
  lifecycle failure surfaces. Nearly every remediation commit is an integration tax,
  not a design flaw.
- **Foundational constraint: the deepest root cause.** Running the pipeline on an
  intermittent Windows laptop spawned a whole category of toil — WDAC blocking
  unsigned binaries, the PowerShell `--newest-first` splat bug that silently killed the
  daily job for ~a week, pre-commit resolving to system Python, "only progresses when
  my laptop is on" (the entire reason for Wave 2), and the boot-snapshot-then-manual-
  reboot refresh dance.
- **Execution: mostly good, a few workflow misses.** PR #13 authored but never merged,
  drifted 10 commits, re-applied as #22; the pandas hold created a stale Dependabot PR
  that later conflicted; the flaky MCP smoke test sat red across PRs for days because a
  tight timeout + a `bash -e` footgun hid the real signal.
- **Prompts / direction: not the problem.** Clear and decisive throughout — if
  anything, too effective at greenlighting new surfaces.

The honest reframe: it was rocky because a **lot** was built, on deliberately cheap and
heterogeneous infrastructure, so the bill shows up as integration friction rather than
design debt.

## How to decide what to subtract and consolidate

Don't argue surface-by-surface from taste. Score each surface on four axes, let
**usage** dominate, and apply a fixed decision rule.

### The four axes

1. **Real consumer (weight ×3 — decisive).** Is anything actually using it in the last
   ~30–90 days? This is the one axis you cannot eyeball — it lives in the audit logs.
   - MCP: check the `scprs-query-log` audit dataset (tool-call records) — read with
     `HF_AUDIT_TOKEN`. Zero non-self calls ⇒ no external consumer.
   - Web app: same query-log dataset (web-app query records).
   - Copilot / charts / reports: is there a **live M365 agent** you actually open? Note
     the standing fact (see `docs/COPILOT_STUDIO.md` / memory): *free* Microsoft Copilot
     cannot consume a custom MCP connector — so this channel only has a consumer if you
     pay for M365 Copilot Studio and actively use the agent.
2. **Maintenance cost (×2).** Commits + remediation + unique dependencies + unique
   failure modes it introduces (its own token, its own image, its own CVE stream,
   its own keep-warm). Use the tables above as the baseline.
3. **Uniqueness (×2).** Does it do something no other surface does? A public browser UI
   (web app) and a machine/tool channel (MCP) are genuinely different. Copilot +
   charts + reports mostly re-expose what MCP already returns.
4. **Coupling to remove (×1, inverted).** How much else must change to drop it? Low here
   by design — the shared guard means a front end can be removed without touching the
   query core. Low coupling = cheap to cut.

### The decision rule

Compute `keep_score = 3·consumer + 2·uniqueness − 2·cost` (consumer/uniqueness/cost each
scored 0–2).

- **Keep** — real consumer **and** unique. Invest normally.
- **Consolidate** — real consumer but overlaps another surface. Merge it behind the
  shared guard; don't run it as a separate deploy/image/token.
- **Freeze** — no current consumer but cheap to keep dormant and plausibly future-useful.
  Stop patching proactively; pin it, exclude it from the CVE/dep treadmill, document it
  as frozen. **Frozen still costs** on every dep bump unless you also drop it from CI.
- **Retire** — no consumer **and** ongoing cost (own token/image/CVE stream/keep-warm).
  Delete the deploy, revoke its token, remove its requirements file from CI.

### Applied (provisional — fill the `consumer?` column from the audit log)

| Surface | Consumer? | Unique? | Cost | Provisional disposition |
|---|---|---|---|---|
| **MCP server (stdio)** | You, via Claude Code/Desktop | High | Med | **Keep** — primary channel |
| **MCP server (remote HTTP)** | ? (was for Copilot) | Med | High (own token, image, keep-warm) | **Keep only if** a non-Copilot remote client uses it; else fold away |
| **Web app (Model B)** | ? — check query log | High (only browser UI) | Med (Gradio CVEs, Gemini free tier) | **Keep** if used; else **Freeze** |
| **Copilot Studio connector** | ? — needs paid M365 + active agent | Low (re-exposes MCP) | Med | **Retire/Freeze** unless a live agent exists |
| **Charts / reports** | Mainly Copilot reports | Low | Low–Med | **Freeze** if Copilot is frozen |
| **keep-warm workflow** | Serves Copilot staleness only | — | Low but noisy | **Retire** with the Copilot channel |

The pattern the numbers expose: **Copilot Studio + charts/reports + keep-warm + the
remote-HTTP MCP mode form one cluster whose only justification is an M365 agent.** If you
don't run a paid, active Copilot agent, freezing/retiring that whole cluster is the single
biggest simplification available — it removes a token, an image's keep-warm, a CVE stream,
and a connector schema in one move, with near-zero coupling because the query core is
shared.

## Highest-leverage next actions

1. **Pull the audit log** (`scprs-query-log`, via `HF_AUDIT_TOKEN`) and fill the
   `consumer?` column. One query settles most of the decisions above.
2. **Finish Wave 2 (#23).** It removes the most expensive foundational constraint — the
   laptop — and ends the "only progresses when my laptop is on" problem.
3. **Freeze the M365/Copilot cluster** unless the audit log or a live agent proves a
   consumer. Drop its requirements file(s) from the CVE/dep treadmill so "frozen" is
   actually free.
4. **Extend the Wave 1 observability instinct to deploys.** A post-deploy healthcheck
   gate would have caught the token-scope `RUNTIME_ERROR`s before users did.
5. **Tighten branch discipline.** Merge or close within a day; the #13 drift and the
   stale Dependabot conflict both trace to long-lived branches.

## One-line conclusion

The foundation is sound; the pain was breadth on cheap infra. Subtract the surfaces with
no consumer, consolidate the rest behind the guard you already share, and finish removing
the laptop from the loop.

---

## Postscript — 2026-07-20, one day later

Actions 1, 2, and 4 are done; the audit settled the table:

- **Audit log pulled**: 3 records ever (2 web-app records on 07-13, almost certainly
  self-testing; 1 MCP `run_sql` by the owner on 07-17; **zero** chart/report/Copilot
  calls in the log's life). The `consumer?` column resolves to: MCP **Keep** (owner's
  primary channel, now also the go-live verification channel), web app **Freeze**
  (already private), Copilot cluster **Retire**.
- **Wave 2 finished the same day** (#23/#27/#28): enrichment on a 6h cron with
  stale-first BU rotation, warehouse + serve refresh chained in CI, both laptop
  scheduled tasks disabled. The laptop is out of the loop.
- **Deploy observability shipped** (#26/#29/#30): freshness errors no longer gate the
  publish (the deadlock), go-live is a factory reboot, and every run ends by proving —
  over the token-gated MCP channel — that the Space serves that run's build.
- The retrospective's laptop thesis got a same-day exclamation point: the daily enrich
  job had been **silently dead since 07-17** (the health check's `enrichment_stalled`
  error was the alarm), so the daily refresh had been faithfully republishing a frozen
  snapshot. Both failure modes are now structurally impossible in CI.

Remaining from this document: the Copilot-cluster teardown mechanics (action 3).
