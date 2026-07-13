"""Tests for chart rendering + HTML report building (src/charting.py)."""

import pytest

charting = pytest.importorskip("src.charting", reason="matplotlib not installed")

_COLS = ["supplier", "total"]
_ROWS = [{"supplier": "Beta", "total": 250.0}, {"supplier": "Acme", "total": 100.0}]


def _is_png(b: bytes) -> bool:
    return b[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("kind", ["bar", "line", "pie"])
def test_render_chart_returns_png(kind):
    png = charting.render_chart(_COLS, _ROWS, kind=kind, title="T")
    assert _is_png(png) and len(png) > 500


def test_render_chart_autodetects_numeric_column():
    # 'total' is the only numeric column, so it becomes the value axis.
    png = charting.render_chart(_COLS, _ROWS, kind="bar")
    assert _is_png(png)


def test_render_chart_rejects_bad_kind():
    with pytest.raises(ValueError):
        charting.render_chart(_COLS, _ROWS, kind="doughnut")


def test_render_chart_needs_numeric_column():
    with pytest.raises(ValueError):
        charting.render_chart(["a", "b"], [{"a": "x", "b": "y"}], kind="bar")


def test_render_chart_empty():
    with pytest.raises(ValueError):
        charting.render_chart(_COLS, [], kind="bar")


def test_build_report_html_is_self_contained():
    png = charting.render_chart(_COLS, _ROWS, kind="bar")
    html_doc = charting.build_report_html(
        "Exec Report",
        [
            {
                "heading": "Top suppliers",
                "narrative": "Beta leads.",
                "columns": _COLS,
                "rows": _ROWS,
                "chart_png": png,
            },
        ],
        generated_at="2026-07-13",
    )
    assert "<!doctype html>" in html_doc.lower()
    assert "Exec Report" in html_doc and "Top suppliers" in html_doc
    assert "Beta leads." in html_doc
    assert "data:image/png;base64," in html_doc  # chart embedded, no external asset
    assert "<table>" in html_doc and "250.0" in html_doc  # data table rendered


def test_build_report_html_escapes_html():
    html_doc = charting.build_report_html("<x>", [{"heading": "<h&>", "narrative": "a<b"}])
    assert "<x>" not in html_doc and "&lt;x&gt;" in html_doc
    assert "&lt;h&amp;&gt;" in html_doc
