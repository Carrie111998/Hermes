"""OpenAI-compatible API server platform adapter facade.

The implementation is split across focused mixins under ``gateway.api_server_*``
to keep this import path stable without keeping a multi-thousand-line module.
"""

from __future__ import annotations

from gateway.api_server_shared import *
from gateway.api_server_core import APIServerCoreMixin
from gateway.api_server_sessions import APIServerSessionsMixin
from gateway.api_server_chat import APIServerChatMixin
from gateway.api_server_sse import APIServerSSEMixin
from gateway.api_server_responses import APIServerResponsesMixin
from gateway.api_server_jobs import APIServerJobsMixin
from gateway.api_server_runs import APIServerRunsMixin
from gateway.api_server_lifecycle import APIServerLifecycleMixin


class APIServerAdapter(
    APIServerCoreMixin,
    APIServerSessionsMixin,
    APIServerChatMixin,
    APIServerSSEMixin,
    APIServerResponsesMixin,
    APIServerJobsMixin,
    APIServerRunsMixin,
    APIServerLifecycleMixin,
    BasePlatformAdapter,
):
    """OpenAI-compatible HTTP API server adapter."""


__all__ = [name for name in globals() if not name.startswith("__")]
