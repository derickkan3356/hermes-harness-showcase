"""Job ledger for the Hong Kong job digest — canonical copy in the hermes-harness repo.

`~/.hermes/plugins/job_ledger` is a symlink to this directory. It registers
`job_candidates`, `job_read` and `job_feedback` in the `jobs` toolset, which
`platform_toolsets.api_server` lists so the Open WebUI Automation that runs the
digest can reach it, and a `post_llm_call` hook that records what the finished
digest showed and posted. No tool takes a URL: what is fetched is the queries in
`profile.yaml`, so text in an ad cannot steer it.
"""

from __future__ import annotations

from .tools import (
    JOB_CANDIDATES_SCHEMA,
    JOB_FEEDBACK_SCHEMA,
    JOB_READ_SCHEMA,
    handle_job_candidates,
    handle_job_feedback,
    handle_job_read,
    on_post_llm_call,
    worker_available,
)


def register(ctx) -> None:
    ctx.register_tool(name="job_candidates", toolset="jobs", schema=JOB_CANDIDATES_SCHEMA,
                      handler=handle_job_candidates, check_fn=worker_available, emoji="💼")
    ctx.register_tool(name="job_read", toolset="jobs", schema=JOB_READ_SCHEMA,
                      handler=handle_job_read, check_fn=worker_available, emoji="📄")
    ctx.register_tool(name="job_feedback", toolset="jobs", schema=JOB_FEEDBACK_SCHEMA,
                      handler=handle_job_feedback, check_fn=worker_available, emoji="📝")
    ctx.register_hook("post_llm_call", on_post_llm_call)
