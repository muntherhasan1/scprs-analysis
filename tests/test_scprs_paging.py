"""collect_po_details completeness against a fake results grid (no browser).

A day may only be reported complete when the DISTINCT (document, version) rows
covered reach the grid's own total (a document has one row per version): the
grid order can shift between pages (a document shown twice, another never),
and a grid that never renders must not read as an empty day. Both used to be
recorded as done days (2026-10 review)."""

import contextlib
import json

import pytest

from src import failure_evidence, scprs


class _Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    def press_sequentially(self, value, delay=0):
        self.page.vals[self.sel] = value

    def _index(self):
        return int(self.sel.split("$")[1].rstrip("']"))

    def count(self):
        grid = self.page.grid()
        if self.sel.startswith("a[id^='PURCHASE_DOC$']"):
            return len(grid)
        if self.sel.startswith(("[id='PURCHASE_DOC$", "[id='VERSION_NBR$")):
            return 1 if self._index() < len(grid) else 0
        if self.sel == scprs._NEXT_PAGE:
            return 1 if self.page.pg + 1 < len(self.page.pages) else 0
        return 1

    def is_visible(self):
        return self.count() > 0

    def inner_text(self):
        # Grid cells are "DOC" or "DOC@VERSION" (version defaults to 0).
        doc, _, ver = self.page.grid()[self._index()].partition("@")
        if self.sel.startswith("[id='VERSION_NBR$"):
            return ver or "0"
        self.page.last = doc
        return doc

    def click(self):
        if self.sel == scprs._NEXT_PAGE and not self.page.stuck:
            self.page.pg += 1


class _Page:
    url = scprs.SEARCH_URL

    def __init__(self, pages, total, loaded=True, stuck=False, goto_error=None):
        self.pages, self.total, self.loaded = pages, total, loaded
        self.stuck, self.goto_error = stuck, goto_error  # Next never advances / goto fails
        self.pg, self.vals, self.last, self.drilled = 0, {}, None, []
        self.events: list[str] = []  # "capture" / "browser-close", in order
        self.keyboard = self

    # what failure_evidence.PageWatch needs
    def on(self, *_a):
        pass

    def screenshot(self, *, path, **_k):
        self.events.append("capture")
        with open(path, "wb") as f:
            f.write(b"png")
        return b"png"

    def title(self):
        return "SCPRS search"

    def evaluate(self, *_a):
        return self.inner_text("body")

    def content(self):
        return "<html></html>"

    def grid(self):
        return self.pages[self.pg] if self.loaded else []

    def press(self, key):
        pass

    def fill(self, sel, value):
        self.vals[sel] = value

    def type(self, sel, value, delay=0):
        self.vals[sel] = self.vals.get(sel, "") + value

    def goto(self, *a, **k):
        if self.goto_error:
            raise self.goto_error

    def click(self, sel):
        pass

    def locator(self, sel):
        return _Loc(self, sel)

    def wait_for_timeout(self, ms):
        pass

    def input_value(self, sel):
        return self.vals.get(sel, "")

    def inner_text(self, sel):
        if not self.loaded:
            return "Processing..."
        start = sum(len(p) for p in self.pages[: self.pg]) + 1
        return f"{start} to {start + len(self.grid()) - 1} of {self.total}"


