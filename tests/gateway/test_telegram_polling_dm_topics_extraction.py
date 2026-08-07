"""Regression tests for the wave-1 s2 extraction of the Telegram god file.

The s2 shard lifted two clusters out of ``plugins/platforms/telegram/
adapter.py`` into mixin modules:

* ``polling_mixin.PollingMixin`` — the getUpdates polling-resilience cluster
  (drain, generation-scoped progress tracking, network-error reconnect
  ladder, PTB retry-loop disarm, 409-conflict recovery).
* ``dm_topics_mixin.DmTopicsMixin`` — the private-DM forum-topic cluster
  (create / ensure / rename topics, persist ``thread_id`` back into
  ``config.yaml``, handoff-thread creation).

Every method is lifted VERBATIM; these tests pin the behavior of the pure /
self-contained methods so a later refactor that changes the lifted bodies or
their MRO placement cannot silently change observable semantics.

Pattern (same as ``tests/gateway/test_telegram_auth_check.py``): build bare
instances with ``object.__new__`` and stub only the state the method under
test touches. The mixin modules are imported through the adapter module first
so the (safe, partial-init) circular import resolves in the same order the
gateway uses at runtime.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import plugins.platforms.telegram.adapter  # noqa: F401  (loads mixins via MRO)
from plugins.platforms.telegram.dm_topics_mixin import DmTopicsMixin
from plugins.platforms.telegram.polling_mixin import (
    _POLLING_GENERATION_CONTEXT,
    PollingMixin,
)


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────
def _bare_polling_adapter(**overrides):
    adapter = object.__new__(PollingMixin)
    adapter.name = "test-telegram"
    adapter._app = None
    adapter._bot = None
    adapter._polling_teardown_started = False
    adapter._polling_generation = 0
    adapter._polling_progress_event = asyncio.Event()
    adapter._polling_progress_accepting = False
    adapter._polling_progress_verifier_task = None
    adapter._polling_network_error_count = 0
    adapter._polling_conflict_count = 0
    adapter._polling_conflict_recovery_generation = None
    adapter._send_path_degraded = False
    adapter._general_request_drain_lock = None
    adapter._background_tasks = set()
    adapter._polling_error_task = None
    adapter.has_fatal_error = False
    for k, v in overrides.items():
        setattr(adapter, k, v)
    return adapter


def _bare_dm_adapter(**overrides):
    adapter = object.__new__(DmTopicsMixin)
    adapter.name = "test-telegram"
    adapter._bot = None
    adapter._dm_topics = {}
    adapter._dm_topics_config = []
    for k, v in overrides.items():
        setattr(adapter, k, v)
    return adapter


class _FakePollingRequest:
    """PTB request double: slotted like HTTPXRequest, with parse + do_request."""

    __slots__ = ("_payload_envelope", "do_request_calls")

    def __init__(self, envelope=None):
        object.__setattr__(self, "_payload_envelope", envelope)
        object.__setattr__(self, "do_request_calls", 0)

    def parse_json_payload(self, payload):
        if self._payload_envelope is None:
            raise ValueError("no envelope")
        return self._payload_envelope

    async def do_request(self, *args, **kwargs):
        object.__setattr__(self, "do_request_calls", self.do_request_calls + 1)
        return 200, b'{"ok": true, "result": []}'


# ──────────────────────────────────────────────────────────────────────
# PollingMixin: generation + progress bookkeeping
# ──────────────────────────────────────────────────────────────────────
class TestBeginPollingGeneration:
    def test_increments_generation_and_returns_fresh_event(self):
        adapter = _bare_polling_adapter()
        gen1, ev1 = adapter._begin_polling_generation()
        assert gen1 == 1
        assert adapter._polling_generation == 1
        assert adapter._polling_progress_accepting is True
        assert adapter._send_path_degraded is True
        assert isinstance(ev1, asyncio.Event)
        assert not ev1.is_set()

        gen2, ev2 = adapter._begin_polling_generation()
        assert gen2 == 2
        assert ev2 is not ev1, "each generation gets its own progress event"

    def test_teardown_started_returns_current_generation_without_increment(self):
        adapter = _bare_polling_adapter(_polling_teardown_started=True)
        adapter._polling_generation = 7
        gen, ev = adapter._begin_polling_generation()
        assert gen == 7
        assert adapter._polling_generation == 7, "no increment during teardown"
        assert adapter._polling_progress_accepting is False
        assert adapter._send_path_degraded is True

    def test_previous_verifier_task_is_cancelled(self):
        cancelled = []

        class _Task:
            def done(self):
                return False

            def cancel(self):
                cancelled.append(True)

        adapter = _bare_polling_adapter(_polling_progress_verifier_task=_Task())
        adapter._begin_polling_generation()
        assert cancelled == [True]
        assert adapter._polling_progress_verifier_task is None


class TestRecordPollingProgress:
    def test_records_only_current_generation(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()  # -> generation 1, accepting
        adapter._record_polling_progress(0)  # stale generation: ignored
        assert not adapter._polling_progress_event.is_set()
        adapter._record_polling_progress(1)
        assert adapter._polling_progress_event.is_set()
        assert adapter._polling_network_error_count == 0
        assert adapter._send_path_degraded is False

    def test_resets_conflict_count_when_not_in_conflict_recovery(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()
        adapter._polling_conflict_count = 3
        adapter._record_polling_progress(1)
        assert adapter._polling_conflict_count == 0
        assert adapter._polling_conflict_recovery_generation is None

    def test_keeps_conflict_count_during_conflict_recovery_generation(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()
        adapter._polling_conflict_recovery_generation = 1
        adapter._polling_conflict_count = 2
        adapter._record_polling_progress(1)
        assert adapter._polling_conflict_count == 2, "conflict count survives recovery gen"
        assert adapter._polling_conflict_recovery_generation is None

    def test_noop_during_teardown(self):
        adapter = _bare_polling_adapter(_polling_teardown_started=True)
        adapter._begin_polling_generation()
        adapter._record_polling_progress(1)
        assert not adapter._polling_progress_event.is_set()


class TestObservePollingRequestResult:
    def test_records_progress_on_ok_envelope(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()
        req = _FakePollingRequest(envelope={"ok": True, "result": [{"update_id": 1}]})
        adapter._observe_polling_request_result(req, 1, (200, b"payload"))
        assert adapter._polling_progress_event.is_set()

    def test_ignores_non_2xx_status(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()
        req = _FakePollingRequest(envelope={"ok": True, "result": []})
        adapter._observe_polling_request_result(req, 1, (409, b"payload"))
        assert not adapter._polling_progress_event.is_set()

    def test_ignores_ok_false_envelope(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()
        req = _FakePollingRequest(envelope={"ok": False, "error_code": 429})
        adapter._observe_polling_request_result(req, 1, (200, b"payload"))
        assert not adapter._polling_progress_event.is_set()

    def test_ignores_parse_failure(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()
        req = _FakePollingRequest(envelope=None)  # parse raises
        adapter._observe_polling_request_result(req, 1, (200, b"bad"))
        assert not adapter._polling_progress_event.is_set()


class TestInstrumentPollingRequest:
    @pytest.mark.asyncio
    async def test_wrapped_do_request_observes_result(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()  # generation 1
        req = _FakePollingRequest(envelope={"ok": True, "result": []})
        wrapped = adapter._instrument_polling_request(req)

        token = _POLLING_GENERATION_CONTEXT.set(1)
        try:
            status, payload = await wrapped.do_request("timeout=30")
        finally:
            _POLLING_GENERATION_CONTEXT.reset(token)

        assert status == 200
        assert wrapped.do_request_calls == 1
        assert adapter._polling_progress_event.is_set(), "observed progress recorded"

    @pytest.mark.asyncio
    async def test_wrapped_do_request_no_generation_is_observation_noop(self):
        adapter = _bare_polling_adapter()
        adapter._begin_polling_generation()
        req = _FakePollingRequest(envelope={"ok": True, "result": []})
        wrapped = adapter._instrument_polling_request(req)
        status, _ = await wrapped.do_request()
        assert status == 200
        assert not adapter._polling_progress_event.is_set()


class TestGetGeneralRequestDrainLock:
    def test_lazily_creates_and_caches_lock(self):
        adapter = _bare_polling_adapter()
        lock1 = adapter._get_general_request_drain_lock()
        lock2 = adapter._get_general_request_drain_lock()
        assert isinstance(lock1, asyncio.Lock)
        assert lock1 is lock2


class TestDrainGeneralConnectionsAfterPoolTimeout:
    @pytest.mark.asyncio
    async def test_drains_general_request_pool(self):
        class _FakeReq:
            def __init__(self):
                self.shutdown_calls = 0
                self.initialize_calls = 0

            async def shutdown(self):
                self.shutdown_calls += 1

            async def initialize(self):
                self.initialize_calls += 1

        general = _FakeReq()
        adapter = _bare_polling_adapter(
            _bot=SimpleNamespace(_request=(None, general)),
        )
        await adapter._drain_general_connections_after_pool_timeout()
        assert general.shutdown_calls == 1
        assert general.initialize_calls == 1

    @pytest.mark.asyncio
    async def test_noop_without_bot(self):
        adapter = _bare_polling_adapter(_bot=None, _app=None)
        await adapter._drain_general_connections_after_pool_timeout()  # must not raise


class TestDisarmPtbRetryLoop:
    @pytest.mark.asyncio
    async def test_sets_polling_task_stop_event(self):
        stop_event = asyncio.Event()
        updater = SimpleNamespace(_polling_task_stop_event=stop_event)
        adapter = _bare_polling_adapter(_app=SimpleNamespace(updater=updater))
        adapter._disarm_ptb_retry_loop()
        assert stop_event.is_set()

    @pytest.mark.asyncio
    async def test_prefers_mangled_attr_name(self):
        stop_event = asyncio.Event()
        updater = SimpleNamespace(
            _Updater__polling_task_stop_event=stop_event,
            _polling_task_stop_event=asyncio.Event(),  # would be set if probed second
        )
        adapter = _bare_polling_adapter(_app=SimpleNamespace(updater=updater))
        adapter._disarm_ptb_retry_loop()
        assert stop_event.is_set()
        assert not updater._polling_task_stop_event.is_set()

    @pytest.mark.asyncio
    async def test_noop_without_updater(self):
        adapter = _bare_polling_adapter(_app=None)
        adapter._disarm_ptb_retry_loop()  # must not raise


class TestSchedulePollingRecovery:
    @pytest.mark.asyncio
    async def test_schedules_recovery_task_and_tracks_it(self):
        adapter = _bare_polling_adapter()
        adapter._handle_polling_network_error = AsyncMock()  # type: ignore[attr-defined]
        adapter._schedule_polling_recovery(OSError("boom"), reason="test")
        await asyncio.sleep(0)
        assert adapter._polling_error_task is not None
        assert adapter._polling_error_task in adapter._background_tasks
        assert adapter._send_path_degraded is True
        adapter._polling_error_task.cancel()

    @pytest.mark.asyncio
    async def test_noop_when_recovery_already_in_flight(self):
        in_flight = asyncio.create_task(asyncio.sleep(30))
        adapter = _bare_polling_adapter(
            _polling_error_task=in_flight,
            _background_tasks={in_flight},
        )
        adapter._schedule_polling_recovery(OSError("boom"), reason="test")
        assert adapter._polling_error_task is in_flight, "no duplicate recovery scheduled"
        in_flight.cancel()

    @pytest.mark.asyncio
    async def test_noop_during_teardown(self):
        adapter = _bare_polling_adapter(_polling_teardown_started=True)
        adapter._schedule_polling_recovery(OSError("boom"), reason="test")
        assert adapter._polling_error_task is None


# ──────────────────────────────────────────────────────────────────────
# DmTopicsMixin
# ──────────────────────────────────────────────────────────────────────
class TestCreateDmTopic:
    @pytest.mark.asyncio
    async def test_creates_forum_topic_and_returns_thread_id(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=42))
        adapter = _bare_dm_adapter(_bot=bot)
        thread_id = await adapter._create_dm_topic(123456789, name="General")
        assert thread_id == 42
        bot.create_forum_topic.assert_awaited_once_with(chat_id=123456789, name="General")

    @pytest.mark.asyncio
    async def test_forwards_icon_options(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=7))
        adapter = _bare_dm_adapter(_bot=bot)
        await adapter._create_dm_topic(
            1, name="A11y", icon_color=7322096, icon_custom_emoji_id="emoji"
        )
        bot.create_forum_topic.assert_awaited_once_with(
            chat_id=1, name="A11y", icon_color=7322096, icon_custom_emoji_id="emoji"
        )

    @pytest.mark.asyncio
    async def test_duplicate_topic_returns_none_without_raising(self):
        bot = AsyncMock()
        bot.create_forum_topic = AsyncMock(
            side_effect=RuntimeError("Bad Request: topic_name_duplicate")
        )
        adapter = _bare_dm_adapter(_bot=bot)
        assert await adapter._create_dm_topic(1, name="General") is None

    @pytest.mark.asyncio
    async def test_noop_without_bot(self):
        adapter = _bare_dm_adapter(_bot=None)
        assert await adapter._create_dm_topic(1, name="General") is None


class TestCreateHandoffThread:
    @pytest.mark.asyncio
    async def test_returns_string_thread_id(self):
        adapter = _bare_dm_adapter()
        adapter._create_dm_topic = AsyncMock(return_value=99)  # type: ignore[attr-defined]
        assert await adapter.create_handoff_thread("123456", "Handoff") == "99"

    @pytest.mark.asyncio
    async def test_non_numeric_chat_returns_none(self):
        adapter = _bare_dm_adapter()
        assert await adapter.create_handoff_thread("not-a-number", "Handoff") is None

    @pytest.mark.asyncio
    async def test_failed_topic_creation_returns_none(self):
        adapter = _bare_dm_adapter()
        adapter._create_dm_topic = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        assert await adapter.create_handoff_thread("123456", "Handoff") is None


class TestEnsureDmTopic:
    @pytest.mark.asyncio
    async def test_returns_cached_thread_id(self):
        adapter = _bare_dm_adapter(_dm_topics={"123456:General": 42})
        result = await adapter.ensure_dm_topic("123456", "General")
        assert result == "42"

    @pytest.mark.asyncio
    async def test_returns_persisted_config_thread_id(self):
        adapter = _bare_dm_adapter(
            _dm_topics_config=[
                {
                    "chat_id": 123456,
                    "topics": [{"name": "General", "thread_id": 42}],
                }
            ]
        )
        result = await adapter.ensure_dm_topic("123456", "General")
        assert result == "42"
        assert adapter._dm_topics["123456:General"] == 42, "config thread_id cached"

    @pytest.mark.asyncio
    async def test_creates_topic_when_missing_and_persists(self):
        adapter = _bare_dm_adapter()
        adapter._create_dm_topic = AsyncMock(return_value=42)  # type: ignore[attr-defined]
        adapter._persist_dm_topic_thread_id = MagicMock()  # sync method — not awaited
        result = await adapter.ensure_dm_topic("123456", "General")
        assert result == "42"
        adapter._create_dm_topic.assert_awaited_once_with(
            123456, name="General", icon_color=None, icon_custom_emoji_id=None
        )
        adapter._persist_dm_topic_thread_id.assert_called_once_with(
            123456, "General", 42, replace_existing=False
        )
        assert adapter._dm_topics["123456:General"] == 42

    @pytest.mark.asyncio
    async def test_empty_name_returns_none(self):
        adapter = _bare_dm_adapter()
        assert await adapter.ensure_dm_topic("123456", "  ") is None

    @pytest.mark.asyncio
    async def test_non_numeric_chat_returns_none(self):
        adapter = _bare_dm_adapter()
        assert await adapter.ensure_dm_topic("abc", "General") is None


class TestRenameDmTopic:
    @pytest.mark.asyncio
    async def test_renames_forum_topic(self):
        bot = AsyncMock()
        bot.edit_forum_topic = AsyncMock()
        adapter = _bare_dm_adapter(_bot=bot)
        await adapter.rename_dm_topic(123456, 42, "New Name")
        bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=123456, message_thread_id=42, name="New Name"
        )

    @pytest.mark.asyncio
    async def test_noop_without_bot(self):
        adapter = _bare_dm_adapter(_bot=None)
        await adapter.rename_dm_topic(123456, 42, "New Name")  # must not raise


class TestPersistDmTopicThreadId:
    def test_writes_new_thread_id_into_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("platforms: {}\n", encoding="utf-8")
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        written = {}

        def _fake_atomic_write(path, data, **kwargs):
            written["path"] = path
            written["data"] = data

        monkeypatch.setattr("hermes_cli.config.atomic_config_write", _fake_atomic_write)

        adapter = _bare_dm_adapter()
        adapter._persist_dm_topic_thread_id(123456, "General", 42)

        assert written["path"] == config_file
        dm_topics = written["data"]["platforms"]["telegram"]["extra"]["dm_topics"]
        assert dm_topics == [{"chat_id": 123456, "topics": [{"name": "General", "thread_id": 42}]}]

    def test_updates_existing_topic_thread_id_with_replace(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "platforms:\n  telegram:\n    extra:\n      dm_topics:\n"
            "        - chat_id: 123456\n          topics:\n"
            "            - name: General\n              thread_id: 1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        written = {}

        def _fake_atomic_write(path, data, **kwargs):
            written["data"] = data

        monkeypatch.setattr("hermes_cli.config.atomic_config_write", _fake_atomic_write)

        adapter = _bare_dm_adapter()
        adapter._persist_dm_topic_thread_id(123456, "General", 42, replace_existing=True)

        topics = written["data"]["platforms"]["telegram"]["extra"]["dm_topics"][0]["topics"]
        assert topics == [{"name": "General", "thread_id": 42}]

    def test_missing_config_file_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        adapter = _bare_dm_adapter()
        adapter._persist_dm_topic_thread_id(123456, "General", 42)  # must not raise


class TestSetupDmTopics:
    @pytest.mark.asyncio
    async def test_loads_persisted_thread_ids_into_cache(self):
        adapter = _bare_dm_adapter(
            _dm_topics_config=[
                {
                    "chat_id": 123456,
                    "topics": [{"name": "General", "thread_id": 42}],
                }
            ]
        )
        adapter._create_dm_topic = AsyncMock(return_value=99)  # type: ignore[attr-defined]
        await adapter._setup_dm_topics()
        assert adapter._dm_topics["123456:General"] == 42
        adapter._create_dm_topic.assert_not_awaited(), "persisted topics are not re-created"

    @pytest.mark.asyncio
    async def test_creates_topics_without_thread_id(self):
        adapter = _bare_dm_adapter(
            _dm_topics_config=[
                {
                    "chat_id": 123456,
                    "topics": [{"name": "New", "icon_color": 7322096}],
                }
            ]
        )
        adapter._create_dm_topic = AsyncMock(return_value=77)  # type: ignore[attr-defined]
        adapter._persist_dm_topic_thread_id = MagicMock()  # sync method — not awaited
        await adapter._setup_dm_topics()
        adapter._create_dm_topic.assert_awaited_once_with(
            chat_id=123456,
            name="New",
            icon_color=7322096,
            icon_custom_emoji_id=None,
        )
        assert adapter._dm_topics["123456:New"] == 77
        adapter._persist_dm_topic_thread_id.assert_called_once_with(123456, "New", 77)

    @pytest.mark.asyncio
    async def test_noop_without_config(self):
        adapter = _bare_dm_adapter(_dm_topics_config=[])
        await adapter._setup_dm_topics()  # must not raise
