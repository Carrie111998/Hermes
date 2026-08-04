"""Pure provider-selection precedence shared by runtime and read-only audits."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


GetEnv = Callable[[str, str], str | None]


def resolve_requested_provider_from_model_config(
    model_config: Any,
    requested: str | None = None,
    *,
    getenv: GetEnv = os.getenv,
) -> str:
    """Resolve explicit, configured, then environment provider selection.

    This is intentionally a pure, credential-free slice of runtime routing.
    Callers that need profile-scoped environment reads can supply their own
    ``getenv`` function; read-only diagnostics use the process environment.
    """
    if requested and requested.strip():
        return requested.strip().lower()

    if isinstance(model_config, dict):
        configured = model_config.get("provider")
        if isinstance(configured, str) and configured.strip():
            return configured.strip().lower()

    env_provider = (getenv("HERMES_INFERENCE_PROVIDER", "") or "").strip().lower()
    return env_provider or "auto"