class _Detail:
    def __init__(self, page):
        self.page = page
        page.drilled.append(page.last)

    def wait_for_load_state(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return "<html></html>"

    def close(self):
        pass


def _fake_playwright(page):
    class Ctx:
        def new_page(self):
            return page

        @contextlib.contextmanager
        def expect_page(self, timeout=0):
            class Pop:
                pass

            pop = Pop()
            yield pop
            pop.value = _Detail(page)

    class PW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def chromium(self):
            return self

        def launch(self, **k):
            return self

        def new_context(self, **k):
            return Ctx()

        def close(self):
            page.events.append("browser-close")

    return PW


def _collect(monkeypatch, page, **kw):
    monkeypatch.setattr(scprs, "sync_playwright", _fake_playwright(page))
    stats = {}
    res = scprs.collect_po_details(
        "9999", "01/02/2025", "01/02/2025", stats=stats, log=lambda *a: None, **kw
    )
    return [r["document"] for r in res], stats


def test_shifting_grid_order_is_not_a_complete_day(monkeypatch):
    page = _Page([["D1", "D2"], ["D2", "D3"]], total=4)  # D2 twice, D4 never shown
    docs, stats = _collect(monkeypatch, page)
    assert docs == ["D1", "D2", "D3"] and page.drilled == ["D1", "D2", "D3"]  # D2 drilled once
    assert stats["complete"] is False


def test_unrendered_grid_is_not_an_empty_day(monkeypatch):
    docs, stats = _collect(monkeypatch, _Page([["D1"]], total=1, loaded=False))
    assert docs == [] and stats["complete"] is False


def test_document_listed_once_per_version_is_a_complete_day(monkeypatch):
    # The grid total counts (document, version) rows: 0690 2026-01-27 lists
    # S251017090 at versions 0 and 1 under "1 to 7 of 7". Drill it once; the
    # day is complete. Counting distinct documents (6 of 7) never finished it.
    page = _Page([["D1", "D2@0", "D2@1", "D3"]], total=4)
    docs, stats = _collect(monkeypatch, page)
    assert docs == ["D1", "D2", "D3"] and page.drilled == ["D1", "D2", "D3"]
    assert stats["complete"] is True


def test_version_rows_count_for_skipped_documents(monkeypatch):
    page = _Page([["D1@0", "D1@1"], ["D2"]], total=3)
    docs, stats = _collect(monkeypatch, page, skip_docs={"D1", "D2"})
    assert docs == [] and stats["complete"] is True and stats["skipped"] == 2


def test_full_coverage_with_skips_is_complete(monkeypatch):
    page = _Page([["D1", "D2"], ["D3", "D4"]], total=4)
    docs, stats = _collect(monkeypatch, page, skip_docs={"D2"})
    assert docs == ["D1", "D3", "D4"]
    assert stats == {"total": 4, "drilled": 3, "skipped": 1, "complete": True}


# -------------------------------------------------------------- failure evidence
# collect_po_details captures what the search page showed (src/failure_evidence.py)
# whenever a day ends up incomplete for a reason a log line can't explain.


def _captures():
    d = failure_evidence.EVIDENCE_DIR
    return [json.loads((p / "report.json").read_text()) for p in sorted(d.iterdir())]


def test_unrendered_grid_captures_what_the_page_showed(monkeypatch):
    page = _Page([["D1"]], total=1, loaded=False)
    _collect(monkeypatch, page)
    [report] = _captures()
    assert report["label"] == "drill-9999-01-02-2025-01-02-2025"
    assert "results grid did not load" in report["error"]
    assert report["visible_text"] == "Processing..."
    assert page.events == ["capture", "browser-close"]


def test_stalled_next_page_captures_what_the_page_showed(monkeypatch):
    page = _Page([["D1"], ["D2"]], total=2, stuck=True)
    docs, stats = _collect(monkeypatch, page)
    assert docs == ["D1"] and stats["complete"] is False  # unchanged behaviour
    [report] = _captures()
    assert "next-page click did not advance past page 1" in report["error"]
    assert page.events == ["capture", "browser-close"]


def test_drill_error_is_captured_before_the_browser_closes(monkeypatch):
    page = _Page([["D1"]], total=1, goto_error=RuntimeError("net::ERR_EMPTY_RESPONSE"))
    with pytest.raises(RuntimeError, match="ERR_EMPTY_RESPONSE"):
        _collect(monkeypatch, page)
    [report] = _captures()
    assert report["error"] == "RuntimeError: net::ERR_EMPTY_RESPONSE"
    assert page.events == ["capture", "browser-close"]


def test_complete_day_captures_nothing(monkeypatch):
    page = _Page([["D1", "D2"], ["D3"]], total=3)
    _, stats = _collect(monkeypatch, page)
    assert stats["complete"] is True
    assert _captures() == [] and page.events == ["browser-close"]
