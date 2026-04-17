"""Lock in that notification components use events.paths, not get_hermes_home."""
from pathlib import Path

import pytest

_FILES = [
    "events/bus.py",
    "events/producers/mailbox_watcher.py",
    "events/subscribers/audit_logger.py",
    "events/subscribers/telegram_mirror.py",
    "events/subscribers/telegram_notifier.py",
    "events/subscribers/whatsapp_escalator.py",
    "events/subscribers/digest_composer.py",
]


@pytest.mark.parametrize("relpath", _FILES)
def test_file_uses_events_paths_not_get_hermes_home(relpath):
    repo_root = Path(__file__).resolve().parents[2]
    src = (repo_root / relpath).read_text(encoding="utf-8")
    assert "get_hermes_home" not in src, f"{relpath} still uses get_hermes_home"
    assert "events.paths" in src or "from events import paths" in src, (
        f"{relpath} must import events.paths"
    )
