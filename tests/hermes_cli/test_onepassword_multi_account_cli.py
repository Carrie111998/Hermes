"""CLI coverage for per-reference 1Password account routing."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from rich.console import Console

from hermes_cli import onepassword_secrets_cli as op_cli


def test_set_accepts_account_and_token_env_and_persists_structured_mapping(monkeypatch):
    parser = argparse.ArgumentParser()
    op_cli.register_cli(parser)
    args = parser.parse_args(
        [
            "set",
            "WORK_API_KEY",
            "op://Work/Hermes/key",
            "--account",
            "work.1password.com",
            "--token-env",
            "OP_TOKEN_WORK",
        ]
    )
    cfg = {"secrets": {"onepassword": {"enabled": True, "env": {}}}}
    saved = []
    monkeypatch.setattr(op_cli, "load_config", lambda: cfg)
    monkeypatch.setattr(op_cli, "save_config", lambda value: saved.append(value))

    assert args.func(args) == 0
    assert saved == [cfg]
    assert cfg["secrets"]["onepassword"]["env"]["WORK_API_KEY"] == {
        "reference": "op://Work/Hermes/key",
        "account": "work.1password.com",
        "service_account_token_env": "OP_TOKEN_WORK",
    }


def test_set_explicit_empty_token_env_selects_desktop_auth(monkeypatch):
    parser = argparse.ArgumentParser()
    op_cli.register_cli(parser)
    args = parser.parse_args(
        [
            "set",
            "PERSONAL_API_KEY",
            "op://Personal/Hermes/key",
            "--account",
            "personal.1password.com",
            "--token-env",
            "",
        ]
    )
    cfg = {"secrets": {"onepassword": {"enabled": True, "env": {}}}}
    monkeypatch.setattr(op_cli, "load_config", lambda: cfg)
    monkeypatch.setattr(op_cli, "save_config", lambda value: None)

    assert args.func(args) == 0
    assert cfg["secrets"]["onepassword"]["env"]["PERSONAL_API_KEY"] == {
        "reference": "op://Personal/Hermes/key",
        "account": "personal.1password.com",
        "service_account_token_env": "",
    }


def test_token_accepts_account_and_token_env_overrides(monkeypatch, tmp_path):
    parser = argparse.ArgumentParser()
    op_cli.register_cli(parser)
    args = parser.parse_args(
        [
            "token",
            "--account",
            "work.1password.com",
            "--token-env",
            "OP_TOKEN_WORK",
            "--token",
            "candidate-token",
        ]
    )
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    saved_env = {}
    seen = {}
    monkeypatch.setattr(
        op_cli,
        "load_config",
        lambda: {"secrets": {"onepassword": {"enabled": True}}},
    )
    monkeypatch.setattr(op_cli.op_src, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setattr(op_cli.op_src, "clear_caches", lambda *a, **k: None)
    monkeypatch.setattr(
        op_cli,
        "save_env_value",
        lambda name, value: saved_env.__setitem__(name, value),
    )

    def fake_whoami(binary, account, *, token_value=""):
        seen.update(account=account, token_value=token_value)
        return "authenticated"

    monkeypatch.setattr(op_cli, "_op_whoami", fake_whoami)

    assert args.func(args) == 0
    assert saved_env == {"OP_TOKEN_WORK": "candidate-token"}
    assert seen == {
        "account": "work.1password.com",
        "token_value": "candidate-token",
    }


def test_candidate_token_whoami_uses_isolated_auth_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("OP_SESSION_personal", "personal-session")
    monkeypatch.setenv("OP_ACCOUNT", "personal.1password.com")
    monkeypatch.setenv("OP_CONNECT_HOST", "https://connect.invalid")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "connect-secret")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(cmd=cmd, env=kwargs["env"], stdin=kwargs["stdin"])
        return subprocess.CompletedProcess(cmd, 0, stdout="work account\n", stderr="")

    monkeypatch.setattr(op_cli.subprocess, "run", fake_run)

    assert op_cli._op_whoami(
        Path("/fake/op"),
        "work.1password.com",
        token_value="work-candidate",
    ) == "work account"
    assert captured["env"]["OP_SERVICE_ACCOUNT_TOKEN"] == "work-candidate"
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "OP_SESSION_personal" not in captured["env"]
    assert "OP_ACCOUNT" not in captured["env"]
    assert "OP_CONNECT_HOST" not in captured["env"]
    assert "OP_CONNECT_TOKEN" not in captured["env"]
    assert captured["stdin"] is subprocess.DEVNULL


def test_token_explicit_empty_account_overrides_profile_default(monkeypatch, tmp_path):
    parser = argparse.ArgumentParser()
    op_cli.register_cli(parser)
    args = parser.parse_args(
        [
            "token",
            "--account",
            "",
            "--token-env",
            "OP_TOKEN_PERSONAL",
            "--token",
            "candidate-token",
        ]
    )
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    seen = {}
    monkeypatch.setattr(
        op_cli,
        "load_config",
        lambda: {
            "secrets": {
                "onepassword": {
                    "enabled": True,
                    "account": "work.1password.com",
                }
            }
        },
    )
    monkeypatch.setattr(op_cli.op_src, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setattr(op_cli.op_src, "clear_caches", lambda: None)
    monkeypatch.setattr(op_cli, "save_env_value", lambda name, value: None)

    def fake_whoami(binary, account, *, token_value=""):
        seen["account"] = account
        return "authenticated"

    monkeypatch.setattr(op_cli, "_op_whoami", fake_whoami)

    assert args.func(args) == 0
    assert seen["account"] == ""


def test_status_checks_auth_readiness_per_effective_route(
    monkeypatch, tmp_path, capsys
):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setenv("OP_TOKEN_WORK", "configured")
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setattr(
        op_cli,
        "load_config",
        lambda: {
            "secrets": {
                "onepassword": {
                    "enabled": True,
                    "env": {
                        "WORK": {
                            "reference": "op://Work/Service/credential",
                            "account": "work.1password.com",
                            "service_account_token_env": "OP_TOKEN_WORK",
                        }
                    },
                }
            }
        },
    )
    monkeypatch.setattr(op_cli.op_src, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setattr(
        op_cli,
        "_op_whoami",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("token-backed routes must not probe desktop auth")
        ),
    )

    assert op_cli.cmd_status(argparse.Namespace()) == 0
    assert "skip 1Password" not in capsys.readouterr().out


def test_status_normalizes_null_and_non_string_default_token_env(
    monkeypatch, capsys
):
    monkeypatch.setattr(op_cli.op_src, "find_op", lambda binary_path="": None)
    for raw_value, expected in (
        (None, "OP_SERVICE_ACCOUNT_TOKEN"),
        (123, "123"),
    ):
        monkeypatch.setattr(
            op_cli,
            "load_config",
            lambda raw_value=raw_value: {
                "secrets": {
                    "onepassword": {
                        "enabled": False,
                        "service_account_token_env": raw_value,
                    }
                }
            },
        )

        assert op_cli.cmd_status(argparse.Namespace()) == 0
        assert expected in capsys.readouterr().out


def test_status_marks_inherited_account_and_token_fields_independently(
    monkeypatch, capsys
):
    monkeypatch.setattr(op_cli, "Console", lambda: Console(width=180))
    monkeypatch.setattr(
        op_cli,
        "load_config",
        lambda: {
            "secrets": {
                "onepassword": {
                    "enabled": False,
                    "account": "default.1password.com",
                    "service_account_token_env": "OP_TOKEN_DEFAULT",
                    "env": {
                        "ACCOUNT_ONLY": {
                            "reference": "op://Work/Service/credential",
                            "account": "work.1password.com",
                        },
                        "TOKEN_ONLY": {
                            "reference": "op://Personal/Service/credential",
                            "service_account_token_env": "OP_TOKEN_PERSONAL",
                        },
                    },
                }
            }
        },
    )
    monkeypatch.setattr(op_cli.op_src, "find_op", lambda binary_path="": None)

    assert op_cli.cmd_status(argparse.Namespace()) == 0
    output = capsys.readouterr().out
    assert "OP_TOKEN_DEFAULT *" in output
    assert "default.1password.com *" in output


def test_status_probes_structured_desktop_route_with_strict_auth(
    monkeypatch, capsys
):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "unrelated-token")
    monkeypatch.setenv("OP_SESSION_unrelated", "unrelated-session")
    monkeypatch.setattr(
        op_cli,
        "load_config",
        lambda: {
            "secrets": {
                "onepassword": {
                    "enabled": True,
                    "env": {
                        "DESKTOP": {
                            "reference": "op://Vault/Item/field",
                            "service_account_token_env": "",
                        }
                    },
                }
            }
        },
    )
    monkeypatch.setattr(
        op_cli.op_src, "find_op", lambda binary_path="": Path("/fake/op")
    )
    monkeypatch.setattr(op_cli, "_op_version", lambda _binary: "2.0")
    calls = []

    def fake_whoami(binary, account, *, token_value="", strict_auth=False):
        calls.append((account, token_value, strict_auth))
        return None

    monkeypatch.setattr(op_cli, "_op_whoami", fake_whoami)

    assert op_cli.cmd_status(argparse.Namespace()) == 0
    capsys.readouterr()
    assert calls == [("", "", True)]
