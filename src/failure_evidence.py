"""Evidence from a failed browser scrape: what the page showed, not just which wait timed out.

A Playwright timeout only says which element never appeared. The 2026-11
eProcure failures ("RESP_INQA_WK_ZZ_EVENT_STATUS never became visible") could
not be told apart after the fact: a slow relay, a WAF block page and a script
error all read the same. ``PageWatch`` listens to a page from the moment it is
created and, when a scrape fails, ``capture`` writes to
``data/failure-evidence/<UTC stamp>-<label>/``:

- ``screenshot.png`` — the full page as rendered at failure time;
- ``page.html`` — the DOM at failure time;
- ``report.json`` — error, URL, title, visible text excerpt, console errors,
  page errors, failed requests, HTTP >= 400 responses, and the requests still
  in flight with their age (a hung relay call shows up here).

It also prints one ``failure evidence [<label>]: ...`` digest line, which lands
in the run log (``src/triage.py`` quotes it in the issue) and, on Actions, as a
warning annotation on the run page. The workflows upload the directory as a
``failure-evidence-*`` run artifact.

Rules: capture never raises into the scraper (every probe is guarded and its
error recorded instead); at most ``MAX_CAPTURES`` per process, so a flapping
site cannot fill the runner disk; if the screenshot times out the renderer is
treated as hung and the DOM probes (which have no timeout) are skipped. The
repo is public, so artifacts are downloadable by anyone: URL query strings are
dropped and request/response headers, bodies and cookies are never recorded.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "failure-evidence"
DIGEST_PREFIX = "failure evidence"
MAX_CAPTURES = 8
SCREENSHOT_TIMEOUT_MS = 15_000
_KEEP = 40  # most recent entries kept per event list
_TEXT_CHARS = 2000


def _strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:80] or "page"


def _one_line(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _annotation_escape(text: str) -> str:
    # GitHub workflow-command data escaping.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class PageWatch:
    """Record what a Playwright page does, so a failure can be captured with context.

    Create it right after ``new_page()`` (listeners only see what happens after
    they attach) and call ``capture(error)`` in the failure path, before the
    context closes.
    """

    _captures = 0  # per process, across all watches

    def __init__(self, page, label: str, *, out_dir: Path | None = None) -> None:
        self.page = page
        self.label = _slug(label)
        self.out_dir = Path(out_dir) if out_dir else EVIDENCE_DIR
        self.console: list[str] = []
        self.page_errors: list[str] = []
        self.failed_requests: list[dict] = []
        self.http_errors: list[dict] = []
        self._in_flight: dict[object, tuple[str, str, float]] = {}
        self.listener_errors = 0  # events a listener could not record
        for event, handler in (
            ("console", self._on_console),
            ("pageerror", self._on_page_error),
            ("request", self._on_request),
            ("requestfinished", self._on_finished),
            ("requestfailed", self._on_failed),
            ("response", self._on_response),
        ):
            try:
                page.on(event, handler)
            except Exception:  # noqa: BLE001 - a listener error must never reach Playwright
                self.listener_errors += 1

    @classmethod
    def reset_limit(cls) -> None:
        """Reset the per-process capture count (tests; long-lived processes)."""
        cls._captures = 0

    # -- listeners (each guarded: an exception here must never reach Playwright)

    def _push(self, bucket: list, item) -> None:
        bucket.append(item)
        del bucket[:-_KEEP]

    def _on_console(self, msg) -> None:
        try:
            if msg.type in ("error", "warning"):
                self._push(self.console, f"{msg.type}: {_one_line(msg.text, 300)}")
        except Exception:  # noqa: BLE001 - a listener error must never reach Playwright
            self.listener_errors += 1

    def _on_page_error(self, exc) -> None:
        try:
            self._push(self.page_errors, _one_line(str(exc), 500))
        except Exception:  # noqa: BLE001 - a listener error must never reach Playwright
            self.listener_errors += 1

    def _on_request(self, request) -> None:
        try:
            self._in_flight[request] = (request.method, _strip_query(request.url), time.monotonic())
        except Exception:  # noqa: BLE001 - a listener error must never reach Playwright
            self.listener_errors += 1

    def _on_finished(self, request) -> None:
        self._in_flight.pop(request, None)

    def _on_failed(self, request) -> None:
        try:
            self._in_flight.pop(request, None)
            self._push(
                self.failed_requests,
                {
                    "method": request.method,
                    "url": _strip_query(request.url),
                    "failure": _one_line(str(request.failure or ""), 200),
                },
            )
        except Exception:  # noqa: BLE001 - a listener error must never reach Playwright
            self.listener_errors += 1

    def _on_response(self, response) -> None:
        try:
            if response.status >= 400:
                self._push(
                    self.http_errors, {"status": response.status, "url": _strip_query(response.url)}
                )
        except Exception:  # noqa: BLE001 - a listener error must never reach Playwright
            self.listener_errors += 1

    # -- capture

    def capture(self, error: BaseException | str) -> Path | None:
        """Write the evidence directory and print its digest. Never raises."""
        try:
            return self._capture(error)
        except Exception as e:  # noqa: BLE001 - last line of defence
            print(
                f"{DIGEST_PREFIX} [{self.label}]: capture itself failed ({type(e).__name__}: {e})"
            )
            return None

    def _capture(self, error: BaseException | str) -> Path | None:
        if PageWatch._captures >= MAX_CAPTURES:
            print(
                f"{DIGEST_PREFIX} [{self.label}]: not captured — the limit of "
                f"{MAX_CAPTURES} captures per run was reached"
            )
            return None
        PageWatch._captures += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        out = self.out_dir / f"{stamp}-{self.label}"
        out.mkdir(parents=True, exist_ok=True)

        probe_errors: list[str] = []

        def probe(name, fn):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 - recorded, not raised
                probe_errors.append(f"{name}: {type(e).__name__}: {_one_line(str(e), 300)}")
                return None

        err_text = (
            f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        )
        report: dict = {
            "label": self.label,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": err_text[:4000],
            "url": probe("url", lambda: _strip_query(self.page.url)),
        }
        shot = probe(
            "screenshot",
            lambda: self.page.screenshot(
                path=str(out / "screenshot.png"), full_page=True, timeout=SCREENSHOT_TIMEOUT_MS
            ),
        )
        renderer_hung = shot is None and any(
            e.startswith("screenshot:") and "Timeout" in e for e in probe_errors
        )
        if renderer_hung:
            probe_errors.append("dom probes skipped: the screenshot timed out, renderer looks hung")
        else:
            report["title"] = probe("title", self.page.title)
            report["visible_text"] = probe(
                "visible_text",
                lambda: self.page.evaluate(
                    "n => document.body ? document.body.innerText.slice(0, n) : ''", _TEXT_CHARS
                ),
            )
            html = probe("html", self.page.content)
            if html:
                (out / "page.html").write_text(html, encoding="utf-8")
        now = time.monotonic()
        report["in_flight"] = [
            {"method": m, "url": u, "age_s": round(now - t0, 1)}
            for m, u, t0 in sorted(self._in_flight.values(), key=lambda v: v[2])
        ][:_KEEP]
        report["http_errors"] = self.http_errors
        report["failed_requests"] = self.failed_requests
        report["console"] = self.console
        report["page_errors"] = self.page_errors
        report["probe_errors"] = probe_errors
        report["listener_errors"] = self.listener_errors
        report["files"] = sorted(p.name for p in out.iterdir())
        (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

        digest = self.digest(report, out)
        print(f"{DIGEST_PREFIX} [{self.label}]: {digest}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=Failure evidence ({self.label})::{_annotation_escape(digest)}")
        return out

    @staticmethod
    def digest(report: dict, out: Path) -> str:
        """One line a person can read in the log or the triage issue."""
        parts = [f"title={_one_line(report.get('title') or '', 80)!r}", f"url={report.get('url')}"]
        text = _one_line(report.get("visible_text") or "", 160)
        if text:
            parts.append(f"text={text!r}")
        in_flight = report.get("in_flight") or []
        if in_flight:
            oldest = in_flight[0]
            parts.append(
                f"{len(in_flight)} request(s) in flight, oldest {oldest['age_s']:.0f}s: "
                f"{oldest['method']} {oldest['url']}"
            )
        else:
            parts.append("0 requests in flight")
        for key, name in (
            ("http_errors", "HTTP>=400"),
            ("failed_requests", "failed requests"),
            ("console", "console errors/warnings"),
            ("page_errors", "page errors"),
        ):
            items = report.get(key) or []
            if items:
                last = items[-1]
                shown = (
                    f"{last['status']} {last['url']}"
                    if key == "http_errors"
                    else f"{last['url']} ({last['failure']})"
                    if key == "failed_requests"
                    else _one_line(str(last), 120)
                )
                parts.append(f"{len(items)} {name}, last: {shown}")
        if "screenshot.png" not in (report.get("files") or []):
            parts.append("no screenshot")
        parts.append(f"saved {out.name}")
        return "; ".join(parts)
