"""Language Server Protocol (LSP) integration for Hermes Agent.

Hermes runs full language servers (pyright, gopls, rust-analyzer,
typescript-language-server, etc.) as subprocesses and pipes their
``textDocument/publishDiagnostics`` output into the post-write lint
delta filter used by ``write_file`` and ``patch``.

LSP is **gated on git workspace detection** — if the agent's cwd is
inside a git repository, LSP runs against that workspace; otherwise the
file_operations layer falls back to its existing in-process syntax
checks.  This keeps users on user-home cwd's (e.g. Telegram gateway
chats) from spawning daemons they don't need.

Public API:

    from agent.lsp import get_service

    svc = get_service()
    if svc and svc.enabled_for(path):
        await svc.touch_file(path)
        diags = svc.diagnostics_for(path)

The bulk of the wiring is internal — most callers only need the layer
in :func:`tools.file_operations.FileOperations._check_lint_delta`,
which is already wired (see that module).

Architecture is documented in ``website/docs/user-guide/features/lsp.md``.
"""
from __future__ import annotations

import atexit
import logging
import threading
from typing import Optional

from agent.lsp.manager import LSPService
from hermes_constants import hermes_home_key

logger = logging.getLogger("agent.lsp")

_service: Optional[LSPService] = None
_service_scope: Optional[str] = None
_services: dict[str, LSPService] = {}
_atexit_registered = False
_service_lock = threading.Lock()


def _current_scope() -> str:
    """Return the active profile/home key for service isolation."""
    return hermes_home_key()


def get_service() -> Optional[LSPService]:
    """Return the active profile/home-scoped LSP service, or None when disabled.

    The service is created lazily on first call.  ``None`` is returned
    when LSP is disabled in config, when no workspace can be detected,
    or when the platform doesn't support subprocess-based LSP servers.
    Services are shared within one Hermes home and isolated across homes,
    so a multiplexed process cannot reuse another profile's clients.

    On first creation, registers an :mod:`atexit` handler that tears
    down spawned language servers on Python exit so a long-running
    CLI or gateway session doesn't leak pyright/gopls/etc. processes
    when it terminates.
    """
    global _service, _service_scope, _atexit_registered
    scope = _current_scope()
    with _service_lock:
        _service = _services.get(scope)
        _service_scope = scope
        if _service is None:
            _service = LSPService.create_from_config()
            if _service is not None:
                _services[scope] = _service
        if not _atexit_registered:
            # ``atexit`` handlers run in LIFO order on normal Python
            # exit and on SystemExit, but NOT on os._exit() or
            # uncaught signals.  Language servers are stateless
            # subprocesses — losing them on SIGKILL is fine; they'll
            # be reaped by the kernel along with their parent.  We
            # care about clean exits where Python flushes stdio
            # before terminating; without this hook every
            # ``hermes chat`` exit would leak pyright processes that
            # outlive the parent for a few seconds while their
            # stdout buffers drain.
            atexit.register(_atexit_shutdown)
            _atexit_registered = True
    return _service if (_service is not None and _service.is_active()) else None


def shutdown_service() -> None:
    """Tear down the active profile's LSP service if one was started.

    Explicit restart/shutdown commands run in one active Hermes profile and
    must not terminate language servers owned by another multiplexed profile.
    Safe to call multiple times; safe to call when no service was created.
    """
    global _service, _service_scope
    scope = _current_scope()
    with _service_lock:
        service = _services.pop(scope, None)
        if _service_scope == scope:
            _service = None
            _service_scope = None
    if service is not None:
        try:
            service.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP shutdown error: %s", e)


def _shutdown_all_services() -> None:
    """Tear down every profile service during process exit."""
    global _service, _service_scope
    with _service_lock:
        services = list(_services.values())
        if _service is not None and _service not in services:
            services.append(_service)
        _services.clear()
        _service = None
        _service_scope = None
    for service in services:
        try:
            service.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP shutdown error: %s", e)


def _atexit_shutdown() -> None:
    """atexit-registered wrapper.  Logs at debug because by the time
    atexit fires the user has already seen the agent's final output —
    a noisy shutdown line on top of that is just clutter."""
    try:
        _shutdown_all_services()
    except Exception as e:  # noqa: BLE001
        logger.debug("atexit LSP shutdown failed: %s", e)


__all__ = ["get_service", "shutdown_service", "LSPService"]
