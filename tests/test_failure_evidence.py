"""Offline tests for src/failure_evidence.py, against a fake Playwright page.

CI has no browser; these lock down the contract the scrapers rely on: capture
writes the evidence, never raises, stays bounded, and records nothing that
should not land in a public artifact. (A real-Chromium check is in the PR.)
"""

import json
from types import SimpleNamespace

import pytest

from src import failure_evidence
from src.failure_evidence import PageWatch


class FakeTimeout(Exception):
    pass


class FakePage:
    def __init__(self, *, screenshot_error=None, title="Event Search", text="Search Events"):
        self.handlers = {}
        self.url = "https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx?ICSID=secret#x"
        self._screenshot_error = screenshot_error
        self._title = title
        self._text = text
        self.dom_calls = 0

    def on(self, event, handler):
        self.handlers[event] = handler

    def emit(self, event, arg):
        self.handlers[event](arg)

    def screenshot(self, *, path, full_page, timeout):
        assert full_page and timeout == failure_evidence.SCREENSHOT_TIMEOUT_MS
        if self._screenshot_error:
            raise self._screenshot_error
        with open(path, "wb") as f:
            f.write(b"\x89PNG fake")
        return b"\x89PNG fake"

    def title(self):
        self.dom_calls += 1
        return self._title

    def evaluate(self, _script, n):
        self.dom_calls += 1
        return self._text[:n]

    def content(self):
        self.dom_calls += 1
        return "<html><body>Search Events</body></html>"


class _Request:  # identity-hashable, like Playwright's own Request objects
    def __init__(self, url, method, failure):
        self.url, self.method, self.failure = url, method, failure


def _request(url, method="GET", failure=None):
    return _Request(url, method, failure)


@pytest.fixture(autouse=True)
def _fresh_limit(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    PageWatch.reset_limit()
    yield
    PageWatch.reset_limit()


def test_capture_writes_screenshot_html_and_report(tmp_path, capsys):
    page = FakePage()
    watch = PageWatch(page, "events-historical:2026-attempt1", out_dir=tmp_path)
    hung = _request("https://caleprocure.ca.gov/nlx3/psc/x?tok=1", method="POST")
    page.emit("request", hung)
    done = _request("https://caleprocure.ca.gov/app.js")
    page.emit("request", done)
    page.emit("requestfinished", done)
    page.emit("response", SimpleNamespace(status=403, url="https://caleprocure.ca.gov/blocked?q=1"))
    page.emit("console", SimpleNamespace(type="error", text="Uncaught TypeError: x is undefined"))
    page.emit("console", SimpleNamespace(type="log", text="noise"))

    out = watch.capture(FakeTimeout("Page.wait_for_selector: Timeout 120000ms exceeded."))

    assert out is not None and out.parent == tmp_path
    assert out.name.endswith("events-historical-2026-attempt1")  # label slugged for a path
    assert (out / "screenshot.png").exists() and (out / "page.html").exists()
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["error"].startswith("FakeTimeout: Page.wait_for_selector")
    assert report["title"] == "Event Search"
    assert report["visible_text"] == "Search Events"
    assert [r["url"] for r in report["in_flight"]] == ["https://caleprocure.ca.gov/nlx3/psc/x"]
    assert report["http_errors"] == [{"status": 403, "url": "https://caleprocure.ca.gov/blocked"}]
    assert report["console"] == ["error: Uncaught TypeError: x is undefined"]
    line = capsys.readouterr().out
    assert line.startswith("failure evidence [events-historical-2026-attempt1]: ")
    assert "1 request(s) in flight" in line and "POST https://caleprocure.ca.gov/nlx3/psc/x" in line
    assert "1 HTTP>=400, last: 403" in line


def test_nothing_secret_reaches_the_evidence(tmp_path):
    """Public repo, public artifacts: query strings (PeopleSoft session ids) are
    dropped everywhere; headers and cookies are never read in the first place."""
    page = FakePage()
    watch = PageWatch(page, "registry", out_dir=tmp_path)
    page.emit("request", _request("https://suppliers.fiscal.ca.gov/psc/x?ICSID=abc123"))
    page.emit(
        "requestfailed",
        _request(
            "https://suppliers.fiscal.ca.gov/psc/y?ICSID=abc123", failure="net::ERR_EMPTY_RESPONSE"
        ),
    )
    out = watch.capture("boom")
    report_text = (out / "report.json").read_text(encoding="utf-8")
    assert "ICSID" not in report_text and "secret" not in report_text
    assert json.loads(report_text)["failed_requests"] == [
        {
            "method": "GET",
            "url": "https://suppliers.fiscal.ca.gov/psc/y",
            "failure": "net::ERR_EMPTY_RESPONSE",
        }
    ]


def test_hung_renderer_skips_dom_probes(tmp_path):
    """A screenshot timeout means the renderer is stuck; title/evaluate/content
    have no timeout and would hang the run until timeout-minutes kills it."""
    page = FakePage(screenshot_error=FakeTimeout("Timeout 15000ms exceeded."))
    out = PageWatch(page, "x", out_dir=tmp_path).capture("boom")
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert page.dom_calls == 0
    assert any("renderer looks hung" in e for e in report["probe_errors"])
    assert not (out / "page.html").exists()


def test_a_failing_probe_is_recorded_not_raised(tmp_path):
    page = FakePage()

    def broken():
        raise RuntimeError("Target page, context or browser has been closed")

    page.content = broken
    out = PageWatch(page, "x", out_dir=tmp_path).capture("boom")
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["title"] == "Event Search"  # the other probes still ran
    assert any(e.startswith("html: RuntimeError") for e in report["probe_errors"])


def test_capture_never_raises_even_when_the_disk_does(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    assert PageWatch(FakePage(), "x", out_dir=blocker).capture("boom") is None
    assert "capture itself failed" in capsys.readouterr().out


def test_captures_are_capped_per_process(tmp_path, capsys):
    outs = [
        PageWatch(FakePage(), f"p{i}", out_dir=tmp_path).capture("boom")
        for i in range(failure_evidence.MAX_CAPTURES + 2)
    ]
    assert sum(o is not None for o in outs) == failure_evidence.MAX_CAPTURES
    assert "limit of 8 captures per run was reached" in capsys.readouterr().out


def test_annotation_only_on_actions(tmp_path, capsys, monkeypatch):
    PageWatch(FakePage(), "a", out_dir=tmp_path).capture("boom")
    assert "::warning" not in capsys.readouterr().out
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    PageWatch(FakePage(title="50% off\nnow"), "b", out_dir=tmp_path).capture("boom")
    annotation = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::warning")]
    assert len(annotation) == 1 and "50%25 off" in annotation[0]  # workflow-command escaped


def test_listener_errors_never_reach_playwright(tmp_path):
    page = FakePage()
    PageWatch(page, "x", out_dir=tmp_path)
    watch = PageWatch(page, "y", out_dir=tmp_path)
    for event in ("console", "request", "requestfailed", "response"):
        page.emit(event, object())  # attributes missing: must be swallowed, and counted
    assert watch.listener_errors == 4
