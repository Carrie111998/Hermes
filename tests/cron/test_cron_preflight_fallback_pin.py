"""Provider-key preflight must honor the per-job fallback pin.

Finding (engineering review, ufc-watch candidate): the pre-dispatch
provider-key probe consulted ONLY the global ``fallback_providers`` chain
(``get_fallback_chain(cfg)``), so a job pinned with an atomic
``fallback_provider`` + ``fallback_model`` pair was preflight-BLOCKED on a
missing primary key even though the auth-fallback path would have rescued
the run via the pin. The probe now consults the job's effective chain via
``_resolve_job_fallback_chain(job, cfg)`` — the same resolution run_job
itself uses.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so module reloads never touch the real home."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


def _raise_auth_error(monkeypatch):
    """Make primary-provider resolution fail with a missing credential."""
    from hermes_cli import runtime_provider as _rtp
    from hermes_cli.auth import AuthError

    def _boom(**_kw):
        raise AuthError("No API key found for provider 'test'", provider="test")

    monkeypatch.setattr(_rtp, "resolve_runtime_provider", _boom)


def test_pinned_fallback_without_global_chain_not_blocked(hermes_env, monkeypatch):
    """Pinned fallback + no global chain + missing primary key => the
    preflight probe must NOT block (the pin is the rescue path)."""
    import cron.scheduler as sched

    _raise_auth_error(monkeypatch)
    job = {
        "id": "j1",
        "provider": "test",
        "model": "m1",
        "fallback_provider": "other",
        "fallback_model": "m2",
    }
    assert sched._preflight_check_provider_key(job, {}) is None


def test_no_fallback_anywhere_still_blocks(hermes_env, monkeypatch):
    """No pin and no global chain + missing primary key => blocked, as
    before (pre-existing behavior preserved)."""
    import cron.scheduler as sched

    _raise_auth_error(monkeypatch)
    job = {"id": "j2", "provider": "test", "model": "m1"}
    reason = sched._preflight_check_provider_key(job, {})
    assert reason is not None
    assert "provider credential missing" in reason


def test_unpinned_job_still_follows_global_chain(hermes_env, monkeypatch):
    """Unrelated jobs keep global/default behavior: a configured global
    chain still skips the probe for a job without a pin."""
    import cron.scheduler as sched

    _raise_auth_error(monkeypatch)
    job = {"id": "j3", "provider": "test", "model": "m1"}
    cfg = {"fallback_providers": [{"provider": "other", "model": "m2"}]}
    assert sched._preflight_check_provider_key(job, cfg) is None
