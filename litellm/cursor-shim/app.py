import json
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask


UPSTREAM_BASE_URL = os.getenv("LITELLM_UPSTREAM_URL", "http://litellm:4001")
RESPONSES_MODEL_PREFIXES = tuple(
    prefix.strip()
    for prefix in os.getenv(
        "CURSOR_SHIM_RESPONSES_MODEL_PREFIXES",
        "cpa-openai-,cliproxyapi-,copilot-shim-resp",
    ).split(",")
    if prefix.strip()
)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
DEBUG_COPILOT_CLAUDE = os.getenv("CURSOR_SHIM_DEBUG_COPILOT_CLAUDE", "false").lower() == "true"

app = FastAPI()
client = httpx.AsyncClient(timeout=None)


def _debug_log(label: str, data: Any) -> None:
    print(f"[cursor-shim] {label}: {json.dumps(data, ensure_ascii=True, default=str)}", flush=True)


def _filter_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def _get_model_name(payload: object) -> str | None:
    if isinstance(payload, dict) and isinstance(payload.get("model"), str):
        return payload["model"]
    return None


def _matches_any_prefix(model_name: str | None, prefixes: tuple[str, ...]) -> bool:
    return isinstance(model_name, str) and any(model_name.startswith(prefix) for prefix in prefixes)


def _is_responses_model(payload: object) -> bool:
    return _matches_any_prefix(_get_model_name(payload), RESPONSES_MODEL_PREFIXES)


def _is_copilot_model(payload: object) -> bool:
    model_name = _get_model_name(payload)
    return isinstance(model_name, str) and model_name.startswith("copilot-")


def _is_copilot_claude_model(payload: object) -> bool:
    model_name = _get_model_name(payload)
    return isinstance(model_name, str) and model_name.startswith("copilot-claude-")


def _looks_like_responses_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False

    if "input" in payload:
        return True

    # Cursor BYOK commonly includes these Responses-only fields even when
    # it posts to /chat/completions.
    response_markers = {
        "previous_response_id",
        "reasoning",
        "text",
        "truncation",
        "prompt_cache_retention",
        "store",
        "include",
    }
    return any(field in payload for field in response_markers)


def _rewrite_upstream_path(request_path: str, payload: object) -> str:
    normalized_path = "/" + request_path.lstrip("/")
    if (
        normalized_path.endswith("/chat/completions")
        and _is_responses_model(payload)
        and _looks_like_responses_payload(payload)
    ):
        return normalized_path[: -len("/chat/completions")] + "/responses"
    return normalized_path


def _sanitize_responses_payload(payload: object) -> object:
    if not isinstance(payload, dict) or not _is_responses_model(payload):
        return payload

    sanitized = dict(payload)

    # CLIProxyAPI's OpenAI-compatible Responses implementation currently
    # rejects some optional OpenAI fields that Cursor includes.
    for field in (
        "metadata",
    ):
        sanitized.pop(field, None)

    return sanitized


def _convert_content_part_to_text(part: object) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        if isinstance(part.get("text"), str):
            return part["text"]
        if isinstance(part.get("input_text"), str):
            return part["input_text"]
    return str(part)


def _convert_input_to_messages(input_value: object) -> list[dict[str, object]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]

    if not isinstance(input_value, list):
        return [{"role": "user", "content": str(input_value)}]

    messages: list[dict[str, object]] = []
    for item in input_value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue

        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue

        role = item.get("role", "user")
        content = item.get("content", item.get("text", ""))
        if isinstance(content, list):
            content = "\n".join(_convert_content_part_to_text(part) for part in content)
        messages.append({"role": role, "content": content})

    return messages


def _normalize_messages_to_plain_text(messages: object) -> list[dict[str, object]]:
    if not isinstance(messages, list):
        return [{"role": "user", "content": str(messages)}]

    normalized: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict):
            normalized.append({"role": "user", "content": str(message)})
            continue

        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(_convert_content_part_to_text(part) for part in content)
        elif not isinstance(content, str):
            content = str(content)

        normalized.append({"role": role, "content": content})

    return normalized


