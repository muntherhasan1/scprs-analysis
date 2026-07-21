# Remote MCP server (query the warehouse from anywhere)

This exposes the read-only gold warehouse as a **remote, token-gated MCP
endpoint**, so any MCP client (Claude Desktop, Claude Code, Cursor, …) can query
it in natural language from anywhere. This is "Model A": the server makes **no**
Anthropic API calls — each user's own client does the language reasoning — so
there's no per-token metering on our side, only (free-tier) hosting.

Same code as the local stdio server (`src/mcp_server.py`); `http` mode just adds
a Streamable HTTP transport and a bearer-token gate.

## Security model

- **Read-only by construction** — SQLite opened `?mode=ro`; `run_sql` takes a
  single `SELECT`/`WITH`; table names are allowlisted from `sqlite_master`.
- **Token-gated** — every request needs `Authorization: Bearer <token>`
  (constant-time compared). `/healthz` (and the `/files/` report URLs) are the
  only open paths. The server **refuses to start** in http mode without a token.
- **Per-user, revocable tokens** — set `MCP_AUTH_TOKENS` (`label:token` pairs) to
  give each user their own token; the matched *label* is the principal recorded
  in the audit log and used for per-user rate limiting. See "Issuing & managing
  access tokens" below. A single shared `MCP_AUTH_TOKEN` still works (principal
  `default`); the two merge if both are set.
- **Public data** — SCPRS is a public portal and `data/` holds no PII, so the
  exposure risk is low; the token is there to attribute usage, prevent abuse, and
  cap runaway cost.
- Tokens are deploy-time **secrets** — never baked into the image or committed.

## Test locally

```bash
# From a venv/environment with `mcp` installed:
MCP_AUTH_TOKEN=$(openssl rand -hex 24) python -m src.mcp_server http
# In another shell:
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/healthz          # 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/mcp      # 401
```

Or via Docker (BuildKit picks up `Dockerfile.mcp.dockerignore`, which bakes in
`data/warehouse.db`):

```bash
docker build -f Dockerfile.mcp -t scprs-mcp .
docker run -e MCP_AUTH_TOKEN=$(openssl rand -hex 24) -p 8000:8000 scprs-mcp
```

## Deploy to Hugging Face Spaces

