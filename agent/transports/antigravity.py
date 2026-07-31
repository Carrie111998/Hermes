"""Antigravity CLI (via MCP Bridge) transport for Hermes Agent."""

from typing import Any, Dict, List, Optional
from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, ToolCall, Usage
from agent.transports import register_transport


class AntigravityTransport(ProviderTransport):
    """Transport layer for Antigravity CLI via durable MCP Bridge."""

    @property
    def api_mode(self) -> str:
        return "antigravity_mcp"

    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        # Return OpenAI-formatted messages directly
        return messages

    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        return tools or []

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        return {
            "model": model,
            "messages": messages,
            "tools": tools,
            **params,
        }

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        if isinstance(response, str):
            content = response
        elif isinstance(response, dict):
            content = response.get("content", "") or response.get("text", "")
        else:
            content = str(response)

        return NormalizedResponse(
            content=content,
            role="assistant",
            finish_reason="stop",
            usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            raw=response,
        )


register_transport("antigravity_mcp", AntigravityTransport)
