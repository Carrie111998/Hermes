"""Env integration tests — managed .env applied last with override."""
import os

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
    (home / ".env").write_text("OPENAI_API_BASE=https://user.example/v1\n", encoding="utf-8")
    (managed / ".env").write_text("OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_managed_env_beats_shell(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    monkeypatch.setenv("OPENAI_API_BASE", "https://shell.example/v1")
    (managed / ".env").write_text("OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_managed_env_preserves_dotenv_stream_semantics(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    monkeypatch.setenv("MANAGED_SCOPE_EXPORT_SENTINEL", "user")
    (managed / ".env").write_bytes(
        b"export MANAGED_SCOPE_EXPORT_SENTINEL=managed\n"
        b"MANAGED_SCOPE_BASE=prefix\n"
        b"MANAGED_SCOPE_EXPANDED=${MANAGED_SCOPE_BASE}-suffix\n"
        b"MANAGED_SCOPE_COMMENTED=value # admin comment\n"
        b"MANAGED_SCOPE_LATIN1=caf\xe9\n"
    )

    load_hermes_dotenv(hermes_home=str(home))

    assert os.environ["MANAGED_SCOPE_EXPORT_SENTINEL"] == "managed"
    assert os.environ["MANAGED_SCOPE_EXPANDED"] == "prefix-suffix"
    assert os.environ["MANAGED_SCOPE_COMMENTED"] == "value"
    assert os.environ["MANAGED_SCOPE_LATIN1"] == "café"


def test_managed_env_reexpands_against_current_process_environment(
    env_homes,
    monkeypatch,
):
    from hermes_cli import managed_scope

    _home, managed = env_homes
    (managed / ".env").write_text(
        "MANAGED_SCOPE_DYNAMIC=${MANAGED_SCOPE_DYNAMIC_SOURCE}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MANAGED_SCOPE_DYNAMIC_SOURCE", "one")
    managed_scope.invalidate_managed_cache()
    assert managed_scope.load_managed_env()["MANAGED_SCOPE_DYNAMIC"] == "one"

    monkeypatch.setenv("MANAGED_SCOPE_DYNAMIC_SOURCE", "two")
    assert managed_scope.load_managed_env()["MANAGED_SCOPE_DYNAMIC"] == "two"


def test_managed_env_leaves_unmanaged_keys_alone(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    (home / ".env").write_text("USER_ONLY=keepme\n", encoding="utf-8")
    (managed / ".env").write_text("OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["USER_ONLY"] == "keepme"
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_no_managed_env_is_noop(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes  # managed dir exists but has no .env
    monkeypatch.setenv("SOME_VALUE", "from_shell")
    (home / ".env").write_text("SOME_VALUE=from_user\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["SOME_VALUE"] == "from_user"
