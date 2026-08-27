"""Azure AI Projects client adapter for Foundry agent references."""

from __future__ import annotations

from typing import Any


def _has_agent_reference(kwargs: dict[str, Any]) -> bool:
    extra_body = kwargs.get("extra_body")
    return isinstance(extra_body, dict) and isinstance(
        extra_body.get("agent_reference"), dict
    )


def _clean_agent_reference_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "input",
        "extra_body",
        "timeout",
        "extra_headers",
        "extra_query",
    }
    return {key: value for key, value in kwargs.items() if key in allowed}


class _ConcreteResponse:
    """Facade that prevents non-streaming SDK responses from looking iterable."""

    def __init__(self, response: Any):
        self._response = response

    def __getattr__(self, name: str) -> Any:
        if name in {"__iter__", "__aiter__"}:
            raise AttributeError(name)
        return getattr(self._response, name)


class _AgentReferenceResponses:
    def __init__(self, responses: Any):
        self._responses = responses

    def create(self, **kwargs: Any) -> Any:
        if _has_agent_reference(kwargs):
            kwargs = _clean_agent_reference_kwargs(kwargs)
            return _ConcreteResponse(self._responses.create(**kwargs))
        return self._responses.create(**kwargs)

    def stream(self, **kwargs: Any) -> Any:
        if _has_agent_reference(kwargs):
            kwargs = _clean_agent_reference_kwargs(kwargs)
            return _ConcreteResponse(self._responses.create(**kwargs))
        return self._responses.stream(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._responses, name)


class AzureProjectAgentClient:
    """OpenAI-client facade backed by ``AIProjectClient.get_openai_client()``."""

    def __init__(self, *, endpoint: str, credential: Any | None = None):
        try:
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential
        except Exception as exc:
            raise RuntimeError(
                "Azure Project Agent support requires azure-ai-projects>=2.1.0 "
                "and azure-identity. Install them with: "
                "pip install 'azure-ai-projects>=2.1.0' azure-identity"
            ) from exc

        project_client = AIProjectClient(
            endpoint=endpoint,
            credential=credential or DefaultAzureCredential(),
        )
        self._client = project_client.get_openai_client()
        self.responses = _AgentReferenceResponses(self._client.responses)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
