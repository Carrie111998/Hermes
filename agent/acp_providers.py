"""Provider registry for the shared local ACP transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agent import copilot_acp_provider, prime_agent_acp_provider


@dataclass(frozen=True)
class ACPProviderConfig:
    name: str
    marker_base_url: str
    resolve_command: Callable[[], str]
    resolve_args: Callable[[], list[str]]


_CONFIGS = {
    copilot_acp_provider.PROVIDER: ACPProviderConfig(
        name=copilot_acp_provider.PROVIDER,
        marker_base_url=copilot_acp_provider.MARKER_BASE_URL,
        resolve_command=copilot_acp_provider.resolve_command,
        resolve_args=copilot_acp_provider.resolve_args,
    ),
    prime_agent_acp_provider.PROVIDER: ACPProviderConfig(
        name=prime_agent_acp_provider.PROVIDER,
        marker_base_url=prime_agent_acp_provider.MARKER_BASE_URL,
        resolve_command=prime_agent_acp_provider.resolve_command,
        resolve_args=prime_agent_acp_provider.resolve_args,
    ),
}


def get_acp_provider_config(provider: str) -> ACPProviderConfig:
    if provider not in _CONFIGS:
        raise ValueError(f"Unknown ACP provider: {provider}")
    return _CONFIGS[provider]


def provider_from_base_url(base_url: str) -> str:
    for config in _CONFIGS.values():
        if base_url.startswith(config.marker_base_url):
            return config.name
    return copilot_acp_provider.PROVIDER
