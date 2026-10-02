"""CV per job — canonical copy in the hermes-harness repo.

`~/.hermes/plugins/cv` is a symlink to this directory. It registers `cv_master`,
`cv_write`, `cv_edit` and `cv_render` in the `cv` toolset, which
`platform_toolsets.api_server` lists so an Open WebUI chat can reach it. The
master is `cv/master.yaml` in this repo; the draft is one per chat; the files go
to `~/.hermes/outbox/<session id>/`, where the `hermes_session` Pipe attaches
them to the reply. No tool takes a path or a URL.
"""

from __future__ import annotations

from .tools import (
    CV_EDIT_SCHEMA,
    CV_MASTER_SCHEMA,
    CV_RENDER_SCHEMA,
    CV_WRITE_SCHEMA,
    handle_cv_edit,
    handle_cv_master,
    handle_cv_render,
    handle_cv_write,
    worker_available,
)


def register(ctx) -> None:
    ctx.register_tool(name="cv_master", toolset="cv", schema=CV_MASTER_SCHEMA,
                      handler=handle_cv_master, check_fn=worker_available, emoji="🗂️")
    ctx.register_tool(name="cv_write", toolset="cv", schema=CV_WRITE_SCHEMA,
                      handler=handle_cv_write, check_fn=worker_available, emoji="✍️")
    ctx.register_tool(name="cv_edit", toolset="cv", schema=CV_EDIT_SCHEMA,
                      handler=handle_cv_edit, check_fn=worker_available, emoji="✏️")
    ctx.register_tool(name="cv_render", toolset="cv", schema=CV_RENDER_SCHEMA,
                      handler=handle_cv_render, check_fn=worker_available, emoji="📄")
