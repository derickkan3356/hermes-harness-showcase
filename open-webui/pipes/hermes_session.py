"""
title: Hermes Agent
author: hermes-harness
version: 0.1.0
license: MIT
description: Routes a chat to the Hermes API server with a per-chat session id, so each turn sees the previous turns' tool calls and tool results.
"""

# `tools/owui-pipe.sh` pushes this file into Open WebUI, which keeps functions in
# its database.  The file name is the function id, and that id is the model id in
# the chat and in Automations.  Docs: docs/open-webui.md.
#
# Open WebUI sends only the visible conversation, so a follow-up never sees the
# searches behind the last answer.  Hermes keeps them in ~/.hermes/state.db and
# will rebuild the conversation from there — tool calls and results included —
# when the request carries X-Hermes-Session-Id.  A plain Open WebUI connection
# can only send fixed headers, and one fixed session id would merge every chat
# into one transcript; a Pipe can key the header on the chat.

import io
import json
import logging
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

# Sampling parameters are forwarded as they arrive.  Everything else in the
# body is Open WebUI's own bookkeeping (chat_id, features, tool_ids, ...) and
# the API server has no use for it.
_SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
)

# A Hermes session id is interpolated into on-disk artifact names, and the API
# server rejects anything path-shaped. Open WebUI's temporary chats and channels
# carry a prefix ("temporary:", "channel:"), so the id is not always a bare uuid.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Open WebUI loads this file once per function, as module function_<id>.
# The second id is the same source pushed again by tools/owui-pipe.sh.
# Its model id is a gateway model_routes alias (config.yaml).
_OBLITERATED_FUNCTION = "hermes_session_obliterated"
_OBLITERATED_MODEL = "qwen3.8-27b-obliterated"

# Background tasks (title, tags, follow-ups) call this pipe, then go straight to
# LM Studio with the same weights the chat uses. They do not go through Hermes:
# a title is not an agent turn. The ids are what LM Studio has loaded.
_WEIGHTS = {
    "hermes_session": "qwen/qwen3.8-27b",
    "hermes_session_obliterated": _OBLITERATED_MODEL,
}

# Hermes streams its tool-progress markers as a custom SSE event.  The chat
# does not render them, and their payload is not a chat completion chunk.
_PROGRESS_EVENT = "event: hermes.tool.progress"

log = logging.getLogger(__name__)