def _sanitize_copilot_chat_payload(payload: object) -> object:
    if not isinstance(payload, dict) or not _is_copilot_model(payload) or _is_responses_model(payload):
        return payload

    sanitized = dict(payload)

    if "messages" not in sanitized and "input" in sanitized:
        sanitized["messages"] = _convert_input_to_messages(sanitized.pop("input"))

    if "max_output_tokens" in sanitized and "max_tokens" not in sanitized:
        sanitized["max_tokens"] = sanitized.pop("max_output_tokens")

    for field in (
        "metadata",
        "store",
        "truncation",
        "prompt_cache_retention",
        "previous_response_id",
        "include",
        "reasoning",
        "text",
        "parallel_tool_calls",
    ):
        sanitized.pop(field, None)

    if _is_copilot_claude_model(payload):
        # Claude-family Copilot chat endpoints appear stricter than the OpenAI
        # family and reject several optional OpenAI chat parameters.
        sanitized["messages"] = _normalize_messages_to_plain_text(sanitized.get("messages", []))

        if system_value := sanitized.pop("system", None):
            if isinstance(system_value, list):
                system_text = "\n".join(_convert_content_part_to_text(part) for part in system_value)
            else:
                system_text = _convert_content_part_to_text(system_value)
            sanitized["messages"] = [{"role": "system", "content": system_text}] + sanitized["messages"]

        for field in (
            "temperature",
            "top_p",
            "n",
            "logprobs",
            "top_logprobs",
            "response_format",
            "seed",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "user",
            "tools",
            "tool_choice",
            "stream_options",
        ):
            sanitized.pop(field, None)

        if DEBUG_COPILOT_CLAUDE:
            preview_messages = []
            for message in sanitized.get("messages", [])[:4]:
                if isinstance(message, dict):
                    preview_messages.append(
                        {
                            "role": message.get("role"),
                            "content_type": type(message.get("content")).__name__,
                            "content_preview": (
                                message.get("content", "")[:200]
                                if isinstance(message.get("content"), str)
                                else message.get("content")
                            ),
                        }
                    )
            _debug_log(
                "copilot_claude_sanitized_request",
                {
                    "model": sanitized.get("model"),
                    "keys": sorted(sanitized.keys()),
                    "stream": sanitized.get("stream"),
                    "messages_preview": preview_messages,
                },
            )

    return sanitized


def _extract_chat_message_from_response(payload: dict) -> tuple[str | None, list[dict] | None, str]:
    output_items = payload.get("output") or []
    text_parts: list[str] = []
    tool_calls: list[dict] = []

    for item in output_items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])

        elif item_type in {"function_call", "custom_tool_call"}:
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": item.get("name", "tool"),
                        "arguments": item.get("arguments") or item.get("input") or "",
                    },
                }
            )

    content = "".join(text_parts) if text_parts else None
    finish_reason = "tool_calls" if tool_calls else "stop"
    return content, tool_calls or None, finish_reason


def _responses_usage_to_chat_usage(payload: dict) -> dict | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None

    prompt_tokens = usage.get("input_tokens")
    completion_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")

    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return None

    return {
        "prompt_tokens": prompt_tokens or 0,
        "completion_tokens": completion_tokens or 0,
        "total_tokens": total_tokens or ((prompt_tokens or 0) + (completion_tokens or 0)),
    }


def _translate_responses_json_to_chat_completion(payload: dict) -> dict:
    content, tool_calls, finish_reason = _extract_chat_message_from_response(payload)
    translated = {
        "id": payload.get("id", f"chatcmpl-{int(time.time() * 1000)}"),
        "object": "chat.completion",
        "created": payload.get("created_at", int(time.time())),
        "model": payload.get("model"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                },
                "finish_reason": finish_reason,
            }
        ],
    }

    usage = _responses_usage_to_chat_usage(payload)
    if usage is not None:
        translated["usage"] = usage

    return translated


def _encode_sse_event(data: dict) -> bytes:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")


