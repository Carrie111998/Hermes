"""Compatibility facade for the modular Codex-first Gateway bridge.

New implementation code lives under :mod:`gateway.codex`.  Keep importing
bridge contracts from this module when compatibility with Phase 1/2 callers is
required.
"""

from __future__ import annotations

import logging

from gateway.codex.executor import (
    _CODEX_USER_INPUT_METHODS,
    _public_progress_for_item,
    _structured_codex_user_question,
    _unwrap_thread_item,
    CodexSdkExecutor,
    CodexUserQuestion,
)
from gateway.codex.gateway_mixin import GatewayBridgeResult, GatewayCodexBridgeMixin
from gateway.codex.protocol import (
    BRIDGE_PHASES,
    TERMINAL_PHASES,
    _utc_now,
    BridgeEventProjector,
    BridgeExecutionResult,
    BridgeMapping,
    BridgeOrigin,
    BridgeReply,
    BridgeReplyMapping,
    BridgeRequest,
    CaptureResult,
    CodexExecutor,
    PendingQuestion,
    ProgressEvent,
    ReplyCaptureResult,
    request_fingerprint,
)
from gateway.codex.service import (
    _needs_user_error,
    CodexBridgeService,
    validate_workspace,
)
from gateway.codex.settings import (
    _DEFAULT_COMMAND_PREFIX,
    _coerce_string_list,
    CodexBridgeSettings,
    legacy_workers_auto_dispatch_enabled,
    load_codex_bridge_settings,
)
from gateway.codex.store import BridgeStore, _validate_reply_origin


logger = logging.getLogger(__name__)