class Pipe:
    class Valves(BaseModel):
        base_url: str = Field(
            default=os.getenv("HERMES_API_BASE_URL", "http://127.0.0.1:8642/v1"),
            description="Hermes API server, up to and including /v1.",
        )
        api_key: str = Field(
            default=os.getenv("HERMES_API_SERVER_KEY", ""),
            description="API_SERVER_KEY from ~/.hermes/.env. Session continuation is 403 without it.",
        )
        model: str = Field(
            default="hermes-agent",
            description="Model id the API server advertises. Any other value is read as a model override.",
        )
        session_prefix: str = Field(
            default="owui-",
            description="Hermes session id is this plus the Open WebUI chat id.",
        )
        connect_timeout: float = Field(
            default=15.0,
            description="Seconds to connect. A turn itself is not timed out: tool calls take minutes.",
        )
        lmstudio_url: str = Field(
            default=os.getenv("LMSTUDIO_BASE_URL", "http://100.64.0.2:1234/v1"),
            description="LM Studio, up to and including /v1. Background tasks go here, not through Hermes.",
        )
        outbox_dir: str = Field(
            default=os.path.expanduser("~/.hermes/outbox"),
            description="Where Hermes tools leave files for a session (outbox/<session id>/). Attached after the turn.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _session_id(self, chat_id: Optional[str], task: Optional[str]) -> Optional[str]:
        """The Hermes session for this chat, or None to stay stateless.

        A background task (title, tags) is not part of the conversation and
        must not write into the chat's transcript. A temporary chat gets a
        session like any other: Open WebUI does not keep it, Hermes records
        every turn either way, and the follow-up is the point.
        """
        if task or not chat_id:
            return None
        return f"{self.valves.session_prefix}{_UNSAFE.sub('-', chat_id)}"

    def _function_id(self) -> str:
        return type(self).__module__.removeprefix("function_")

    def _model(self) -> str:
        if self._function_id() == _OBLITERATED_FUNCTION:
            return _OBLITERATED_MODEL
        return self.valves.model

    def _weights(self) -> str:
        return _WEIGHTS.get(self._function_id(), _WEIGHTS["hermes_session"])

    def _payload(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self._model(),
            "messages": body.get("messages", []),
            "stream": bool(body.get("stream", False)),
        }
        for key in _SAMPLING_KEYS:
            if body.get(key) is not None:
                payload[key] = body[key]
        return payload

    def _headers(self, session_id: Optional[str]) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.valves.api_key:
            headers["Authorization"] = f"Bearer {self.valves.api_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        return headers

    async def pipe(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __task__: Optional[str] = None,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Any = None,
    ):
        session_id = self._session_id(__chat_id__, __task__)
        payload = self._payload(body)
        headers = self._headers(session_id)
        base = self.valves.base_url
        if __task__:
            # Same weights as this chat. A fixed task model would load the other
            # 27B beside it.
            payload["model"] = self._weights()
            headers = {"Content-Type": "application/json"}
            base = self.valves.lmstudio_url
        url = f"{base.rstrip('/')}/chat/completions"
        timeout = httpx.Timeout(
            connect=self.valves.connect_timeout, read=None, write=60.0, pool=10.0
        )

        deliver = None
        if session_id and __user__ and __request__ is not None:
            async def deliver() -> str:
                return await self._deliver(session_id, __user__, __request__, __metadata__ or {}, __event_emitter__)

        if payload["stream"]:
            return self._stream(url, payload, headers, timeout, deliver)
        return await self._complete(url, payload, headers, timeout, deliver)

    async def _complete(self, url, payload, headers, timeout, deliver=None) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code != 200:
                raise Exception(f"{url} {response.status_code}: {response.text[:500]}")
            result = response.json()
        if deliver:
            links = await deliver()
            if links and result.get("choices"):
                message = result["choices"][0].setdefault("message", {})
                message["content"] = (message.get("content") or "") + links
        return result

    async def _deliver(self, session_id: str, user: dict, request: Any, metadata: dict, emit: Any) -> str:
        """Attach the files a tool left in outbox/<session id>/ to this reply.

        Each file is uploaded into Open WebUI's file store (no text extraction),
        shown on the message through a `files` event, and moved to `sent/` so
        the next turn does not attach it again. Returns markdown download links,
        because the preview's own download link is easy to miss on a phone.
        """
        box = Path(self.valves.outbox_dir) / session_id
        files = sorted(p for p in box.glob("*") if p.is_file()) if box.is_dir() else []
        if not files:
            return ""
        from fastapi import UploadFile
        from open_webui.models.chats import Chats
        from open_webui.models.users import Users
        from open_webui.routers.files import upload_file_handler

        owner = await Users.get_user_by_id(user["id"])
        sent_dir = box / "sent"
        sent_dir.mkdir(exist_ok=True)
        attached: List[Dict[str, Any]] = []
        for path in files:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                item = await upload_file_handler(
                    request,
                    file=UploadFile(file=io.BytesIO(path.read_bytes()), filename=path.name,
                                    headers={"content-type": ctype}),
                    metadata={},
                    process=False,
                    user=owner,
                )
            except Exception as exc:
                log.error("[hermes_session] upload of %s failed: %s", path, exc)
                continue
            shutil.move(str(path), sent_dir / path.name)
            attached.append({"type": "file", "id": item.id, "url": item.id, "name": path.name,
                             "size": (item.meta or {}).get("size"), "meta": {"content_type": ctype}})
            log.info("[hermes_session] attached %s as %s", path.name, item.id)
        if not attached:
            return ""
        chat_id, message_id = metadata.get("chat_id"), metadata.get("message_id")
        if chat_id and message_id:
            try:
                await Chats.insert_chat_files(chat_id=chat_id, message_id=message_id,
                                              file_ids=[f["id"] for f in attached], user_id=owner.id)
            except Exception as exc:
                log.warning("[hermes_session] linking files to chat %s failed: %s", chat_id, exc)
        if emit:
            await emit({"type": "files", "data": {"files": attached}})
        return "\n\n" + "\n".join(
            f"- [{f['name']}](/api/v1/files/{f['id']}/content?attachment=true)" for f in attached
        )

    async def _stream(self, url, payload, headers, timeout, deliver=None) -> AsyncGenerator[str, None]:
        """Forward the API server's SSE stream line by line.

        Closing this generator closes the HTTP connection, which is how Stop
        reaches Hermes: the API server reads the disconnect and interrupts the
        agent.  Open WebUI closes it on cancel (`process_chat_response`).
        """
        client = httpx.AsyncClient(timeout=timeout)
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", "replace")
                    raise Exception(f"{url} {response.status_code}: {detail[:500]}")
                skip_next_data = False
                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        skip_next_data = line.strip() == _PROGRESS_EVENT
                        continue
                    if not line.startswith("data:"):
                        continue
                    if skip_next_data:
                        skip_next_data = False
                        continue
                    if line.strip() == "data: [DONE]":
                        # Open WebUI appends its own terminator.
                        break
                    yield line
            if deliver:
                links = await deliver()
                if links:
                    chunk = {"choices": [{"index": 0, "delta": {"content": links}}]}
                    yield "data: " + json.dumps(chunk)
        finally:
            await client.aclose()
