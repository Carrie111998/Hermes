"""Strict scheduler owner/provider policy contracts."""

from __future__ import annotations

import pytest


def _write(home, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, "auto"),
        ({"cron": {}}, "auto"),
        ({"cron": {"scheduler_owner": " gateway "}}, "gateway"),
        ({"cron": {"scheduler_owner": "DESKTOP"}}, "desktop"),
    ],
)
def test_owner_contract(config, expected):
    from cron.scheduler_provider import resolve_cron_scheduler_owner

    assert resolve_cron_scheduler_owner(config=config) == expected


@pytest.mark.parametrize("invalid", ["", "both", 42, None])
def test_malformed_owner_fails_closed(invalid):
    from cron.scheduler_provider import resolve_cron_scheduler_owner

    assert (
        resolve_cron_scheduler_owner(config={"cron": {"scheduler_owner": invalid}})
        is None
    )


@pytest.mark.parametrize(
    "body",
    [
        'cron:\n  scheduler_owner: "unterminated\n',
        "cron: desktop\n",
        "cron:\n  scheduler_owner: both\n",
        "null\n",
    ],
)
def test_malformed_and_explicit_null_files_fail_closed(tmp_path, body):
    from cron.scheduler_runtime import read_scheduler_ownership_policy_strict

    _write(tmp_path, body)
    assert read_scheduler_ownership_policy_strict(hermes_home=tmp_path) is None


@pytest.mark.parametrize("body", ["", "# comments only\n", "---\n# comment\n"])
def test_empty_files_use_safe_auto_builtin(tmp_path, body):
    from cron.scheduler_runtime import read_scheduler_ownership_policy_strict

    _write(tmp_path, body)
    policy = read_scheduler_ownership_policy_strict(hermes_home=tmp_path)
    assert policy is not None
    assert (policy.mode, policy.configured_provider) == ("auto", "builtin")


def test_exact_home_env_expansion_and_managed_precedence(tmp_path, monkeypatch):
    from cron.scheduler_runtime import read_scheduler_ownership_policy_strict

    selected = tmp_path / "selected"
    poison = tmp_path / "poison"
    managed = tmp_path / "managed"
    _write(selected, "cron:\n  scheduler_owner: ${OWNER}\n  provider: builtin\n")
    _write(poison, "cron:\n  scheduler_owner: desktop\n")
    _write(managed, "cron:\n  scheduler_owner: gateway\n  provider: ${PROVIDER}\n")
    monkeypatch.setenv("HERMES_HOME", str(poison))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("OWNER", "desktop")
    monkeypatch.setenv("PROVIDER", "chronos")

    policy = read_scheduler_ownership_policy_strict(hermes_home=selected)
    assert policy is not None
    assert (policy.mode, policy.configured_provider) == ("gateway", "chronos")


def test_default_config_documents_safe_auto():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["cron"]["scheduler_owner"] == "auto"
