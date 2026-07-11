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
- **Token-gated** — every request needs `Authorization: Bearer $MCP_AUTH_TOKEN`
  (constant-time compared). `/healthz` is the only open path. The server
  **refuses to start** in http mode without a token.
- **Public data** — SCPRS is a public portal and `data/` holds no PII, so the
  exposure risk is low; the token is there to prevent abuse and runaway cost.
- The `MCP_AUTH_TOKEN` is a deploy-time secret — never baked into the image.

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

## Deploy to Hugging Face Spaces (recommended — genuinely free)

Public Docker Spaces run free on HF's CPU hardware. The Space is a separate git
repo; `deploy/hf-space/sync.sh` assembles it from this repo (reusing
`Dockerfile.mcp` as the Space's `Dockerfile`) and pushes it, with the 24 MB DB
tracked via git-LFS.

```bash
pip install huggingface_hub && huggingface-cli login   # one-time
git lfs install
# Create the Space once (Docker SDK): https://huggingface.co/new-space
python -m src.warehouse build                          # ensure data/warehouse.db exists
HF_SPACE=<user>/scprs-warehouse-mcp bash deploy/hf-space/sync.sh
```

Then in the Space's **Settings → Variables and secrets**, add secret
`MCP_AUTH_TOKEN` (a long random string). Endpoint:
`https://<user>-scprs-warehouse-mcp.hf.space/mcp`. Free Spaces sleep after
inactivity and cold-start on the next request.

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

**Claude Desktop** — if it lacks native remote-MCP config, use the `mcp-remote`
bridge in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "scprs-warehouse-remote": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<app>.fly.dev/mcp",
               "--header", "Authorization: Bearer <your-token>"]
    }
  }
}
```

Then just ask questions — e.g. *"Which canonical suppliers had the highest total
value, and what do they mostly supply?"* The client calls `list_marts` /
`data_dictionary` / `run_sql` under the hood.

## Refreshing the data

The DB is a read-only snapshot baked into the image. To publish fresh data:

```bash
python -m src.warehouse build      # rebuild data/warehouse.db locally
fly deploy                         # rebuild + ship the image with the new DB
```

(The scraper stays local — only the built warehouse is shipped.)
