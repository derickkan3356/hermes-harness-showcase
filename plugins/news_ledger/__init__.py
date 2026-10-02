"""News ledger for the AI news digest — canonical copy in the hermes-harness repo.

`~/.hermes/plugins/news_ledger` is a symlink to this directory. It registers
`news_candidates` and `news_read` in the `news` toolset, which `platform_toolsets.api_server`
lists so the Open WebUI Automation that runs the digest can reach it, and a
`post_llm_call` hook that records what the finished digest posted. Neither tool
takes a URL: the sources are the table in `sources.yaml`, so text on a page
cannot steer what gets fetched.
"""

from __future__ import annotations

from .tools import (
    NEWS_CANDIDATES_SCHEMA,
    NEWS_READ_SCHEMA,
    handle_news_candidates,
    handle_news_read,
    on_post_llm_call,
    worker_available,
)


def register(ctx) -> None:
    ctx.register_tool(
        name="news_candidates",
        toolset="news",
        schema=NEWS_CANDIDATES_SCHEMA,
        handler=handle_news_candidates,
        check_fn=worker_available,
        emoji="📰",
    )
    ctx.register_tool(
        name="news_read",
        toolset="news",
        schema=NEWS_READ_SCHEMA,
        handler=handle_news_read,
        check_fn=worker_available,
        emoji="📖",
    )
    ctx.register_hook("post_llm_call", on_post_llm_call)
