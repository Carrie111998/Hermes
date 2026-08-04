"""Tests for GHSA-3vpc-7q5r-276h — Telegram webhook secret required.

Previously, when TELEGRAM_WEBHOOK_URL was set but TELEGRAM_WEBHOOK_SECRET
was not, python-telegram-bot received secret_token=None and the webhook
endpoint accepted any HTTP POST.

The fix refuses to start the adapter in webhook mode without the secret.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


class TestTelegramWebhookSecretRequired:
    """Direct source-level check of the webhook-secret guard.

    The guard is embedded in TelegramAdapter.connect() and hard to isolate
    via mocks (requires a full python-telegram-bot ApplicationBuilder
    chain). These tests exercise it via source inspection — verifying the
    check exists, raises RuntimeError with the advisory link, and only
    fires in webhook mode. End-to-end validation is covered by CI +
    manual deployment tests.
    """

    def _get_source(self) -> str:
        """Return adapter + polling-mixin sources concatenated.

        The webhook-start block (and its secret guard) moved into
        ``telegram_polling.py`` as ``_start_webhook`` during the adapter
        god-file slice; scanning both files keeps this pin valid across
        either layout.
        """
        repo = Path(_repo)
        adapter = (repo / "plugins" / "platforms" / "telegram" / "adapter.py").read_text(encoding="utf-8")
        polling = (repo / "plugins" / "platforms" / "telegram" / "telegram_polling.py").read_text(encoding="utf-8")
        return adapter + "\n" + polling

    def test_webhook_branch_checks_secret(self):
        """The webhook branch must read TELEGRAM_WEBHOOK_SECRET and refuse
        when empty (GHSA-3vpc-7q5r-276h)."""
        src = self._get_source()
        # The guard must appear after TELEGRAM_WEBHOOK_URL is set
        assert re.search(
            r'TELEGRAM_WEBHOOK_SECRET.*?\.strip\(\)\s*\n\s*if not webhook_secret:',
            src, re.DOTALL,
        ), (
            "The webhook transport (_start_webhook) must strip "
            "TELEGRAM_WEBHOOK_SECRET and raise when the secret is empty — "
            "see GHSA-3vpc-7q5r-276h"
        )


    def test_polling_branch_has_no_secret_guard(self):
        """Polling mode must NOT require the webhook secret — polling
        authenticates via the bot token, not a webhook secret."""
        src = self._get_source()
        # The guard must live inside the webhook-start block
        # (_start_webhook's `if webhook_url:` branch), not in the polling
        # branch that connect() falls into when webhook mode is off.
        webhook_block = re.search(
            r'if webhook_url:\s*\n(.*?)\n\s*return bool\(webhook_url\)',
            src, re.DOTALL,
        )
        assert webhook_block, (
            "telegram_polling.py _start_webhook() must gate webhook startup "
            "on TELEGRAM_WEBHOOK_URL (see GHSA-3vpc-7q5r-276h)"
        )
        webhook_body = webhook_block.group(1)
        assert "TELEGRAM_WEBHOOK_SECRET" in webhook_body
        # The polling branch in connect() (after the _start_webhook dispatch)
        # must not contain the secret guard.
        polling_branch = re.search(
            r'if not webhook_started:\s*\n(.*?)\n\s*self\._mark_connected\(\)',
            src, re.DOTALL,
        )
        if polling_branch:
            assert "TELEGRAM_WEBHOOK_SECRET" not in polling_branch.group(1)
