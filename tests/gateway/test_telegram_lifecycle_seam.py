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


def test_run_post_connect_housekeeping_redacts_without_nameerror(monkeypatch):
    """Regression: _run_post_connect_housekeeping must not NameError on the
    redaction helper.

    Blind-review pass B found the moved method calls
    _redact_telegram_error_text(e) with no lazy import — the NameError was
    swallowed by the except-Exception wrapper, so the command-menu
    registration silently failed instead of logging the redacted reason
    (the 'swallowed error plays truncated output as complete' class). The
    lazy import must be present so the error path actually redacts.

    Behavioral: force the command-menu step to raise, then drive the method;
    pre-fix the except handler NameErrors on the unimported helper (caught
    here as the regression), post-fix it completes.
    """
    import hermes_cli.commands as hc
    import plugins.platforms.telegram.telegram_lifecycle as tl

    inst = TelegramLifecycleMixin.__new__(TelegramLifecycleMixin)
    inst.name = "probe"
    inst._bot = object()  # truthy so the command-menu step proceeds past the
    # 'if not self._bot: return' guard to the raising helper
    inst._status_indicator_online = False
    inst._dm_topics_config = {}
    inst._post_connect_task = None

    def _boom(*a, **k):
        raise RuntimeError("menu registry boom")

    monkeypatch.setattr(hc, "telegram_menu_commands", _boom)
    monkeypatch.setattr(hc, "telegram_menu_max_commands", lambda: 10)

    # The redaction helper must be reachable post-fix. Pre-fix this call
    # raises NameError inside the except handler, which the outer
    # except/CancelledError re-raise would NOT catch (NameError is not
    # CancelledError) — so it propagates. That propagation IS the assertion:
    # post-fix the method completes without raising.
    import asyncio

    async def drive():
        await inst._run_post_connect_housekeeping()

    # Post-fix: completes. Pre-fix: NameError propagates.
    asyncio.run(drive())
    # If we get here, no NameError — the lazy import is present and the
    # except path executed (it swallowed the RuntimeError via the helper).
    # The helper is imported lazily INSIDE the method body (the circular-
    # import adaptation), so it is not a module attribute — completion
    # without raising IS the regression proof.
