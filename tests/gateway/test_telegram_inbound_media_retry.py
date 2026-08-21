from types import SimpleNamespace

try:
    from telegram.error import TimedOut
except ImportError:  # pragma: no cover - fallback if python-telegram-bot is absent
    class TimedOut(Exception):
        pass


class FakeSource:
    def __init__(self):
        self.calls = 0

    async def get_file(self):
        self.calls += 1
        if self.calls < 3:
            raise TimedOut("timed out")
        return SimpleNamespace(file_path="x.ogg")


def test_get_file_retries_on_timedout_then_succeeds():
    import asyncio

    from plugins.platforms.telegram.adapter import telegram_get_file_with_retry

    src = FakeSource()
    file_obj = asyncio.run(telegram_get_file_with_retry(src, attempts=3, delay=0))
    assert src.calls == 3
    assert file_obj.file_path == "x.ogg"


class AlwaysTimeoutSource:
    def __init__(self):
        self.calls = 0

    async def get_file(self):
        self.calls += 1
        raise TimedOut("timed out")


def test_get_file_raises_after_attempts_exhausted():
    import asyncio

    import pytest

    from plugins.platforms.telegram.adapter import telegram_get_file_with_retry

    src = AlwaysTimeoutSource()
    with pytest.raises(TimedOut):
        asyncio.run(telegram_get_file_with_retry(src, attempts=3, delay=0))
    assert src.calls == 3
