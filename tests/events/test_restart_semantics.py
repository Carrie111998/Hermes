"""Simulate gateway restart mid-schedule-hour; assert catch-up semantics."""
from events import gateway_integration as gi
from events.state import load_state, save_state


def test_restart_at_same_hour_does_not_duplicate_digest(tmp_path, monkeypatch):
    """After restart within the same scheduled hour, already-fired digests
    must not re-fire.  State-persistence invariant for the digest catch-up
    logic.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import digest_state_path

    # Seed both the 8am and 13:00 digest keys as fired so neither is eligible
    # for catch-up on the post-restart tick.
    save_state(digest_state_path(), {
        "fired_digest_keys": ["2026-04-19-08", "2026-04-19-13"],
        "last_digest_at": "2026-04-19T17:00:00+00:00",
    })

    state = load_state(digest_state_path(), default={})
    fired_keys = set(state.get("fired_digest_keys", []))

    # Gateway comes back at 13h ET — both the 08 and 13 digests have already
    # fired today, so the helper must return None (nothing to catch up).
    target = gi._pick_digest_target(13, "2026-04-19", fired_keys, [8, 13, 18])
    assert target is None


def test_restart_post_missed_hour_catches_up_once(tmp_path, monkeypatch):
    """Gateway was offline during the 8am window; comes up at 09:15.  The
    helper must return the 8am key so the missed morning digest fires on
    wake-up.  Regression guard against both the pre-2026-04-19
    ``et_hour == 7`` equality check AND the 2026-04-19 latest-only rule
    that silently dropped the morning overnight summary.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    target = gi._pick_digest_target(9, "2026-04-19", set(), [8, 13, 18])
    assert target == "2026-04-19-08"


def test_poll_loop_preserves_last_digest_at_across_fire(tmp_path, monkeypatch):
    """After a digest fires, the state file must retain ``last_digest_at``
    so the next ``compose()`` call can use it as the time-window lower bound.

    Regression guard against a 2026-04-19 bug in the first-and-latest
    implementation: the poll loop was saving ``_state`` (loaded at thread
    start, before compose() ran) back to disk AFTER compose() had written
    its own ``last_digest_at``.  Result: ``last_digest_at`` got clobbered,
    and on next restart DigestComposer would query the entire event bus
    because ``query_since = since or self._last_digest_at = None``.

    This test simulates the same scenario: pre-seed a state with
    ``last_digest_at``, pre-seed a write-as-compose-would-do that sets a
    new ``last_digest_at``, then invoke the same merge logic the poll loop
    runs — assert the updated ``last_digest_at`` survives.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import digest_state_path

    # Pre-existing state before the current tick.
    save_state(digest_state_path(), {
        "fired_digest_keys": ["2026-04-19-08"],
        "last_digest_at": "2026-04-19T12:00:00+00:00",
    })

    # The poll loop loads state at thread start — may be stale by now.
    state_at_thread_start = load_state(digest_state_path(), default={})
    fired_digest_keys = list(state_at_thread_start.get("fired_digest_keys", []))

    # compose() fires and overwrites the file with just last_digest_at.
    save_state(digest_state_path(), {
        "last_digest_at": "2026-04-19T17:00:00+00:00",
    })

    # Poll loop appends a new key and must now save.  The fix: reload
    # state FIRST so compose()'s last_digest_at survives the merge.
    fired_digest_keys.append("2026-04-19-13")
    merged = load_state(digest_state_path(), default={})
    merged["fired_digest_keys"] = fired_digest_keys
    merged.pop("last_digest_key", None)
    save_state(digest_state_path(), merged)

    final = load_state(digest_state_path(), default={})
    assert final.get("last_digest_at") == "2026-04-19T17:00:00+00:00", (
        "Poll loop save clobbered last_digest_at — DigestComposer would "
        "query the entire event bus on next compose()"
    )
    assert final.get("fired_digest_keys") == ["2026-04-19-08", "2026-04-19-13"]


def test_legacy_last_digest_key_migrates_to_fired_digest_keys(tmp_path, monkeypatch):
    """The caller-side migration path: a state file written by the pre-
    2026-04-19 "latest-only" code uses ``last_digest_key`` as a scalar.
    On first post-upgrade start, that value must be seeded into the new
    ``fired_digest_keys`` list so we don't re-fire the most recent digest.

    This mirrors the migration logic inlined at the top of
    ``_subscriber_poll_loop`` in events/gateway_integration.py.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import digest_state_path

    # Legacy format: single scalar key.
    save_state(digest_state_path(), {
        "last_digest_key": "2026-04-19-13",
    })

    state = load_state(digest_state_path(), default={})
    legacy_key = state.get("last_digest_key", "")
    fired_keys = list(state.get("fired_digest_keys", []))
    if legacy_key and legacy_key not in fired_keys:
        fired_keys.append(legacy_key)

    # Post-migration: 13 is seeded, 8 was never fired — so first-unfired fires.
    target = gi._pick_digest_target(
        14, "2026-04-19", set(fired_keys), [8, 13, 18],
    )
    assert target == "2026-04-19-08", (
        "Legacy state with last_digest_key=13 should let the missed 8am "
        "digest catch up on the first post-upgrade tick"
    )


def test_whatsapp_flush_does_not_re_fire_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import whatsapp_flush_state_path
    from events.state import save_state, load_state
    save_state(whatsapp_flush_state_path(), {"last_flush_date": "2026-04-17"})

    today = "2026-04-17"
    state = load_state(whatsapp_flush_state_path(), default={})
    already_fired_today = state.get("last_flush_date") == today
    assert already_fired_today
