"""Simulate gateway restart mid-schedule-hour; assert no duplicate digest."""
from events import gateway_integration as gi
from events.state import load_state, save_state


def test_restart_at_same_hour_does_not_duplicate_digest(tmp_path, monkeypatch):
    """After restart within the same scheduled hour, the digest must not
    re-fire.  State-persistence invariant for the digest catch-up logic."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import digest_state_path

    save_state(digest_state_path(), {
        "last_digest_key": "2026-04-19-13",
        "last_digest_at": "2026-04-19T17:00:00+00:00",
    })

    state = load_state(digest_state_path(), default={})
    last_digest_key = state.get("last_digest_key", "")

    # Gateway comes back up at 13h ET on the same day — the 13:00 digest
    # has already fired, so the helper must return None.
    target = gi._pick_digest_target(13, "2026-04-19", last_digest_key, [8, 13, 18])
    assert target is None


def test_restart_post_missed_hour_catches_up_once(tmp_path, monkeypatch):
    """Gateway was offline during the 8am window; comes up at 09:15.  The
    helper must return the 8am key so the missed digest fires on wake-up.
    Regression guard against the pre-2026-04-19 ``et_hour == 7`` equality
    check that dropped missed hours forever."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    target = gi._pick_digest_target(9, "2026-04-19", "", [8, 13, 18])
    assert target == "2026-04-19-08"


def test_whatsapp_flush_does_not_re_fire_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import whatsapp_flush_state_path
    from events.state import save_state, load_state
    save_state(whatsapp_flush_state_path(), {"last_flush_date": "2026-04-17"})

    today = "2026-04-17"
    state = load_state(whatsapp_flush_state_path(), default={})
    already_fired_today = state.get("last_flush_date") == today
    assert already_fired_today
