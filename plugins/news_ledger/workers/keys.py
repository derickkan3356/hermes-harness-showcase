"""The keys an item is recognised by, and the keys a finished post is matched on.

Stdlib only, so `stories.py` and the eval's replay can use them without the
pullers' dependencies.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STOP = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "is",
    "are", "at", "by", "from", "how", "why", "what", "new", "using", "via",
}


def url_key(url: str) -> str:
    try:
        s = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = s.netloc.lower().removeprefix("www.").removeprefix("amp.")
    path = re.sub(r"/amp/?$", "", s.path).rstrip("/") or "/"
    q = [(k, v) for k, v in parse_qsl(s.query) if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))]
    return urlunsplit(("", host, path, urlencode(sorted(q)), ""))


def title_key(title: str) -> str:
    t = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return " ".join(w for w in t.split() if w not in STOP)


# A story in this domain is usually about a named thing — a paper, a repo, a
# model — and each has an exact identifier in a link. A GitHub release link keeps
# its tag, because `vllm-project/vllm` is linked every day for reasons that have
# nothing to do with any one release.
_ARXIV = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/)(\d{4}\.\d{4,5})")
_GITHUB = re.compile(r"github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?(?:/releases/tag/([\w.+-]+))?(?=[/\"'\s<>?#)]|$)")
_HF = re.compile(r"huggingface\.co/(?!papers/|blog/|spaces/|datasets/|docs/|api/|models\b)([\w-]+/[\w.-]+?)(?=[/\"'\s<>?#)]|$)")


def artifacts(text: str) -> set[str]:
    found: set[str] = set()
    for m in _ARXIV.finditer(text or ""):
        found.add(f"arxiv:{m.group(1)}")
    for m in _GITHUB.finditer(text or ""):
        repo = m.group(1).lower().rstrip(".")
        found.add(f"github:{repo}@{m.group(2).lower()}" if m.group(2) else f"github:{repo}")
    for m in _HF.finditer(text or ""):
        found.add(f"hf:{m.group(1).lower().rstrip('.')}")
    return found


def keys(url: str, body: str) -> tuple[list[str], list[str]]:
    """(primary, mentioned) artifacts.

    Primary is what the item itself points at. Mentioned is what its body links
    to — a newsletter that links forty things is about none of them in
    particular, so a mention counts as corroboration and never merges stories.
    """
    primary = artifacts(url)
    mentioned = artifacts(body) - primary
    return sorted(primary), sorted(mentioned)




_URL = re.compile(r"https?://[^\s)\]>\"'<]+")


def keys_in_text(text: str) -> set[str]:
    """Every link and artifact in a finished post, as match keys."""
    found = {f"url:{url_key(u.rstrip('.,;'))}" for u in _URL.findall(text or "")}
    return found | artifacts(text)
