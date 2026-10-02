"""Items into stories: which items are one story, and which stories to offer.

Stdlib only, so the eval can replay a collected run through exactly this code.

An item is one row from one source. A story is the set of items about the same
thing. Items join a story through **merge keys**, each exact except the last:

  url      the normalized link
  title    the exact normalized title, three words or more
  artifact an arXiv id, GitHub repo (with release tag) or HuggingFace repo the
           item itself points at
  model    a model name in the title, when the title names exactly one: a
           HuggingFace repo name with its org, size and quantization stripped, or
           failing that a product and dotted version (`Claude Opus 5.5`)
  fuzzy    a close match on the normalized title, four words or more, between
           different publishers, with every version number equal

An artifact an item only *mentions* in its body is not a merge key. A daily
roundup links forty things; merging on those would glue forty stories into one.
A mention adds the roundup's source to the story's witnesses instead.

Corroboration is the number of distinct source families among a story's items
and mentioners. A family is a publisher: the arXiv feeds are one witness, and so
are the Hacker News queries.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

from keys import url_key as feeds_url_key

# --- model names --------------------------------------------------------------

# Stripped from the end of a repo name, repeatedly, so every upload of one
# release reduces to one name: `Qwen3-8B-Instruct-AWQ` and `Qwen3-8B` are
# `qwen3`, `DeepSeek-V4.1-Flash-NVFP4` is `deepseek-v4.1-flash`.
_SUFFIX = re.compile(
    r"-(?:gguf|ggml|mlx|awq|gptq|exl2|exl3|onnx|fp8|fp16|bf16|fp4|nvfp4|mxfp4|int4|int8|"
    r"\d+bit|w4a16|w8a8|q\d(?:_k)?(?:_[msl])?|instruct|chat|it|base|hf|pytorch|jax|"
    r"\d+(?:\.\d+)?[bmk](?:-a\d+(?:\.\d+)?b)?|preview|dynamic|unsloth|i1|imatrix)$"
)
_GENERIC = {"model", "models", "base", "test", "demo", "llm", "chat", "embedding", "reranker"}


def model_name(repo_id: str) -> str | None:
    name = repo_id.split("/")[-1].lower()
    prev = None
    while prev != name:
        prev, name = name, _SUFFIX.sub("", name)
    name = name.strip("-._")
    if name in _GENERIC or len(name) < 5 or not re.search(r"[a-z]", name):
        return None
    if len(name) < 8 and not re.search(r"\d", name):
        return None  # short and no version number: too likely to be a common word
    return name


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9.]+", "-", text.lower())).strip("-")


def names_in_title(title: str, vocabulary: set[str]) -> set[str]:
    s = f"-{_slug(title).replace('.-', '-')}-"
    hits = {n for n in vocabulary if f"-{n}-" in s}
    # `deepseek-v4` inside `deepseek-v4.1-flash` is not a second model
    return {n for n in hits if not any(n != m and n in m for m in hits)}


# A product and its version in a headline — `Claude Opus 5.5`, `Qwen3.8` — for a
# model with no HuggingFace repo to take a name from. The version needs a dot:
# `GPT-6` alone also names a year's worth of stories about GPT-6.
_VERSION = re.compile(r"(?:^|-)(?:([a-z]{2,})-v?(\d+(?:\.\d+)+)|([a-z]{2,}\d+(?:\.\d+)+))(?=-|$)")


def versions_in_title(title: str) -> set[str]:
    s = _slug(title)
    out = set()
    for m in _VERSION.finditer(s):
        out.add(f"{m.group(1)}-{m.group(2)}" if m.group(1) else m.group(3))
    return out


def vocabulary(items: list[dict], known: set[str] = frozenset()) -> set[str]:
    names = set(known)
    for it in items:
        for a in it["primary"] + it["mentioned"]:
            if a.startswith("hf:"):
                n = model_name(a[3:])
                if n:
                    names.add(n)
    return names


# --- merging ------------------------------------------------------------------

class _Union:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def merge_keys(it: dict, names: set[str]) -> list[str]:
    ks = []
    if it.get("url_key"):
        ks.append(f"url:{it['url_key']}")
    # The exact normalized title. For a Google News item it is the only key there
    # is, since its link is opaque — without it the ledger could not recognise a
    # story it has already posted. Short titles are left out: "Spreadsheets" from
    # two publishers is two things.
    if len(it["title_key"].split()) >= 3 or it.get("opaque_url"):
        ks.append(f"title:{it['title_key']}")
    ks.extend(it["primary"])
    if it["kind"] == "hf_models":
        n = model_name(it["url"].split("huggingface.co/")[-1])
        if n:
            ks.append(f"model:{n}")
    else:
        # A title that names two models is a comparison, and merging on either
        # name would join two stories through it.
        found = names_in_title(it["title"], names) or versions_in_title(it["title"])
        if len(found) == 1:
            ks.append(f"model:{found.pop()}")
    return ks


# Titles a person wrote. `org/Model — model release` is assembled by the puller
# and is the same template for every upload, so it would match everything.
_HEADLINE_KINDS = {"feed", "algolia", "gnews", "arxiv_query", "hf"}


def _fuzzy_pairs(items: list[dict], threshold: float = 0.80) -> list[tuple[int, int]]:
    """One story carried by different publishers under different links.

    Two rules keep it from matching a publisher's own template. Items from the
    same family never match: a publisher does not post one story twice, but it
    does write "Sales workflows with ChatGPT Work" and "Finance workflows with
    ChatGPT Work". And every token with a digit in it must agree: "GPT-5.5 System
    Card" and "GPT-5.4 System Card" are 0.95 alike and two different models.
    """
    index: dict[str, list[int]] = defaultdict(list)
    pairs = []
    for i, it in enumerate(items):
        if it["kind"] not in _HEADLINE_KINDS:
            continue
        words = it["title_key"].split()
        if len(words) < 4:
            continue
        digits = {w for w in words if any(c.isdigit() for c in w)}
        toks = {w for w in words if len(w) > 3}
        cands = {j for t in toks for j in index.get(t, [])}
        for j in cands:
            other = items[j]
            if other["family"] == it["family"] or other["url_key"] == it["url_key"]:
                continue
            if {w for w in other["title_key"].split() if any(c.isdigit() for c in w)} != digits:
                continue
            if SequenceMatcher(None, it["title_key"], other["title_key"]).ratio() >= threshold:
                pairs.append((j, i))
        for t in toks:
            index[t].append(i)
    return pairs


def build_stories(items: list[dict], known_names: set[str] = frozenset()) -> list[dict]:
    names = vocabulary(items, known_names)
    uf = _Union(len(items))
    owner: dict[str, int] = {}
    item_keys = []
    for i, it in enumerate(items):
        ks = merge_keys(it, names)
        item_keys.append(ks)
        for k in ks:
            if k in owner:
                uf.union(owner[k], i)
            else:
                owner[k] = i
    for a, b in _fuzzy_pairs(items):
        uf.union(a, b)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(items)):
        groups[uf.find(i)].append(i)

    stories = []
    root_of_key = {}
    for root, members in groups.items():
        ks = sorted({k for i in members for k in item_keys[i]})
        for k in ks:
            root_of_key[k] = root
        stories.append({"_root": root, "members": members, "keys": ks, "mentioned_by": set(),
                        "item_keys": {i: item_keys[i] for i in members}})

    # a mention corroborates the story that owns the artifact, without joining it
    by_root = {s["_root"]: s for s in stories}
    for i, it in enumerate(items):
        for a in it["mentioned"]:
            root = root_of_key.get(a)
            if root is not None and root != uf.find(i):
                by_root[root]["mentioned_by"].add(i)

    return [_finish(s, items) for s in stories]


# An arXiv abstract runs about 1,100 characters and a release note 1,500; at 700
# most of an abstract reaches the model without a fetch, and 30 stories stay
# near 25k characters.
SUMMARY = 700


# Which item's title a story leads with: the lab's own headline (Google News
# restricted to its site) over a write-up, a write-up over a forum post, and any
# headline a person wrote over `org/Model — model release`.
_LEAD = {"gnews": 0, "feed": 1, "algolia": 2, "arxiv_query": 3, "hf": 3, "hf_models": 4}


def _finish(s: dict, items: list[dict]) -> dict:
    members = [items[i] for i in s["members"]]
    mentioners = [items[i] for i in s["mentioned_by"]]
    lead = sorted(members, key=lambda it: (_LEAD.get(it["kind"], 5), it["published"] or ""))
    families = {it["family"] for it in members} | {it["family"] for it in mentioners}
    buckets = defaultdict(int)
    for it in members:
        buckets[it["bucket"]] += 1
    # A Google News redirect goes last: web_extract can read it only through
    # the browser layer, which is slow, so any direct link is tried first.
    urls = []
    for it in sorted(lead + mentioners, key=lambda it: it["opaque_url"]):
        if it["url"] not in urls:
            urls.append(it["url"])
    # Everything that identifies this story in a finished post: any member's link,
    # opaque ones included, and any artifact a member points at.
    match = sorted({f"url:{it['url_key'] or feeds_url_key(it['url'])}" for it in members}
                   | {a for it in members for a in it["primary"]})
    # Each item on its own, lead first: what a finished post is matched against,
    # so the ledger records the items the post cited and not the ones merged
    # beside them.
    order = sorted(s["members"], key=lambda i: (_LEAD.get(items[i]["kind"], 5), items[i]["published"] or ""))
    parts = [{"title": items[i]["title"],
              "match": sorted({f"url:{items[i]['url_key'] or feeds_url_key(items[i]['url'])}"} | set(items[i]["primary"])),
              "keys": s["item_keys"][i]} for i in order]
    published = min((it["published"] for it in members if it["published"]), default=None)
    ident = hashlib.sha1(min(s["keys"] or [lead[0]["title_key"]]).encode()).hexdigest()[:10]
    return {
        "id": ident,
        "title": lead[0]["title"],
        "bucket": max(buckets, key=lambda b: (buckets[b], b)),
        "published": published,
        # The summary is what relevance is scored on and what the model reads, so
        # it is the fullest one a member has. A Google News row says only who
        # published it and an upload row only its download count; a write-up
        # says more than a forum post.
        "summary": max((it["summary"] for it in members if it.get("summary") and it["kind"] not in ("gnews", "hf_models")),
                       key=len, default=next((it["summary"] for it in lead if it.get("summary")), ""))[:SUMMARY],
        "urls": urls[:4],
        "link": urls[0] if urls else "",
        "match": match,
        "parts": parts,
        "sources": sorted({it["source"] for it in members}),
        "mentioned_by": sorted({it["source"] for it in mentioners}),
        "corroboration": len(families),
        "keys": s["keys"],
        "items": len(members),
        # for news_read: whose text to keep, in the order to read it
        "_readers": lead + mentioners,
        "_members": {id(it) for it in members},
    }


# --- window and selection -----------------------------------------------------

def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def in_window(it: dict, since: datetime, now: datetime) -> bool:
    t = parse_time(it["published"])
    return t is None or since < min(t, now) <= now


def select(stories: list[dict], limit: int) -> list[dict]:
    """Best first within each bucket, then take buckets in turn.

    A bucket is a routing label, not a quota, but ranking every bucket on one list
    lets the loud ones take every slot: a model release is carried by five
    sources, a text-to-SQL paper by one.
    """
    def score(s):
        rel = s.get("relevance")
        return (s["corroboration"], rel if rel is not None else 0.0, s["published"] or "")

    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for s in sorted(stories, key=score, reverse=True):
        by_bucket[s["bucket"]].append(s)
    out: list[dict] = []
    queues = [q for _, q in sorted(by_bucket.items())]
    while len(out) < limit and any(queues):
        for q in queues:
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out
