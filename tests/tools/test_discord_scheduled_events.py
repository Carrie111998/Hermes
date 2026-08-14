"""Tests for tools.discord_api.scheduled_events request builders."""

import pytest

from tools.discord_api.scheduled_events import (
    ScheduledEventError,
    create_scheduled_event_request,
    delete_scheduled_event_request,
    edit_scheduled_event_request,
    list_scheduled_events_request,
)

GUILD = "123456789012345678"
EVENT = "987654321098765432"
CHANNEL = "111222333444555666"

START = "2026-09-01T18:00:00+00:00"
END = "2026-09-01T21:00:00+00:00"

LONG_NAME_100 = "n" * 100
LONG_NAME_101 = "n" * 101
DESC_1000 = "d" * 1000
DESC_1001 = "d" * 1001


# ---------------------------------------------------------------- create


def test_create_minimal_external_defaults():
    req = create_scheduled_event_request(
        GUILD, name="Game Night", scheduled_start_time=START, scheduled_end_time=END
    )
    assert req["method"] == "POST"
    assert req["path"] == f"/guilds/{GUILD}/scheduled-events"
    assert req["json"] == {
        "name": "Game Night",
        "scheduled_start_time": START,
        "entity_type": 1,
        "privacy_level": 2,
        "scheduled_end_time": END,
    }


def test_create_stage_event_with_channel():
    req = create_scheduled_event_request(
        GUILD,
        name="Stage Show",
        scheduled_start_time=START,
        entity_type=2,
        channel_id=CHANNEL,
    )
    assert req["path"] == f"/guilds/{GUILD}/scheduled-events"
    assert req["json"]["entity_type"] == 2
    assert req["json"]["channel_id"] == CHANNEL
    assert "scheduled_end_time" not in req["json"]


def test_create_voice_event_with_channel():
    req = create_scheduled_event_request(
        GUILD,
        name="Voice Chat",
        scheduled_start_time=START,
        entity_type=3,
        channel_id=CHANNEL,
    )
    assert req["json"]["entity_type"] == 3
    assert req["json"]["channel_id"] == CHANNEL


def test_create_full_body():
    req = create_scheduled_event_request(
        GUILD,
        name="Movie Night",
        scheduled_start_time=START,
        entity_type=1,
        description="Watch classics",
        privacy_level=2,
        scheduled_end_time=END,
    )
    assert req["json"]["description"] == "Watch classics"
    assert req["json"]["privacy_level"] == 2
    assert "channel_id" not in req["json"]


def test_create_external_requires_end_time():
    with pytest.raises(ScheduledEventError):
        create_scheduled_event_request(GUILD, name="No End", scheduled_start_time=START)


def test_create_external_rejects_channel_id():
    with pytest.raises(ScheduledEventError):
        create_scheduled_event_request(
            GUILD,
            name="Bad Channel",
            scheduled_start_time=START,
            channel_id=CHANNEL,
            scheduled_end_time=END,
        )


def test_create_invalid_entity_type():
    with pytest.raises(ScheduledEventError):
        create_scheduled_event_request(
            GUILD, name="X", scheduled_start_time=START, entity_type=5
        )


def test_create_start_time_must_be_iso8601():
    for bad in ("not-a-time", "2026-13-99T99:99:99", "2026-09-01", 12345):
        with pytest.raises(ScheduledEventError):
            create_scheduled_event_request(
                GUILD,
                name="X",
                scheduled_start_time=bad,
                scheduled_end_time=END,
            )


def test_create_start_time_accepts_valid_variants():
    for variant in ("2026-09-01T18:00:00Z", "2026-09-01 18:00:00", "2026-09-01T18:00:00+02:00"):
        req = create_scheduled_event_request(
            GUILD,
            name="X",
            scheduled_start_time=variant,
            scheduled_end_time=END,
        )
        assert req["json"]["scheduled_start_time"] == variant


def test_create_end_time_validation():
    with pytest.raises(ScheduledEventError):
        create_scheduled_event_request(
            GUILD, name="X", scheduled_start_time=START, scheduled_end_time="tomorrow"
        )


