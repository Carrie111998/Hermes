"""Cross-profile gateway restart environment isolation."""

from pathlib import Path

import pytest

import hermes_cli.gateway as gateway


def test_cross_profile_restart_env_replaces_source_profile_secrets(
    monkeypatch, tmp_path: Path
):
    source_home = tmp_path / "hermes"
    target_home = source_home / "profiles" / "testing"
    source_home.mkdir()
    target_home.mkdir(parents=True)

    (source_home / ".env").write_text(
        "DISCORD_BOT_TOKEN=source-token\n"
        "DISCORD_ALLOWED_USERS=source-user\n"
        "CUSTOM_PLUGIN_SECRET=source-secret\n",
        encoding="utf-8",
    )
    (target_home / ".env").write_text(
        "OPENROUTER_API_KEY=target-provider-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_PROFILE_NAME", "default")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "inherited-source-token")
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "inherited-source-user")
    monkeypatch.setenv("CUSTOM_PLUGIN_SECRET", "inherited-source-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "inherited-source-provider-key")
    monkeypatch.setenv("PATH", "safe-path")

    restart_env = gateway._profile_gateway_restart_env("testing")

    assert "DISCORD_BOT_TOKEN" not in restart_env
    assert "DISCORD_ALLOWED_USERS" not in restart_env
    assert "CUSTOM_PLUGIN_SECRET" not in restart_env
    assert restart_env["OPENROUTER_API_KEY"] == "target-provider-key"
    assert restart_env["HERMES_HOME"] == str(target_home.resolve())
    assert restart_env["HERMES_PROFILE"] == "testing"
    assert restart_env["HERMES_PROFILE_NAME"] == "testing"
    assert restart_env["PATH"] == "safe-path"


def test_same_profile_restart_env_preserves_shell_credentials(monkeypatch, tmp_path):
    profile_home = tmp_path / "hermes" / "profiles" / "testing"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "shell-only-token")

    restart_env = gateway._profile_gateway_restart_env("testing")

    assert restart_env["DISCORD_BOT_TOKEN"] == "shell-only-token"


def test_named_profile_restart_to_default_pins_default_identity(monkeypatch, tmp_path):
    default_home = tmp_path / "hermes"
    source_home = default_home / "profiles" / "testing"
    source_home.mkdir(parents=True)

    (source_home / ".env").write_text(
        "DISCORD_BOT_TOKEN=source-token\n",
        encoding="utf-8",
    )
    (default_home / ".env").write_text(
        "OPENROUTER_API_KEY=default-provider-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    monkeypatch.setenv("HERMES_PROFILE", "testing")
    monkeypatch.setenv("HERMES_PROFILE_NAME", "testing")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "inherited-source-token")

    restart_env = gateway._profile_gateway_restart_env("default")

    assert "DISCORD_BOT_TOKEN" not in restart_env
    assert restart_env["OPENROUTER_API_KEY"] == "default-provider-key"
    assert restart_env["HERMES_HOME"] == str(default_home.resolve())
    assert restart_env["HERMES_PROFILE"] == "default"
    assert restart_env["HERMES_PROFILE_NAME"] == "default"


def test_profile_restart_passes_isolated_env_to_watcher(monkeypatch):
    isolated_env = {"PATH": "safe-path", "HERMES_HOME": "target-home"}
    popen_calls = []

    monkeypatch.setattr(
        gateway,
        "_gateway_run_args_for_profile",
        lambda _profile: ["python", "-m", "hermes_cli.main", "gateway", "run"],
    )
    monkeypatch.setattr(
        gateway,
        "_profile_gateway_restart_env",
        lambda _profile: isolated_env,
    )
    monkeypatch.setattr(
        gateway.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )

    assert gateway.launch_detached_profile_gateway_restart("testing", 1234) is True

    assert len(popen_calls) == 1
    assert popen_calls[0][1]["env"] is isolated_env


@pytest.mark.parametrize(
    "profile_args",
    [
        ["--profile", "testing"],
        ["-p", "testing"],
        ["--profile=testing"],
    ],
)
def test_captured_profile_cmdline_restart_uses_isolated_env(
    monkeypatch, profile_args
):
    isolated_env = {"PATH": "safe-path", "HERMES_HOME": "target-home"}
    profile_calls = []
    spawn_calls = []
    run_argv = [
        "python",
        "-m",
        "hermes_cli.main",
        *profile_args,
        "gateway",
        "run",
    ]

    monkeypatch.setattr(
        gateway,
        "_profile_gateway_restart_env",
        lambda profile: profile_calls.append(profile) or isolated_env,
    )
    monkeypatch.setattr(
        gateway,
        "_spawn_gateway_restart_watcher",
        lambda *args, **kwargs: spawn_calls.append((args, kwargs)) or True,
    )

    assert gateway.launch_detached_gateway_restart_by_cmdline(1234, run_argv) is True

    assert profile_calls == ["testing"]
    assert spawn_calls == [((1234, run_argv), {"watcher_env": isolated_env})]


def test_attached_short_profile_is_not_treated_as_a_hermes_selector():
    run_argv = [
        "python",
        "-m",
        "hermes_cli.main",
        "-ptesting",
        "gateway",
        "run",
    ]

    # Keep this aligned with main._apply_profile_override(), which accepts
    # ``-p testing`` and ``--profile=testing`` but not argparse's attached
    # short-option spelling.
    assert gateway._profile_from_gateway_argv(run_argv) is None
