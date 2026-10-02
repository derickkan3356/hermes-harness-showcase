# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "curl_cffi>=0.7",
#     "trafilatura>=1.12",
#     "pypdf>=5.0",
# ]
# ///
"""Layer 1 of the fetch cascade: one HTTP request, then main-text extraction.

Runs as its own process under ``uv run --script`` so that `trafilatura` and
`pypdf` never enter Hermes' venv, which `hermes update` owns. uv builds the
environment from the header above on first use and caches it; `fetch_http.py.lock`
pins it. Everything here is stdlib-plus-those-three so a repo clone can rebuild it.

Protocol: one JSON object on stdin, one on stdout.

    in   {"urls": [str | {"url", "method", "headers", "body"}], "timeout": float,
          "format": "markdown"|"html"|"raw", "concurrency": int}
    out  {"results": [{"url", "status", "content_type", "title", "text",
                       "source_bytes", "error"}]}

A URL is a GET. An object is one request: `method` (default GET), `headers`
added to the session's, and `body` sent as is — a string, or a JSON value that
is serialized. It is for a site whose data sits behind a POST API; which headers
and body that site wants is the caller's knowledge, not this layer's.

`format: "raw"` is for code that parses the bytes itself — a feed, a JSON API.
It returns after the status check, with the body base64-encoded in an extra
`body` field, and extracts nothing, so any content type is accepted. A raw body
over `MAX_BYTES` is refused, not cut: a truncated feed parses to fewer items and
no error. A raw row also carries the response `headers`, success or not, so a
caller can read `Retry-After` on a 429. The quality gate and the browser layer judge page text, so a raw
result never goes through them; the caller runs this worker directly.

`concurrency` fetches that many URLs at once (default 1). Pacing a host that
asks for it is the caller's job: this worker does not know that arXiv wants one
call every three seconds.

The caller decides whether a result is good enough (`provider.py` holds the
quality gate). This script reports what happened and never raises: a URL that
fails comes back as a row with `error` set, so the result list always lines up
with the input list.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple, Union

from curl_cffi import requests as curl_requests
from curl_cffi.curl import CurlError

from extract_common import (
    ACCEPT_LANGUAGE,
    MAX_BYTES,
    USER_AGENT,
    extract_html,
    extract_pdf,
    row,
)

# libcurl, not a Python HTTP client: Fastly in front of arXiv answers 406 Not
# Acceptable to Python's TLS handshake and 200 to curl's, with identical
# headers, query and IP. `curl_cffi` is libcurl, so the handshake is curl's.
PAGE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7"
RAW_ACCEPT = "*/*"

_local = threading.local()


def session(fmt: str, timeout: float) -> curl_requests.Session:
    """One session per thread: a curl handle is not safe to share between threads."""
    if getattr(_local, "session", None) is None:
        _local.session = curl_requests.Session(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": RAW_ACCEPT if fmt == "raw" else PAGE_ACCEPT,
                "Accept-Language": ACCEPT_LANGUAGE,
            },
            allow_redirects=True,
            # HTTP/2 through curl_cffi stalls after the first 1 MiB on some
            # servers — budget.gov.hk's PDFs never finish. HTTP/1.1 does not,
            # and still gets past arXiv's Fastly rule.
            http_version="v1",
            # (connect, read): the read timeout is the longest gap between bytes,
            # not the whole transfer, so a large PDF on a slow server still lands.
            timeout=(timeout, timeout),
        )
    return _local.session


def read_body(response: Any) -> Tuple[bytes, bool]:
    """The body, stopped at `MAX_BYTES`, and whether there was more."""
    chunks: List[bytes] = []
    size = 0
    for chunk in response.iter_content():
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BYTES:
            return b"".join(chunks)[:MAX_BYTES], True
    return b"".join(chunks), False


def curl_error(exc: Exception) -> str:
    # libcurl appends a pointer to its error-code page to every message.
    message = str(exc).split(" See https://curl.se/")[0]
    return f"{type(exc).__name__}: {message}"


def fetch_one(spec: Union[str, Dict[str, Any]], fmt: str, timeout: float) -> Dict[str, Any]:
    if isinstance(spec, str):
        spec = {"url": spec}
    url = str(spec.get("url") or "")
    method = str(spec.get("method") or "GET").upper()
    body = spec.get("body")
    if body is not None and not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False)
    try:
        response = session(fmt, timeout).request(
            method, url, headers=spec.get("headers") or None,
            data=body.encode("utf-8") if body is not None else None, stream=True)
    except CurlError as exc:
        return row(url, error=curl_error(exc))

    headers = {k.lower(): v for k, v in response.headers.items()} if fmt == "raw" else None
    try:
        status = response.status_code
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if status >= 400:
            failed = row(url, status=status, content_type=content_type, error=f"HTTP {status}")
            if headers is not None:
                failed["headers"] = headers
            return failed
        try:
            data, cut = read_body(response)
        except CurlError as exc:
            return row(url, status=status, content_type=content_type, error=curl_error(exc))
    finally:
        response.close()

    if fmt == "raw":
        if cut:
            return row(url, status=status, content_type=content_type,
                       error=f"body over {MAX_BYTES} bytes, refused rather than cut")
        result = row(url, status=status, content_type=content_type, source_bytes=len(data))
        result["body"] = base64.b64encode(data).decode("ascii")
        result["headers"] = headers
        return result

    # Servers lie about content-type often enough that the PDF magic number is
    # the more reliable test.
    is_pdf = content_type == "application/pdf" or data[:5] == b"%PDF-"
    is_html = content_type in ("", "text/html", "application/xhtml+xml", "application/xml", "text/xml")
    is_plain = content_type.startswith("text/") and not is_html

    try:
        if is_pdf:
            title, text = extract_pdf(data)
        elif is_html:
            title, text = extract_html(data.decode("utf-8", errors="replace"), url, fmt)
        elif is_plain:
            title, text = "", data.decode("utf-8", errors="replace")
        else:
            return row(url, status=status, content_type=content_type,
                       error=f"unsupported content type {content_type!r}")
    except Exception as exc:  # noqa: BLE001 — one bad page must not kill the batch
        return row(url, status=status, content_type=content_type,
                   error=f"extraction failed: {type(exc).__name__}: {exc}")

    return row(url, status=status, content_type=content_type, title=title, text=text,
               source_bytes=len(data))


def main() -> None:
    request = json.load(sys.stdin)
    urls: List[Union[str, Dict[str, Any]]] = list(request.get("urls") or [])
    timeout = float(request.get("timeout") or 20.0)
    fmt = str(request.get("format") or "markdown")
    concurrency = max(1, int(request.get("concurrency") or 1))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda url: fetch_one(url, fmt, timeout), urls))

    json.dump({"results": results}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
