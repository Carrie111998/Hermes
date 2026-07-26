"""Exact-value redaction for credentials loaded into Hermes.

Kept independent of :mod:`agent.redact` so env/config/profile loaders can
register values without changing the pattern redactor's import-time settings.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

CONFIGURED_SECRET_MARKER = "[REDACTED_CONFIGURED_SECRET]"
_MIN_SECRET_LENGTH = 8
_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|KEY)(?:_|$)",
    re.IGNORECASE,
)

_lock = threading.Lock()
_values: tuple[str, ...] = ()


def _is_secret_name(name: object) -> bool:
    return _SECRET_NAME_RE.search(str(name)) is not None


def _collect_env_values(environ: Mapping[str, Any]) -> set[str]:
    return {
        value
        for name, value in environ.items()
        if _is_secret_name(name)
        and isinstance(value, str)
        and len(value) >= _MIN_SECRET_LENGTH
    }


def _collect_config_values(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                _is_secret_name(key)
                and isinstance(child, str)
                and len(child) >= _MIN_SECRET_LENGTH
            ):
                found.add(child)
            else:
                found.update(_collect_config_values(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_collect_config_values(child))
    return found


def refresh_configured_secret_values(
    environ: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Register configured values without exposing the registry to callers."""
    discovered = _collect_env_values(os.environ if environ is None else environ)
    if config is not None:
        discovered.update(_collect_config_values(config))
    register_secret_values(discovered)


def register_secret_values(values: Iterable[object]) -> None:
    """Register values from an already trusted secret-only source."""
    global _values
    discovered = {
        value
        for value in values
        if isinstance(value, str) and len(value) >= _MIN_SECRET_LENGTH
    }
    if not discovered:
        return
    with _lock:
        merged = set(_values).union(discovered)
        if len(merged) != len(_values):
            _values = tuple(sorted(merged, key=len, reverse=True))


def redact_configured_secret_values(text: str) -> str:
    """Replace registered opaque credential values in *text*."""
    values = _values
    for secret in values:
        text = text.replace(secret, CONFIGURED_SECRET_MARKER)
    return text


# Support direct RedactingFormatter use outside normal startup.
refresh_configured_secret_values()
