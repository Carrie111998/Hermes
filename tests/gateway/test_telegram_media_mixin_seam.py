"""Seam-identity regression for the Telegram media mixin (adapter god-file slice A4).

The media-send extraction moved 15 ``TelegramAdapter`` methods (media sends,
typing helpers, ``get_chat_info``) plus their module-level helpers into
``TelegramMediaMixin`` (``plugins/platforms/telegram/telegram_media.py``).
This test pins the seam identity contract: every moved name must be
reachable on ``TelegramAdapter`` as the *same function object* the mixin
owns (MRO resolution, no shadowing or duplication), and the module-level
helpers/constant moved with the cluster must remain importable from the
``adapter`` namespace (tests and callers import them there).
"""
import sys
from unittest.mock import MagicMock

from gateway.config import PlatformConfig  # noqa: F401  (adapter import side-effect)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram import adapter as telegram_mod  # noqa: E402
from plugins.platforms.telegram.telegram_media import (  # noqa: E402
    TelegramMediaMixin,
    _coerce_duration_seconds,
    _probe_voice_duration_seconds,
    _MEDIA_SEND_READ_TIMEOUT,
)

MOVED_METHODS = [
    "_missing_media_path_error",
    "_telegram_media_too_large_note",
    "_telegram_media_size_allowed",
    "send_voice",
    "send_multiple_images",
    "send_image_file",
    "send_document",
    "send_video",
    "send_image",
    "send_animation",
    "_is_transient_typing_error",
    "_record_typing_cooldown",
    "_typing_in_cooldown",
    "send_typing",
    "get_chat_info",
]


def test_telegram_media_mixin_seam_identity():
    """Every moved method resolves on TelegramAdapter to the mixin's object."""
    adapter_cls = telegram_mod.TelegramAdapter
    assert issubclass(adapter_cls, TelegramMediaMixin)
    for name in MOVED_METHODS:
        assert getattr(adapter_cls, name) is getattr(TelegramMediaMixin, name), name


def test_telegram_media_mixin_helpers_re_exported_identically():
    """Moved module-level names keep resolving through the adapter namespace."""
    assert telegram_mod._coerce_duration_seconds is _coerce_duration_seconds
    assert telegram_mod._probe_voice_duration_seconds is _probe_voice_duration_seconds
    assert telegram_mod._MEDIA_SEND_READ_TIMEOUT == _MEDIA_SEND_READ_TIMEOUT == 60.0
