"""Backward-compatible Copilot names for the provider-neutral ACP client.

New ACP integrations should import :class:`ACPClient` from ``agent.acp_client``.
This module remains so existing Copilot callers and third-party imports do not
break while the transport implementation lives in a provider-neutral module.
"""

from agent.acp_client import ACPClient, _is_gh_copilot_deprecation_message
from agent.acp_client import subprocess as subprocess

CopilotACPClient = ACPClient

__all__ = [
    "ACPClient",
    "CopilotACPClient",
    "_is_gh_copilot_deprecation_message",
]
