# Microsoft 365 agent over the SCPRS warehouse (Copilot Studio + MCP)

Build a Copilot Studio agent that connects to the remote **MCP server**
([REMOTE_MCP.md](REMOTE_MCP.md)) and produces **query results, charts, and
executive reports** inside Microsoft Teams / M365 Copilot. The MCP server is the
data + reporting backend (it makes no LLM calls); Copilot's own model writes the
SQL and prose, calling the server's tools.

> Microsoft's Copilot Studio UI changes often. The steps below follow the
> documented MCP flow; adapt labels to the current portal. MCP support may be
> behind a preview toggle or an admin/DLP policy in your tenant.

## What you'll connect to

| Setting | Value |
|---|---|
| MCP endpoint | `https://munther-hasan-scprs-warehouse-mcp.hf.space/mcp` |
| Transport | **Streamable HTTP** |
| Auth | header `Authorization: Bearer <MCP_AUTH_TOKEN>` (your Space secret) |
| Tools | `list_marts`, `describe_table`, `data_dictionary`, `run_sql`, `generate_chart`, `generate_report` |

Keep `<MCP_AUTH_TOKEN>` out of shared docs — it's the same secret set on the
Space. Rotate it (regenerate + update the Space secret) if it leaks.

## Prerequisites

- A Microsoft 365 **work/school** tenant with **Copilot Studio** access (and, to
  surface the agent in the Copilot app, **M365 Copilot** licensing).
- Rights to **create custom connectors** and **publish agents** — or an admin who
  can approve them. Connecting an external endpoint often triggers a DLP review.
- The MCP server running (it is, on the HF Space) and its bearer token.

## Step 1 — Create the agent

1. Go to <https://copilotstudio.microsoft.com> and pick your environment.
2. **Create → New agent**. Name it e.g. *SCPRS Procurement Analyst*; give it a
   one-line description. Skip the generative-knowledge sources (our data comes
   from the tool, not uploaded files).

## Step 2 — Add the MCP server as a tool (native wizard)

Use Copilot Studio's built-in wizard — **no custom connector needed** (that's a
premium Power Platform feature and often unavailable). In the agent: **Tools →
+ Add a tool → Model Context Protocol → Add a Model Context Protocol server**, and
fill in **exactly**:

| Field | Value |
|---|---|
| Server name | `SCPRS Warehouse` |
| Server description | `Read-only California SCPRS procurement warehouse` (required) |
| **Server URL** | `https://munther-hasan-scprs-warehouse-mcp.hf.space/mcp` — the **full path including `/mcp`** |
| Authentication | **API key** |
| Type | **Header** |
| Header name | `Authorization` |
| API key / value | `Bearer <MCP_AUTH_TOKEN>` |

Then **Create**. Copilot Studio calls the server, loads the six tools
(`list_marts`, `describe_table`, `data_dictionary`, `run_sql`, `generate_chart`,
`generate_report`), and you enable them.

Two mistakes cause **"Connector request failed — couldn't retrieve the requested
items"** (a 401 from the server):
- **URL missing `/mcp`** — it must be the complete endpoint path, not the host.
- **Header value wrong** — header name must be `Authorization` and the value
  `Bearer <token>`. (The server also accepts the bare `<token>` without `Bearer`,
  so either works — but the header name must be `Authorization`.)

> There are no MCP "resources" to load — this server exposes **tools only**, so an
> empty resources list is normal, not an error.

> If your tenant *does* have premium custom connectors, you can instead import
> `deploy/copilot-studio/mcp-connector.swagger.yaml` (declares
> `x-ms-agentic-protocol: mcp-streamable-1.0`) via make.powerapps.com → Custom
> connectors — but the native wizard above is the simpler path.

## Step 3 — Agent instructions (paste this)

This is the important part: Copilot's model writes the SQL, so it needs the same
domain rules that make the answers correct. Paste into the agent's **Instructions**:

