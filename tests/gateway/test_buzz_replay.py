"""Focused stdlib tests for the governed exact-event replay seam."""

import asyncio
import copy
import os
import subprocess
import sys
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hermes_cli.subcommands.gateway import build_gateway_parser
from plugins.platforms.buzz.nostr_auth import (
    event_id,
    public_key_hex,
    schnorr_sign,
    schnorr_verify,
)
from plugins.platforms.buzz.replay import (
    CLAIMED,
    COMPLETED,
    FAILED,
    ReplayError,
    ReplayLedger,
    dispatch_exact_event,
    replay_db_path,
    replay_state,
    run_replay,
    validate_event,
)
from gateway.platforms.base import ProcessingOutcome


SELF_PRIVATE_KEY = "00" * 31 + "03"
AUTHOR_PRIVATE_KEY = "00" * 31 + "02"
SELF_PUBKEY = public_key_hex(SELF_PRIVATE_KEY)
AUTHOR_PUBKEY = public_key_hex(AUTHOR_PRIVATE_KEY)
CHANNEL = "dd6f4e86-52f7-4627-b14a-39ac568af123"
PARENT_EVENT_ID = "c" * 64


class _FakeAdapter:
    def __init__(self, *, allowed=True, mentioned=True):
        self._self_pubkey = SELF_PUBKEY
        self._allowed_pubkeys = {AUTHOR_PUBKEY} if allowed else set()
        self._mentioned = mentioned

    def _is_mentioned(self, _content):
        return self._mentioned


def _event(*, content="@Chip resolve this", kind=9, channel=CHANNEL, recipient=SELF_PUBKEY):
    tags = [["h", channel], ["e", PARENT_EVENT_ID, "", "reply"]]
    if recipient is not None:
        tags.append(["p", recipient])
    event = {
        "pubkey": AUTHOR_PUBKEY,
        "created_at": 1_700_000_000,
        "kind": kind,
        "tags": tags,
        "content": content,
    }
    event["id"] = event_id(event)
    event["sig"] = schnorr_sign(
        bytes.fromhex(event["id"]),
        AUTHOR_PRIVATE_KEY,
        auxiliary_randomness=bytes(32),
    ).hex()
    return event


def _resign(event):
    event = copy.deepcopy(event)
    event["id"] = event_id(event)
    event["sig"] = schnorr_sign(
        bytes.fromhex(event["id"]),
        AUTHOR_PRIVATE_KEY,
        auxiliary_randomness=bytes(32),
    ).hex()
    return event


class ReplayLedgerTests(unittest.TestCase):
    def test_replay_claim_is_durable_and_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ReplayLedger(Path(tmp) / "replay.db", profile="omar")
            event_id_value = "a" * 64

            self.assertEqual(
                ledger.claim(event_id_value),
                {"claimed": True, "status": CLAIMED},
            )
            self.assertEqual(
                ledger.claim(event_id_value),
                {"claimed": False, "status": CLAIMED, "reason": "already_claimed"},
            )
            self.assertTrue(ledger.fail(event_id_value, {"outcome": "FAILED"}))
            self.assertEqual(
                ledger.claim(event_id_value),
                {"claimed": False, "status": FAILED, "reason": "already_terminal"},
            )
            ledger.close()


