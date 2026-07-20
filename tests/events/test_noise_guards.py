"""Tests for events.noise_guards (v3 P4/P6)."""

from events.noise_guards import FlapGuard, RepeatGuard, is_noop_cron_output


# ----------------------------------------------------------------- noop guard

def test_empty_output_is_noop():
    assert is_noop_cron_output("")
    assert is_noop_cron_output(None)
    assert is_noop_cron_output("   \n  ")


def test_silent_marker_only_is_noop():
    assert is_noop_cron_output("[SILENT]")
    assert is_noop_cron_output("[SILENT] ")
    assert is_noop_cron_output("[silent]")


def test_noop_phrases():
    assert is_noop_cron_output("no work")
    assert is_noop_cron_output("Nothing to do.")
    assert is_noop_cron_output("OK")


def test_content_after_silent_marker_survives():
    assert not is_noop_cron_output(
        "[SILENT] errors=1 — first error: GET https://api.github.com/...")


def test_substantive_output_is_not_noop():
    assert not is_noop_cron_output("synced 42 rows in 3.1s")


# --------------------------------------------------------------- repeat guard

def test_repeat_suppressed_within_window():
    g = RepeatGuard(window_seconds=1800)
    assert not g.is_repeat("t1", "devflow-bridge: tick", now=0)
    assert g.is_repeat("t1", "devflow-bridge: tick", now=300)
    assert g.suppressed_count == 1


def test_repeat_allowed_after_window():
    g = RepeatGuard(window_seconds=1800)
    assert not g.is_repeat("t1", "msg", now=0)
    assert not g.is_repeat("t1", "msg", now=1801)


def test_sliding_window_keeps_suppressing_steady_stream():
    """A message repeating every 5 min never re-delivers (the window slides)."""
    g = RepeatGuard(window_seconds=1800)
    assert not g.is_repeat("t1", "m", now=0)
    for i in range(1, 20):
        assert g.is_repeat("t1", "m", now=i * 300)


def test_different_topics_independent():
    g = RepeatGuard()
    assert not g.is_repeat("t1", "m", now=0)
    assert not g.is_repeat("t2", "m", now=1)


def test_lru_bounded():
    g = RepeatGuard(max_entries=10)
    for i in range(50):
        g.is_repeat("t", f"m{i}", now=i)
    assert len(g._seen) <= 10


# ----------------------------------------------------------------- flap guard

def test_same_state_reannounce_suppressed():
    g = FlapGuard()
    assert g.observe("wa", "down", now=0).deliver
    assert not g.observe("wa", "down", now=60).deliver
    assert not g.observe("wa", "down", now=120).deliver


def test_state_change_delivers():
    g = FlapGuard()
    assert g.observe("wa", "down", now=0).deliver
    d = g.observe("wa", "up", now=60)
    assert d.deliver and d.note is None


def test_flapping_collapses_then_mutes():
    g = FlapGuard(window_seconds=900, flap_threshold=4, mute_seconds=1800)
    assert g.observe("wa", "down", now=0).deliver
    assert g.observe("wa", "up", now=100).deliver
    assert g.observe("wa", "down", now=200).deliver
    d = g.observe("wa", "up", now=300)  # 4th transition in window
    assert d.deliver and "flapping" in d.note
    # muted now
    assert not g.observe("wa", "down", now=400).deliver
    assert not g.observe("wa", "up", now=500).deliver


def test_post_mute_stabilization_note():
    g = FlapGuard(window_seconds=900, flap_threshold=4, mute_seconds=1800)
    for i, s in enumerate(["down", "up", "down", "up"]):
        g.observe("wa", s, now=i * 100)
    assert not g.observe("wa", "down", now=500).deliver  # muted
    d = g.observe("wa", "up", now=500 + 1801)
    assert d.deliver
    assert "stabilized" in d.note
    assert "up" in d.note


def test_keys_independent():
    g = FlapGuard()
    g.observe("wa", "down", now=0)
    d = g.observe("telegram", "down", now=1)
    assert d.deliver
