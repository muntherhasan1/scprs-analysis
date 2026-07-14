---
title: SCPRS Warehouse MCP
emoji: 📊
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 8000
pinned: false
short_description: Read-only, token-gated MCP endpoint for the SCPRS warehouse
---

# SCPRS Warehouse MCP (remote)

Read-only [MCP](https://modelcontextprotocol.io/) endpoint over the SCPRS gold
warehouse, so any MCP client (Claude Desktop/Code, Cursor, …) can query it in
natural language. The server makes **no** LLM API calls — your client does the
reasoning.

**This Space is generated — do not edit here.** Source lives in
[`scprs-analysis`](https://github.com/muntherhasan1/scprs-analysis); it's pushed
via `deploy/hf-space/sync.sh`. See `docs/REMOTE_MCP.md` there.

## Use it

1. In the Space **Settings → Variables and secrets**, add a secret
   `MCP_AUTH_TOKEN` (a long random string). The container refuses to start
   without it.
2. Point your MCP client at `https://<user>-scprs-warehouse-mcp.hf.space/mcp`
   with header `Authorization: Bearer <your-token>`.

Endpoint is token-gated; `/healthz` is the only open path.
