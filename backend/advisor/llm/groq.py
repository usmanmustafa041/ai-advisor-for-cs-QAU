"""Groq via its OpenAI-compatible chat API (streaming SSE). Used as the hosted fallback for Gemini."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from advisor.llm.base import GenParams, Message, MidStreamError, ProviderError, ProviderStatus, Usage
from advisor.llm.ollama import _ThinkFilter

BASE = "https://api.groq.com/openai/v1"


def _safe(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str, model: str, base_url: str = BASE):
        self.api_key = api_key
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.last_usage = Usage()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _body(self, messages: list[Message], params: GenParams) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": params.temperature,
            "max_completion_tokens": params.max_tokens,
            "stream": True,
        }
        if params.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _raise_for(self, status: int, payload: str) -> None:
        try:
            detail = json.loads(payload).get("error", {}).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            detail = payload[:200]
        detail = _safe(detail, self.api_key)
        if status in (401, 403):
            raise ProviderError(self.name, "auth", detail or "API key rejected", retryable=False)
        if status == 429:
            raise ProviderError(self.name, "quota", detail or "rate limit or quota exceeded")
        if status == 404 or ("model" in detail.lower() and "not exist" in detail.lower()):
            raise ProviderError(self.name, "model_missing", detail or f"model {self.model} not found", retryable=False)
        if status in (400, 413, 422):
            raise ProviderError(self.name, "bad_request", detail or f"HTTP {status}", retryable=False)
        raise ProviderError(self.name, "unavailable", detail or f"HTTP {status}")

    async def stream(self, messages: list[Message], params: GenParams) -> AsyncIterator[str]:
        filt = _ThinkFilter()  # reasoning models (e.g. qwen3) may emit <think> blocks
        emitted = False
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(params.timeout, connect=10)) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=self._body(messages, params)
                ) as resp:
                    if resp.status_code != 200:
                        self._raise_for(resp.status_code, (await resp.aread()).decode("utf-8", "replace"))
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if event.get("error"):
                            raise ProviderError(self.name, "unavailable", _safe(str(event["error"]), self.api_key))
                        usage = (event.get("x_groq") or {}).get("usage") or event.get("usage")
                        if usage:
                            self.last_usage.prompt_tokens = usage.get("prompt_tokens")
                            self.last_usage.completion_tokens = usage.get("completion_tokens")
                        for choice in event.get("choices", []):
                            text = filt.feed((choice.get("delta") or {}).get("content") or "")
                            if text:
                                if not emitted:
                                    self.last_usage = Usage(first_token_ms=int((time.monotonic() - started) * 1000))
                                emitted = True
                                yield text
            tail = filt.flush()
            if tail:
                yield tail
        except ProviderError as exc:
            if emitted and not isinstance(exc, MidStreamError):
                raise MidStreamError(self.name, exc.kind, exc.message) from exc
            raise
        except httpx.TimeoutException as exc:
            err = ProviderError(self.name, "timeout", f"timed out: {type(exc).__name__}")
            raise (MidStreamError(self.name, "timeout", err.message) if emitted else err) from exc
        except httpx.HTTPError as exc:
            err = ProviderError(self.name, "unavailable", _safe(str(exc), self.api_key))
            raise (MidStreamError(self.name, "unavailable", err.message) if emitted else err) from exc

    async def complete(self, messages: list[Message], params: GenParams) -> str:
        return "".join([chunk async for chunk in self.stream(messages, params)])

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{self.base_url}/models", headers=self._headers())
        if resp.status_code != 200:
            self._raise_for(resp.status_code, resp.text)
        skip = ("whisper", "tts", "guard", "playai", "orpheus", "distil")  # speech / safety classifiers, not chat
        return sorted(
            m["id"] for m in resp.json().get("data", [])
            if m.get("active", True) and not any(s in m["id"].lower() for s in skip)
        )

    async def status(self) -> ProviderStatus:
        try:
            models = await self.list_models()
        except ProviderError as exc:
            return ProviderStatus(self.name, True, False, self.model, f"{exc.kind}: {exc.message}")
        except httpx.HTTPError as exc:
            return ProviderStatus(self.name, True, False, self.model, _safe(str(exc), self.api_key))
        ok = self.model in models
        return ProviderStatus(
            self.name, True, ok, self.model,
            "ready" if ok else f"key works, but model '{self.model}' is not available to it", models,
        )
