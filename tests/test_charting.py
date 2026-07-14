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
    assert "<table>" in html_doc and "$250.00" in html_doc  # money formatted to 2dp


def test_build_report_html_escapes_html():
    html_doc = charting.build_report_html("<x>", [{"heading": "<h&>", "narrative": "a<b"}])
    assert "<x>" not in html_doc and "&lt;x&gt;" in html_doc
    assert "&lt;h&amp;&gt;" in html_doc


def test_fmt_cell_money_years_and_ids():
    # Money → $ + thousands + exactly 2 decimals (the >2-decimal bug).
    assert charting._fmt_cell("total_spend", 1234567.8900001) == "$1,234,567.89"
    assert charting._fmt_cell("grand_total", 250.0) == "$250.00"
    # Years and ids: raw digits, no grouping / decimals.
    assert charting._fmt_cell("fiscal_year", 2026) == "2026"
    assert charting._fmt_cell("supplier_id", 12345.0) == "12345"
    # Count-like column that shares a hint word stays a plain grouped integer.
    assert charting._fmt_cell("total_documents", 1500) == "1,500"
    # Percent columns share money hints ("value") but must render as % not $.
    assert charting._fmt_cell("pct_noncompetitive_value", 100.0) == "100.0%"
    assert charting._fmt_cell("value_pct_change", -12.5) == "-12.5%"
    # Non-numeric passes through (escaped).
    assert charting._fmt_cell("supplier", "Acme") == "Acme"


def test_html_table_orders_fiscal_year_descending():
    cols = ["fiscal_year", "total_spend"]
    rows = [
        {"fiscal_year": 2024, "total_spend": 10.0},
        {"fiscal_year": 2026, "total_spend": 30.0},
        {"fiscal_year": 2025, "total_spend": 20.0},
    ]
    table = charting._html_table(cols, rows)
    # Newest year first.
    assert table.index("2026") < table.index("2025") < table.index("2024")


def test_html_table_keeps_order_for_multikey_year_tables():
    # fiscal_year repeats (supplier×year) → not a unique key → query order kept.
    cols = ["fiscal_year", "supplier", "total_spend"]
    rows = [
        {"fiscal_year": 2025, "supplier": "A", "total_spend": 5.0},
        {"fiscal_year": 2026, "supplier": "B", "total_spend": 9.0},
        {"fiscal_year": 2025, "supplier": "C", "total_spend": 7.0},
    ]
    ordered = charting._order_rows(cols, rows)
    assert [r["supplier"] for r in ordered] == ["A", "B", "C"]
