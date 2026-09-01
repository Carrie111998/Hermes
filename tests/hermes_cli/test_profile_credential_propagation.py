"""Cross-profile propagation of a rotated provider credential.

``hermes model`` / ``hermes setup`` only write the ACTIVE profile's ``.env``.
A rotated provider key is one credential for the whole machine, so sibling
profiles that share the provider keep running on the dead key. These tests
drive the real propagation path against real on-disk fixtures in a temp
HERMES_HOME: default home + sibling profiles under ``profiles/``.

All fake secrets are constructed at runtime so no key-shaped literal ever
lands in the repo.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

ENV_VAR = "DEEPSEEK_API_KEY"
OLD_KEY = "dk-" + "a" * 24
NEW_KEY = "dk-" + "b" * 24


@pytest.fixture
def multi_profile_home(monkeypatch, tmp_path):
    """HERMES_HOME at the default home, with sibling profiles pre-created.

    Layout:

        <home>/.env                      — active (default) home, OLD_KEY
        <home>/profiles/alpha/.env       — OLD_KEY (should be updated)
        <home>/profiles/beta/.env        — var absent (must stay untouched)
        <home>/profiles/gamma/.env       — NEW_KEY already (in sync)
        <home>/profiles/empty/           — profile without .env (skipped)
        <home>/profiles/.deleted/        — tombstone dir (must be ignored)
    """
    home = tmp_path / "multi_home"
    home.mkdir()
    profiles = home / "profiles"
    profiles.mkdir()
    (profiles / ".deleted").mkdir()

    (home / ".env").write_text(f"{ENV_VAR}={OLD_KEY}\n", encoding="utf-8")
    for name, key in (("alpha", OLD_KEY), ("beta", ""), ("gamma", NEW_KEY)):
        p = profiles / name
        p.mkdir()
        # beta gets an .env WITHOUT the var — it must stay untouched.
        (p / ".env").write_text(
            f"{ENV_VAR}={key}\n" if key else "OTHER_TOKEN=zz\n", encoding="utf-8"
        )
    (profiles / "empty").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.config import invalidate_env_cache

    invalidate_env_cache()
    return home


def _invalidate():
    from hermes_cli.config import invalidate_env_cache

    invalidate_env_cache()


def _read_env_value(path):
    from hermes_cli.config import load_env

    token = None
    from hermes_constants import set_hermes_home_override

    token = set_hermes_home_override(str(path.parent))
    try:
        return load_env().get(ENV_VAR)
    finally:
        from hermes_constants import reset_hermes_home_override

        reset_hermes_home_override(token)


# ---------------------------------------------------------------------------
# propagate_provider_env_credential_to_profiles
# ---------------------------------------------------------------------------


def test_propagation_updates_stale_sibling_only(multi_profile_home):
    from hermes_cli.credential_lifecycle import (
        propagate_provider_env_credential_to_profiles,
    )

    result = propagate_provider_env_credential_to_profiles(ENV_VAR, NEW_KEY)

    assert result["updated"] == ["alpha"]
    assert "gamma" in result["in_sync"]
    assert "beta" in result["skipped"]
    # Alpha got the new key; gamma still has it; beta stayed untouched.
    assert _read_env_value(multi_profile_home / "profiles" / "alpha" / ".env") == NEW_KEY
    assert _read_env_value(multi_profile_home / "profiles" / "gamma" / ".env") == NEW_KEY
    assert _read_env_value(multi_profile_home / "profiles" / "beta" / ".env") is None
    # A profile without .env must not gain one.
    assert not (multi_profile_home / "profiles" / "empty" / ".env").exists()
    # The active (default) home keeps its own value — propagation never
    # touches the home the save already wrote.
    assert _read_env_value(multi_profile_home / ".env") == OLD_KEY


def test_propagation_from_profile_updates_default_home(monkeypatch, tmp_path):
    home = tmp_path / "root"
    home.mkdir()
    (home / ".env").write_text(f"{ENV_VAR}={OLD_KEY}\n", encoding="utf-8")
    worker = home / "profiles" / "worker"
    worker.mkdir(parents=True)
    (worker / ".env").write_text(f"{ENV_VAR}=dk-{'c' * 24}\n", encoding="utf-8")
    # sister profile, also stale
    sister = home / "profiles" / "sister"
    sister.mkdir()
    (sister / ".env").write_text(f"{ENV_VAR}={OLD_KEY}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(worker))
    _invalidate()

    from hermes_cli.credential_lifecycle import (
        propagate_provider_env_credential_to_profiles,
    )

    result = propagate_provider_env_credential_to_profiles(ENV_VAR, NEW_KEY)

    assert sorted(result["updated"]) == ["default", "sister"]
    assert _read_env_value(home / ".env") == NEW_KEY
    assert _read_env_value(sister / ".env") == NEW_KEY
    # The active profile's own .env was NOT rewritten by propagation (the
    # wizard already wrote the key there before propagating).
    assert _read_env_value(worker / ".env") != NEW_KEY


def test_dry_run_writes_nothing(multi_profile_home):
    from hermes_cli.credential_lifecycle import (
        propagate_provider_env_credential_to_profiles,
    )

    result = propagate_provider_env_credential_to_profiles(
        ENV_VAR, NEW_KEY, apply=False
    )

    assert result["updated"] == ["alpha"]
    assert _read_env_value(multi_profile_home / "profiles" / "alpha" / ".env") == OLD_KEY


def test_duplicate_key_lines_are_collapsed(multi_profile_home):
    """A target .env with the key assigned twice keeps dead until collapsed.

    load_env() lets the LAST assignment win, so a stale duplicate line
    resurrects the old value even after the first line was updated. The
    propagation write must leave exactly ONE assignment behind.
    """
    alpha_env = multi_profile_home / "profiles" / "alpha" / ".env"
    alpha_env.write_text(
        f"{ENV_VAR}={OLD_KEY}\nOTHER_TOKEN=zz\n{ENV_VAR}=dk-{NEW_KEY[3:]}x\n",
        encoding="utf-8",
    )

    from hermes_cli.credential_lifecycle import (
        propagate_provider_env_credential_to_profiles,
    )

    result = propagate_provider_env_credential_to_profiles(ENV_VAR, NEW_KEY)

    assert result["updated"] == ["alpha"]
    text = alpha_env.read_text(encoding="utf-8")
    assert text.count(f"{ENV_VAR}=") == 1
    assert f"{ENV_VAR}={NEW_KEY}" in text
    assert "OTHER_TOKEN=zz" in text
    assert _read_env_value(alpha_env) == NEW_KEY


def test_config_yaml_mirror_scrubbed_per_profile(multi_profile_home):
    """A sibling's config.yaml mirror of the OLD key follows the rotation."""
    alpha = multi_profile_home / "profiles" / "alpha"
    (alpha / "config.yaml").write_text(
        f"model:\n  api_key: {OLD_KEY}\n", encoding="utf-8"
    )

    from hermes_cli.credential_lifecycle import (
        propagate_provider_env_credential_to_profiles,
    )

    result = propagate_provider_env_credential_to_profiles(ENV_VAR, NEW_KEY)

    assert result["updated"] == ["alpha"]
    text = (alpha / "config.yaml").read_text(encoding="utf-8")
    assert OLD_KEY not in text
    assert NEW_KEY in text


