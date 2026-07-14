"""Chart rendering + self-contained HTML report building over query results.

Host-agnostic and dependency-light: pure functions that take already-fetched rows
(from ``warehouse_query.run_select``) and return bytes / strings. No database, no
LLM, no network — trivially testable and reused by the MCP server's
``generate_chart`` / ``generate_report`` tools.

Styling follows a validated data-viz palette: a single blue lead hue for
magnitude, an ordered categorical palette for share, recessive axes, compact
number formatting, and horizontal bars for rankings. matplotlib uses the
non-interactive ``Agg`` backend and a writable config dir so it renders headlessly.
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
from matplotlib.ticker import FuncFormatter  # noqa: E402

CHART_KINDS = ("bar", "line", "pie")

# Validated palette (light surface).
_BLUE = "#2a78d6"  # lead hue (magnitude)
_CATEGORICAL = [
    "#2a78d6",
    "#1baf7a",
    "#eda100",
    "#008300",
    "#4a3aa7",
    "#e34948",
    "#e87ba4",
    "#eb6834",
]
_INK = "#0b0b0b"  # primary text
_INK2 = "#52514e"  # secondary text
_MUTED = "#898781"  # axis labels
_GRID = "#e1e0d9"  # hairline gridline
_BASE = "#c3c2b7"  # baseline / axis
_SURFACE = "#ffffff"  # chart surface (sits on a white tile in the report)


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _compact(v: float) -> str:
    """Human-compact number: 1.2B / 184.9M / 340K / 87."""
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e3:
        return f"{v / 1e3:.0f}K"
    if a and a < 1:
        return f"{v:.2f}"
    return f"{v:.0f}"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


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


def _style_axes(ax, *, xgrid: bool) -> None:
    """Recessive axes: drop top/right spines, hairline grid, muted ticks."""
    ax.set_facecolor(_SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_BASE)
        ax.spines[side].set_linewidth(0.8)
    ax.grid(axis="x" if xgrid else "y", color=_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=_MUTED, labelsize=9, length=0)


def render_chart(
    columns: list[str],
    rows: list[dict],
    kind: str = "bar",
    title: str = "",
    x: str | None = None,
    y: str | None = None,
    max_points: int = 20,
) -> bytes:
    """Render ``rows`` as a styled PNG chart and return the image bytes.

    ``kind`` — ``bar`` (horizontal ranking), ``line`` (trend), or ``pie`` (share);
    ``x``/``y`` name the label and value columns (auto-detected when omitted). At
    most ``max_points`` rows plot.
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

    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"]})
    fig, ax = plt.subplots(figsize=(8.2, 4.8), dpi=160)
    fig.patch.set_facecolor(_SURFACE)
    try:
        if kind == "pie":
            wedges, _ = ax.pie(
                values,
                colors=_CATEGORICAL[: len(values)],
                startangle=90,
                wedgeprops={"edgecolor": _SURFACE, "linewidth": 2},
            )
            legend = [
                f"{_truncate(lbl, 28)} · {_compact(v)}"
                for lbl, v in zip(labels, values, strict=False)
            ]
            ax.legend(
                wedges,
                legend,
                loc="center left",
                bbox_to_anchor=(1.0, 0.5),
                frameon=False,
                fontsize=9,
                labelcolor=_INK2,
            )
            ax.axis("equal")
        elif kind == "line":
            ax.plot(
                labels,
                values,
                color=_BLUE,
                linewidth=2,
                marker="o",
                markersize=6,
                markerfacecolor=_SURFACE,
                markeredgecolor=_BLUE,
                markeredgewidth=1.6,
            )
            _style_axes(ax, xgrid=False)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: _compact(v)))
            for lab in ax.get_xticklabels():
                lab.set_rotation(30)
                lab.set_ha("right")
        else:  # bar — horizontal ranking, top value at top
            pos = range(len(labels))
            ax.barh(list(pos), values, color=_BLUE, height=0.66)
            ax.set_yticks(list(pos))
            ax.set_yticklabels([_truncate(lbl, 30) for lbl in labels], fontsize=9, color=_INK2)
            ax.invert_yaxis()
            _style_axes(ax, xgrid=True)
            ax.spines["left"].set_visible(False)
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: _compact(v)))
            span = max(values) if values else 0
            for i, v in zip(pos, values, strict=False):
                ax.text(
                    v + span * 0.012,
                    i,
                    _compact(v),
                    va="center",
                    ha="left",
                    fontsize=8.5,
                    color=_INK2,
                )
            ax.margins(x=0.14)
        ax.set_title(
            title or f"{y} by {x}", color=_INK, fontsize=12.5, fontweight="bold", loc="left", pad=12
        )
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=_SURFACE)
    finally:
        plt.close(fig)
    return buf.getvalue()


