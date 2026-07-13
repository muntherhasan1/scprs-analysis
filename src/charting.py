"""Chart rendering + self-contained HTML report building over query results.

Host-agnostic and dependency-light: pure functions that take already-fetched rows
(from ``warehouse_query.run_select``) and return bytes / strings. No database, no
LLM, no network — so it is trivially testable and reusable by the MCP server's
``generate_chart`` / ``generate_report`` tools (and anything else).

matplotlib uses the non-interactive ``Agg`` backend and a writable config dir, so
it renders headlessly inside a container with no display.
"""

from __future__ import annotations

import base64
import html
import io
import os
import tempfile

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "mpl"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

CHART_KINDS = ("bar", "line", "pie")


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick_axes(columns: list[str], rows: list[dict], x: str | None, y: str | None):
    """Choose x (label) and y (numeric) columns: honour explicit picks, else take
    the first column as x and the first numeric column as y."""
    x = x or (columns[0] if columns else None)
    if not y:
        for c in columns:
            if c != x and any(_to_float(r.get(c)) is not None for r in rows):
                y = c
                break
    if not x or not y:
        raise ValueError("need one label column and one numeric column to chart")
    return x, y


def render_chart(
    columns: list[str],
    rows: list[dict],
    kind: str = "bar",
    title: str = "",
    x: str | None = None,
    y: str | None = None,
    max_points: int = 20,
) -> bytes:
    """Render ``rows`` as a PNG chart and return the image bytes.

    ``kind`` is one of ``bar`` / ``line`` / ``pie``; ``x``/``y`` name the label and
    value columns (auto-detected when omitted). At most ``max_points`` rows plot.
    """
    if not rows or not columns:
        raise ValueError("no data to chart")
    kind = (kind or "bar").lower()
    if kind not in CHART_KINDS:
        raise ValueError(f"kind must be one of {CHART_KINDS}, got {kind!r}")
    x, y = _pick_axes(columns, rows, x, y)
    data = rows[:max_points]
    labels = [str(r.get(x)) for r in data]
    values = [_to_float(r.get(y)) or 0.0 for r in data]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    try:
        if kind == "pie":
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
            ax.axis("equal")
        elif kind == "line":
            ax.plot(labels, values, marker="o")
            ax.set_xlabel(x)
            ax.set_ylabel(y)
            ax.tick_params(axis="x", labelrotation=45)
        else:  # bar
            ax.bar(labels, values)
            ax.set_xlabel(x)
            ax.set_ylabel(y)
            ax.tick_params(axis="x", labelrotation=45)
        ax.set_title(title or f"{y} by {x}")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
    finally:
        plt.close(fig)
    return buf.getvalue()


def _html_table(columns: list[str], rows: list[dict], max_rows: int = 15) -> str:
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
    body = []
    for r in rows[:max_rows]:
        cells = "".join(
            f"<td>{'' if r.get(c) is None else html.escape(str(r.get(c)))}</td>" for c in columns
        )
        body.append(f"<tr>{cells}</tr>")
    extra = ""
    if len(rows) > max_rows:
        extra = f'<p class="muted">…and {len(rows) - max_rows} more row(s).</p>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>{extra}"


_STYLE = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.7rem; margin-bottom: .2rem; }
h2 { font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid #8883; padding-bottom: .3rem; }
.meta, .muted { color: #8a8a8a; font-size: .85rem; }
img { max-width: 100%; height: auto; margin: .5rem 0; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; margin: .5rem 0; }
th, td { border: 1px solid #8883; padding: .35rem .5rem; text-align: left; }
th { background: #8881; }
"""


def build_report_html(title: str, sections: list[dict], generated_at: str = "") -> str:
    """Assemble a self-contained HTML executive report.

    Each section is a dict: ``heading`` (str), optional ``narrative`` (str),
    optional ``chart_png`` (bytes, embedded as a data URI), optional
    ``columns``/``rows`` (rendered as a table). Charts are inlined, so the returned
    HTML needs no external assets.
    """
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="meta">Generated {html.escape(generated_at)} · SCPRS procurement warehouse</p>'
        if generated_at
        else '<p class="meta">SCPRS procurement warehouse</p>',
    ]
    for s in sections:
        parts.append(f"<section><h2>{html.escape(str(s.get('heading', '')))}</h2>")
        if s.get("narrative"):
            parts.append(f"<p>{html.escape(str(s['narrative']))}</p>")
        if s.get("chart_png"):
            b64 = base64.b64encode(s["chart_png"]).decode("ascii")
            parts.append(f'<img alt="chart" src="data:image/png;base64,{b64}">')
        if s.get("columns") and s.get("rows"):
            parts.append(_html_table(s["columns"], s["rows"]))
        parts.append("</section>")
    parts.append("</body></html>")
    return "\n".join(parts)
