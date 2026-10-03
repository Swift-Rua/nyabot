"""Responses API transport using the local ChatGPT plan OAuth connection."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import aiohttp

from services.chatgpt_auth import (
    ChatGPTAuthError as LocalChatGPTAuthError,
    get_valid_access_token,
    mark_plan_paused,
)


API_BASE = "https://api.openai.com/v1"
_session: aiohttp.ClientSession | None = None
_models_cache: list[dict[str, Any]] | None = None


class ChatGPTAPIError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ChatGPTPlanLimitError(ChatGPTAPIError):
    pass


class ChatGPTAuthRequiredError(ChatGPTAPIError):
    pass


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)
        # Match urllib's behavior on Windows and honor the configured system proxy.
        _session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _error_parts(payload: Any) -> tuple[str, str]:
    error = payload.get("error", payload) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        return "", str(error)
    code = str(error.get("code") or error.get("type") or "")
    message = str(error.get("message") or error.get("detail") or code or "Request failed")
    return code, message


def _raise_api_error(payload: Any, *, status_code: int | None = None) -> None:
    code, message = _error_parts(payload)
    if code == "subscription_sharing_usage_limit_exceeded":
        raise ChatGPTPlanLimitError(message, code=code, status_code=status_code or 429)
    if code == "subscription_sharing_usage_unavailable":
        raise ChatGPTAPIError(message, code=code, status_code=status_code or 503)
    if status_code in (401, 403) or code in {"invalid_token", "token_expired"}:
        raise ChatGPTAuthRequiredError(message, code=code, status_code=status_code)
    if status_code == 429:
        raise ChatGPTAPIError(message, code=code or "rate_limit", status_code=status_code)
    raise ChatGPTAPIError(message, code=code, status_code=status_code)


async def _get_access_token() -> str:
    try:
        return await asyncio.to_thread(get_valid_access_token)
    except LocalChatGPTAuthError as exc:
        raise ChatGPTAuthRequiredError(str(exc), code="chatgpt_auth_required") from exc


async def _read_error(response: aiohttp.ClientResponse) -> None:
    try:
        payload = await response.json(content_type=None)
    except Exception:
        payload = {"error": {"message": (await response.text())[:500]}}
    code, _ = _error_parts(payload)
    if code == "subscription_sharing_usage_limit_exceeded":
        await asyncio.to_thread(mark_plan_paused)
    _raise_api_error(payload, status_code=response.status)


async def fetch_models() -> list[dict[str, Any]]:
    """Fetch models visible to this signed-in ChatGPT account."""
    global _models_cache
    token = await _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with _get_session().get(f"{API_BASE}/models", headers=headers) as response:
        if response.status >= 400:
            await _read_error(response)
        payload = await response.json(content_type=None)

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ChatGPTAPIError("The ChatGPT model list response had an unexpected format.")
    _models_cache = [
        model
        for model in models
        if isinstance(model, dict)
        and model.get("visibility") == "list"
        and isinstance(model.get("slug"), str)
    ]
    if not _models_cache:
        raise ChatGPTAPIError("No models are currently listed for this ChatGPT account.")
    return list(_models_cache)


async def _choose_model() -> str:
    requested = os.getenv("CHATGPT_MODEL", "").strip()
    models = await fetch_models() if _models_cache is None else list(_models_cache)
    available = {model["slug"] for model in models}
    if requested:
        if requested not in available:
            raise ChatGPTAPIError(
                f"CHATGPT_MODEL '{requested}' is not listed for the connected account. "
                "Run `python -m services.chatgpt_auth models` to see available models."
            )
        return requested
    return models[0]["slug"]


async def _mark_if_plan_limit(code: str) -> None:
    if code == "subscription_sharing_usage_limit_exceeded":
        await asyncio.to_thread(mark_plan_paused)


async def generate_text(instructions: str, input_text: str) -> str:
    """Generate text with a streamed, non-stored Responses API request."""
    model = await _choose_model()
    token = await _get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": input_text}],
        "store": False,
        "stream": True,
    }

    parts: list[str] = []
    completed = False
    async with _get_session().post(
        f"{API_BASE}/responses",
        headers=headers,
        json=body,
    ) as response:
        if response.status >= 400:
            await _read_error(response)

        async for raw_line in response.content:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    parts.append(delta)
            elif event_type in {"response.failed", "error"}:
                response_body = event.get("response")
                error = response_body.get("error") if isinstance(response_body, dict) else event.get("error")
                code, message = _error_parts({"error": error} if error else event)
                await _mark_if_plan_limit(code)
                _raise_api_error({"error": error} if error else event)
            elif event_type == "response.incomplete":
                details = event.get("response", {}).get("incomplete_details", {})
                reason = details.get("reason", "unknown") if isinstance(details, dict) else "unknown"
                raise ChatGPTAPIError(f"ChatGPT response was incomplete: {reason}", code="response_incomplete")
            elif event_type == "response.completed":
                completed = True

    if not completed:
        raise ChatGPTAPIError("ChatGPT stream ended before response.completed.", code="incomplete_stream")
    return "".join(parts).strip()