```text
You are a California SCPRS procurement analyst. Answer questions about state
procurement by querying a read-only data warehouse through your tools, and produce
charts and executive reports on request. Always base answers on tool results — never
invent numbers — and note that figures come from public SCPRS data.

TOOLS
- run_sql(query): run ONE read-only SELECT/WITH; returns rows. Use for query results.
- generate_chart(sql, kind, title): render a bar|line|pie PNG from a SELECT; returns
  {chart_url}. ALWAYS show that URL to the user as a Markdown image AND a link:
  "![<title>](<chart_url>)" then "[Open chart](<chart_url>)". Do not just describe
  the chart — the user cannot see it unless you include the URL.
- generate_report(title, sections_json): build an HTML executive report; returns
  {report_url}. sections_json is a JSON ARRAY STRING (a single string), each item
  {"heading","sql","narrative","chart":"bar|line|pie|none"} — e.g.
  [{"heading":"Top suppliers","sql":"SELECT ...","narrative":"...","chart":"bar"}].
  Give the user the report_url as a clickable link.
- list_marts(), describe_table(name), data_dictionary(): inspect the schema if unsure.

WHICH TOOL
- A single figure or list -> run_sql, then state the answer plainly.
- "show / chart / graph / visualize" -> generate_chart, then include the chart_url
  as a Markdown image and a link.
- "report / executive summary / brief / deck" -> generate_report with 2-5 sections;
  write the narrative yourself to interpret each result, then give the user the link.

WRITING SQL (SQLite dialect) — these rules prevent wrong answers:
- gold_document is the PRIMARY, COMPLETE source for spend / supplier / category /
  department / time: one row per purchase document with grand_total, supplier_name,
  canonical_name, acquisition_type, acquisition_sub_type, department_name, status,
  start_date, calendar_year, fiscal_year. Use SUM(grand_total) for spend.
- gold_line_item covers only ~13% of documents (item-level detail; big vendors often
  have zero lines). NEVER use it for spend totals or "top suppliers" — it undercounts.
- Vendor rollups (one row per real company): GROUP BY canonical_name.
- fiscal_year is the California fiscal year (Jul 1-Jun 30, labelled by the year it
  ends in). Work out the current fiscal year from today's date (FY = calendar year,
  +1 if the month is July or later). "Last/previous fiscal year" = current FY - 1;
  "past N fiscal years" = the N most recent COMPLETE years. Do NOT use
  MAX(fiscal_year) as "now" — the data contains future-dated contracts.
- Category questions ("IT Services", "IT Goods", "Telecom", "NON-IT Services"):
  filter acquisition_type with '=' or a PREFIX LIKE ('IT Services%') — NOT
  '%IT Services%', which also matches 'NON-IT Services'.
- "Encumbrance Only" is a bookkeeping placeholder, not a good/service the state
  bought. For "what did X spend the most ON", exclude it:
  WHERE acquisition_type NOT LIKE 'Encumbrance%'.
- When filtering by a name the user typed, match loosely: UPPER(col) LIKE UPPER('%x%').
- For follow-up questions, REUSE the previous query's exact filters (same supplier,
  category, and fiscal_year) and change only what's newly asked; keep totals
  reconcilable with your previous answer.

REPORTS
For an executive report, pick 2-5 sections that answer the question, each with a
SELECT and a chart where a trend/breakdown helps (bar for rankings, line for time,
pie for share). Write a one to three sentence narrative per section. Return the
report link and a short spoken summary of the headline findings.
```

## Step 4 — Test

Use the Copilot Studio **test pane**. Try:
- *"Who are the top 10 suppliers by total spend?"* → expects a `run_sql` on
  `gold_canonical_supplier_spend`.
- *"Chart the top 5 suppliers for IT Services."* → `generate_chart` (bar).
- *"Give me an executive report on procurement spending trends and top categories."*
  → `generate_report`, and the reply includes a `/files/...` link.

Watch the tool-call trace to confirm it's hitting `run_sql` / `generate_chart` /
`generate_report` with sensible SQL.

## Step 5 — Publish

1. **Publish** the agent.
2. **Channels / Settings → Channels** → enable **Microsoft Teams** and/or
   **Microsoft 365 Copilot**.
3. Submit for **admin approval** if required (Teams admin center → Manage apps, and
   the Copilot agent approval flow). The external MCP endpoint may need DLP/admin
   sign-off.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| **Tools/resources don't load when adding the server** | The connector must declare `x-ms-agentic-protocol: mcp-streamable-1.0` — import `deploy/copilot-studio/mcp-connector.swagger.yaml` rather than pasting a URL. Also make the API-key value literally `Bearer <token>`. (No resources is normal — the server exposes tools only.) |
| First call is slow / times out | The HF Space was asleep (cold start). Retry; or move the server to always-on hosting (Azure App Service B1) — see REMOTE_MCP.md. |
| 401 Unauthorized | Bearer token wrong/missing in the connection. It must be `Bearer <token>` in the `Authorization` header. |
| 421 Invalid Host header | The server's `MCP_ALLOWED_HOSTS` must include the Space host (it does by default via the deploy). |
| Report link won't open | `/files/*` is unauthenticated by design; if it 404s the Space likely restarted (reports are currently transient — regenerate). |
| Wrong/empty numbers | Usually the model used `gold_line_item` or `MAX(fiscal_year)` — the instructions above steer away from both; make sure they're pasted in full. |

## Notes & limits

- **Reports are transient** — served from the Space's ephemeral disk, so a link is
  for viewing now, not a permanent archive. Persistent report history is a bounded
  add (sync to a storage account / dataset).
- **Cold start** — an idle Space sleeps; the first agent call wakes it. For a
  production agent, always-on hosting (Azure) removes this.
- The server stays **read-only and query-only**; the agent can only read public
  procurement data.
