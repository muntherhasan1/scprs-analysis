"""Presentation behavior of the NL web app: table formatting/ordering and the
trend/distribution chart heuristic (2026-07-27 demo feedback)."""

import pytest

pytest.importorskip("gradio")  # web-image dep (requirements-web.txt), not in CI's set

from src import web_app  # noqa: E402

PNG_MAGIC = b"\x89PNG"


def _result(columns, rows):
    return {"columns": columns, "rows": rows}


def test_md_table_formats_money_and_orders_years_desc():
    md = web_app._md_table(
        _result(
            ["fiscal_year", "total_spend"],
            [
                {"fiscal_year": 2024, "total_spend": 1234567.891},
                {"fiscal_year": 2026, "total_spend": 500.0},
                {"fiscal_year": 2025, "total_spend": 42.0},
            ],
        )
    )
    lines = md.splitlines()
    assert lines[2] == "| 2026 | $500.00 |"  # newest year first
    assert lines[3] == "| 2025 | $42.00 |"
    assert lines[4] == "| 2024 | $1,234,567.89 |"


def test_md_table_escapes_pipes():
    md = web_app._md_table(_result(["supplier"], [{"supplier": "A|B"}]))
    assert "A&#124;B" in md
    assert "A|B" not in md


def test_chart_for_time_series_result_even_without_trend_word():
    png = web_app._maybe_chart_png(
        "What is total spend by fiscal year?",
        _result(
            ["fiscal_year", "total_spend"],
            [{"fiscal_year": 2024 + i, "total_spend": 100.0 * i} for i in range(3)],
        ),
    )
    assert png is not None and png[:4] == PNG_MAGIC


def test_chart_for_distribution_question():
    png = web_app._maybe_chart_png(
        "How much spend was competitive versus not? Show the distribution.",
        _result(
            ["competitive_flag", "total_spend"],
            [
                {"competitive_flag": "Competitive", "total_spend": 14.0},
                {"competitive_flag": "Non-Competitive", "total_spend": 55.0},
                {"competitive_flag": "Other", "total_spend": 3.0},
            ],
        ),
    )
    assert png is not None and png[:4] == PNG_MAGIC


def test_no_chart_for_plain_ranking():
    png = web_app._maybe_chart_png(
        "Which 10 suppliers had the highest total spend?",
        _result(
            ["canonical_name", "total_value"],
            [{"canonical_name": f"S{i}", "total_value": float(i)} for i in range(10)],
        ),
    )
    assert png is None


def test_no_chart_for_tiny_results():
    png = web_app._maybe_chart_png(
        "Show the monthly trend",
        _result(["year_month", "total_spend"], [{"year_month": "2026-01", "total_spend": 1.0}]),
    )
    assert png is None
