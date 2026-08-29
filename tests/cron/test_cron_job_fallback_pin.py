"""Per-job fallback pin (fallback_provider + fallback_model) — data layer.

The pin is an ATOMIC pair, conditionally persisted (absent keys = job
follows the global fallback_providers chain; existing records stay
byte-identical). Scheduler consumption lives in
cron.scheduler._resolve_job_fallback_chain (covered in
test_cron_fallback_chain_phrase.py); these tests pin the
create/update/persist/validate contract and the cronjob tool wiring.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    return home


def test_create_requires_atomic_pair(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="atomic pair"):
        create_job(prompt="p", schedule="every 5m",
                   fallback_provider="openai-codex")
    with pytest.raises(ValueError, match="atomic pair"):
        create_job(prompt="p", schedule="every 5m",
                   fallback_model="gpt-5.5")


def test_create_persists_pair_conditionally(hermes_env):
    from cron.jobs import create_job, get_job

    pinned = create_job(
        prompt="p", schedule="every 5m",
        fallback_provider="openai-codex", fallback_model="gpt-5.5",
    )
    reloaded = get_job(pinned["id"])
    assert reloaded["fallback_provider"] == "openai-codex"
    assert reloaded["fallback_model"] == "gpt-5.5"

    plain = create_job(prompt="p", schedule="every 5m")
    reloaded_plain = get_job(plain["id"])
    assert "fallback_provider" not in reloaded_plain
    assert "fallback_model" not in reloaded_plain


def test_update_set_and_clear_pair(hermes_env):
    from cron.jobs import create_job, get_job, update_job

    job = create_job(prompt="p", schedule="every 5m")
    update_job(job["id"], {
        "fallback_provider": "openai-codex",
        "fallback_model": "gpt-5.5",
    })
    assert get_job(job["id"])["fallback_provider"] == "openai-codex"

    # Clearing both axes removes the keys entirely (legacy shape).
    update_job(job["id"], {"fallback_provider": "", "fallback_model": ""})
    reloaded = get_job(job["id"])
    assert "fallback_provider" not in reloaded
    assert "fallback_model" not in reloaded


def test_update_rejects_half_pair(hermes_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt="p", schedule="every 5m")
    with pytest.raises(ValueError, match="atomic pair"):
        update_job(job["id"], {"fallback_provider": "openai-codex"})
    # Failed update leaves the stored record untouched.
    from cron.jobs import get_job
    assert "fallback_provider" not in get_job(job["id"])


def test_cronjob_tool_create_and_update_fallback_pin(hermes_env):
    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob

    created = json.loads(
        cronjob(
            action="create",
            prompt="p",
            schedule="every 5m",
            fallback_provider="openai-codex",
            fallback_model="gpt-5.5",
            deliver="local",
        )
    )
    assert created.get("success") is True
    assert created["job"]["fallback_provider"] == "openai-codex"
    assert created["job"]["fallback_model"] == "gpt-5.5"

    updated = json.loads(
        cronjob(
            action="update",
            job_id=created["job_id"],
            fallback_provider="",
            fallback_model="",
        )
    )
    assert updated.get("success") is True
    reloaded = get_job(created["job_id"])
    assert "fallback_provider" not in reloaded


def test_cronjob_tool_rejects_half_pair(hermes_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(
            action="create",
            prompt="p",
            schedule="every 5m",
            fallback_provider="openai-codex",
            deliver="local",
        )
    )
    assert result.get("success") is False
    assert "atomic pair" in result.get("error", "")


def test_model_dispatch_cannot_set_fallback_pin(hermes_env):
    """Standing spend-routing policy: like model/provider, the model-facing
    cronjob handler must NOT accept fallback pins from agent arguments."""
    from tools.cronjob_tools import _cronjob_handler

    out = json.loads(
        _cronjob_handler(
            {
                "action": "create",
                "prompt": "p",
                "schedule": "every 5m",
                "deliver": "local",
                # Attempted pin via model args — must be ignored.
                "fallback_provider": "openai-codex",
                "fallback_model": "gpt-5.5",
            }
        )
    )
    assert out.get("success") is True
    from cron.jobs import get_job
    reloaded = get_job(out["job_id"])
    assert "fallback_provider" not in reloaded
    assert "fallback_model" not in reloaded