class ReplayValidationTests(unittest.TestCase):
    def _assert_rejected(self, event, requested=None, adapter=None, expected_parent=None):
        requested = requested or event["id"]
        adapter = adapter or _FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            with ReplayLedger(Path(tmp) / "replay.db", profile="omar") as ledger:
                with self.assertRaises(ReplayError) as raised:
                    validate_event(
                        event,
                        requested,
                        adapter=adapter,
                        watched_channels={CHANNEL},
                        expected_parent_event_id=expected_parent,
                    )
                self.assertEqual(raised.exception.code, "validation_failed")
                self.assertIsNone(ledger.get(requested.lower()))

    def test_bad_id_signature_kind_channel_recipient_and_author_fail_before_claim(self):
        valid = _event()
        self._assert_rejected(valid, requested="b" * 64)

        bad_signature = copy.deepcopy(valid)
        bad_signature["sig"] = "00" * 64
        self._assert_rejected(bad_signature)

        self._assert_rejected(_resign({**valid, "kind": 1}))
        self._assert_rejected(
            _resign(
                {
                    **valid,
                    "tags": [
                        ["h", "other"],
                        ["e", PARENT_EVENT_ID, "", "reply"],
                        ["p", SELF_PUBKEY],
                    ],
                }
            )
        )
        self._assert_rejected(
            _resign(
                {
                    **valid,
                    "tags": [
                        ["h", CHANNEL],
                        ["e", PARENT_EVENT_ID, "", "reply"],
                        ["p", "f" * 64],
                    ],
                }
            )
        )
        self._assert_rejected(_resign({**valid, "pubkey": "e" * 64}))
        self._assert_rejected(_resign({**valid, "tags": [["h", CHANNEL], ["e", PARENT_EVENT_ID, "", "root"], ["p", SELF_PUBKEY]]}))

    def test_mention_and_allowlist_gates_are_not_bypassed(self):
        self._assert_rejected(_event(content="plain text"), adapter=_FakeAdapter(mentioned=False))
        self._assert_rejected(_event(), adapter=_FakeAdapter(allowed=False))

    def test_valid_event_reports_parent_and_signature(self):
        event = _event()
        result = validate_event(
            event,
            event["id"],
            adapter=_FakeAdapter(),
            watched_channels={CHANNEL},
        )
        self.assertEqual(result["parent_event_id"], PARENT_EVENT_ID)
        self.assertTrue(result["parent_tag_valid"])
        self.assertTrue(result["parent_expected_match"])
        self.assertTrue(result["signature_valid"])
        self.assertTrue(
            schnorr_verify(
                bytes.fromhex(event["id"]), event["pubkey"], event["sig"]
            )
        )
        with self.assertRaises(ReplayError):
            validate_event(
                event,
                event["id"],
                adapter=_FakeAdapter(),
                watched_channels={CHANNEL},
                expected_parent_event_id="d" * 64,
            )

    def test_signature_verifier_matches_bip340_vector_zero(self):
        self.assertTrue(
            schnorr_verify(
                bytes(32),
                "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9",
                "e907831f80848d1069a5371b402410364bdf1c5f8307b0084c55f1ce2dca8215"
                "25f66a4a85ea8b71e482a74f382d2ce5ebeee8fdb2172f477df4900d310536c0",
            )
        )


class ReplayStateTests(unittest.TestCase):
    def test_only_exact_seen_marker_is_bypassed_and_original_is_unchanged(self):
        event_id_value = "a" * 64
        other_id = "b" * 64
        state = {
            "chat_type": "group",
            "last_ts": 10,
            "seen": OrderedDict([(event_id_value, None), (other_id, None)]),
        }
        replayed = replay_state(state, event_id_value)
        self.assertNotIn(event_id_value, replayed["seen"])
        self.assertIn(other_id, replayed["seen"])
        self.assertIn(event_id_value, state["seen"])
        with self.assertRaises(ReplayError) as raised:
            replay_state(state, "c" * 64)
        self.assertEqual(raised.exception.code, "event_not_in_startup_seen_set")


class _DispatchAdapter:
    def __init__(self, *, crash=False, spawn=True):
        self._session_tasks = {}
        self.calls = 0
        self.crash = crash
        self.spawn = spawn
        self.processing_outcomes = []

    async def on_processing_complete(self, _event, outcome):
        self.processing_outcomes.append(outcome)

    async def _handle_event(self, _channel, _state, _event):
        self.calls += 1
        if not self.spawn:
            return

        async def _session():
            await asyncio.sleep(0)
            if self.crash:
                raise RuntimeError("ambiguous turn failure")
            await self.on_processing_complete(_event, ProcessingOutcome.SUCCESS)

        self._session_tasks["session-key"] = asyncio.create_task(_session())


class ReplayDispatchTests(unittest.TestCase):
    def test_dispatch_uses_one_normalized_adapter_path_and_one_session(self):
        adapter = _DispatchAdapter()
        result = asyncio.run(
            dispatch_exact_event(
                adapter,
                CHANNEL,
                {"id": "a" * 64},
                "a" * 64,
                {"seen": OrderedDict([("a" * 64, None)])},
            )
        )
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(result["dispatch"]["handler"], "BuzzAdapter._handle_event")
        self.assertEqual(result["dispatch"]["new_session_dispatches"], 1)
        self.assertEqual(result["outcome"]["status"], COMPLETED)

    def test_ambiguous_post_dispatch_crash_stays_claimed(self):
        adapter = _DispatchAdapter(crash=True)
        result = asyncio.run(
            dispatch_exact_event(
                adapter,
                CHANNEL,
                {"id": "a" * 64},
                "a" * 64,
                {"seen": OrderedDict([("a" * 64, None)])},
            )
        )
        self.assertEqual(result["session"]["status"], "CRASH_AMBIGUOUS")
        self.assertEqual(result["outcome"], {"status": CLAIMED, "code": "session_crash_ambiguous"})