**Cost note:** HF Docker Spaces on free cpu-basic now require a **PRO**
subscription (~$9/mo) — only *static* Spaces are free (`create_repo` returns
HTTP 402 without PRO). The Space still sleeps when idle and cold-starts on the
next request. `deploy/hf-space/deploy.py` assembles the Space from this repo
(reusing `Dockerfile.mcp` as the Space's `Dockerfile`) and pushes it through the
HF API, with the 24 MB DB tracked via git-LFS.

```bash
pip install huggingface_hub
hf auth login                                          # or: huggingface-cli login
# subscribe to PRO once: https://huggingface.co/pro    (Docker Spaces need it)
python -m src.warehouse build                          # ensure data/warehouse.db exists
HF_SPACE=<user>/scprs-warehouse-mcp \
  MCP_AUTH_TOKEN=$(openssl rand -hex 24) \
  python deploy/hf-space/deploy.py                     # creates + pushes the Space
```

The example above sets a single shared `MCP_AUTH_TOKEN` for a first deploy. For
**per-user, revocable** access pass `MCP_AUTH_TOKENS=alice:tok1,bob:tok2` instead
(or as well) — see **Issuing & managing access tokens** below, which is the way
you add/remove users day-to-day without a redeploy.

**Auto-deploy (CI, Wave 3).** After the first manual deploy, the
`Deploy MCP Space` workflow (`.github/workflows/deploy-mcp.yml`) redeploys the
Space automatically whenever its shipped code changes on `main` (the file set in
`deploy.py`'s `COPIES` — `mcp_server.py`, `warehouse_query.py`, the Dockerfile,
`requirements-mcp.txt`, etc.). It runs `deploy.py` with the `HF_DEPLOY_TOKEN`
Actions secret, then `python -m src.deploy_check` verifies the Space came back
**RUNNING on the just-pushed commit's sha** (not the old image) and answers
`/healthz` — a broken build fails the job loudly. `deploy.py` degrades gracefully
under a repo-scoped token: it skips `create_repo` if the Space exists and treats
variable/secret writes as best-effort (they're already set on a redeploy), so the
same script works for both a broad-token first deploy and a scoped-token CI
redeploy. Data deploys (the serve DB) were already continuous; this closes the
loop for code. Only the MCP Space auto-deploys — the chat Space is frozen.

`deploy.py` also sets the `MCP_ALLOWED_HOSTS` **variable** to
`<user>-<space>.hf.space` (so the MCP SDK's DNS-rebinding Host guard stays on
behind HF's proxy — otherwise `/mcp` returns `421 Invalid Host header`) and, if
`MCP_AUTH_TOKEN` / `MCP_AUTH_TOKENS` is in the env, the matching **secret**.
Otherwise add the token secret yourself in **Settings → Variables and secrets**
(the container refuses to start without at least one token). Endpoint:
`https://<user>-scprs-warehouse-mcp.hf.space/mcp`.

> The older `deploy/hf-space/sync.sh` pushes via `git` instead of the API. It
> needs a **classic Write token** that sets a git credential — an `hf auth login`
> OAuth token authenticates the API but not git-over-HTTPS, so `sync.sh` fails
> with "Invalid username or password." Prefer `deploy.py`.

## Issuing & managing access tokens

Each user gets their **own** token so you can attribute queries in the audit log,
rate-limit per person, and revoke one user without disrupting the rest. Tokens
live only in the Space secret `MCP_AUTH_TOKENS` — never in the repo or the image.

### The format

`MCP_AUTH_TOKENS` is a comma-separated list of `label:token` pairs:

```
alice:9f3c…, bob:1a7e…, dana:c02b…
```

- **label** — a short principal name (the person/agent). Appears in the audit log
  and is the rate-limit key. Keep it human-readable: `alice`, `copilot-prod`.
- **token** — the secret the user puts in their client. Generate a strong random
  one **per user** (never reuse):

  ```powershell
  python -c "import secrets; print(secrets.token_hex(24))"   # 48-hex-char secret
  ```

A malformed pair (missing `:`, empty label/token) makes the server **fail at
boot** rather than silently lock someone out — so a typo is caught immediately.

### Where they're set

`MCP_AUTH_TOKENS` is a **secret** on the Space: **Settings → Variables and secrets
→ New secret**. Editing it there and saving triggers a Space restart, so changes
take effect within a minute. (`deploy.py` can also set it if `MCP_AUTH_TOKENS` is
in the deploy environment, but for day-to-day user churn, edit the secret directly
in the Space UI — no redeploy needed, and no token ever touches your shell history
or the repo.)

### Add a user

1. Generate a token: `python -c "import secrets; print(secrets.token_hex(24))"`.
2. In **Space → Settings → Variables and secrets**, edit `MCP_AUTH_TOKENS` and
   append `,newlabel:newtoken`.
3. Save (the Space restarts). Send the user **only their token** and the endpoint
   URL — over a secure channel, not email/chat in the clear.

### Revoke a user

Edit `MCP_AUTH_TOKENS`, delete that user's `label:token` pair, save. Their token
stops working on the next restart; everyone else's keeps working. (Rotate instead
of revoke by replacing just their token value.)

### Rate limiting (optional)

Set the **variable** `MCP_RATE_LIMIT_PER_MIN` (e.g. `30`) to cap requests **per
principal** per minute — one heavy user can't starve the others. `0` or unset
disables it. It's a per-process in-memory fixed window (fine for the single-worker
Space); it is not a defense against a distributed flood — that's the host's job.

### Verify a token works

```powershell
$env:TOK="alice-token-here"
# 200 — open health check, no token needed:
curl.exe -s -o NUL -w "%{http_code}`n" https://munther-hasan-scprs-warehouse-mcp.hf.space/healthz
# 401 without a token, 200/406 with one (406 = MCP handshake needs the right Accept header; auth still passed):
curl.exe -s -o NUL -w "%{http_code}`n" -X POST https://munther-hasan-scprs-warehouse-mcp.hf.space/mcp
curl.exe -s -o NUL -w "%{http_code}`n" -X POST -H "Authorization: Bearer $env:TOK" https://munther-hasan-scprs-warehouse-mcp.hf.space/mcp
```

A `401` with the token means the token isn't in `MCP_AUTH_TOKENS` (or the Space
hasn't restarted yet). Anything **other than 401** with the token means auth
passed — the real client handshake will then succeed.

## Deploy to Fly.io (alternative — usage-based, needs a card)

Fly is not truly free — it bills usage above a small allowance and requires a
card — but `fly.toml` scales the machine to zero when idle, keeping a tiny app
to cents/month; the first request after idle pays a short cold start.

```bash
fly launch --no-deploy                         # or: fly apps create <name>; edit app= in fly.toml
fly secrets set MCP_AUTH_TOKEN=$(openssl rand -hex 24)   # store the token
fly deploy                                     # builds Dockerfile.mcp, ships it
fly secrets list                               # confirm MCP_AUTH_TOKEN is set
```

Your endpoint is then `https://<app>.fly.dev/mcp`.

## Connect a client

**Claude Code** (or any `.mcp.json`-based client):

```json
{
  "mcpServers": {
    "scprs-warehouse-remote": {
      "type": "http",
      "url": "https://<app>.fly.dev/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

Or via the CLI:

```bash
claude mcp add --transport http scprs-warehouse-remote https://<app>.fly.dev/mcp \
  --header "Authorization: Bearer <your-token>"
```

**Claude Desktop (end-user setup).** Claude Desktop has no native remote-MCP
field, so it connects through the **`mcp-remote`** bridge (a small npm helper that
speaks stdio to Desktop and forwards to the HTTP endpoint with your bearer token).

*Prerequisite:* **Node.js** installed (provides `npx`). Get it from
<https://nodejs.org> (LTS) if `node -v` fails in a terminal.

1. **Open the config file.** Easiest: Claude Desktop → **Settings → Developer →
   Edit Config** — this opens the correct file regardless of how Desktop was
   installed. (On the **Microsoft Store / MSIX** build the file is *not* at
   `%APPDATA%\Claude\...`; it lives under the package container at
   `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`.
   Editing the `%APPDATA%` path on that build looks fine but does nothing.)

2. **Add the server.** On **Windows** wrap the launch in `cmd /c` so the sandbox
   can find `npx` (a bare `"command": "npx"` often fails to spawn on the Store
   build):

   ```json
   {
     "mcpServers": {
       "scprs-warehouse-remote": {
         "command": "cmd",
         "args": ["/c", "npx", "-y", "mcp-remote",
                  "https://munther-hasan-scprs-warehouse-mcp.hf.space/mcp",
                  "--header", "Authorization: Bearer YOUR_TOKEN_HERE"]
       }
     }
   }
   ```

   On **macOS/Linux** drop the `cmd /c` wrapper — use `"command": "npx"` with the
   args from `"-y"` onward. Replace `YOUR_TOKEN_HERE` with the token you were
   issued (the label is *not* included — just the secret).

3. **Fully restart Claude Desktop** (quit from the tray/menu, not just close the
   window) so it reloads the config and launches the bridge.

4. **Confirm it connected** — the server appears with a tools/plug icon in the
   chat; you should see `list_marts`, `data_dictionary`, `describe_table`,
   `run_sql`, `generate_chart`, `generate_report`. Then just ask, e.g. *"Which
   canonical suppliers had the highest total value, and what do they mostly
   supply?"* — the model calls `list_marts` / `data_dictionary` / `run_sql` for you.

**First-call cold start:** the Space sleeps when idle, so the very first query
after a quiet period can take ~30–60 s to wake the container, then it's fast.

**Troubleshooting:**

- *Server shows an error / won't start* → check the bridge log at
  `…\LocalCache\Roaming\Claude\logs\mcp-server-scprs-warehouse-remote.log` (MSIX)
  or `%APPDATA%\Claude\logs\...` (standard installer).
- *401 in the log* → the token is wrong, or not in the Space's `MCP_AUTH_TOKENS`
  yet (ask the admin; the Space must have restarted after being added).
- *`npx` not found / spawn error* → Node isn't installed or the `cmd /c` wrapper
  is missing on Windows.
- *`421 Invalid Host header`* → server-side `MCP_ALLOWED_HOSTS` misconfig, not a
  client problem — tell the admin.

## Charts & executive reports

Two tools go beyond raw data (all over the same read-only guard):

- **`generate_chart(sql, kind, title, x, y)`** — runs a `SELECT` and returns a
  **PNG** (`kind` = `bar`/`line`/`pie`). MCP clients that render images show it
  inline.
- **`generate_report(title, sections)`** — each section is a heading + a `SELECT`
  + optional prose + optional chart; returns a link to a **self-contained HTML
  report** (charts embedded) served at an unauthenticated `/files/<unguessable>`
  capability URL, so it opens in any browser or is embeddable in a Copilot card.

Rendering is matplotlib (Agg backend); the server still makes no LLM calls — the
calling model writes the SQL and the prose. This is what a **Microsoft 365 /
Copilot Studio agent** uses to produce query results *and* executive reports:
connect the agent to this MCP server, and it calls `run_sql` for numbers,
`generate_chart` for a visual, and `generate_report` for a shareable summary.

## Audit log (optional)

The server can persist an **audit trail of every `run_sql` / `generate_chart` /
`generate_report` call** — the SQL sent, row count, and error/outcome (never the
result rows, and never any secret). This is what tells you what a Copilot Studio
agent actually queried; the end user's natural-language prompt never reaches the
server (it stays on the Copilot side), so the SQL is what we can see and record.

It's **off by default** — a no-op unless you set two secrets on the Space
(**Settings → Variables and secrets**), matching the web app's capture:

- `QUERY_LOG_DATASET` — a **private** HF dataset id to sync to, e.g.
  `muntherhasan1/scprs-query-log` (created on first write).
- `QUERY_LOG_TOKEN` — an HF token with **write** access to that dataset (a Space's
  filesystem is ephemeral, so records are appended locally and committed to the
  dataset every few minutes via `CommitScheduler`). Dedicated so `HF_TOKEN` can
  stay **read-only** (it only needs read on the serve-DB dataset for `data_sync`);
  falls back to `HF_TOKEN` if unset.

Records carry `source: "mcp"` (vs `"web"` for the NL app), so both front ends can
share one dataset. To avoid the two Spaces clobbering each other's file (the
`CommitScheduler` syncs by overwriting a path), each writer owns its own
`data/queries-<space>.jsonl`, namespaced by the HF `SPACE_ID`. `QUERY_LOG_DIR`
(the local staging dir) defaults to a writable `/app/query_logs` in the image.

## Refreshing the data

On Hugging Face the DB is **not** baked into the image — the Space fetches
`warehouse-serve.db` from the private `WAREHOUSE_DATASET` at boot
(`src/data_sync.ensure_local_db`), so refreshing data is decoupled from code
deploys. The flow is: rebuild the warehouse → export the slim serve DB → publish it
to the dataset → factory-reboot the Space so it re-fetches.

See **[docs/PIPELINE.md](PIPELINE.md)** for the full procedure, the token model
(`HF_WAREHOUSE_TOKEN`), and the `scripts/refresh_pipeline.ps1` automation.

(The scraper stays local — only the built serve DB is shipped to the dataset.)