def _looks_numeric(rows: list[dict], col: str) -> bool:
    seen = [r.get(col) for r in rows[:20] if r.get(col) is not None]
    return bool(seen) and all(_to_float(v) is not None for v in seen)


def _html_table(columns: list[str], rows: list[dict], max_rows: int = 15) -> str:
    numeric = {c for c in columns if _looks_numeric(rows, c)}
    head = "".join(
        f'<th class="{"num" if c in numeric else ""}">{html.escape(str(c))}</th>' for c in columns
    )
    body = []
    for r in rows[:max_rows]:
        cells = "".join(
            f'<td class="{"num" if c in numeric else ""}">'
            f"{'' if r.get(c) is None else html.escape(str(r.get(c)))}</td>"
            for c in columns
        )
        body.append(f"<tr>{cells}</tr>")
    extra = ""
    if len(rows) > max_rows:
        extra = f'<p class="muted">…and {len(rows) - max_rows} more row(s).</p>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>{extra}"


_STYLE = """
:root {
  --plane: #f4f4f2; --card: #ffffff; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --grid: #e6e5df; --accent: #2a78d6; --ring: rgba(11,11,11,0.08);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--plane); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased; }
.report { max-width: 860px; margin: 0 auto; padding: 40px 20px 64px; }
header { border-bottom: 3px solid var(--accent); padding-bottom: 16px; margin-bottom: 8px; }
h1 { font-size: 1.85rem; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 4px; }
.meta { color: var(--muted); font-size: .85rem; }
section { background: var(--card); border: 1px solid var(--ring); border-radius: 12px;
  padding: 22px 24px; margin-top: 22px; box-shadow: 0 1px 2px rgba(11,11,11,0.04); }
h2 { font-size: 1.15rem; font-weight: 650; margin: 0 0 10px;
  display: flex; align-items: center; gap: 9px; }
h2::before { content: ""; width: 8px; height: 8px; border-radius: 2px; background: var(--accent); }
.narrative { color: var(--ink2); margin: 0 0 16px; }
img { display: block; max-width: 100%; height: auto; margin: 4px 0 18px;
  border-radius: 8px; border: 1px solid var(--ring); }
table { border-collapse: collapse; width: 100%; font-size: .88rem;
  font-variant-numeric: tabular-nums; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--grid); }
th { color: var(--muted); font-weight: 600; font-size: .78rem; text-transform: uppercase;
  letter-spacing: .04em; border-bottom: 1.5px solid var(--grid); }
tbody tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; }
.muted { color: var(--muted); font-size: .8rem; margin: 8px 0 0; }
footer { color: var(--muted); font-size: .78rem; margin-top: 28px; text-align: center; }
"""


def build_report_html(title: str, sections: list[dict], generated_at: str = "") -> str:
    """Assemble a polished, self-contained HTML executive report.

    Each section is a dict: ``heading`` (str), optional ``narrative`` (str),
    optional ``chart_png`` (bytes, embedded as a data URI), optional
    ``columns``/``rows`` (rendered as a table). Charts are inlined, so the returned
    HTML needs no external assets.
    """
    meta = f"Generated {html.escape(generated_at)} · " if generated_at else ""
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body>",
        '<div class="report"><header>',
        f"<h1>{html.escape(title)}</h1>",
        f'<div class="meta">{meta}SCPRS procurement warehouse · public data</div>',
        "</header>",
    ]
    for s in sections:
        parts.append(f"<section><h2>{html.escape(str(s.get('heading', '')))}</h2>")
        if s.get("narrative"):
            parts.append(f'<p class="narrative">{html.escape(str(s["narrative"]))}</p>')
        if s.get("chart_png"):
            b64 = base64.b64encode(s["chart_png"]).decode("ascii")
            parts.append(f'<img alt="chart" src="data:image/png;base64,{b64}">')
        if s.get("columns") and s.get("rows"):
            parts.append(_html_table(s["columns"], s["rows"]))
        parts.append("</section>")
    parts.append(
        "<footer>Figures derived from California SCPRS (public procurement data). "
        "Generated automatically — verify material figures against source records.</footer>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)
