"""Gateway routing and invariants for ``/refresh``."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from threading import Barrier, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.gateway.test_gateway_command_dispatch_minimal import _make_event, _make_runner


class _AttemptInt(int):
    pass


class _AttemptFloat(float):
    pass


class _RaisingAttemptDict(dict):
    def get(self, key, default=None):
        raise RuntimeError("metadata access failed")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"model_attempted": True}, True),
        ({"model_attempted": False}, False),
        ({"api_calls": 1}, True),
        ({"api_calls": 0}, False),
        ({"api_calls": -1}, False),
        ({"api_calls": 1.0}, True),
        ({"api_calls": 0.0}, False),
        ({"api_calls": -1.0}, False),
        ({"api_calls": 1.5}, False),
        ({"api_calls": float("nan")}, False),
        ({"api_calls": float("inf")}, False),
        ({"api_calls": float("-inf")}, False),
        ({"api_calls": True}, False),
        ({"api_calls": False}, False),
        ({"api_calls": "1"}, False),
        ({"api_calls": "1.0"}, False),
        ({"api_calls": object()}, False),
        ({"api_calls": _AttemptInt(1)}, False),
        ({"api_calls": _AttemptFloat(1.0)}, False),
        (_RaisingAttemptDict(api_calls=1), False),
        (object(), False),
    ],
    ids=(
        "explicit-attempt",
        "explicit-no-attempt",
        "positive-int",
        "zero-int",
        "negative-int",
        "positive-integral-float",
        "zero-float",
        "negative-integral-float",
        "positive-fractional-float",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "true-is-not-a-call-count",
        "false-is-not-a-call-count",
        "integer-string",
        "float-string",
        "arbitrary-call-count",
        "int-subclass",
        "float-subclass",
        "raising-dict-subclass",
        "arbitrary-result",
    ),
)
def test_gateway_legacy_signature_actual_attempt_metadata_is_strict_and_never_raises(
    result, expected
):
    from gateway.run import _validated_actual_model_attempt

    assert _validated_actual_model_attempt(result) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["missing", object(), 17])
async def test_gateway_refresh_global_fallback_uses_default_adapters_in_platform_scopes(
    tmp_path, monkeypatch, profile
):
    """Unresolvable profile metadata must retain the global adapter owner."""
    from agent import skill_commands
    from gateway.config import Platform
    from gateway.session_context import get_session_env

    global_home = tmp_path / ".hermes"
    global_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(global_home))

    runner, primary = _make_runner()
    event = _make_event("/refresh")
    event.source.profile = profile
    discord = MagicMock(name="default-discord")
    runner.adapters = {
        Platform.TELEGRAM: primary,
        Platform.DISCORD: discord,
    }
    runner._profile_adapters = {}
    observed = []

    def reload():
        observed.append(
            (
                "reload",
                Path(skill_commands.get_hermes_home()),
                get_session_env("HERMES_SESSION_PLATFORM"),
            )
        )
        return {"added": [], "removed": [], "unchanged": [], "total": 0, "commands": 0}

    def refresh(label):
        def run():
            observed.append(
                (
                    label,
                    Path(skill_commands.get_hermes_home()),
                    get_session_env("HERMES_SESSION_PLATFORM"),
                )
            )
        return run

    monkeypatch.setattr(skill_commands, "reload_skills", reload)
    primary.refresh_skill_group = MagicMock(side_effect=refresh("telegram"))
    discord.refresh_skill_group = MagicMock(side_effect=refresh("discord"))

    await runner._reload_skills_and_refresh_adapters(event.source)

    assert observed == [
        ("reload", global_home, "telegram"),
        ("telegram", global_home, "telegram"),
        ("discord", global_home, "discord"),
    ]


@pytest.mark.asyncio
async def test_gateway_refresh_resyncs_only_invoking_profile_adapters_in_their_platform_scopes(
    tmp_path, monkeypatch
):
    """A secondary Telegram refresh must not touch primary or leak into Discord."""
    from agent import skill_commands
    from gateway.config import Platform
    from gateway.session_context import get_session_env

    runner, primary = _make_runner()
    runner.config.multiplex_profiles = True
    secondary_home = tmp_path / "profiles" / "secondary"
    secondary_home.mkdir(parents=True)
    event = _make_event("/refresh")
    event.source.profile = "secondary"
    secondary_telegram = MagicMock(name="secondary-telegram")
    secondary_discord = MagicMock(name="secondary-discord")
    runner._profile_adapters = {
        "secondary": {
            Platform.TELEGRAM: secondary_telegram,
            Platform.DISCORD: secondary_discord,
        }
    }
    runner._resolve_profile_home_for_source = lambda source: secondary_home

    observed = []

    def reload():
        observed.append(("reload", Path(skill_commands.get_hermes_home()), get_session_env("HERMES_SESSION_PLATFORM")))
        return {"added": [], "removed": [], "unchanged": [], "total": 0, "commands": 0}

    def refresh(label):
        def run():
            observed.append((label, Path(skill_commands.get_hermes_home()), get_session_env("HERMES_SESSION_PLATFORM")))
        return run

    monkeypatch.setattr(skill_commands, "reload_skills", reload)
    primary.refresh_skill_group = MagicMock(side_effect=refresh("primary"))
    secondary_telegram.refresh_skill_group = MagicMock(side_effect=refresh("telegram"))
    secondary_discord.refresh_skill_group = MagicMock(side_effect=refresh("discord"))

    await runner._reload_skills_and_refresh_adapters(event.source)

    primary.refresh_skill_group.assert_not_called()
    secondary_telegram.refresh_skill_group.assert_called_once_with()
    secondary_discord.refresh_skill_group.assert_called_once_with()
    assert observed == [
        ("reload", secondary_home, "telegram"),
        ("telegram", secondary_home, "telegram"),
        ("discord", secondary_home, "discord"),
    ]


@pytest.mark.asyncio
async def test_gateway_soft_refresh_routes_and_queues_context_without_transcript_mutation():
    runner, _adapter = _make_runner()
    _adapter.refresh_skill_group = MagicMock(return_value=(1, []))
    runner.session_store.append_to_transcript = MagicMock()
    runner._evict_cached_agent = MagicMock()
    result = SimpleNamespace(
        context_note="[fresh profile context]",
        report="Refreshed skills and memory. Gateway not restarted.",
    )

    with patch("agent.session_refresh.build_soft_refresh", return_value=result):
        output = await runner._handle_message(_make_event("/refresh"))

    assert output == result.report
    session_key = runner._session_key_for_source(_make_event("/refresh").source)
    assert [r["note"] for r in runner._pending_refresh_notes[session_key]] == [
        result.context_note
    ]
    runner.session_store.append_to_transcript.assert_not_called()
    runner._evict_cached_agent.assert_not_called()
    _adapter.refresh_skill_group.assert_called_once_with()


@pytest.mark.asyncio
async def test_gateway_refresh_branch_delegates_to_existing_branch_handler():
    runner, _adapter = _make_runner()
    runner._reload_skills_and_refresh_adapters = AsyncMock(return_value={})
    runner._handle_branch_command = AsyncMock(return_value="branched")
    event = _make_event("/refresh --branch")

    output = await runner._handle_refresh_command(event)

    assert output == "branched"
    runner._reload_skills_and_refresh_adapters.assert_awaited_once_with(event.source)
    runner._handle_branch_command.assert_awaited_once_with(event)


def test_gateway_refresh_note_claim_rejects_old_synthetic_command_and_other_session():
    runner, _adapter = _make_runner()
    refresh_event = _make_event("/refresh")
    note = {
        "note": "[fresh context]",
        "after": refresh_event.timestamp,
        "generation": 7,
    }
    note["token"] = "token-a"
    note["reserved_by"] = None
    runner._pending_refresh_notes = {"session-a": [note]}

    older = _make_event("older queued prompt")
    older.internal = False
    older.timestamp = refresh_event.timestamp - timedelta(seconds=1)
    assert runner._claim_refresh_context_note("session-a", older, 7) is None

    synthetic = _make_event("automatic continuation")
    synthetic.timestamp = refresh_event.timestamp + timedelta(seconds=1)
    synthetic.internal = True
    assert runner._claim_refresh_context_note("session-a", synthetic, 7) is None

    command = _make_event("/status")
    command.timestamp = refresh_event.timestamp + timedelta(seconds=2)
    assert runner._claim_refresh_context_note("session-a", command, 7) is None

    genuine = _make_event("real next turn")
    genuine.internal = False
    genuine.timestamp = refresh_event.timestamp + timedelta(seconds=3)
    assert runner._claim_refresh_context_note("session-b", genuine, 7) is None
    assert runner._claim_refresh_context_note("session-a", genuine, 6) is None
    assert runner._claim_refresh_context_note("session-a", genuine, 7) == {
        "token": "token-a",
        "note": "[fresh context]",
    }
    assert runner._claim_refresh_context_note("session-a", genuine, 8) is None

    runner._finish_refresh_context_note("session-a", "token-a", 7, attempted=False)
    assert runner._claim_refresh_context_note("session-a", genuine, 8)["note"] == "[fresh context]"
    runner._finish_refresh_context_note("session-a", "token-a", 8, attempted=True)
    assert "session-a" not in runner._pending_refresh_notes


def test_gateway_second_refresh_survives_first_reservation_commit():
    runner, _adapter = _make_runner()
    event = _make_event("real next turn")
    event.internal = False
    records = [
        {"token": "one", "note": "NOTE-1", "after": event.timestamp - timedelta(seconds=2), "generation": 1, "reserved_by": None},
        {"token": "two", "note": "NOTE-2", "after": event.timestamp - timedelta(seconds=1), "generation": 2, "reserved_by": None},
    ]
    runner._pending_refresh_notes = {"session-a": records}

    first = runner._claim_refresh_context_note("session-a", event, 2)
    assert first == {"token": "one", "note": "NOTE-1"}
    runner._finish_refresh_context_note("session-a", "one", 2, attempted=True)

    second = runner._claim_refresh_context_note("session-a", event, 3)
    assert second == {"token": "two", "note": "NOTE-2"}


@pytest.mark.parametrize(
    ("after", "event_at"),
    [
        (
            datetime.fromtimestamp(1_700_000_000),
            lambda seconds: datetime.fromtimestamp(
                1_700_000_000 + seconds, timezone(timedelta(hours=5, minutes=30))
            ),
        ),
        (
            datetime.fromtimestamp(1_700_000_000, timezone(timedelta(hours=-7))),
            lambda seconds: datetime.fromtimestamp(1_700_000_000 + seconds),
        ),
    ],
    ids=("refresh-naive-event-aware", "refresh-aware-event-naive"),
)
def test_gateway_refresh_note_claim_orders_mixed_datetime_awareness_exactly_once(
    after, event_at
):
    runner, _adapter = _make_runner()
    note = {
        "token": "token-a",
        "note": "[fresh context]",
        "after": after,
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [note]}
    event = _make_event("real next turn")
    event.internal = False

    event.timestamp = event_at(-1)
    assert runner._claim_refresh_context_note("session-a", event, 7) is None
    assert note["reserved_by"] is None

    event.timestamp = event_at(0)
    assert runner._claim_refresh_context_note("session-a", event, 7) is None
    assert note["reserved_by"] is None

    event.timestamp = event_at(1)
    assert runner._claim_refresh_context_note("session-a", event, 7) == {
        "token": "token-a",
        "note": "[fresh context]",
    }
    assert note["reserved_by"] == 7
    assert runner._claim_refresh_context_note("session-a", event, 8) is None


@pytest.mark.parametrize(
    ("event_timestamp", "after"),
    [
        ("not-a-datetime", datetime.now()),
        (datetime.now(), "not-a-datetime"),
        (None, datetime.now()),
        (datetime.now(), None),
    ],
)
def test_gateway_refresh_note_claim_rejects_malformed_timestamps_without_consuming(
    event_timestamp, after
):
    runner, _adapter = _make_runner()
    note = {
        "token": "token-a",
        "note": "[fresh context]",
        "after": after,
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [note]}
    event = _make_event("real next turn")
    event.internal = False
    event.timestamp = event_timestamp

    assert runner._claim_refresh_context_note("session-a", event, 7) is None
    assert note["reserved_by"] is None
    assert runner._pending_refresh_notes == {"session-a": [note]}


class _MalformedTimestamp(datetime):
    timestamp_result = None
    timestamp_error = None

    def timestamp(self):
        if self.timestamp_error is not None:
            raise self.timestamp_error
        return self.timestamp_result


def _malformed_datetime(result=None, error=None):
    value = _MalformedTimestamp(2026, 1, 1, tzinfo=timezone.utc)
    value.timestamp_result = result
    value.timestamp_error = error
    return value


class _HugeFractionTimestamp(datetime):
    def timestamp(self):
        return Fraction(10**1000, 1)


@pytest.mark.parametrize(
    "malformed",
    [
        _malformed_datetime("1700000000"),
        _malformed_datetime(float("nan")),
        _malformed_datetime(float("inf")),
        _malformed_datetime(float("-inf")),
        _malformed_datetime(True),
        _malformed_datetime(error=RuntimeError("broken timestamp")),
        _HugeFractionTimestamp(2026, 1, 1, tzinfo=timezone.utc),
    ],
    ids=("string", "nan", "positive-infinity", "negative-infinity", "bool", "raises", "huge-fraction"),
)
def test_gateway_refresh_note_claim_skips_bad_datetime_subclass_and_claims_later_record(
    malformed,
):
    runner, _adapter = _make_runner()
    event = _make_event("real next turn")
    event.internal = False
    valid_after = event.timestamp - timedelta(seconds=1)
    bad_note = {
        "token": "bad",
        "note": "BAD",
        "after": malformed,
        "generation": 7,
        "reserved_by": None,
    }
    valid_note = {
        "token": "valid",
        "note": "VALID",
        "after": valid_after,
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [bad_note, valid_note]}

    assert runner._claim_refresh_context_note("session-a", event, 7) == {
        "token": "valid",
        "note": "VALID",
    }
    assert bad_note["reserved_by"] is None
    assert valid_note["reserved_by"] == 7


@pytest.mark.parametrize(
    "malformed",
    [
        _malformed_datetime("1700000000"),
        _malformed_datetime(float("nan")),
        _malformed_datetime(float("inf")),
        _malformed_datetime(float("-inf")),
        _malformed_datetime(True),
        _malformed_datetime(error=RuntimeError("broken timestamp")),
        _HugeFractionTimestamp(2026, 1, 1, tzinfo=timezone.utc),
    ],
    ids=("string", "nan", "positive-infinity", "negative-infinity", "bool", "raises", "huge-fraction"),
)
def test_gateway_refresh_note_claim_rejects_bad_event_datetime_without_consuming(malformed):
    runner, _adapter = _make_runner()
    note = {
        "token": "valid",
        "note": "VALID",
        "after": datetime.now(),
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [note]}
    event = _make_event("real next turn")
    event.internal = False
    event.timestamp = malformed

    assert runner._claim_refresh_context_note("session-a", event, 7) is None
    assert note["reserved_by"] is None
    assert runner._pending_refresh_notes == {"session-a": [note]}


def test_gateway_refresh_note_claim_handles_naive_datetime_min_without_crashing():
    runner, _adapter = _make_runner()
    note = {
        "token": "minimum",
        "note": "MINIMUM",
        "after": datetime.min,
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [note]}
    event = _make_event("real next turn")
    event.internal = False

    claim = runner._claim_refresh_context_note("session-a", event, 7)

    assert claim in (None, {"token": "minimum", "note": "MINIMUM"})
    assert note["reserved_by"] is (None if claim is None else 7)


def test_gateway_refresh_note_claim_preserves_one_microsecond_near_datetime_max():
    runner, _adapter = _make_runner()
    after = datetime.max.replace(tzinfo=timezone.utc) - timedelta(microseconds=1)
    note = {
        "token": "boundary",
        "note": "BOUNDARY",
        "after": after,
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [note]}
    event = _make_event("real next turn")
    event.internal = False
    event.timestamp = after + timedelta(microseconds=1)

    assert runner._claim_refresh_context_note("session-a", event, 7) == {
        "token": "boundary",
        "note": "BOUNDARY",
    }


@pytest.mark.parametrize(
    "bad_record",
    [
        None,
        "not-a-record",
        {},
        {"token": "bad", "note": "BAD", "after": datetime.now(), "generation": "7", "reserved_by": None},
        {"token": "", "note": "BAD", "after": datetime.now(), "generation": 7, "reserved_by": None},
        {"token": "bad", "note": "BAD", "after": "yesterday", "generation": 7, "reserved_by": None},
        {"token": "bad", "note": "BAD", "after": datetime.now(), "generation": 7, "reserved_by": {}},
    ],
    ids=("none", "non-dict", "missing-fields", "bad-generation", "empty-token", "bad-after", "bad-reservation"),
)
def test_gateway_refresh_note_claim_skips_structurally_bad_record_before_valid(bad_record):
    runner, _adapter = _make_runner()
    event = _make_event("real next turn")
    event.internal = False
    valid = {
        "token": "valid",
        "note": "VALID",
        "after": event.timestamp - timedelta(seconds=1),
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [bad_record, valid]}

    assert runner._claim_refresh_context_note("session-a", event, 7) == {
        "token": "valid",
        "note": "VALID",
    }
    assert valid["reserved_by"] == 7
    if isinstance(bad_record, dict):
        assert bad_record.get("reserved_by") != 7


def test_gateway_refresh_note_claim_missing_token_never_reserves_record():
    runner, _adapter = _make_runner()
    event = _make_event("real next turn")
    event.internal = False
    missing_token = {
        "note": "BAD",
        "after": event.timestamp - timedelta(seconds=1),
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [missing_token]}

    assert runner._claim_refresh_context_note("session-a", event, 7) is None
    assert missing_token["reserved_by"] is None


def test_gateway_refresh_note_claim_is_atomic_across_concurrent_claimants():
    runner, _adapter = _make_runner()
    event = _make_event("real next turn")
    event.internal = False
    note = {
        "token": "only-token",
        "note": "ONLY",
        "after": event.timestamp - timedelta(seconds=1),
        "generation": 7,
        "reserved_by": None,
    }
    runner._pending_refresh_notes = {"session-a": [note]}
    barrier = Barrier(3)
    claims = []

    def claim(generation):
        barrier.wait()
        claims.append(runner._claim_refresh_context_note("session-a", event, generation))

    threads = [Thread(target=claim, args=(generation,)) for generation in (7, 8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    successful = [claim for claim in claims if claim is not None]
    assert successful == [{"token": "only-token", "note": "ONLY"}]
    assert note["reserved_by"] in (7, 8)