# ---------------------------------------------------------------------------
# _prompt_api_key — the wizard offers propagation after a key save
# ---------------------------------------------------------------------------


def _pconfig():
    from hermes_cli.auth import PROVIDER_REGISTRY

    return PROVIDER_REGISTRY["deepseek"]


def test_prompt_replace_offers_and_applies_propagation(multi_profile_home, capsys):
    from hermes_cli import main as m

    with patch("builtins.input", side_effect=["r", "y"]), patch(
        "hermes_cli.secret_prompt.masked_secret_prompt", return_value=NEW_KEY
    ):
        key, abort = m._prompt_api_key(
            _pconfig(), OLD_KEY, provider_id="deepseek"
        )

    assert key == NEW_KEY
    assert abort is False
    assert (
        _read_env_value(multi_profile_home / "profiles" / "alpha" / ".env") == NEW_KEY
    )
    out = capsys.readouterr().out
    assert "stale value: alpha" in out
    assert "Key propagated to: alpha" in out


def test_prompt_replace_propagation_declined_by_default(multi_profile_home, capsys):
    from hermes_cli import main as m

    with patch("builtins.input", side_effect=["r", ""]), patch(
        "hermes_cli.secret_prompt.masked_secret_prompt", return_value=NEW_KEY
    ):
        key, abort = m._prompt_api_key(
            _pconfig(), OLD_KEY, provider_id="deepseek"
        )

    assert key == NEW_KEY
    assert abort is False
    # Sibling profiles keep the old key when the user declines.
    assert (
        _read_env_value(multi_profile_home / "profiles" / "alpha" / ".env") == OLD_KEY
    )


