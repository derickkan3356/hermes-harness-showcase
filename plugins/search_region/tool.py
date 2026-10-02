"""The `web_search` the agent sees: a region it can aim, and no silent empty answer.

This replaces Hermes' built-in `web_search` tool. Everything underneath it stays
upstream — the handler calls `tools.web_tools.web_search_tool()`, which is what
picks the backend from `web.search_backend`, runs the search cache and the
single-flight lock, and reports a dead backend. Two things are added around it:

1. `region` (from the model, per call) prepends the region's place name to the
   query text. `docs/web-search.md` has the numbers: it takes ambiguous Hong
   Kong queries from 1/8 to 7/8. It is a per-call parameter and not config
   because one pinned region answers the wrong place for questions about
   somewhere else.
2. A failed or empty search is rewritten into an error that tells the model what
   to do about it. An empty result list becomes `success: false` — backends
   return `{"success": true, "data": {"web": []}}` when they find nothing, which
   reads as a finished search. A backend error keeps its own text and gains the
   instruction.

   The instruction is the part that matters. With a dead Brave key the tool
   already answered `{"success": false, "error": "Brave Search returned HTTP
   422"}`, and the 27B called the tool, read that, and then answered the question
   from memory with a source URL it had never opened and no mention that the
   search had failed. A tool that fails loudly is not enough on its own: the
   model has to be told, in the result it is reading, that answering anyway is
   the wrong move.

   Both checks sit here rather than in a backend so they hold whatever
   `web.search_backend` is set to.

`web_search_tool` is imported directly, so calling it does not route through the
registry entry this tool replaced.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

# The place name is written in the region's own script: an English name on a
# Chinese query reads as two languages to the index.
PLACES: Dict[str, str] = {
    "hk": "香港",
    "cn": "中国",
    "jp": "日本",
}

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": (
        "Search the web for information. Returns up to 5 results by default "
        "with titles, URLs, and descriptions. The query is passed through to "
        "the configured backend, so operators such as site:domain, "
        "filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when "
        "the backend supports them. Returns an error when the search finds "
        "nothing — report that instead of treating it as an empty answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The search query to look up on the web. You may include "
                    "backend-supported operators such as site:example.com, "
                    "filetype:pdf, intitle:word, -term, or \"exact phrase\"."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5,
            },
            "region": {
                "type": "string",
                "enum": ["hk", "cn", "jp"],
                "description": (
                    "The place the question is about: hk (Hong Kong), cn "
                    "(mainland China), jp (Japan). Aims the search at that "
                    "place's sites, which matters most when the query text "
                    "names no place — 'weather forecast' with region hk "
                    "returns the Hong Kong Observatory instead of US sites. "
                    "Set it to the place actually asked about, and leave it "
                    "out when the question is not about one place: a region "
                    "that disagrees with the query answers for the wrong place."
                ),
            },
        },
        "required": ["query"],
    },
}


def _audit(record: Dict[str, Any]) -> None:
    """Append one call to `$SEARCH_REGION_LOG`, when that path is set.

    How often the model sets `region`, and whether it sets the right one, is
    only answerable from real calls. The outcome is recorded too: a search engine
    that has stopped working is visible here and nowhere else the user looks,
    because the model does not reliably say so. `tools/facts.sh web` reads it.
    Off unless the env var is set.
    """
    path = os.environ.get("SEARCH_REGION_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%F %T"), **record},
                                ensure_ascii=False) + "\n")
    except OSError as exc:  # noqa: BLE001 — never fail a search over the audit
        logger.warning("SEARCH_REGION_LOG write failed: %s", exc)


# Appended to every failed search. It tells the model what to do, not what to
# say: asked to do something it acts, asked to relay something about the tools it
# does not. A version of this text that led with "your reply must say the search
# failed" stopped the model fetching at all and sent it back to answering from
# memory — the demand about wording crowded out the one about evidence. Whether
# the user hears that search is down is handled by `tools/facts.sh web`, which
# does not depend on the model.
REPORT_INSTRUCTION = (
    "No results were retrieved, so nothing here has been verified against the "
    "web. Tell the user that the search failed and what you were looking for. "
    "Do not answer from memory as though the search had succeeded, and do not "
    "cite a source you have not opened."
)


def handle_web_search(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Run one search: rewrite for the region, then fail loudly on nothing."""
    from tools.web_tools import web_search_tool

    query = str(args.get("query") or "")
    limit = args.get("limit", 5)
    region = str(args.get("region") or "").strip().lower()

    place = PLACES.get(region)
    sent_query = query
    if place and place not in query:
        sent_query = f"{place} {query}"
        logger.info("web_search region=%s: %r -> %r", region, query, sent_query)

    raw = web_search_tool(sent_query, limit)
    record = {"query": query, "region": region or None, "sent": sent_query}

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        # Upstream returned something this wrapper does not understand. Pass it
        # through rather than inventing a shape the agent has never seen.
        _audit({**record, "outcome": "unreadable"})
        return raw

    if payload.get("success") and not (payload.get("data") or {}).get("web"):
        logger.info("web_search '%s': 0 results", sent_query)
        _audit({**record, "outcome": "empty"})
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"The search for {sent_query!r} found nothing. The backend "
                    "answered normally — the query matched no pages. Try fewer "
                    "or different words, or drop any quoted phrase. "
                    + REPORT_INSTRUCTION
                ),
            },
            ensure_ascii=False,
        )

    if not payload.get("success"):
        upstream = str(payload.get("error") or "the search backend failed")
        logger.info("web_search '%s' failed: %s", sent_query, upstream)
        _audit({**record, "outcome": "failed", "backend_error": upstream})
        return json.dumps(
            {
                "success": False,
                "error": f"Web search is unavailable: {upstream} "
                         + REPORT_INSTRUCTION,
            },
            ensure_ascii=False,
        )

    _audit({**record, "outcome": "ok",
            "results": len((payload.get("data") or {}).get("web") or [])})
    return raw
