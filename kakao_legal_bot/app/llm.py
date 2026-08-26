"""Provider-agnostic chat completion with tool use.

Anthropic Messages and OpenAI Chat Completions, spoken over plain httpx so
the container does not carry two SDKs it barely uses. The tool loop is the
same for both: call → run the requested tools → feed the results back →
repeat until the model answers in prose or we hit the round limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


@dataclass
class LlmResult:
    text: str = ""
    tools_used: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    rounds: int = 0
    stop_reason: str = ""


class LlmError(RuntimeError):
    pass


class LlmClient:
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        max_tokens: int = 2000,
        temperature: float = 0.2,
        max_tool_rounds: int = 4,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 120.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.provider = provider
        self.extra_headers = extra_headers or {}
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_tool_rounds = max_tool_rounds
        self.timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
        *,
        model: str = "",
        max_tokens: int = 0,
    ) -> LlmResult:
        if self.provider in {"openai", "openrouter"}:
            return await self._complete_openai(system, messages, tools, model, max_tokens)
        if self.provider == "gemini":
            return await self._complete_gemini(system, messages, tools, model, max_tokens)
        return await self._complete_anthropic(system, messages, tools, model, max_tokens)

    # ── Anthropic ────────────────────────────────────────────────────────
    async def _complete_anthropic(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> LlmResult:
        if not self.api_key:
            raise LlmError("ANTHROPIC_API_KEY 가 없습니다")
        client = await self._http()
        by_name = {tool.name: tool for tool in tools}
        tool_payload = [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in tools
        ]
        convo = list(messages)
        result = LlmResult()

        for round_index in range(self.max_tool_rounds + 1):
            body: dict[str, Any] = {
                "model": model or self.model,
                "max_tokens": max_tokens or self.max_tokens,
                "temperature": self.temperature,
                "system": system,
                "messages": convo,
            }
            if tool_payload and round_index < self.max_tool_rounds:
                body["tools"] = tool_payload

            try:
                response = await client.post(
                    f"{self.base_url}/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise LlmError(f"LLM 호출 실패: {exc}") from exc
            if response.status_code >= 400:
                raise LlmError(f"LLM {response.status_code}: {response.text[:300]}")
            payload = response.json()

            blocks = payload.get("content") or []
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            result.stop_reason = str(payload.get("stop_reason") or "")
            result.rounds = round_index + 1

            if not tool_uses:
                result.text = "\n".join(t for t in texts if t).strip()
                return result

            convo.append({"role": "assistant", "content": blocks})
            tool_results = await self._run_tools(
                [(b.get("id", ""), b.get("name", ""), b.get("input") or {}) for b in tool_uses],
                by_name,
                result,
            )
            convo.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": call_id, "content": output}
                        for call_id, output in tool_results
                    ],
                }
            )
            # Partial prose alongside a tool call is a preface, not an
            # answer; keep it only if the loop ends without a final text.
            if texts and not result.text:
                result.text = "\n".join(t for t in texts if t).strip()

        return result

    # ── OpenAI ───────────────────────────────────────────────────────────
    async def _complete_openai(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> LlmResult:
        if not self.api_key:
            raise LlmError("OPENAI_API_KEY 가 없습니다")
        client = await self._http()
        by_name = {tool.name: tool for tool in tools}
        tool_payload = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in tools
        ]
        convo: list[dict[str, Any]] = [{"role": "system", "content": system}]
        convo.extend(_anthropic_to_openai(messages))
        result = LlmResult()

        # OpenRouter follows the classic chat-completions spec: it reads
        # `max_tokens` and ignores `max_completion_tokens`, which would leave
        # the output length unbounded — and unbounded output is a bill.
        limit_field = "max_tokens" if self.provider == "openrouter" else "max_completion_tokens"

        for round_index in range(self.max_tool_rounds + 1):
            body: dict[str, Any] = {
                "model": model or self.model,
                limit_field: max_tokens or self.max_tokens,
                "temperature": self.temperature,
                "messages": convo,
            }
            if tool_payload and round_index < self.max_tool_rounds:
                body["tools"] = tool_payload

            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", **self.extra_headers},
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise LlmError(f"LLM 호출 실패: {exc}") from exc
            if response.status_code >= 400:
                raise LlmError(f"LLM {response.status_code}: {response.text[:300]}")
            payload = response.json()

            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            result.stop_reason = str(choice.get("finish_reason") or "")
            result.rounds = round_index + 1

            if not calls:
                result.text = (message.get("content") or "").strip()
                return result

            convo.append(message)
            parsed: list[tuple[str, str, dict[str, Any]]] = []
            for call in calls:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                parsed.append((call.get("id", ""), function.get("name", ""), arguments))
            outputs = await self._run_tools(parsed, by_name, result)
            for call_id, output in outputs:
                convo.append({"role": "tool", "tool_call_id": call_id, "content": output})
            if message.get("content") and not result.text:
                result.text = str(message["content"]).strip()

        return result

    # ── Gemini ───────────────────────────────────────────────────────────
    async def _complete_gemini(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> LlmResult:
        if not self.api_key:
            raise LlmError("GEMINI_API_KEY 가 없습니다")
        client = await self._http()
        by_name = {tool.name: tool for tool in tools}
        declarations = [
            {
                "name": tool.name,
                "description": tool.description,
                **(
                    {"parameters": _gemini_schema(tool.input_schema)}
                    if (tool.input_schema.get("properties") or {})
                    else {}
                ),
            }
            for tool in tools
        ]
        contents = _to_gemini_contents(messages)
        result = LlmResult()

        for round_index in range(self.max_tool_rounds + 1):
            body: dict[str, Any] = {
                "contents": contents,
                "generationConfig": {
                    "temperature": self.temperature,
                    "maxOutputTokens": max_tokens or self.max_tokens,
                },
            }
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            if declarations and round_index < self.max_tool_rounds:
                body["tools"] = [{"functionDeclarations": declarations}]

            url = f"{self.base_url}/models/{model or self.model}:generateContent"
            try:
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise LlmError(f"LLM 호출 실패: {exc}") from exc
            if response.status_code >= 400:
                raise LlmError(f"LLM {response.status_code}: {response.text[:300]}")
            payload = response.json()

            candidates = payload.get("candidates") or []
            if not candidates:
                # A blocked prompt comes back 200 with no candidate at all.
                blocked = (payload.get("promptFeedback") or {}).get("blockReason")
                raise LlmError(f"응답이 차단되었습니다: {blocked or 'no candidate'}")

            candidate = candidates[0]
            parts = (candidate.get("content") or {}).get("parts") or []
            result.stop_reason = str(candidate.get("finishReason") or "")
            result.rounds = round_index + 1

            texts = [part["text"] for part in parts if isinstance(part.get("text"), str)]
            calls = [part["functionCall"] for part in parts if "functionCall" in part]

            if not calls:
                text = "\n".join(t for t in texts if t).strip()
                if not text and result.stop_reason in {"SAFETY", "PROHIBITED_CONTENT", "RECITATION"}:
                    raise LlmError(f"응답이 차단되었습니다: {result.stop_reason}")
                result.text = text
                return result

            # Echo the model turn verbatim — Gemini rejects a functionResponse
            # that does not directly follow its own functionCall.
            contents.append({"role": "model", "parts": parts})
            outputs = await self._run_tools(
                [
                    (str(call.get("name") or ""), str(call.get("name") or ""), call.get("args") or {})
                    for call in calls
                ],
                by_name,
                result,
            )
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": name,
                                # Gemini requires an object here, never a bare string.
                                "response": {"result": output},
                            }
                        }
                        for name, output in outputs
                    ],
                }
            )
            if texts and not result.text:
                result.text = "\n".join(t for t in texts if t).strip()

        return result

    # ── shared ───────────────────────────────────────────────────────────
    async def _run_tools(
        self,
        calls: list[tuple[str, str, dict[str, Any]]],
        by_name: dict[str, ToolSpec],
        result: LlmResult,
    ) -> list[tuple[str, str]]:
        async def run(call_id: str, name: str, arguments: dict[str, Any]) -> tuple[str, str]:
            tool = by_name.get(name)
            if tool is None:
                return call_id, f"[오류] 알 수 없는 도구: {name}"
            try:
                output = await tool.handler(arguments)
            except Exception as exc:  # noqa: BLE001 — a broken tool must not kill the turn
                log.warning("tool %s failed: %s", name, exc)
                return call_id, f"[오류] {name} 실행 실패: {exc}"
            return call_id, output or "[결과 없음]"

        result.tools_used.extend(name for _, name, _ in calls)
        return list(await asyncio.gather(*(run(cid, name, args) for cid, name, args in calls)))


# Gemini's function declarations take an OpenAPI subset, not full JSON
# Schema, and reject keys they don't know. Passing only these through keeps a
# future tool definition from 400-ing the whole turn.
_GEMINI_SCHEMA_KEYS = frozenset(
    {"type", "description", "properties", "required", "items", "enum", "nullable", "format"}
)


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {
                name: _gemini_schema(sub) for name, sub in value.items() if isinstance(sub, dict)
            }
        elif key == "items" and isinstance(value, dict):
            cleaned[key] = _gemini_schema(value)
        else:
            cleaned[key] = value
    return cleaned


def _to_gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Our Anthropic-shaped seed history as Gemini `contents`."""
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = "model" if message.get("role") == "assistant" else "user"
        content = message.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(
                str(block.get("text") or "")
                for block in content or []
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if text.strip():
            contents.append({"role": role, "parts": [{"text": text}]})
    return contents


def _anthropic_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten Anthropic-style content blocks into OpenAI plain strings.

    Only used for the seed history — mid-loop OpenAI messages are already
    in OpenAI shape.
    """
    converted: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            converted.append({"role": message.get("role", "user"), "content": content})
            continue
        parts: list[str] = []
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        converted.append({"role": message.get("role", "user"), "content": "\n".join(parts)})
    return converted
