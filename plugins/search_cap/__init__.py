"""Stop an Obliterated search loop at 20 web searches.

``~/.hermes/plugins/search_cap`` is a symlink to this directory. Hermes calls
``llm_request`` middleware before every LM Studio request. The count is the
``web_search`` calls after the latest real user message. A new message starts
it again at zero. Hermes' mid-turn continue lines stay on that count: chat
completions strips underscore keys before this middleware runs, so the match
is the text in ``agent/conversation_loop.py``. At 20, on
``qwen3.8-27b-obliterated``, the next request goes out with no tools,
duplicate tool rounds from this turn removed, and one line telling the model
to answer from the results it has. Hermes' 50-search halt ends the turn if
that request does not.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

OBLITERATED_MODEL = "qwen3.8-27b-obliterated"
WEB_SEARCH_LIMIT = 20
_ANSWER_NUDGE = (
    "搜尋已經做滿 20 次，到上限。"
    "用上面已經有嘅結果直接答，講你搵到咩。"
    "唔好再叫工具。"
)


def register(ctx) -> None:
    ctx.register_middleware("llm_request", on_llm_request)


def on_llm_request(request: dict, model: str | None = None, **kwargs: Any) -> dict | None:
    rewritten = cap_search(request, model=model)
    if rewritten is None:
        return None
    logger.info(
        "search-cap: %s web_search calls on %s, next request has no tools",
        _web_search_count((request.get("messages") or [])[_turn_start(request.get("messages") or []):]),
        OBLITERATED_MODEL,
    )
    return {"request": rewritten, "source": "search-cap", "reason": "web_search cap 20"}


def cap_search(request: dict, model: str | None = None) -> dict | None:
    """Return a tool-free request once Obliterated has searched 20 times.

    ``None`` means the request is unchanged.
    """
    if not isinstance(request, dict):
        return None
    if not _is_obliterated(request, model):
        return None
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    start = _turn_start(messages)
    turn = messages[start:]
    if _web_search_count(turn) < WEB_SEARCH_LIMIT:
        return None

    rewritten = dict(request)
    rewritten["messages"] = messages[:start] + _collapse_duplicate_rounds(turn) + [
        {"role": "user", "content": _ANSWER_NUDGE}
    ]
    rewritten.pop("tools", None)
    rewritten.pop("tool_choice", None)
    return rewritten


def _is_obliterated(request: dict, model: str | None) -> bool:
    if model == OBLITERATED_MODEL:
        return True
    return request.get("model") == OBLITERATED_MODEL


# Mid-turn continue lines from agent/conversation_loop.py. convert_messages
# strips underscore keys before llm_request middleware, so the text is the match.
_EMPTY_TOOL_RESPONSE_NUDGE = (
    "You just executed tool calls but returned an "
    "empty response. Please process the tool "
    "results above and continue with the task."
)
_DROPPED_TOOLCALL_NUDGE = (
    "Your previous turn indicated a tool call but none was "
    "included. Do not narrate a plan or restate intent — issue "
    "the actual tool call now to continue the task."
)
_LENGTH_CONTINUATION_NETWORK_STUB = (
    "[System: The previous response was cut off by a "
    "network error mid-stream. Continue exactly where "
    "you left off. Do not restart or repeat prior text. "
    "Finish the answer directly.]"
)
_LENGTH_CONTINUATION_OUTPUT_LIMIT = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off. Do not "
    "restart or repeat prior text. Finish the answer directly.]"
)
_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX = "[System: Your previous tool call "
_SYNTHETIC_USER_TEXTS = {
    _EMPTY_TOOL_RESPONSE_NUDGE,
    _DROPPED_TOOLCALL_NUDGE,
    _LENGTH_CONTINUATION_NETWORK_STUB,
    _LENGTH_CONTINUATION_OUTPUT_LIMIT,
    (
        "[System: Your previous response contained only internal reasoning and "
        "never produced a visible answer or tool call. Do not keep thinking. "
        "Produce your final answer as plain text now (or make the tool call "
        "you were planning).]"
    ),
    (
        "[System: Continue now. Execute the required tool calls and only "
        "send your final answer after completing the task.]"
    ),
}


def _turn_start(messages: list) -> int:
    """Index of the latest real user message. Earlier searches belong to previous turns."""
    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, dict) and _is_real_user(message):
            start = index
    return start


def _is_real_user(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    return not _is_synthetic_user_text(message.get("content"))


def _is_synthetic_user_text(content: Any) -> bool:
    if not isinstance(content, str):
        return False
    text = content.strip()
    if text == _ANSWER_NUDGE or text in _SYNTHETIC_USER_TEXTS:
        return True
    return text.startswith(_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX)


def _web_search_count(messages: list) -> int:
    count = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in _tool_calls(message):
            if _call_name(call) == "web_search":
                count += 1
    return count


def _collapse_duplicate_rounds(messages: list) -> list:
    """Drop a tool round whose every call was already kept earlier in the turn."""
    kept: list = []
    seen: set[tuple[str, str]] = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = _tool_calls(message) if isinstance(message, dict) else []
        if not calls:
            kept.append(message)
            index += 1
            continue

        end = index + 1
        while end < len(messages) and isinstance(messages[end], dict) and messages[end].get("role") == "tool":
            end += 1
        signatures = [_call_signature(call) for call in calls]
        if signatures and all(signature in seen for signature in signatures):
            index = end
            continue
        kept.extend(messages[index:end])
        seen.update(signatures)
        index = end
    return kept


def _tool_calls(message: dict) -> list:
    if message.get("role") != "assistant":
        return []
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _call_name(call: dict) -> str:
    function = call.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    name = call.get("name")
    return str(name) if name else ""


def _call_signature(call: dict) -> tuple[str, str]:
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    raw = function.get("arguments") if isinstance(function, dict) else None
    return (_call_name(call), _canonical_args(raw))


def _canonical_args(raw: Any) -> str:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if isinstance(raw, dict):
        return json.dumps(raw, sort_keys=True, ensure_ascii=False)
    if raw is None:
        return ""
    return str(raw)
