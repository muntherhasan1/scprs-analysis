---
title: SCPRS Warehouse Chat
emoji: 💬
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Ask the SCPRS procurement warehouse in plain English
---

# SCPRS Warehouse Chat

Ask California's SCPRS procurement warehouse questions in plain English and get a
short answer, the SQL behind it, and the underlying rows. **Read-only, public
data** — no login. The natural-language→SQL step uses a free-tier LLM
(Google Gemini); every query runs through a hardened read-only guard
(single `SELECT`/`WITH`, connection opened `?mode=ro`).

**This Space is generated — do not edit here.** Source lives in
[`scprs-analysis`](https://github.com/muntherhasan1/scprs-analysis); it's pushed
via `deploy/hf-chat/deploy.py`. See `docs/REMOTE_MCP.md` there.

## Configuration

Set one secret in **Settings → Variables and secrets**:

- `GEMINI_API_KEY` — a free key from <https://aistudio.google.com/apikey>. With
  no billing account attached, the free tier rate-limits rather than charges.

Optional: `GEMINI_MODEL` (default `gemini-2.5-flash`).
