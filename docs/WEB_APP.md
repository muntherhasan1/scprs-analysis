# Natural-language web app (ask the warehouse in plain English)

A public web page where **anyone with the link** types a question in plain
English and gets a short answer, the SQL behind it, and the underlying rows — no
login, no client to install. This is the broad-reach counterpart to the
[remote MCP server](REMOTE_MCP.md): both are read-only front ends over the same
gold warehouse and share the hardened query guard in `src/warehouse_query.py`.

Why a web app and not "Microsoft Copilot": the free, pre-installed Copilot in
Windows/Teams can't consume a custom data source — MCP/connector extensibility
lives only in the paid/developer Copilot surfaces (Copilot Studio, GitHub
Copilot). A browser page is the one interface everyone already has, and the link
drops cleanly into Teams or email.

## How it works

```
question ──▶ nl_query.generate_sql ──▶ warehouse_query.run_select ──▶ nl_query.summarize ──▶ answer
             (free-tier Gemini)          (read-only, SELECT-only)       (free-tier Gemini)
```

- **`src/warehouse_query.py`** — the single hardened surface: connection opened
  `?mode=ro` (writes impossible), `run_select` accepts one `SELECT`/`WITH` only,
  object names checked against a live `gold_*`/`lv_*`/`dim_*`/`fact_*` allowlist.
- **`src/nl_query.py`** — Google Gemini writes the SQL and phrases the answer.
  The model never touches the database; its SQL always goes through the guard.
- **`src/web_app.py`** — a Gradio chat UI (`app.py` launches it).

**Cost:** the Gemini free tier is genuinely free — with **no billing account
attached** the key is rate-limited, never charged. Data is public (SCPRS), so the
open, read-only endpoint is low-risk.

## Run locally

```bash
pip install -r requirements-web.txt
export GEMINI_API_KEY=...           # free key: https://aistudio.google.com/apikey
python -m src.warehouse build       # ensure data/warehouse.db exists
python -m src.web_app               # serves http://127.0.0.1:7860
```

## Deploy to a Hugging Face Space

Same API-push flow as the MCP server (see [REMOTE_MCP.md](REMOTE_MCP.md) for the
PRO-subscription and token caveats). Docker Space, `Dockerfile.web`, DB via LFS.

```bash
pip install huggingface_hub && hf auth login
python -m src.warehouse build
HF_SPACE=<user>/scprs-warehouse-chat \
  GEMINI_API_KEY=<free-key> \
  python deploy/hf-chat/deploy.py
```

`deploy.py` creates/pushes the Space and, if `GEMINI_API_KEY` is in the env, sets
it as a Space secret (otherwise add it in **Settings → Variables and secrets** —
the app shows a clear error until it's set). App URL:
`https://<user>-scprs-warehouse-chat.hf.space`. Optional `GEMINI_MODEL` variable
(default `gemini-2.5-flash`).

## Refreshing the data

The DB is a read-only snapshot baked into the image. To publish fresh data,
rebuild the warehouse locally and redeploy:

```bash
python -m src.warehouse build
HF_SPACE=<user>/scprs-warehouse-chat python deploy/hf-chat/deploy.py
```
