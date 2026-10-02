"""Turning fetched bytes into readable text. Shared by every layer's worker.

The layers differ in how they get a page — a plain GET, a rendered browser — and
not in what counts as its text. Keeping extraction in one place is what makes a
layer's result comparable to the layer before it.

Each worker declares these dependencies in its own PEP 723 header; this module
is imported from the worker's own directory, so it carries no header itself.
"""

from __future__ import annotations

import io
from typing import Any, Dict, Tuple

# A page served to a script's default User-Agent is not always the page served to a person.
# Naming a real browser costs nothing and moves a handful of sites from the bot
# wall into layer 1.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "zh-HK,zh;q=0.9,en;q=0.8,ja;q=0.7"

MAX_BYTES = 10 * 1024 * 1024

# An image's alt text is often the only place a value appears. The Hong Kong
# Observatory's rainfall legend is a table whose middle column is nothing but
# icons: drop the images and five rows read as a label, a blank, and a sentence
# about what the blank meant.
#
# Getting that text out costs a second extraction pass. Anything that changes the
# markup — trafilatura's own `include_images`, or rewriting each image into a span
# — also changes which block trafilatura picks as the main content, and it does
# not always pick better: `include_images` cut news.rthk.hk from 69 headlines to
# 17, and inlining spans cut thestandard.com.hk from 7,034 characters to 473.
#
# So the page is extracted both ways and the longer result wins. Length is the
# same signal the quality gate runs on, and the two readings differ only in where
# the alt text went, so the shorter one is the pass that lost a block.


def row(url: str, **fields: Any) -> Dict[str, Any]:
    """One result, with every field the provider expects always present."""
    base = {
        "url": url, "status": 0, "content_type": "", "title": "", "text": "",
        "source_bytes": 0, "error": "",
    }
    base.update(fields)
    return base


def inline_image_alt(html: str) -> str:
    """Rewrite every `<img>` into a span holding its alt text; drop the rest.

    An image with no alt text carries nothing a reader can use, so it goes.
    """
    from lxml import html as lxml_html

    try:
        tree = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001 — unparseable markup extracts as it is
        return html

    for img in list(tree.iter("img")):
        alt = (img.get("alt") or "").strip()
        parent = img.getparent()
        if parent is None:
            continue
        if not alt:
            parent.remove(img)
            continue
        img.tag = "span"
        tail = img.tail
        img.attrib.clear()
        img.text = alt
        img.tail = tail

    return lxml_html.tostring(tree, encoding="unicode")


def extract_html(html: str, url: str, fmt: str) -> Tuple[str, str]:
    """Return (title, text) for an HTML document."""
    import trafilatura

    settings = dict(
        url=url,
        output_format="markdown" if fmt == "markdown" else "txt",
        include_comments=False,
        include_tables=True,
        with_metadata=False,
    )
    plain = trafilatura.extract(html, **settings) or ""
    with_alt = trafilatura.extract(inline_image_alt(html), **settings) or ""
    text = with_alt if len(with_alt) > len(plain) else plain

    title = ""
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta is not None:
            title = (meta.title or "").strip()
    except Exception:  # noqa: BLE001 — a missing title never fails a fetch
        title = ""
    return title, text


def extract_pdf(body: bytes) -> Tuple[str, str]:
    """Return (title, text) for a PDF.

    `trafilatura` is an HTML extractor. Without this branch a PDF looks like a
    page with no text, the quality gate fails it, and the cascade opens a browser
    that cannot read it either.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    title = ""
    try:
        title = ((reader.metadata or {}).get("/Title") or "").strip()
    except Exception:  # noqa: BLE001 — metadata is optional in the format
        title = ""
    pages = [(page.extract_text() or "") for page in reader.pages]
    return title, "\n\n".join(p for p in pages if p.strip())
