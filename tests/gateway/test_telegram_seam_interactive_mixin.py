"""Seam-identity regression for the TelegramInteractiveMixin extraction (shard A3).

Guards the adapter god-file decomposition: every method moved into
``TelegramInteractiveMixin`` must resolve on ``TelegramAdapter`` to *the very
same function object* that lives on the mixin — and must NOT be re-defined in
``TelegramAdapter``'s own ``__dict__`` (that would silently fork the seam and
let the two copies drift).  Also pins the moved class attributes (template
attrs, page sizes, gmail-triage dispatch table) to the mixin.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported (mirrors
# test_telegram_approval_buttons.py / test_telegram_clarify_buttons.py)
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from plugins.platforms.telegram.telegram_interactive import (  # noqa: E402
    TelegramInteractiveMixin,
)

# Every method extracted from adapter.py shard A3 (lines 2535-3948 of the
# original god file) into TelegramInteractiveMixin.
MOVED_METHODS = [
    "send_update_prompt",
    "_ea_escape",
    "send_exec_approval",
    "send_slash_confirm",
    "send_clarify",
    "send_model_picker",
    "send_choice_picker",
    "_handle_choice_picker_callback",
    "_build_provider_keyboard",
    "_build_model_keyboard",
    "_handle_model_picker_callback",
    "_notify_clarify_expired",
    "_handle_callback_query",
    "_handle_gmail_triage_callback",
]

# Class attributes that moved with the cluster (template attrs for the shared
# exec-approval core, picker page sizes, gmail-triage dispatch table).
MOVED_CLASS_ATTRS = [
    "_EA_HEADER",
    "_EA_CODE_OPEN",
    "_EA_CODE_CLOSE",
    "_EA_SMART_DENY_LINE",
    "_EA_CMD_BUDGET",
    "_PROVIDER_PAGE_SIZE",
    "_MODEL_PAGE_SIZE",
    "_GT_VERB_DISPATCH",
]


def test_every_moved_method_resolves_to_the_mixin_function_object():
    """getattr(TelegramAdapter, name) IS getattr(TelegramInteractiveMixin, name)."""
    for name in MOVED_METHODS:
        adapter_attr = getattr(TelegramAdapter, name)
        mixin_attr = getattr(TelegramInteractiveMixin, name)
        assert adapter_attr is mixin_attr, (
            f"{name}: TelegramAdapter.{name} is not TelegramInteractiveMixin.{name}"
        )
        assert callable(adapter_attr)


def test_moved_methods_are_not_redefined_on_the_adapter_class():
    """The extraction is a real move: no duplicate definitions on TelegramAdapter."""
    for name in MOVED_METHODS:
        assert name not in TelegramAdapter.__dict__, (
            f"{name} is still defined directly on TelegramAdapter — seam forked"
        )
        assert name in TelegramInteractiveMixin.__dict__, (
            f"{name} missing from TelegramInteractiveMixin.__dict__"
        )


def test_moved_class_attrs_resolve_to_the_mixin():
    """Class attributes that moved with the cluster keep their mixin home."""
    for name in MOVED_CLASS_ATTRS:
        adapter_attr = getattr(TelegramAdapter, name)
        mixin_attr = getattr(TelegramInteractiveMixin, name)
        assert adapter_attr is mixin_attr, (
            f"{name}: TelegramAdapter.{name} is not TelegramInteractiveMixin.{name}"
        )
        assert name in TelegramInteractiveMixin.__dict__, (
            f"{name} missing from TelegramInteractiveMixin.__dict__"
        )


def test_mixin_does_not_import_the_adapter_module():
    """No import cycle: the mixin module must not import the adapter module."""
    import plugins.platforms.telegram.telegram_interactive as ti

    source = Path(ti.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        # Module-level imports only: methods legitimately lazy-import
        # rebindable adapter globals (InlineKeyboardButton, ParseMode, ...).
        if line.startswith("import") or line.startswith("from"):
            assert "telegram.adapter" not in line, (
                f"mixin module-level import pulls the adapter: {line}"
            )