def test_create_name_bounds():
    for bad in ("", LONG_NAME_101):
        with pytest.raises(ScheduledEventError):
            create_scheduled_event_request(
                GUILD, name=bad, scheduled_start_time=START, scheduled_end_time=END
            )
    # boundary values are fine
    assert create_scheduled_event_request(
        GUILD, name="n", scheduled_start_time=START, scheduled_end_time=END
    )["json"]["name"] == "n"
    assert create_scheduled_event_request(
        GUILD, name=LONG_NAME_100, scheduled_start_time=START, scheduled_end_time=END
    )["json"]["name"] == LONG_NAME_100


def test_create_description_max():
    assert create_scheduled_event_request(
        GUILD,
        name="X",
        scheduled_start_time=START,
        description=DESC_1000,
        scheduled_end_time=END,
    )["json"]["description"] == DESC_1000
    with pytest.raises(ScheduledEventError):
        create_scheduled_event_request(
            GUILD,
            name="X",
            scheduled_start_time=START,
            description=DESC_1001,
            scheduled_end_time=END,
        )


def test_create_privacy_level_must_be_2():
    with pytest.raises(ScheduledEventError):
        create_scheduled_event_request(
            GUILD,
            name="X",
            scheduled_start_time=START,
            privacy_level=1,
            scheduled_end_time=END,
        )


def test_create_snowflake_validation():
    for bad_guild in ("abc", -1, 2**63, 1.5, True, None):
        with pytest.raises(ScheduledEventError):
            create_scheduled_event_request(
                bad_guild, name="X", scheduled_start_time=START, scheduled_end_time=END
            )
    with pytest.raises(ScheduledEventError):
        create_scheduled_event_request(
            GUILD,
            name="X",
            scheduled_start_time=START,
            entity_type=2,
            channel_id="not-a-snowflake",
        )


# ----------------------------------------------------------------- edit


def test_edit_only_provided_fields():
    req = edit_scheduled_event_request(GUILD, EVENT, name="Renamed")
    assert req["method"] == "PATCH"
    assert req["path"] == f"/guilds/{GUILD}/scheduled-events/{EVENT}"
    assert req["json"] == {"name": "Renamed"}


def test_edit_multiple_fields_only_provided():
    req = edit_scheduled_event_request(
        GUILD, EVENT, name="Renamed", description="Updated desc"
    )
    assert req["json"] == {"name": "Renamed", "description": "Updated desc"}


def test_edit_no_fields_yields_empty_body():
    req = edit_scheduled_event_request(GUILD, EVENT)
    assert req["method"] == "PATCH"
    assert "json" not in req


def test_edit_accepts_nullable_clear():
    req = edit_scheduled_event_request(GUILD, EVENT, channel_id=None)
    assert req["json"] == {"channel_id": None}


def test_edit_validates_each_field():
    bad_calls = [
        {"name": ""},
        {"name": LONG_NAME_101},
        {"description": DESC_1001},
        {"entity_type": 9},
        {"privacy_level": 1},
        {"scheduled_start_time": "nope"},
        {"scheduled_end_time": "nope"},
        {"channel_id": "bogus"},
        {"unknown_field": 1},
    ]
    for kwargs in bad_calls:
        with pytest.raises(ScheduledEventError):
            edit_scheduled_event_request(GUILD, EVENT, **kwargs)


def test_edit_external_cannot_set_channel_id():
    with pytest.raises(ScheduledEventError):
        edit_scheduled_event_request(GUILD, EVENT, entity_type=1, channel_id=CHANNEL)


def test_edit_snowflake_validation():
    with pytest.raises(ScheduledEventError):
        edit_scheduled_event_request("bad", EVENT, name="X")
    with pytest.raises(ScheduledEventError):
        edit_scheduled_event_request(GUILD, -5, name="X")


# --------------------------------------------------------------- delete


def test_delete_request_shape():
    req = delete_scheduled_event_request(GUILD, EVENT)
    assert req == {"method": "DELETE", "path": f"/guilds/{GUILD}/scheduled-events/{EVENT}"}


def test_delete_validates_snowflakes():
    with pytest.raises(ScheduledEventError):
        delete_scheduled_event_request(GUILD, "abc")
    with pytest.raises(ScheduledEventError):
        delete_scheduled_event_request(2**63, EVENT)


# ----------------------------------------------------------------- list


def test_list_request_shape():
    req = list_scheduled_events_request(GUILD)
    assert req == {"method": "GET", "path": f"/guilds/{GUILD}/scheduled-events"}


def test_list_validates_guild_snowflake():
    with pytest.raises(ScheduledEventError):
        list_scheduled_events_request("not-a-guild")
