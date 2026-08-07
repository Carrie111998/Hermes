import json

import pytest

from devflow_delegation.allowlist import (
    Allowlist,
    AllowlistError,
    load_allowlist,
    path_allowed,
    resolve_target,
)

SAMPLE = {
    "version": "1",
    "targets": {
        "hermes": {
            "repo": "hermes",
            "checkout_path": "~/.hermes",
            "default_branch": "main",
            "remote": "origin",
            "allowed_globs": ["agent-src/**", "scripts/**", "profiles/**", "skills/**", "docs/**"],
            "denied_globs": ["profiles/main/cron/jobs.json", "**/.env", "**/secrets/**"],
            "worktree_base": "~/.hermes/devflow/worktrees",
            "test_commands": ["python -m pytest tests -x -q"],
            "command_timeout_seconds": 1800,
            "required_checks": ["pytest"],
            "risk_ceiling": "medium",
            "max_autonomous_action": "none",
            "live_gateway_imports": True,
            "owners": ["diego"],
            "notify_route": "devflow_firehose",
        }
    },
}


@pytest.fixture
def allowlist_file(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    return p


def test_load_and_resolve(allowlist_file):
    al = load_allowlist(allowlist_file)
    assert al.version == "1"
    t = resolve_target(al, "hermes")
    assert t is not None
    assert t.default_branch == "main"
    assert t.live_gateway_imports is True
    assert resolve_target(al, "not-on-the-list") is None


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(AllowlistError):
        load_allowlist(tmp_path / "nope.json")


def test_malformed_file_fails_closed(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(AllowlistError):
        load_allowlist(p)


def test_path_allowed_enforces_allow_and_deny(allowlist_file):
    t = resolve_target(load_allowlist(allowlist_file), "hermes")
    assert path_allowed(t, "agent-src/events/bus.py") is True
    assert path_allowed(t, "docs/superpowers/specs/x.md") is True
    # denied even though it matches profiles/**
    assert path_allowed(t, "profiles/main/cron/jobs.json") is False
    assert path_allowed(t, "profiles/main/.env") is False
    assert path_allowed(t, "agent-src/secrets/token.json") is False
    # outside every allowed glob
    assert path_allowed(t, "bridges/hermes_to_devflow.py") is False
