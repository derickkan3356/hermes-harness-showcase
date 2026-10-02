# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "playwright==1.62.*",
#     "trafilatura>=1.12",
#     "pypdf>=5.0",
# ]
# ///
"""Layer 2 of the fetch cascade: render the page in Chromium, then extract.

Same protocol and same extraction as layer 1 — only the way the HTML is obtained
changes, so the two layers' results are comparable and the quality gate means the
same thing for both.

The Chromium build is the one Hermes' own browser tool already downloaded, under
Playwright's default cache (`~/.cache/ms-playwright`); this pins `playwright` to
the version that matches it. Nothing is installed here that the machine did not
already have.

The page's own text is taken from the rendered DOM, not from the browser tool's
accessibility snapshot: a snapshot is a control tree for clicking things, not
article text.

    in   {"urls": [str], "timeout": float, "format": "markdown"|"html"}
    out  {"results": [{"url", "status", "content_type", "title", "text",
                       "source_bytes", "error"}]}
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout, sync_playwright

from extract_common import ACCEPT_LANGUAGE, USER_AGENT, extract_html, row

# Many pages never reach network idle — trackers, polling, open sockets. Idle is
# treated as a bonus with its own small budget, not as the thing being waited for.
IDLE_BUDGET_MS = 4000


def read_content(page: Any) -> str:
    """The rendered DOM, waiting out a redirect if one is in flight.

    A page that redirects itself after load — an interstitial, a locale bounce —
    leaves `content()` raising "the page is navigating" until the new document
    commits. Reading a moment later gets the page that redirect was heading for.
    """
    for attempt in range(3):
        try:
            return page.content()
        except PlaywrightError as exc:
            if "navigating" not in exc.message or attempt == 2:
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeout:
                page.wait_for_timeout(1000)
    return page.content()


def fetch_one(context: Any, url: str, fmt: str, timeout_ms: float) -> Dict[str, Any]:
    page = context.new_page()
    try:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeout:
            return row(url, error=f"navigation timed out after {timeout_ms / 1000:.0f}s")
        except PlaywrightError as exc:
            return row(url, error=f"navigation failed: {exc.message.splitlines()[0]}")

        status = response.status if response is not None else 0
        content_type = ""
        if response is not None:
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()

        if status >= 400:
            return row(url, status=status, content_type=content_type, error=f"HTTP {status}")

        # A browser cannot read a PDF into text. Say so rather than returning
        # the viewer's chrome as if it were the document.
        if content_type and "html" not in content_type and "xml" not in content_type:
            return row(url, status=status, content_type=content_type,
                       error=f"not an HTML page ({content_type})")

        try:
            page.wait_for_load_state("networkidle", timeout=IDLE_BUDGET_MS)
        except PlaywrightTimeout:
            pass  # rendered enough; the DOM is what matters, not a quiet network

        html = read_content(page)
        title, text = extract_html(html, url, fmt)
        return row(url, status=status, content_type=content_type or "text/html",
                   title=title or (page.title() or ""), text=text,
                   source_bytes=len(html.encode("utf-8")))
    except Exception as exc:  # noqa: BLE001 — one bad page must not kill the batch
        return row(url, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001 — the page may already be gone
            pass


def main() -> None:
    request = json.load(sys.stdin)
    urls: List[str] = list(request.get("urls") or [])
    timeout_ms = float(request.get("timeout") or 30.0) * 1000
    fmt = str(request.get("format") or "markdown")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                headless=True,
                # WSL and containers often lack the kernel namespaces Chromium's
                # sandbox needs. Hermes' own browser tool drops it the same way.
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except PlaywrightError as exc:
            message = f"Chromium failed to start: {exc.message.splitlines()[0]}"
            json.dump({"results": [row(u, error=message) for u in urls]},
                      sys.stdout, ensure_ascii=False)
            return

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-HK",
            extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
            viewport={"width": 1366, "height": 900},
        )
        try:
            results = [fetch_one(context, url, fmt, timeout_ms) for url in urls]
        finally:
            context.close()
            browser.close()

    json.dump({"results": results}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
