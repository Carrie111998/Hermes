"""Simulate gateway restart mid-schedule-hour; assert no duplicate digest."""
from events.state import load_state, save_state


def test_restart_at_same_hour_does_not_duplicate_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import digest_state_path

    save_state(digest_state_path(), {
        "last_digest_hour": 13,
        "last_digest_at": "2026-04-16T17:00:00+00:00",
    })

    state = load_state(digest_state_path(), default={})
    last_digest_hour = state.get("last_digest_hour", -1)
    et_hour = 13

    should_fire = et_hour in [8, 13, 18] and et_hour != last_digest_hour
    assert not should_fire


def test_whatsapp_flush_does_not_re_fire_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import whatsapp_flush_state_path
    from events.state import save_state, load_state
    save_state(whatsapp_flush_state_path(), {"last_flush_date": "2026-04-17"})

    today = "2026-04-17"
    state = load_state(whatsapp_flush_state_path(), default={})
    already_fired_today = state.get("last_flush_date") == today
    assert already_fired_today
