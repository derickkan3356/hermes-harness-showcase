"""Region-aware `web_search` — canonical copy in the hermes-harness repo.

``~/.hermes/plugins/search_region`` is a symlink to this directory. It replaces
the built-in ``web_search`` tool, which needs
``plugins.entries.search-region.allow_tool_override: true`` in config.yaml.
The search backend itself stays upstream (``web.search_backend``).
"""

from __future__ import annotations

from .tool import WEB_SEARCH_SCHEMA, handle_web_search


def register(ctx) -> None:
    """Replace the built-in web_search with the region-aware wrapper."""
    from tools.web_tools import _web_requires_env, check_web_api_key

    ctx.register_tool(
        name="web_search",
        toolset="web",
        schema=WEB_SEARCH_SCHEMA,
        handler=lambda args, **kw: handle_web_search(args, **kw),
        check_fn=check_web_api_key,
        requires_env=_web_requires_env(),
        emoji="🔍",
        override=True,
    )