def test_prompt_no_siblings_no_extra_prompt(tmp_path, monkeypatch, capsys):
    """Without sibling profiles the flow must not ask anything extra."""
    home = tmp_path / "solo_home"
    home.mkdir()
    (home / ".env").write_text(f"{ENV_VAR}={OLD_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _invalidate()

    from hermes_cli import main as m

    inputs = []

    def fake_input(prompt=""):
        inputs.append(prompt)
        return "r"

    with patch("builtins.input", side_effect=fake_input), patch(
        "hermes_cli.secret_prompt.masked_secret_prompt", return_value=NEW_KEY
    ):
        key, abort = m._prompt_api_key(
            _pconfig(), OLD_KEY, provider_id="deepseek"
        )

    assert key == NEW_KEY
    assert abort is False
    # Exactly one prompt: the K/R/C menu. No propagation follow-up.
    assert len(inputs) == 1
    assert "Propagate" not in capsys.readouterr().out


def test_save_env_value_collapses_duplicate_key_lines(tmp_path, monkeypatch):
    """save_env_value must leave exactly ONE assignment per key.

    load_env() lets the LAST matching line win, so keeping a later duplicate
    after updating the first would resurrect the old value on next load.
    """
    home = tmp_path / "solo_home"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text(
        f"{ENV_VAR}={OLD_KEY}\nOTHER_TOKEN=zz\n{ENV_VAR}=dk-{'e' * 24}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    _invalidate()

    from hermes_cli.config import save_env_value

    save_env_value(ENV_VAR, NEW_KEY)

    text = env_file.read_text(encoding="utf-8")
    assert text.count(f"{ENV_VAR}=") == 1
    assert f"{ENV_VAR}={NEW_KEY}" in text
    assert "OTHER_TOKEN=zz" in text
    _invalidate()
    from hermes_cli.config import load_env

    assert load_env()[ENV_VAR] == NEW_KEY


def test_prompt_first_time_key_offers_propagation(multi_profile_home, capsys):
    """First-time key entry also propagates when accepted."""
    (multi_profile_home / ".env").write_text("", encoding="utf-8")
    _invalidate()

    from hermes_cli import main as m

    with patch("builtins.input", side_effect=["y"]), patch(
        "hermes_cli.secret_prompt.masked_secret_prompt", return_value=NEW_KEY
    ):
        key, abort = m._prompt_api_key(
            _pconfig(), "", provider_id="deepseek"
        )

    assert key == NEW_KEY
    assert abort is False
    assert (
        _read_env_value(multi_profile_home / "profiles" / "alpha" / ".env") == NEW_KEY
    )
    assert "Key propagated to: alpha" in capsys.readouterr().out