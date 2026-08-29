"""Truthful fallback-chain wording + per-job fallback chain resolution.

Incident (2026-08-29, ufc-watch): the job's only configured fallback
resolved to the SAME backend as the primary (zai/glm-5.3 on both), so the
agent's skip predicate skipped it and no fallback was ever tried — yet the
delivered alert claimed "Fallback chain was exhausted or unavailable.",
sending the operator to debug a fallback failure that never happened.

Three wording branches, all grounded in agent.backend_identity's
should_skip_candidate (the same predicate the runtime skip uses):

* same-backend skipped  → every chain entry resolves to the primary's
  deployment; nothing was tried; say so and give the remediation.
* no alternate configured → empty chain; existing guidance wording.
* alternate attempted but failed → at least one genuinely different
  backend exists; the legacy "exhausted or unavailable." is honest.

Plus the per-job pin (fallback_provider + fallback_model) which REPLACES
the global chain for that job only.
"""

import cron.scheduler as scheduler
from cron.scheduler import (
    _fallback_chain_phrase,
    _resolve_job_fallback_chain,
    _summarize_cron_failure_for_delivery,
)

ZAI_CHAIN = [{"provider": "zai", "model": "glm-5.3"}]
REAL_ALT_CHAIN = [{"provider": "openrouter", "model": "anthropic/claude-sonnet-5"}]


def _patch_cfg(monkeypatch, chain):
    monkeypatch.setattr(scheduler, "load_config", lambda: {"fallback_providers": chain})
    monkeypatch.setattr(scheduler, "get_fallback_chain", lambda cfg: list(chain))


# ---------------------------------------------------------------------------
# Branch 1: same-backend skipped (the incident shape)
# ---------------------------------------------------------------------------


def test_same_backend_fallback_is_not_reported_as_exhausted(monkeypatch):
    """EXACT incident shape: primary zai/glm-5.3, only fallback zai/glm-5.3."""
    _patch_cfg(monkeypatch, ZAI_CHAIN)
    job = {"name": "ufc-watch", "id": "fc73758f1e75",
           "provider": "zai", "model": "glm-5.3"}
    msg = _summarize_cron_failure_for_delivery(job, "Request timed out.")
    assert "provider timeout" in msg
    assert "same backend" in msg.lower()
    assert "no fallback was ever tried" in msg.lower()
    assert "exhausted or unavailable" not in msg
    assert "No fallback chain configured" not in msg


def test_same_backend_all_entries_skipped_wording(monkeypatch):
    _patch_cfg(
        monkeypatch,
        [
            {"provider": "zai", "model": "glm-5.3"},
            {"provider": "zai", "model": "glm-5.3", "base_url": ""},
        ],
    )
    job = {"id": "j1", "provider": "zai", "model": "glm-5.3"}
    phrase = _fallback_chain_phrase(job)
    assert "same backend" in phrase.lower()
    assert "exhausted" not in phrase


# ---------------------------------------------------------------------------
# Branch 2: no alternate configured (pre-existing, kept honest)
# ---------------------------------------------------------------------------


def test_no_chain_guidance_unchanged(monkeypatch):
    _patch_cfg(monkeypatch, [])
    job = {"id": "j1", "provider": "zai", "model": "glm-5.3"}
    phrase = _fallback_chain_phrase(job)
    assert "No fallback chain configured" in phrase


# ---------------------------------------------------------------------------
# Branch 3: alternate attempted but failed (legacy wording is honest)
# ---------------------------------------------------------------------------


def test_real_alternate_keeps_exhausted_wording(monkeypatch):
    _patch_cfg(monkeypatch, REAL_ALT_CHAIN)
    job = {"id": "j1", "provider": "zai", "model": "glm-5.3"}
    phrase = _fallback_chain_phrase(job)
    assert phrase == "Fallback chain was exhausted or unavailable."


def test_mixed_chain_with_one_real_alternate_keeps_exhausted(monkeypatch):
    _patch_cfg(monkeypatch, ZAI_CHAIN + REAL_ALT_CHAIN)
    job = {"id": "j1", "provider": "zai", "model": "glm-5.3"}
    assert _fallback_chain_phrase(job) == "Fallback chain was exhausted or unavailable."


def test_unprovable_job_identity_fails_open_to_legacy(monkeypatch):
    """Unpinned job with no snapshots: identity unknown → legacy wording,
    never a guess."""
    _patch_cfg(monkeypatch, ZAI_CHAIN)
    job = {"id": "j1", "name": "unpinned"}
    assert _fallback_chain_phrase(job) == "Fallback chain was exhausted or unavailable."


def test_snapshot_axes_used_when_unpinned(monkeypatch):
    """Unpinned job: creation-time drift snapshots supply the identity."""
    _patch_cfg(monkeypatch, ZAI_CHAIN)
    job = {
        "id": "j1",
        "provider_snapshot": "zai",
        "model_snapshot": "glm-5.3",
    }
    phrase = _fallback_chain_phrase(job)
    assert "same backend" in phrase.lower()


def test_config_error_still_fails_open(monkeypatch):
    def _raise():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(scheduler, "load_config", _raise)
    job = {"id": "j1", "provider": "zai", "model": "glm-5.3"}
    assert _fallback_chain_phrase(job) == "Fallback chain was exhausted or unavailable."


# ---------------------------------------------------------------------------
# Per-job fallback pin (fix 4): resolution + wording integration
# ---------------------------------------------------------------------------


def test_per_job_pin_replaces_global_chain(monkeypatch):
    _patch_cfg(monkeypatch, REAL_ALT_CHAIN)
    job = {"fallback_provider": "openai-codex", "fallback_model": "gpt-5.5"}
    assert _resolve_job_fallback_chain(job, {}) == [
        {"provider": "openai-codex", "model": "gpt-5.5"}
    ]


def test_unpinned_job_follows_global_chain(monkeypatch):
    _patch_cfg(monkeypatch, REAL_ALT_CHAIN)
    assert _resolve_job_fallback_chain({"id": "j"}, {}) == REAL_ALT_CHAIN


def test_per_job_pin_drives_same_backend_wording(monkeypatch):
    """Global chain has a real alternate, but the job's PIN is same-backend:
    the job-effective chain decides the wording."""
    _patch_cfg(monkeypatch, REAL_ALT_CHAIN)
    job = {
        "id": "j1",
        "provider": "zai",
        "model": "glm-5.3",
        "fallback_provider": "zai",
        "fallback_model": "glm-5.3",
    }
    phrase = _fallback_chain_phrase(job)
    assert "same backend" in phrase.lower()
    assert "exhausted" not in phrase


def test_per_job_pin_different_backend_keeps_exhausted(monkeypatch):
    _patch_cfg(monkeypatch, [])
    job = {
        "id": "j1",
        "provider": "zai",
        "model": "glm-5.3",
        "fallback_provider": "openai-codex",
        "fallback_model": "gpt-5.5",
    }
    assert _fallback_chain_phrase(job) == "Fallback chain was exhausted or unavailable."
