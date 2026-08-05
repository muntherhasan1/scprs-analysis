"""Generate scprs-star.Report/report.json: chrome and content visuals.

Implements the SCPRS Power BI build spec (claude.ai/design "SCPRS Power BI Build
Spec", v4): 1600x900 canvas, the Industry sidebar and header on all ten pages, a
hidden Notes page carrying the report definitions, three header slicers synced
across pages 01-09, the Filter State scope card, the registered
industry-theme.json, and every page's content visuals (KPI cards, charts,
tables, matrix - spec section 6) bound to the semantic model. The sidebar's 1px
dividers and 3px active-page bar sit below the 12px minimum Desktop's UI
enforces on shapes, so the chrome is only reachable by writing the report file -
do not rebuild it by hand.

Usage (from the repo root or powerbi/):

    python powerbi/build_report.py            # fresh build; refuses to clobber content
    python powerbi/build_report.py --merge    # regenerate chrome; Desktop-edited or
                                              #   hand-added visuals win by name
    python powerbi/build_report.py --force    # regenerate everything, discard edits

Visual names mark ownership: chrome_* is always regenerated; gen_* is generated
content that --merge leaves alone once it exists in the file (Desktop edits
survive); anything else is hand-built and always preserved by --merge.

A few spec items stay manual in Desktop (documented in docs/POWER_BI.md):
page 10's drill-through field list, the Clear-all bookmark button, constant
lines (SB/DVBE goals, portfolio average), matrix heat shading, conditional
formats, and the report-page tooltip.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPORT = Path(__file__).parent / "scprs-star.Report" / "report.json"

CANVAS_W, CANVAS_H = 1600, 900
GROUND = "#F2F2F3"
INK = "#1D1F20"
MUTED = "#5D5D60"
FAINT = "#7A7A7D"
ACCENT = "#416180"
ACCENT_LIGHT = "#5980A6"
HOVER_FILL = "#E8EEF5"
HAIRLINE = "#D8D8DA"  # flattened #1D1F20 @ 84% transparency
BORDER = "#1D1F2029"

# (section name, nav label, page title, header subtitle)
PAGES = [
    (
        "ReportSection00",
        "01  Spend overview",
        "Spend overview",
        "How much the state books, where it is trending, who the biggest buyers "
        "and incumbents are.",
    ),
    (
        "ReportSection01",
        "02  Departments",
        "Departments",
        "Which departments are growing, and whether a supplier's target agency "
        "is expanding or flat.",
    ),
    (
        "ReportSection02",
        "03  Suppliers",
        "Suppliers",
        "The incumbent map - who already holds the work you want, and across how many departments.",
    ),
    (
        "ReportSection03",
        "04  Acquisition mix",
        "Acquisition mix",
        "Category split and how much of each category is actually competed.",
    ),
    (
        "ReportSection04",
        "05  Competition & HHI",
        "Competition & HHI",
        "Where incumbents are entrenched and where a newcomer has room.",
    ),
    (
        "ReportSection05",
        "06  SB / DVBE / Micro",
        "SB / DVBE / Micro",
        "Certification performance against the 25% SB and 3% DVBE goals.",
    ),
    (
        "ReportSection06",
        "07  CMAS & leveraged",
        "CMAS & leveraged",
        "The vehicles that let a department buy without a fresh solicitation, and the "
        "renewal dates that reopen them.",
    ),
    (
        "ReportSection07",
        "08  Amendments",
        "Amendments",
        "Awards that expanded after signature - a signal of underscoped work and follow-on money.",
    ),
    (
        "ReportSection08",
        "09  Open solicitations",
        "Open solicitations",
        "A bid calendar - what is open now and which departments post most often. "
        "No pipeline dollars: the mart has no value column.",
    ),
    (
        "ReportSection09",
        "10  Document detail",
        "Document detail",
        "Drill-through landing. Right-click any summary visual > Drill through > Document detail.",
    ),
]
NOTES_SECTION = "ReportSectionNotes"
DETAIL_INDEX = 9  # page 10: slicers not synced, back button instead

# §10 - definitions the report must carry (Notes page)
DEFINITIONS = [
    (
        "grand_total",
        "Contract grand totals booked to the fiscal year of the document start date - "
        "not annual cash outlays. Every value axis needs that subtitle.",
    ),
    (
        "Document grain",
        "One row per purchase document current version. version counts revisions; "
        "there are no sibling rows to sum.",
    ),
    (
        "Canonical supplier",
        "Slice on dim_supplier[canonical_name] - the same key the certification "
        "and CMAS marts join on. parent_name rolls up corporate families; supplier_name "
        "is detail-only.",
    ),
    (
        "Line amounts",
        "fact_line covers enriched documents only. Pair any line-level visual with "
        "Enriched Document %.",
    ),
    (
        "competitive_flag",
        "The single source for competition (values: Competitive / "
        "Non-Competitive / "
        "Other). The report never parses acquisition_method text.",
    ),
    (
        "Certification flags",
        "cert_small_business / cert_micro_business / cert_dvbe are "
        "0/1 integers "
        "as of the last refresh. cert_record_count = 0 means the supplier was not matched to a "
        "certification record, which is not the same as uncertified.",
    ),
    (
        "top_supplier_pct",
        "Share held by the single largest supplier in a business_unit x "
        "acquisition_type market - not a top-5 figure.",
    ),
    (
        "Amendment coverage",
        "Version counts are complete in the star. value_growth comes "
        "from a mart "
        "that only sees documents captured at 2+ snapshots - sparse, deepest for BU 8660.",
    ),
    (
        "eProcure events",
        "Posted opportunities carry no estimated value and no category. Counts and "
        "close dates only; they describe future events and are never comparable to booked spend.",
    ),
    (
        "dq_line_reconciles",
        "Per-document flag that line amounts sum to grand_total. Surfaced as "
        "Line Reconciliation Rate rather than filtering rows out silently.",
    ),
    (
        "Refresh",
        "Marts and star Parquet publish up to 8x/day from CI. In the Service set the "
        "credential to Anonymous and skip the test connection - auth rides in the Web.Contents "
        "header via the Token parameter.",
    ),
]


def lit(value):
    return {"expr": {"Literal": {"Value": value}}}


def color(c):
    return {"solid": {"color": lit(f"'{c}'")}}


def container(name, x, y, w, h, z, visual, sync_group=None):
    cfg = {
        "name": name,
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z, "width": w, "height": h}}],
        "singleVisual": visual,
    }
    if sync_group:
        cfg["syncGroup"] = sync_group
    return {
        "x": float(x),
        "y": float(y),
        "z": float(z),
        "width": float(w),
        "height": float(h),
        "config": json.dumps(cfg),
        "filters": "[]",
    }


def rectangle(name, x, y, w, h, fill, z=0):
    return container(
        name,
        x,
        y,
        w,
        h,
        z,
        {
            "visualType": "basicShape",
            "drillFilterOtherVisuals": True,
            "objects": {
                "line": [{"properties": {"show": lit("false"), "weight": lit("0D")}}],
                "fill": [
                    {
                        "properties": {
                            "show": lit("true"),
                            "fillColor": color(fill),
                            "transparency": lit("0D"),
                        }
                    }
                ],
            },
            "vcObjects": {"border": [{"properties": {"show": lit("false")}}]},
        },
    )


def textbox(name, x, y, w, h, paragraphs, z=1):
    """paragraphs: list of (text, font, size_pt, color)."""
    return container(
        name,
        x,
        y,
        w,
        h,
        z,
        {
            "visualType": "textbox",
            "drillFilterOtherVisuals": True,
            "objects": {
                "general": [
                    {
                        "properties": {
                            "paragraphs": [
                                {
                                    "textRuns": [
                                        {
                                            "value": text,
                                            "textStyle": {
                                                "fontFamily": font,
                                                "fontSize": f"{size}pt",
                                                "color": col,
                                            },
                                        }
                                    ],
                                    "horizontalTextAlignment": "left",
                                }
                                for text, font, size, col in paragraphs
                            ]
                        }
                    }
                ]
            },
            "vcObjects": {
                "background": [{"properties": {"show": lit("false")}}],
                "border": [{"properties": {"show": lit("false")}}],
            },
        },
    )


def button(name, x, y, w, h, text, target, active=False, font_size=10, outlined=False, z=10):
    """Blank action button. target: section name for PageNavigation, or 'Back'."""
    default_color = "#FFFFFF" if active else INK
    default_fill, default_alpha = (ACCENT, "0D") if active else (ACCENT, "100D")
    hover_fill, hover_alpha = (ACCENT, "0D") if active else (HOVER_FILL, "0D")
    hover_color = "#FFFFFF" if active else ACCENT
    if target == "Back":
        link = {"show": lit("true"), "type": lit("'Back'")}
    else:
        link = {
            "show": lit("true"),
            "type": lit("'PageNavigation'"),
            "navigationSection": lit(f"'{target}'"),
        }
    return container(
        name,
        x,
        y,
        w,
        h,
        z,
        {
            "visualType": "actionButton",
            "drillFilterOtherVisuals": True,
            # "show" must sit in a selector-LESS entry — Desktop treats a
            # state-scoped show as off and renders an empty button.
            "objects": {
                "icon": [
                    {"properties": {"shapeType": lit("'blank'")}, "selector": {"id": "default"}}
                ],
                "text": [
                    {"properties": {"show": lit("true")}},
                    {
                        "properties": {
                            "text": lit(f"'{text}'"),
                            "fontFamily": lit("'Barlow'"),
                            "fontSize": lit(f"{font_size}D"),
                            "fontColor": color(default_color),
                            "horizontalAlignment": lit("'left'"),
                            "verticalAlignment": lit("'middle'"),
                            "padding": lit("8D"),
                        },
                        "selector": {"id": "default"},
                    },
                    {"properties": {"fontColor": color(hover_color)}, "selector": {"id": "hover"}},
                ],
                "fill": [
                    {"properties": {"show": lit("true")}},
                    {
                        "properties": {
                            "fillColor": color(default_fill),
                            "transparency": lit(default_alpha),
                        },
                        "selector": {"id": "default"},
                    },
                    {
                        "properties": {
                            "fillColor": color(hover_fill),
                            "transparency": lit(hover_alpha),
                        },
                        "selector": {"id": "hover"},
                    },
                ],
                "outline": [
                    {"properties": {"show": lit("true" if outlined else "false")}},
                    {
                        "properties": {
                            "lineColor": color(HAIRLINE),
                            "weight": lit("1D"),
                        },
                        "selector": {"id": "default"},
                    },
                ],
                "visualLink": [{"properties": link}],
            },
            "vcObjects": {"border": [{"properties": {"show": lit("false")}}]},
        },
    )


def slicer(name, x, y, w, h, entity, column, header, mode, sync_name, z=20, orientation=None):
    objects = {
        "data": [{"properties": {"mode": lit(f"'{mode}'")}}],
        "header": [{"properties": {"show": lit("true"), "text": lit(f"'{header}'")}}],
        "selection": [{"properties": {"singleSelect": lit("false")}}],
    }
    if orientation is not None:
        objects["general"] = [{"properties": {"orientation": lit(f"{orientation}D")}}]
    visual = {
        "visualType": "slicer",
        "projections": {"Values": [{"queryRef": f"{entity}.{column}"}]},
        "prototypeQuery": {
            "Version": 2,
            "From": [{"Name": "s", "Entity": entity, "Type": 0}],
            "Select": [
                {
                    "Column": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": column},
                    "Name": f"{entity}.{column}",
                    "NativeReferenceName": column,
                }
            ],
        },
        "drillFilterOtherVisuals": False,
        "objects": objects,
    }
    sync = {"groupName": sync_name, "fieldChanges": True, "filterChanges": True}
    return container(name, x, y, w, h, z, visual, sync_group=sync)


def filter_state_card(name, z=21):
    return container(
        name,
        256,
        108,
        1000,
        20,
        z,
        {
            "visualType": "card",
            "projections": {"Values": [{"queryRef": "_Measures.Filter State"}]},
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "m", "Entity": "_Measures", "Type": 0}],
                "Select": [
                    {
                        "Measure": {
                            "Expression": {"SourceRef": {"Source": "m"}},
                            "Property": "Filter State",
                        },
                        "Name": "_Measures.Filter State",
                        "NativeReferenceName": "Filter State",
                    }
                ],
            },
            "drillFilterOtherVisuals": True,
            "objects": {
                "labels": [
                    {
                        "properties": {
                            "fontSize": lit("9D"),
                            "fontFamily": lit("'Barlow'"),
                            "color": color(MUTED),
                        }
                    }
                ],
                "categoryLabels": [{"properties": {"show": lit("false")}}],
            },
            "vcObjects": {
                "border": [{"properties": {"show": lit("false")}}],
                "background": [{"properties": {"show": lit("false")}}],
            },
        },
    )


# ---------------------------------------------------------------- content --
# Data visuals per build-spec §6. Every generated visual is named gen_* so
# --merge can tell hand-built work from generated work.

FD = "fact_document"
MS = "_Measures"
DD = "dim_date"
DEP = "dim_department"
SUP = "dim_supplier"
ACQ = "dim_acquisition"
GMC = "gold_market_concentration"
GSC = "gold_supplier_certification"
GCM = "gold_supplier_cmas"
GCA = "gold_contract_amendments"
GEP = "gold_eprocure_posted_opportunity"
GED = "gold_eprocure_event_demand"

VALUE_CAVEAT = "grand totals booked to the start-date fiscal year — not annual outlays"

AGG_FUNC = {"Sum": 0, "Avg": 1}


def qref(sel):
    kind, entity, prop = sel[0], sel[1], sel[2]
    return f"{sel[3]}({entity}.{prop})" if kind == "agg" else f"{entity}.{prop}"


def _query(selects):
    """Build prototypeQuery From/Select from (kind, entity, prop[, func]) tuples.

    kind: 'col' raw column, 'meas' model measure, 'agg' default-aggregated column.
    """
    aliases, from_list, select_list = {}, [], []
    for sel in selects:
        entity = sel[1]
        if entity not in aliases:
            aliases[entity] = f"t{len(aliases)}"
            from_list.append({"Name": aliases[entity], "Entity": entity, "Type": 0})
    for sel in selects:
        kind, entity, prop = sel[0], sel[1], sel[2]
        src = {"Expression": {"SourceRef": {"Source": aliases[entity]}}, "Property": prop}
        if kind == "col":
            expr = {"Column": src}
        elif kind == "meas":
            expr = {"Measure": src}
        else:
            expr = {"Aggregation": {"Expression": {"Column": src}, "Function": AGG_FUNC[sel[3]]}}
        select_list.append({**expr, "Name": qref(sel), "NativeReferenceName": prop})
    return from_list, select_list


def data_visual(
    name, x, y, w, h, vtype, roles, title=None, subtitle=None, objects=None, filters=None, z=100
):
    """roles: dict of projection role -> list of select tuples (see _query)."""
    selects, seen = [], set()
    for sels in roles.values():
        for s in sels:
            if qref(s) not in seen:
                seen.add(qref(s))
                selects.append(s)
    from_list, select_list = _query(selects)
    visual = {
        "visualType": vtype,
        "projections": {
            role: [{"queryRef": qref(s)} for s in sels] for role, sels in roles.items()
        },
        "prototypeQuery": {"Version": 2, "From": from_list, "Select": select_list},
        "drillFilterOtherVisuals": True,
    }
    if objects:
        visual["objects"] = objects
    vc_objects = {}
    if title:
        vc_objects["title"] = [{"properties": {"show": lit("true"), "text": lit(f"'{title}'")}}]
    if subtitle:
        vc_objects["subTitle"] = [
            {"properties": {"show": lit("true"), "text": lit(f"'{subtitle}'")}}
        ]
    if vc_objects:
        visual["vcObjects"] = vc_objects
    vc = container(name, x, y, w, h, z, visual)
    if filters:
        vc["filters"] = json.dumps(filters)
    return vc


def kpi(name, x, y, w, title, entity, measure, subtitle=None):
    """KPI card: the label rides as the visual title so it moves with the number."""
    return data_visual(
        name,
        x,
        y,
        w,
        100,
        "card",
        {"Values": [("meas", entity, measure)]},
        title=title,
        subtitle=subtitle,
        objects={"categoryLabels": [{"properties": {"show": lit("false")}}]},
    )


def topn(entity, prop, count, by_entity, by_measure):
    """Visual-level Top N filter: top `count` of entity.prop by by_entity.[by_measure]."""
    from_list = [{"Name": "a", "Entity": entity, "Type": 0}]
    by_alias = "a"
    if by_entity != entity:
        by_alias = "b"
        from_list.append({"Name": "b", "Entity": by_entity, "Type": 0})
    return [
        {
            "name": f"TopN_{prop}",
            "expression": {
                "Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}
            },
            "filter": {
                "Version": 2,
                "From": from_list,
                "Where": [
                    {
                        "Condition": {
                            "TopN": {
                                "Expression": {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Source": "a"}},
                                        "Property": prop,
                                    }
                                },
                                "Count": count,
                                "OrderBy": [
                                    {
                                        "Direction": 2,
                                        "Expression": {
                                            "Measure": {
                                                "Expression": {"SourceRef": {"Source": by_alias}},
                                                "Property": by_measure,
                                            }
                                        },
                                    }
                                ],
                            }
                        }
                    }
                ],
            },
            "type": "TopN",
            "howCreated": 1,
            "isHiddenInViewMode": False,
        }
    ]


TCV = ("meas", FD, "Total Contract Value")
DOCS = ("meas", FD, "Document Count")


def _content_01(c):
    cards = [
        ("TOTAL CONTRACT VALUE", FD, "Total Contract Value", "documents in scope"),
        ("YEAR OVER YEAR", MS, "Value YoY %", "booked value, start-date basis"),
        ("SUPPLIERS", MS, "Canonical Suppliers", "canonical names"),
        ("MEDIAN DOCUMENT", MS, "Median Document Value", "half of awards are smaller"),
        ("COMPETITIVELY BID", MS, "Competitive %", "of value in scope"),
    ]
    vis = [
        kpi(f"{c}_kpi_{i}", 256 + i * 267, 128, 251, t, e, meas, sub)
        for i, (t, e, meas, sub) in enumerate(cards)
    ]
    vis.append(
        data_visual(
            f"{c}_trend",
            256,
            244,
            1320,
            280,
            "areaChart",
            {"Category": [("col", DD, "full_date")], "Y": [TCV]},
            title="Monthly contract value booked",
            subtitle=VALUE_CAVEAT,
        )
    )
    vis.append(
        data_visual(
            f"{c}_top_depts",
            256,
            540,
            429,
            320,
            "barChart",
            {"Category": [("col", DEP, "department_name")], "Y": [TCV]},
            title="Top departments",
            filters=topn(DEP, "department_name", 8, FD, "Total Contract Value"),
        )
    )
    vis.append(
        data_visual(
            f"{c}_top_suppliers",
            701,
            540,
            429,
            320,
            "barChart",
            {"Category": [("col", SUP, "canonical_name")], "Y": [TCV]},
            title="Top suppliers (canonical)",
            filters=topn(SUP, "canonical_name", 8, FD, "Total Contract Value"),
        )
    )
    vis.append(
        data_visual(
            f"{c}_acq_mix",
            1146,
            540,
            429,
            320,
            "barChart",
            {"Category": [("col", ACQ, "acquisition_type")], "Y": [TCV]},
            title="Acquisition mix",
        )
    )
    return vis


def _content_02(c):
    return [
        data_visual(
            f"{c}_dept_spend",
            256,
            128,
            648,
            740,
            "barChart",
            {"Category": [("col", DEP, "department_name")], "Y": [TCV]},
            title="Department spend",
            subtitle=VALUE_CAVEAT,
        ),
        data_visual(
            f"{c}_fy_matrix",
            928,
            128,
            648,
            740,
            "pivotTable",
            {
                "Rows": [("col", DEP, "business_unit"), ("col", DEP, "department_name")],
                "Columns": [("col", DD, "fiscal_year")],
                "Values": [TCV],
            },
            title="Fiscal-year matrix",
        ),
    ]


def _content_03(c):
    return [
        data_visual(
            f"{c}_leaderboard",
            256,
            128,
            1320,
            520,
            "tableEx",
            {
                "Values": [
                    ("col", SUP, "canonical_name"),
                    ("col", SUP, "parent_name"),
                    DOCS,
                    ("meas", MS, "Departments Served"),
                    TCV,
                    ("meas", MS, "Competitive %"),
                ]
            },
            title="Supplier leaderboard",
        ),
        data_visual(
            f"{c}_breadth_depth",
            256,
            672,
            648,
            196,
            "scatterChart",
            {
                "Category": [("col", SUP, "canonical_name")],
                "X": [TCV],
                "Y": [("meas", MS, "Departments Served")],
                "Size": [DOCS],
            },
            title="Breadth vs depth",
        ),
        textbox(
            f"{c}_drill_hint",
            928,
            672,
            648,
            40,
            [("Right-click a supplier → Drill through → Document detail", "Barlow", 9, MUTED)],
            z=100,
        ),
    ]


def _content_04(c):
    return [
        data_visual(
            f"{c}_value_share",
            256,
            128,
            1320,
            180,
            "hundredPercentStackedBarChart",
            {
                "Category": [("col", ACQ, "acquisition_type")],
                "Series": [("col", ACQ, "acquisition_sub_type")],
                "Y": [TCV],
            },
            title="Value share by acquisition type",
        ),
        data_visual(
            f"{c}_comp_vs_not",
            256,
            340,
            648,
            420,
            "barChart",
            {"Category": [("col", ACQ, "acquisition_method")], "Y": [TCV]},
            title="Competitive vs non-competitive",
            subtitle=VALUE_CAVEAT,
        ),
        data_visual(
            f"{c}_comp_by_dept",
            928,
            340,
            648,
            420,
            "barChart",
            {"Category": [("col", DEP, "department_name")], "Y": [("meas", MS, "Competitive %")]},
            title="Competitive share by department",
        ),
    ]


def _content_05(c):
    return [
        data_visual(
            f"{c}_conc_table",
            256,
            128,
            800,
            420,
            "tableEx",
            {
                "Values": [
                    ("col", GMC, "business_unit"),
                    ("col", GMC, "acquisition_type"),
                    ("meas", MS, "Market HHI"),
                    ("meas", MS, "Top Supplier %"),
                    ("agg", GMC, "supplier_count", "Sum"),
                    ("agg", GMC, "market_value", "Sum"),
                ]
            },
            title="Market concentration",
        ),
        data_visual(
            f"{c}_value_vs_hhi",
            1080,
            128,
            496,
            420,
            "scatterChart",
            {
                "Category": [("col", GMC, "business_unit"), ("col", GMC, "acquisition_type")],
                "X": [("agg", GMC, "market_value", "Sum")],
                "Y": [("agg", GMC, "hhi", "Avg")],
                "Size": [("agg", GMC, "supplier_count", "Sum")],
            },
            title="Value vs concentration",
        ),
        data_visual(
            f"{c}_pareto",
            256,
            572,
            1320,
            280,
            "lineStackedColumnComboChart",
            {
                "Category": [("col", SUP, "canonical_name")],
                "Y": [("meas", MS, "Supplier Share")],
                "Y2": [("meas", MS, "Cumulative Supplier Share")],
            },
            title="Share held by top suppliers",
            filters=topn(SUP, "canonical_name", 20, FD, "Total Contract Value"),
        ),
    ]


def _content_06(c):
    cards = [
        ("SB SHARE OF VALUE", "SB % of Value", "Goal 25%"),
        ("DVBE SHARE OF VALUE", "DVBE % of Value", "Goal 3%"),
        ("MICRO BUSINESS SHARE", "Micro Business % of Value", None),
        ("CERTS EXPIRING IN 180 DAYS", "Certifications Expiring 180d", None),
    ]
    vis = [
        kpi(f"{c}_kpi_{i}", 256 + i * 334, 128, 318, t, MS, meas, sub)
        for i, (t, meas, sub) in enumerate(cards)
    ]
    vis.append(
        data_visual(
            f"{c}_share_by_dept",
            256,
            244,
            648,
            616,
            "barChart",
            {
                "Category": [("col", DEP, "department_name")],
                "Y": [
                    ("meas", MS, "SB % of Value"),
                    ("meas", MS, "Micro Business % of Value"),
                    ("meas", MS, "DVBE % of Value"),
                ],
            },
            title="SB + DVBE share of value by department",
        )
    )
    vis.append(
        data_visual(
            f"{c}_cert_leaders",
            928,
            244,
            648,
            616,
            "tableEx",
            {
                "Values": [
                    ("col", GSC, "canonical_name"),
                    ("col", GSC, "certification_types"),
                    ("col", GSC, "registration_count"),
                    ("col", GSC, "latest_cert_end"),
                    TCV,
                ]
            },
            title="Certified supplier leaders",
        )
    )
    return vis


def _content_07(c):
    cards = [
        ("CMAS HOLDER SHARE", "CMAS Holder Share", "of value in scope"),
        ("EXPIRING WITHIN A YEAR", "CMAS Expiring 12m", "renewal watch"),
        ("CMAS HOLDERS", "CMAS Holders", "in scope"),
    ]
    vis = [
        kpi(f"{c}_kpi_{i}", 256 + i * 189, 128, 173, t, MS, meas, sub)
        for i, (t, meas, sub) in enumerate(cards)
    ]
    vis.append(
        data_visual(
            f"{c}_leveraged",
            256,
            244,
            551,
            420,
            "barChart",
            {
                "Y": [
                    ("meas", MS, "Leveraged Value"),
                    ("meas", MS, "Open Market Value"),
                    ("meas", MS, "Non-Competitive Value"),
                ]
            },
            title="Leveraged vs open-market value",
            subtitle=VALUE_CAVEAT,
        )
    )
    vis.append(
        data_visual(
            f"{c}_holders",
            831,
            128,
            745,
            536,
            "tableEx",
            {
                "Values": [
                    ("col", GCM, "canonical_name"),
                    ("col", GCM, "cmas_agreement_count"),
                    ("col", GCM, "base_schedule_count"),
                    ("col", GCM, "cmas_agreement_numbers"),
                    ("col", GCM, "latest_term_end"),
                    TCV,
                ]
            },
            title="CMAS holders",
        )
    )
    return vis


def _content_08(c):
    cards = [
        ("AMENDED DOCUMENTS", "Amended Documents", "version > 0, from the star"),
        ("MEAN AMENDMENT DEPTH", "Mean Amendment Depth", "revisions per document"),
        ("AMENDMENT VALUE GROWTH", "Amendment Value Growth", "2+ snapshot docs only"),
        ("AMENDMENT GROWTH SHARE", "Amendment Growth %", "vs implied original value"),
    ]
    vis = [
        textbox(
            f"{c}_coverage",
            256,
            128,
            1320,
            44,
            [
                (
                    "Coverage caveat: amendments are visible only for documents captured "
                    "at 2+ snapshots. The feed is sparse (deepest for BU 8660) — read this "
                    "page as a signal of where contracts grow, never as a complete "
                    "amendment register.",
                    "Barlow",
                    9,
                    MUTED,
                )
            ],
            z=100,
        )
    ]
    vis += [
        kpi(f"{c}_kpi_{i}", 256 + i * 334, 188, 318, t, MS, meas, sub)
        for i, (t, meas, sub) in enumerate(cards)
    ]
    vis.append(
        data_visual(
            f"{c}_growth_table",
            256,
            304,
            1320,
            380,
            "tableEx",
            {
                "Values": [
                    ("col", GCA, "purchase_document"),
                    ("col", GCA, "business_unit"),
                    ("col", GCA, "amendment_count"),
                    ("col", GCA, "snapshots_captured"),
                    ("col", GCA, "current_value"),
                    ("col", GCA, "value_growth"),
                ]
            },
            title="Contracts that grew the most after award",
        )
    )
    vis.append(
        data_visual(
            f"{c}_version_hist",
            256,
            700,
            1320,
            160,
            "columnChart",
            {"Category": [("col", FD, "version")], "Y": [DOCS]},
            title="Version distribution (all documents)",
        )
    )
    return vis


def _content_09(c):
    cards = [
        ("OPEN EVENTS", "Open Events", "live solicitations"),
        ("DEPARTMENTS POSTING", "Departments Posting", None),
        ("SET-ASIDE EVENTS", "Set-Aside Events %", "SB or DVBE preference"),
        ("CLOSING IN 14 DAYS", "Events Closing 14d", "the act-now number"),
    ]
    vis = [
        kpi(f"{c}_kpi_{i}", 256 + i * 334, 128, 318, t, MS, meas, sub)
        for i, (t, meas, sub) in enumerate(cards)
    ]
    vis.append(
        data_visual(
            f"{c}_open_table",
            256,
            244,
            797,
            616,
            "tableEx",
            {
                "Values": [
                    ("col", GEP, "event_name"),
                    ("col", GEP, "business_unit"),
                    ("col", GEP, "department_name"),
                    ("col", GEP, "sb_only"),
                    ("col", GEP, "dvbe_only"),
                    ("col", GEP, "bid_close_date"),
                    ("meas", MS, "Days To Close"),
                ]
            },
            title="Open solicitations",
        )
    )
    vis.append(
        data_visual(
            f"{c}_repeat_demand",
            1077,
            244,
            499,
            616,
            "lineClusteredColumnComboChart",
            {
                "Category": [("col", GED, "department_name")],
                "Y": [("agg", GED, "event_count", "Sum")],
                "Y2": [("agg", GED, "set_aside_pct", "Avg")],
            },
            title="Repeat demand by department",
            subtitle="by buyer, not commodity — the mart has no category column",
        )
    )
    return vis


def _content_10(c):
    cards = [
        ("DOCUMENTS IN SCOPE", FD, "Document Count", "current versions only"),
        ("TOTAL VALUE", FD, "Total Contract Value", "booked, start-date basis"),
        ("ENRICHED SHARE", MS, "Enriched Document %", "line detail available"),
        ("LINE RECONCILIATION", MS, "Line Reconciliation Rate", "lines sum to header"),
    ]
    vis = [
        kpi(f"{c}_kpi_{i}", 256 + i * 334, 128, 318, t, e, meas, sub)
        for i, (t, e, meas, sub) in enumerate(cards)
    ]
    vis.append(
        data_visual(
            f"{c}_detail_table",
            256,
            244,
            1320,
            616,
            "tableEx",
            {
                "Values": [
                    ("col", FD, "purchase_document"),
                    ("col", FD, "bill_code"),
                    ("col", DEP, "department_name"),
                    ("col", SUP, "supplier_name"),
                    ("col", ACQ, "acquisition_method"),
                    ("col", DD, "full_date"),
                    ("col", FD, "status"),
                    ("col", FD, "version"),
                    ("col", FD, "line_count"),
                    ("col", FD, "grand_total"),
                ]
            },
            title="Document detail",
        )
    )
    return vis


CONTENT = [
    _content_01,
    _content_02,
    _content_03,
    _content_04,
    _content_05,
    _content_06,
    _content_07,
    _content_08,
    _content_09,
    _content_10,
]


def content(section, index):
    return CONTENT[index](f"gen_{section}")


NAV_Y0, NAV_STEP = 156, 32


def chrome(section, index):
    """Sidebar + header chrome for one page. index None = Notes page."""
    p = f"chrome_{section}"
    vis = [
        rectangle(f"{p}_sidebar_divider", 231, 0, 1, CANVAS_H, HAIRLINE),
        textbox(
            f"{p}_kicker", 24, 24, 184, 16, [("POWER BI · REPORT", "Barlow Condensed", 9, ACCENT)]
        ),
        textbox(
            f"{p}_product",
            24,
            44,
            184,
            52,
            [("SCPRS Procurement Warehouse", "Barlow Condensed", 20, INK)],
        ),
        textbox(
            f"{p}_scope",
            24,
            100,
            184,
            34,
            [
                ("21 departments · FY22–FY27", "Barlow", 8, MUTED),
                ("Supplier opportunity view", "Barlow", 8, MUTED),
            ],
        ),
        button(
            f"{p}_notes_btn",
            16,
            820,
            200,
            28,
            "Notes and definitions",
            NOTES_SECTION,
            font_size=9,
            outlined=True,
        ),
        textbox(
            f"{p}_footer",
            24,
            856,
            184,
            36,
            [
                ("Warehouse serve dataset", "Barlow", 7, FAINT),
                ("marts refresh up to 8×/day", "Barlow", 7, FAINT),
            ],
        ),
        rectangle(f"{p}_header_divider", 232, 104, 1368, 1, HAIRLINE),
    ]
    for i, (target, label, _, _) in enumerate(PAGES):
        vis.append(
            button(
                f"{p}_nav_{i:02d}",
                16,
                NAV_Y0 + i * NAV_STEP,
                200,
                30,
                label,
                target,
                active=(i == index),
            )
        )
    if index is not None:
        vis.append(
            rectangle(f"{p}_active_mark", 0, NAV_Y0 + index * NAV_STEP, 3, 30, ACCENT_LIGHT, z=2)
        )
        title, subtitle = PAGES[index][2], PAGES[index][3]
        title_x = 300 if index == DETAIL_INDEX else 256
        vis.append(
            textbox(f"{p}_title", title_x, 24, 700, 40, [(title, "Barlow Condensed", 22, INK)], z=3)
        )
        vis.append(
            textbox(f"{p}_subtitle", title_x, 66, 700, 22, [(subtitle, "Barlow", 9, MUTED)], z=3)
        )
        if index == DETAIL_INDEX:
            vis.append(button(f"{p}_back", 256, 24, 32, 32, "←", "Back", outlined=True))
        else:
            vis.append(
                slicer(
                    f"{p}_slicer_dept",
                    1000,
                    28,
                    180,
                    60,
                    "dim_department",
                    "department_name",
                    "DEPARTMENT",
                    "Dropdown",
                    "sync-department",
                )
            )
            vis.append(
                slicer(
                    f"{p}_slicer_acq",
                    1192,
                    28,
                    160,
                    60,
                    "dim_acquisition",
                    "acquisition_type",
                    "ACQUISITION TYPE",
                    "Dropdown",
                    "sync-acquisition",
                )
            )
            vis.append(
                slicer(
                    f"{p}_slicer_fy",
                    1364,
                    28,
                    212,
                    60,
                    "dim_date",
                    "fiscal_year",
                    "FISCAL YEAR",
                    "Basic",
                    "sync-fiscal-year",
                    orientation=1,
                )
            )
            vis.append(filter_state_card(f"{p}_filter_state"))
    return vis


def notes_content():
    p = f"chrome_{NOTES_SECTION}"
    vis = [
        textbox(
            f"{p}_title",
            256,
            24,
            700,
            40,
            [("Notes and definitions", "Barlow Condensed", 22, INK)],
            z=3,
        ),
        textbox(
            f"{p}_subtitle",
            256,
            66,
            700,
            22,
            [
                (
                    "Definitions the report carries; the fiscal-framing caveat repeats as a "
                    "subtitle on every value chart.",
                    "Barlow",
                    9,
                    MUTED,
                )
            ],
            z=3,
        ),
    ]
    for i, (term, definition) in enumerate(DEFINITIONS):
        col, row = divmod(i, 6)
        vis.append(
            textbox(
                f"{p}_def_{i:02d}",
                256 + col * 672,
                128 + row * 116,
                620,
                104,
                [(term, "Barlow Condensed", 12, INK), (definition, "Barlow", 9, MUTED)],
                z=4,
            )
        )
    return vis


def section_config(hidden=False):
    cfg = {
        "objects": {
            "background": [{"properties": {"color": color(GROUND), "transparency": lit("0D")}}],
            "outspace": [{"properties": {"color": color(GROUND), "transparency": lit("0D")}}],
        }
    }
    if hidden:
        cfg["visibility"] = 1
    return cfg


def build_section(name, display_name, visuals, hidden=False):
    return {
        "config": json.dumps(section_config(hidden)),
        "displayName": display_name,
        "displayOption": 1,
        "filters": "[]",
        "height": float(CANVAS_H),
        "name": name,
        "visualContainers": visuals,
        "width": float(CANVAS_W),
    }


def visual_name(vc):
    try:
        return json.loads(vc["config"]).get("name", "")
    except (KeyError, ValueError):
        return ""


def build(merge: bool, force: bool) -> None:
    sections = []
    for i, (name, _, title, _) in enumerate(PAGES):
        sections.append(
            build_section(name, f"{i + 1:02d} {title}", chrome(name, i) + content(name, i))
        )
    sections.append(
        build_section(
            NOTES_SECTION,
            "Notes and definitions",
            chrome(NOTES_SECTION, None) + notes_content(),
            hidden=True,
        )
    )

    existing = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else None
    if existing:
        preserved = {
            s["name"]: [
                vc
                for vc in s.get("visualContainers", [])
                if not visual_name(vc).startswith("chrome_")
            ]
            for s in existing.get("sections", [])
        }
        if any(preserved.values()) and not (merge or force):
            sys.exit(
                "report.json already holds content visuals - "
                "re-run with --merge to keep them or --force to discard them."
            )
        if merge:
            for s in sections:
                kept = preserved.get(s["name"], [])
                # a visual edited or added in Desktop wins over its regenerated twin
                kept_names = {visual_name(vc) for vc in kept}
                s["visualContainers"] = [
                    vc
                    for vc in s["visualContainers"]
                    if visual_name(vc).startswith("chrome_") or visual_name(vc) not in kept_names
                ]
                s["visualContainers"].extend(kept)
            known = {s["name"] for s in sections}
            # keep pages this script does not own (tooltip pages, experiments)
            sections.extend(s for s in existing.get("sections", []) if s["name"] not in known)

    report = {
        "config": json.dumps(
            {
                "version": "5.43",
                "activeSectionIndex": 0,
                "defaultDrillFilterOtherVisuals": True,
                "themeCollection": {
                    "customTheme": {"name": "industry-theme.json", "type": "RegisteredResources"}
                },
            }
        ),
        "filters": "[]",
        "layoutOptimization": 0,
        "resourcePackages": [
            {
                "resourcePackage": {
                    "name": "RegisteredResources",
                    "type": 1,
                    "disabled": False,
                    "items": [
                        {"name": "industry-theme.json", "path": "industry-theme.json", "type": 202}
                    ],
                }
            }
        ],
        "sections": sections,
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    n_vis = sum(len(s["visualContainers"]) for s in sections)
    print(f"wrote {REPORT} - {len(sections)} sections, {n_vis} visuals")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--merge", action="store_true", help="regenerate chrome, preserve content visuals by name"
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite report.json even if it holds content visuals",
    )
    args = ap.parse_args()
    build(merge=args.merge, force=args.force)
