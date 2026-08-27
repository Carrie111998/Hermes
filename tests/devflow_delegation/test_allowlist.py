import json

import pytest

from devflow_delegation.allowlist import (
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

CANARY_REAL = {
    "version": "1",
    "targets": {
        "sandbox": {
            "repo": "sandbox",
            "checkout_path": "~/devflow-sandbox",
            "default_branch": "main",
            "remote": "origin",
            "allowed_globs": ["src/**"],
            "denied_globs": ["**/.env", "secrets/**"],
            "worktree_base": "~/devflow-sandbox-worktrees",
            "test_commands": [["python", "-c", "print('ok')"]],
            "required_checks": ["test"],
            "risk_ceiling": "low",
            "max_autonomous_action": "create_pr",
            "executor_enabled": True,
            "canary_real": True,
            "implementation_command": ["python", "tools/apply.py"],
            "github_repo": "acme/sandbox",
            "pr_budget": 1,
            "live_gateway_imports": False,
        }
    },
}


def _canary_file(tmp_path, **target_overrides):
    data = json.loads(json.dumps(CANARY_REAL))
    data["targets"]["sandbox"].update(target_overrides)
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


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


def test_non_int_timeout_fails_closed(tmp_path):
    # A non-numeric command_timeout_seconds must fail closed as AllowlistError,
    # not leak a bare ValueError. The emitter catches AllowlistError to decline
    # (target_unresolved); a raw ValueError would bypass that fail-closed path.
    bad = json.loads(json.dumps(SAMPLE))
    bad["targets"]["hermes"]["command_timeout_seconds"] = "30m"
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(AllowlistError):
        load_allowlist(p)


def test_validation_argv_vectors_are_preserved(allowlist_file):
    raw = json.loads(allowlist_file.read_text(encoding="utf-8"))
    raw["targets"]["hermes"]["test_commands"] = [["python", "-c", "print('ok')"]]
    allowlist_file.write_text(json.dumps(raw), encoding="utf-8")

    target = resolve_target(load_allowlist(allowlist_file), "hermes")

    assert target.test_commands == (("python", "-c", "print('ok')"),)


def test_enabled_executor_requires_all_synthetic_safety_gates(allowlist_file):
    raw = json.loads(allowlist_file.read_text(encoding="utf-8"))
    raw["targets"]["hermes"]["executor_enabled"] = True
    allowlist_file.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AllowlistError):
        load_allowlist(allowlist_file)


def test_canary_real_target_loads_with_full_bounded_set(tmp_path):
    target = resolve_target(load_allowlist(_canary_file(tmp_path)), "sandbox")
    assert target is not None
    assert target.canary_real is True
    assert target.synthetic_fixture is False
    assert target.pr_budget == 1
    assert target.pr_budget_window_hours == 24  # default
    assert target.allowed_globs == ("src/**",)


def test_canary_real_defaults_pr_budget_to_one(tmp_path):
    data = json.loads(json.dumps(CANARY_REAL))
    del data["targets"]["sandbox"]["pr_budget"]
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    assert resolve_target(load_allowlist(p), "sandbox").pr_budget == 1


@pytest.mark.parametrize("mutation", [
    {"implementation_command": None},
    {"github_repo": ""},
    {"max_autonomous_action": "none"},
    {"worktree_base": ""},
    {"allowed_globs": []},
    {"risk_ceiling": "high"},
    {"pr_budget": 0},
    {"pr_budget_window_hours": 0},
    {"live_gateway_imports": True},
    {"synthetic_fixture": True},                  # mutually exclusive with canary_real
])
def test_enabled_canary_real_requires_every_bound(tmp_path, mutation):
    # Every bound is load-bearing on an ENABLED canary_real target: dropping any
    # one fails closed with AllowlistError.
    with pytest.raises(AllowlistError):
        load_allowlist(_canary_file(tmp_path, **mutation))


def test_disabled_canary_real_target_loads_without_running(tmp_path):
    # A disabled target is not an error — it simply never runs — so the bounded-set
    # checks (which gate only enabled executors) do not apply.
    target = resolve_target(load_allowlist(_canary_file(tmp_path, executor_enabled=False)), "sandbox")
    assert target is not None and target.executor_enabled is False


def test_non_int_pr_budget_fails_closed(tmp_path):
    with pytest.raises(AllowlistError):
        load_allowlist(_canary_file(tmp_path, pr_budget="lots"))


def test_path_allowed_enforces_allow_and_deny(allowlist_file):
    t = resolve_target(load_allowlist(allowlist_file), "hermes")
    assert path_allowed(t, "agent-src/events/bus.py") is True
    assert path_allowed(t, "docs/superpowers/specs/x.md") is True
    # denied even though it matches profiles/**
    assert path_allowed(t, "profiles/main/cron/jobs.json") is False
    assert path_allowed(t, "profiles/main/.env") is False
    # Python fnmatch does not let **/ consume zero directories, so the allowlist
    # matcher must cover both root and nested secrets/env paths.
    assert path_allowed(t, ".env") is False
    assert path_allowed(t, "agent-src/secrets/token.json") is False
    assert path_allowed(t, "secrets/token.json") is False
    # outside every allowed glob
    assert path_allowed(t, "bridges/hermes_to_devflow.py") is False


def test_agent_fields_have_conservative_defaults(tmp_path):
    target = resolve_target(load_allowlist(_canary_file(tmp_path)), "sandbox")
    assert target.agent_model == "z-ai/glm-5.3-flash"
    assert target.agent_max_iterations == 25
    assert target.agent_max_tokens == 200_000
    assert target.agent_max_files == 10
    assert target.agent_timeout_seconds == 900


def test_agent_fields_are_overridable(tmp_path):
    target = resolve_target(load_allowlist(_canary_file(
        tmp_path, agent_model="anthropic/claude-opus-5", agent_max_iterations=5,
        agent_max_tokens=1000, agent_max_files=2, agent_timeout_seconds=60,
    )), "sandbox")
    assert target.agent_model == "anthropic/claude-opus-5"
    assert target.agent_max_iterations == 5
    assert target.agent_max_tokens == 1000
    assert target.agent_max_files == 2
    assert target.agent_timeout_seconds == 60


@pytest.mark.parametrize("mutation", [
    {"agent_max_iterations": 0},
    {"agent_max_tokens": 0},
    {"agent_max_files": 0},
    {"agent_timeout_seconds": 0},
    {"agent_max_iterations": -1},
    {"agent_model": ""},
    {"agent_max_tokens": "lots"},
])
def test_invalid_agent_config_fails_closed(tmp_path, mutation):
    # Every agent bound is load-bearing: a zero/negative ceiling would remove the
    # bound entirely, and a blank model would silently fall back to provider defaults.
    with pytest.raises(AllowlistError):
        load_allowlist(_canary_file(tmp_path, **mutation))
