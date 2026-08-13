"""Tests for agent.error_classifier.jittered_wait — bounded multiplicative jitter.

S1-04: deterministic exponential backoff is a synchronized-retry-storm hazard
(aider #5165 class). jittered_wait spreads waits by a uniform(0.75, 1.25)
factor after clamping the base to [0, 3600] (JW-3). These tests assert the
BOUNDS and the CLAMP only — no sleeps, deterministic, fast.
"""

import time

import pytest

from agent.error_classifier import jittered_wait


SAMPLES = 500


def _sample(base, n=SAMPLES):
    return [jittered_wait(base) for _ in range(n)]


# ── Bounds ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("base", [0.01, 1.0, 60.0, 3600.0])
def test_stays_within_jitter_bounds(base):
    samples = _sample(base)
    lo, hi = 0.75 * base, 1.25 * base
    assert all(lo <= s <= hi for s in samples)


def test_never_zero_for_positive_base():
    assert all(s > 0 for s in _sample(1.0))


# ── Clamp (JW-3) ──────────────────────────────────────────────────────────

def test_high_base_clamped_to_3600():
    samples = _sample(1e9)
    assert all(2700.0 <= s <= 4500.0 for s in samples)


@pytest.mark.parametrize("base", [0.0, -5.0, float("-inf")])
def test_nonpositive_base_clamped_to_zero(base):
    assert _sample(base) == [0.0] * SAMPLES


# ── Statistical shape ─────────────────────────────────────────────────────

def test_mean_tracks_base():
    samples = _sample(100.0, n=2000)
    assert 97.0 <= sum(samples) / len(samples) <= 103.0


def test_spread_is_real():
    samples = _sample(60.0)
    assert len({round(s, 6) for s in samples}) > 1


# ── No sleeps ─────────────────────────────────────────────────────────────

def test_no_sleep_in_wait_path():
    start = time.monotonic()
    _sample(0.5, n=200)
    assert time.monotonic() - start < 2.0


# ── Call-site bound (transport recovery: min(jittered_wait(3+retry), 8)) ──

def test_transport_call_site_bounds():
    for retry_count in range(5):
        base = 3 + retry_count
        lo, hi = min(0.75 * base, 8.0), min(1.25 * base, 8.0)
        for _ in range(200):
            wait = min(jittered_wait(base), 8.0)
            assert lo <= wait <= hi
