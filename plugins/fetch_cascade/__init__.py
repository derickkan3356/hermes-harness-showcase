"""Local fetch cascade for `web_extract` — canonical copy in the hermes-harness repo.

`~/.hermes/plugins/fetch_cascade` is a symlink to this directory. It registers a
web provider named `local-cascade`; `web.extract_backend: local-cascade` in
config.yaml is what routes `web_extract` calls to it. It registers no tool, so it
needs no `allow_tool_override`.
"""

from __future__ import annotations

from .provider import LocalCascadeProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(LocalCascadeProvider())
