"""Env integration tests — managed .env applied last with override."""

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def env_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return home, managed


def test_managed_env_beats_user_env(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    (home / ".env").write_text(
        "OPENAI_API_BASE=https://user.example/v1\n", encoding="utf-8"
    )
    (managed / ".env").write_text(
        "OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8"
    )
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_no_managed_env_is_noop(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes  # managed dir exists but has no .env
    monkeypatch.setenv("SOME_VALUE", "from_shell")
    (home / ".env").write_text("SOME_VALUE=from_user\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["SOME_VALUE"] == "from_user"


def test_unreadable_managed_env_exists_fail_open(env_homes, monkeypatch, caplog):
    """PermissionError on managed .env.exists() must not brick dotenv load.

    Mirrors /etc/hermes present but .env not stat-readable to the process user.
    Uses a fake Path so tests never touch real /etc/hermes.
    """
    from hermes_cli import managed_scope
    from hermes_cli.env_loader import load_hermes_dotenv

    home, _managed = env_homes
    (home / ".env").write_text(
        "OPENAI_API_BASE=https://user.example/v1\n", encoding="utf-8"
    )
    monkeypatch.setenv("OPENAI_API_BASE", "https://shell.example/v1")

    fake_env = MagicMock(spec=Path)
    fake_env.__str__.return_value = "/etc/hermes/.env"
    fake_env.exists.side_effect = PermissionError(
        13, "Permission denied", "/etc/hermes/.env"
    )

    fake_dir = MagicMock(spec=Path)
    fake_dir.__truediv__.return_value = fake_env
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: fake_dir)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.env_loader"):
        loaded = load_hermes_dotenv(hermes_home=str(home))

    assert loaded == [home / ".env"]
    # User env still applied; managed must not raise or partially apply.
    assert os.environ["OPENAI_API_BASE"] == "https://user.example/v1"
    assert any("managed" in r.getMessage().lower() for r in caplog.records)
    # Never log secret values from a managed file body.
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "sk-" not in joined
    assert "secret" not in joined.lower()


def test_unreadable_managed_env_load_oserror_fail_open(env_homes, monkeypatch):
    """OSError while loading a present managed .env must fail open."""
    from hermes_cli import env_loader, managed_scope
    from hermes_cli.env_loader import load_hermes_dotenv

    home, _managed = env_homes
    (home / ".env").write_text("SOME_VALUE=from_user\n", encoding="utf-8")

    fake_env = MagicMock(spec=Path)
    fake_env.__str__.return_value = str(_managed / ".env")
    fake_env.exists.return_value = True

    fake_dir = MagicMock(spec=Path)
    fake_dir.__truediv__.return_value = fake_env
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: fake_dir)

    real_sanitize = env_loader._sanitize_env_file_if_needed
    real_load = env_loader._load_dotenv_with_fallback

    def _is_managed(path) -> bool:
        return path is fake_env or str(path) == str(fake_env)

    def _sanitize(path):
        if _is_managed(path):
            return None
        return real_sanitize(path)

    def _load(path, override=False):
        # Only the managed path should fail; user/project dotenv stays normal.
        if _is_managed(path):
            raise OSError(13, "Permission denied", str(fake_env))
        return real_load(path, override=override)

    monkeypatch.setattr(env_loader, "_sanitize_env_file_if_needed", _sanitize)
    monkeypatch.setattr(env_loader, "_load_dotenv_with_fallback", _load)

    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["SOME_VALUE"] == "from_user"


def test_managed_env_still_overrides_after_fail_open_path(env_homes, monkeypatch):
    """Valid managed .env must still apply last with override (precedence intact)."""
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    monkeypatch.setenv("OPENAI_API_BASE", "https://shell.example/v1")
    (home / ".env").write_text(
        "OPENAI_API_BASE=https://user.example/v1\nOTHER=user-only\n",
        encoding="utf-8",
    )
    (managed / ".env").write_text(
        "OPENAI_API_BASE=https://org.example/v1\n",
        encoding="utf-8",
    )
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"
    assert os.environ["OTHER"] == "user-only"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory execute bits")
def test_untraversable_managed_dir_fail_open_real_fs(env_homes, monkeypatch):
    """Real FS: managed dir without +x makes .env.exists() raise PermissionError.

    get_managed_dir() still resolves the directory (is_dir on the dir itself
    succeeds), then Path.exists() on the child raises — the original production
    failure mode when /etc/hermes is present but not fully traversable.
    """
    from hermes_cli import managed_scope
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    (home / ".env").write_text("SOME_VALUE=from_user\n", encoding="utf-8")
    (managed / ".env").write_text("SOME_VALUE=from_managed\n", encoding="utf-8")
    managed_scope.invalidate_managed_cache()

    managed.chmod(0o600)  # rw, no execute → children not stat-able
    try:
        # Sanity: this is the exact raise the production bug hit.
        with pytest.raises(PermissionError):
            (managed / ".env").exists()
        load_hermes_dotenv(hermes_home=str(home))
    finally:
        managed.chmod(0o700)

    assert os.environ["SOME_VALUE"] == "from_user"