async def _translate_responses_sse_to_chat_chunks(upstream_response: httpx.Response):
    response_id: str | None = None
    created: int | None = None
    model: str | None = None
    emitted_role = False
    saw_tool_call = False
    usage: dict | None = None
    tool_call_state: dict[str, dict] = {}

    async for line in upstream_response.aiter_lines():
        if not line.startswith("data: "):
            continue

        raw_data = line[6:]
        if raw_data == "[DONE]":
            continue

        try:
            event = json.loads(raw_data)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type in {"response.created", "response.in_progress"} and isinstance(event.get("response"), dict):
            response = event["response"]
            response_id = response_id or response.get("id")
            created = created or response.get("created_at")
            model = model or response.get("model")
            continue

        if event_type == "response.output_item.added" and isinstance(event.get("item"), dict):
            item = event["item"]
            item_type = item.get("type")
            if item_type == "message" and not emitted_role:
                emitted_role = True
                yield _encode_sse_event(
                    {
                        "id": response_id or f"chatcmpl-{int(time.time() * 1000)}",
                        "object": "chat.completion.chunk",
                        "created": created or int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant"},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            elif item_type in {"function_call", "custom_tool_call"}:
                item_id = item.get("id")
                if item_id:
                    tool_index = len(tool_call_state)
                    tool_call_state[item_id] = {
                        "index": tool_index,
                        "id": item.get("call_id") or item_id,
                        "name": item.get("name", "tool"),
                        "arg_bytes": 0,
                    }
                    saw_tool_call = True
                    yield _encode_sse_event(
                        {
                            "id": response_id or f"chatcmpl-{int(time.time() * 1000)}",
                            "object": "chat.completion.chunk",
                            "created": created or int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": tool_index,
                                                "id": tool_call_state[item_id]["id"],
                                                "type": "function",
                                                "function": {
                                                    "name": tool_call_state[item_id]["name"],
                                                    "arguments": "",
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
            continue

        if event_type == "response.output_text.delta":
            yield _encode_sse_event(
                {
                    "id": response_id or f"chatcmpl-{int(time.time() * 1000)}",
                    "object": "chat.completion.chunk",
                    "created": created or int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": event.get("delta", "")},
                            "finish_reason": None,
                        }
                    ],
                }
            )
            continue

        if event_type == "response.function_call_arguments.delta":
            item_id = event.get("item_id")
            state = tool_call_state.get(item_id)
            if state:
                yield _encode_sse_event(
                    {
                        "id": response_id or f"chatcmpl-{int(time.time() * 1000)}",
                        "object": "chat.completion.chunk",
                        "created": created or int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": state["index"],
                                            "function": {
                                                "arguments": event.get("delta", ""),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            continue

        if event_type == "response.custom_tool_call_input.delta":
            item_id = event.get("item_id")
            state = tool_call_state.get(item_id)
            if state:
                yield _encode_sse_event(
                    {
                        "id": response_id or f"chatcmpl-{int(time.time() * 1000)}",
                        "object": "chat.completion.chunk",
                        "created": created or int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": state["index"],
                                            "function": {
                                                "arguments": event.get("delta", ""),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                state["arg_bytes"] = state.get("arg_bytes", 0) + len(event.get("delta", ""))
            continue

        if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "custom_tool_call":
                item_id = item.get("id")
                state = tool_call_state.get(item_id)
                full_args = item.get("input", "")
                if state and isinstance(full_args, str) and full_args and state.get("arg_bytes", 0) == 0:
                    yield _encode_sse_event(
                        {
                            "id": response_id or f"chatcmpl-{int(time.time() * 1000)}",
                            "object": "chat.completion.chunk",
                            "created": created or int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": state["index"],
                                                "function": {
                                                    "arguments": full_args,
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
            continue

        if event_type == "response.completed" and isinstance(event.get("response"), dict):
            response = event["response"]
            response_id = response_id or response.get("id")
            created = created or response.get("created_at")
            model = model or response.get("model")
            usage = _responses_usage_to_chat_usage(response)

    final_chunk = {
        "id": response_id or f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "tool_calls" if saw_tool_call else "stop",
            }
        ],
    }
    if usage is not None:
        final_chunk["usage"] = usage

    yield _encode_sse_event(final_chunk)
    yield b"data: [DONE]\n\n"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str) -> Response:
    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }

    request_body = await request.body()
    payload = None
    if request_body and "application/json" in request.headers.get("content-type", ""):
        try:
            payload = json.loads(request_body)
        except json.JSONDecodeError:
            payload = None

    original_path = "/" + request.url.path.lstrip("/")
    upstream_path = _rewrite_upstream_path(request.url.path, payload)
    if upstream_path.endswith("/responses") and payload is not None:
        payload = _sanitize_responses_payload(payload)
        request_body = json.dumps(payload).encode("utf-8")
    elif upstream_path.endswith("/chat/completions") and payload is not None:
        payload = _sanitize_copilot_chat_payload(payload)
        request_body = json.dumps(payload).encode("utf-8")
    upstream_url = f"{UPSTREAM_BASE_URL.rstrip('/')}{upstream_path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    upstream_request = client.build_request(
        method=request.method,
        url=upstream_url,
        headers=request_headers,
        content=request_body,
    )

    upstream_response = await client.send(upstream_request, stream=True)
    response_headers = _filter_headers(upstream_response.headers)
    translate_responses_back_to_chat = (
        original_path.endswith("/chat/completions")
        and upstream_path.endswith("/responses")
    )

    if request.method == "HEAD":
        await upstream_response.aclose()
        return Response(status_code=upstream_response.status_code, headers=response_headers)

    if "text/event-stream" in upstream_response.headers.get("content-type", ""):
        if translate_responses_back_to_chat:
            return StreamingResponse(
                _translate_responses_sse_to_chat_chunks(upstream_response),
                status_code=upstream_response.status_code,
                headers=response_headers,
                background=BackgroundTask(upstream_response.aclose),
                media_type="text/event-stream",
            )
        return StreamingResponse(
            upstream_response.aiter_raw(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream_response.aclose),
        )

    body = await upstream_response.aread()
    await upstream_response.aclose()

    content_type = upstream_response.headers.get("content-type", "")
    if DEBUG_COPILOT_CLAUDE and upstream_response.status_code >= 400 and _is_copilot_claude_model(payload):
        _debug_log(
            "copilot_claude_upstream_error",
            {
                "status_code": upstream_response.status_code,
                "content_type": content_type,
                "body": body.decode("utf-8", "replace")[:4000],
            },
        )
    if "application/json" in content_type:
        try:
            json_body = json.loads(body)
            if translate_responses_back_to_chat and isinstance(json_body, dict):
                json_body = _translate_responses_json_to_chat_completion(json_body)
            return JSONResponse(
                content=json_body,
                status_code=upstream_response.status_code,
                headers=response_headers,
            )
        except json.JSONDecodeError:
            pass

    return Response(
        content=body,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=content_type or None,
    )
