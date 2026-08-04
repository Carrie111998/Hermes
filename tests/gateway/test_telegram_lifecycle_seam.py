"""Seam-identity regression for the TelegramLifecycleMixin extraction.

The adapter god-file slice must not change method identity: every method
moved into ``TelegramLifecycleMixin`` must still be *the same function
object* when looked up on ``TelegramAdapter`` (the MRO seam). If a future
refactor re-defines a moved method on the adapter class, this test fails.
"""

import sys
from unittest.mock import MagicMock

import pytest


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from plugins.platforms.telegram.telegram_lifecycle import TelegramLifecycleMixin  # noqa: E402


MOVED_METHODS = (
    "_bot_identity_refresh_loop",
    "_start_post_connect_housekeeping",
    "_run_post_connect_housekeeping",
    "connect",
    "_set_status_indicator",
    "_cancel_pending_delivery_tasks",
    "disconnect",
    "_fallback_ips",
    "_looks_like_polling_conflict",
    "_looks_like_network_error",
)


def test_lifecycle_mixin_is_in_adapter_mro():
    assert TelegramLifecycleMixin in TelegramAdapter.__mro__


@pytest.mark.parametrize("name", MOVED_METHODS)
def test_moved_methods_are_seam_identical(name):
    # ``is``-identity: the adapter must resolve each moved method to the very
    # same function object the mixin defines — no redefinition allowed.
    assert getattr(TelegramAdapter, name) is getattr(TelegramLifecycleMixin, name)