class ReplayIntegrationTests(unittest.TestCase):
    def test_real_buzz_adapter_reaches_gateway_runner_handler(self):
        """The governed dispatch uses the production adapter/base/runner seam."""
        from gateway.config import GatewayConfig, Platform, PlatformConfig
        from gateway.run import GatewayRunner
        from plugins.platforms.buzz.adapter import BuzzAdapter

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            buzz_platform = Platform("buzz")
            config = GatewayConfig(
                platforms={
                    buzz_platform: PlatformConfig(
                        enabled=True,
                        typing_indicator=False,
                        extra={"relay_url": "wss://stub.relay"},
                    )
                },
                sessions_dir=home / "sessions",
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch(
                "tools.tirith_security.ensure_installed", return_value=None
            ):
                runner = GatewayRunner(config)
                adapter = BuzzAdapter(config.platforms[buzz_platform])
                adapter.gateway_runner = runner
                runner.adapters[buzz_platform] = adapter

                handled = []

                async def runner_handler(message):
                    handled.append(message)
                    return None

                runner._handle_message = runner_handler
                adapter.set_message_handler(runner._primary_message_handler())
                adapter.set_session_store(runner.session_store)
                adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
                adapter.set_authorization_check(lambda *_args: True)
                adapter._running = True
                adapter._self_pubkey = SELF_PUBKEY
                adapter._self_npub = "npub1chip"
                adapter._display_name = "Chip"
                adapter._allowed_pubkeys = {AUTHOR_PUBKEY}
                adapter._channel_meta[CHANNEL] = {
                    "name": "approvals",
                    "description": "internal",
                }
                adapter._user_names[AUTHOR_PUBKEY] = "Pedro"
                adapter.send_reaction = AsyncMock()

                event = _event(content="@Chip use the real seam")
                state = {
                    "chat_type": "group",
                    "last_ts": event["created_at"],
                    "seen": OrderedDict([(event["id"], None)]),
                }
                result = asyncio.run(
                    dispatch_exact_event(
                        adapter,
                        CHANNEL,
                        event,
                        event["id"],
                        state,
                        wait_timeout=2.0,
                    )
                )

                self.assertEqual(result["outcome"]["status"], COMPLETED)
                self.assertEqual(result["dispatch"]["handler"], "BuzzAdapter._handle_event")
                self.assertEqual(result["dispatch"]["new_session_dispatches"], 1)
                self.assertEqual(len(handled), 1)
                self.assertEqual(handled[0].text, "use the real seam")
                adapter.send_reaction.assert_awaited_once()
                runner._running = False
                if runner._session_db is not None:
                    runner._session_db._db.close()
                runner.session_store._db.close()

    def test_real_base_handler_failure_is_not_completed(self):
        """A base-task exception emits FAILURE and cannot become COMPLETED."""
        from gateway.config import GatewayConfig, Platform, PlatformConfig
        from gateway.run import GatewayRunner
        from gateway.platforms.base import SendResult
        from plugins.platforms.buzz.adapter import BuzzAdapter

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            buzz_platform = Platform("buzz")
            config = GatewayConfig(
                platforms={
                    buzz_platform: PlatformConfig(
                        enabled=True,
                        typing_indicator=False,
                        extra={"relay_url": "wss://stub.relay"},
                    )
                },
                sessions_dir=home / "sessions",
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch(
                "tools.tirith_security.ensure_installed", return_value=None
            ):
                runner = GatewayRunner(config)
                adapter = BuzzAdapter(config.platforms[buzz_platform])
                adapter.gateway_runner = runner
                runner.adapters[buzz_platform] = adapter

                async def failing_handler(_message):
                    await asyncio.sleep(0.05)
                    raise RuntimeError("handler failure")

                runner._handle_message = failing_handler
                adapter.set_message_handler(runner._primary_message_handler())
                adapter.set_session_store(runner.session_store)
                adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
                adapter.set_authorization_check(lambda *_args: True)
                adapter._running = True
                adapter._self_pubkey = SELF_PUBKEY
                adapter._self_npub = "npub1chip"
                adapter._display_name = "Chip"
                adapter._allowed_pubkeys = {AUTHOR_PUBKEY}
                adapter._channel_meta[CHANNEL] = {
                    "name": "approvals",
                    "description": "internal",
                }
                adapter._user_names[AUTHOR_PUBKEY] = "Pedro"
                adapter.send = AsyncMock(return_value=SendResult(success=True))

                event = _event(content="@Chip trigger base failure")
                state = {
                    "chat_type": "group",
                    "last_ts": event["created_at"],
                    "seen": OrderedDict([(event["id"], None)]),
                }
                result = asyncio.run(
                    dispatch_exact_event(
                        adapter,
                        CHANNEL,
                        event,
                        event["id"],
                        state,
                        wait_timeout=2.0,
                    )
                )

                self.assertEqual(result["outcome"]["status"], FAILED)
                self.assertEqual(result["outcome"]["code"], "processing_failed")
                runner._running = False
                if runner._session_db is not None:
                    runner._session_db._db.close()
                runner.session_store._db.close()


class ReplayLifecycleTests(unittest.TestCase):
    EVENT_ID = "d" * 64

    @staticmethod
    def _probe_gateway_lock(home: Path) -> str:
        script = (
            "from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock; "
            "ok=acquire_gateway_runtime_lock(); print(ok); "
            "release_gateway_runtime_lock() if ok else None"
        )
        env = os.environ.copy()
        env["HERMES_HOME"] = str(home)
        env["PYTHONPATH"] = str(Path(__file__).parents[2])
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _replay_patches(self, replay_module, home, *, fetch_event):
        adapter = type("ReplayAdapter", (), {"_channel_state": {}})()
        runner = object()

        async def prepare(_profile):
            return runner, adapter, None, str(home)

        async def watched(_adapter):
            return {CHANNEL}

        def validate(*_args, **_kwargs):
            return {"channel": CHANNEL}

        async def dispatch(*_args, **_kwargs):
            return {
                "dispatch": {
                    "handler": "BuzzAdapter._handle_event",
                    "accepted": True,
                    "new_session_dispatches": 1,
                },
                "session": {"status": "COMPLETED", "task_observed": True},
                "processing": {"outcomes": ["success"], "explicit_success": True},
                "outcome": {"status": COMPLETED},
            }

        return patch.multiple(
            replay_module,
            _prepare_adapter=AsyncMock(side_effect=prepare),
            _watched_channels=AsyncMock(side_effect=watched),
            fetch_event=AsyncMock(side_effect=fetch_event),
            validate_event=validate,
            dispatch_exact_event=AsyncMock(side_effect=dispatch),
            _close_runner=AsyncMock(),
        )

    def test_gateway_start_race_cannot_overlap_replay(self):
        import plugins.platforms.buzz.replay as replay_module

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"

            async def fetch(_adapter, _event_id):
                self.assertEqual(self._probe_gateway_lock(home), "False")
                return {"id": self.EVENT_ID, "created_at": 1_700_000_000}, {"found_count": 1}

            patches = self._replay_patches(replay_module, home, fetch_event=fetch)
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch(
                "hermes_cli.config.get_hermes_home", return_value=home
            ), patches:
                result = asyncio.run(run_replay("omar", self.EVENT_ID))

            self.assertEqual(result["outcome"]["status"], COMPLETED)
            self.assertEqual(self._probe_gateway_lock(home), "True")

    def test_transient_fetch_failure_leaves_no_row_and_controlled_retry_succeeds(self):
        import plugins.platforms.buzz.replay as replay_module

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            calls = 0

            async def fetch(_adapter, _event_id):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ReplayError("relay_fetch_timeout")
                return {"id": self.EVENT_ID, "created_at": 1_700_000_000}, {"found_count": 1}

            patches = self._replay_patches(replay_module, home, fetch_event=fetch)
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False), patch(
                "hermes_cli.config.get_hermes_home", return_value=home
            ), patches:
                first = asyncio.run(run_replay("omar", self.EVENT_ID))
                with ReplayLedger(replay_db_path(home), profile="omar") as ledger:
                    self.assertIsNone(ledger.get(self.EVENT_ID))
                second = asyncio.run(run_replay("omar", self.EVENT_ID))

            self.assertEqual(first["outcome"]["status"], FAILED)
            self.assertTrue(first["outcome"]["retryable"])
            self.assertEqual(first["claim"]["status"], "UNCLAIMED")
            self.assertEqual(second["outcome"]["status"], COMPLETED)
            with ReplayLedger(replay_db_path(home), profile="omar") as ledger:
                self.assertEqual(ledger.get(self.EVENT_ID)["status"], COMPLETED)


class ReplayParserTests(unittest.TestCase):
    def test_replay_buzz_parser_requires_exact_event_id(self):
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        build_gateway_parser(
            subparsers,
            cmd_gateway=lambda _args: None,
            cmd_proxy=lambda _args: None,
            cmd_gateway_enroll=lambda _args: None,
        )
        args = parser.parse_args(["gateway", "replay-buzz", "--event-id", "a" * 64])
        self.assertEqual(args.gateway_command, "replay-buzz")
        self.assertEqual(args.event_id, "a" * 64)
        self.assertIsNone(args.parent_event_id)
        self.assertEqual(args.wait_timeout, 900.0)


if __name__ == "__main__":
    unittest.main()
