"""Request-local trusted runtime values for agent tool subprocesses.

Values are bound by authenticated gateway entry points and bridged only into
subprocess environments. They are never copied into prompts or global process
environment state.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Mapping, Optional

TRUSTED_RUNTIME_ENV_KEYS = frozenset({
    "PAPERCLIP_API_KEY",
    "PAPERCLIP_API_URL",
    "PAPERCLIP_AGENT_ID",
    "PAPERCLIP_COMPANY_ID",
    "PAPERCLIP_ISSUE_WORK_MODE",
    "PAPERCLIP_RUN_ID",
    "PAPERCLIP_TASK_ID",
    "PAPERCLIP_WAKE_REASON",
})

_runtime_env: ContextVar[Optional[dict[str, str]]] = ContextVar(
    "hermes_request_runtime_env", default=None
)


def get_runtime_env() -> Optional[dict[str, str]]:
    """Return bound values, or ``None`` outside a trusted request scope."""
    values = _runtime_env.get()
    return dict(values) if values is not None else None


@contextmanager
def bind_runtime_env(values: Mapping[str, str]) -> Iterator[None]:
    """Bind trusted runtime values for one request and restore on exit."""
    token = _runtime_env.set(dict(values))
    try:
        yield
    finally:
        _runtime_env.reset(token)
