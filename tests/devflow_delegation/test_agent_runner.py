import json
import os
from types import SimpleNamespace

import pytest

from devflow_delegation.agent_policy import CeilingExceeded
from devflow_delegation.agent_runner import build_messages, dispatch_tool, run_agent
from devflow_delegation.allowlist import Allowlist, TargetConfig
from devflow_delegation.contract import parse_request
from devflow_delegation.executor import run_executor_tick
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.lifecycle import transition


def _target(**over):
    values = dict(
        repo="fixture", checkout_path="/unused",
        allowed_globs=("src/**",), denied_globs=("**/.env",),
        test_commands=(("python", "-c", "print('tests passed')"),),
        agent_model="test/model", agent_max_iterations=5,
        agent_max_tokens=10_000, agent_max_files=3, agent_timeout_seconds=60,
    )
    values.update(over)
    return TargetConfig(**values)


def _request():
    return {
        "request_id": "dwr_test",
        "request": {
            "title": "Fix the greeting",
            "problem_statement": "greet() returns the wrong string.",
            "acceptance_criteria": ["greet() returns 'hello'"],
        },
    }


@pytest.fixture
def worktree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def greet():\n    return 'bye'\n", encoding="utf-8")
    return tmp_path


def _message(content=None, tool_calls=None, total_tokens=10):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           usage=SimpleNamespace(total_tokens=total_tokens))


def _call(call_id, name, args):
    return SimpleNamespace(id=call_id,
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def test_build_messages_marks_the_request_as_untrusted_data(worktree):
    messages = build_messages(_request(), _target())
    system = messages[0]["content"]
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    # The request is free text from a producer; it must never be read as instructions.
    assert "untrusted" in system.lower()
    assert "instructions" in system.lower()
    assert "src/**" in system


def test_build_messages_includes_the_problem_and_acceptance_criteria():
    body = build_messages(_request(), _target())[1]["content"]
    assert "greet() returns the wrong string." in body
    assert "greet() returns 'hello'" in body


def test_dispatch_tool_returns_a_tool_error_as_text_not_an_exception(worktree):
    # A refusal must go back to the model so it can correct, not kill the run.
    result = dispatch_tool("write_file", {"path": "tools/evil.py", "content": "x"},
                           worktree=worktree, target=_target())
    assert "allowed scope" in result
    assert not (worktree / "tools" / "evil.py").exists()


def test_dispatch_tool_rejects_an_unknown_tool(worktree):
    assert "unknown tool" in dispatch_tool("rm_rf", {}, worktree=worktree, target=_target()).lower()


def test_run_agent_applies_a_write_and_stops_when_the_model_finishes(worktree):
    responses = [
        _message(tool_calls=[_call("1", "write_file",
                                   {"path": "src/app.py", "content": "def greet():\n    return 'hello'\n"})]),
        _message(content="Fixed the greeting."),
    ]

    def provider_call(**kwargs):
        return responses.pop(0)

    result = run_agent(worktree=worktree, target=_target(), request=_request(),
                       provider_call=provider_call)

    assert result["stopped"] == "model-finished"
    assert result["iterations"] == 2
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "def greet():\n    return 'hello'\n"


def test_run_agent_trips_the_iteration_ceiling(worktree):
    def provider_call(**kwargs):
        # A model that never finishes must not loop forever.
        return _message(tool_calls=[_call("1", "list_files", {"pattern": "**/*"})])

    with pytest.raises(CeilingExceeded, match="iterations"):
        run_agent(worktree=worktree, target=_target(agent_max_iterations=3),
                  request=_request(), provider_call=provider_call)


def test_run_agent_passes_the_configured_model_and_tools(worktree):
    seen = {}

    def provider_call(**kwargs):
        seen.update(kwargs)
        return _message(content="done")

    run_agent(worktree=worktree, target=_target(), request=_request(), provider_call=provider_call)

    assert seen["model"] == "test/model"
    assert {s["function"]["name"] for s in seen["tools"]} == {
        "read_file", "list_files", "write_file", "run_tests"}


# --- C1: dispatch_tool must never raise on valid-but-non-dict JSON args ---


@pytest.mark.parametrize("bad_args", [[], None, 5, "x", "y", True])
def test_dispatch_tool_handles_non_dict_args_without_raising(worktree, bad_args):
    # "[]", "null", "5", "\"x\"" are all syntactically valid JSON that parse to
    # something other than a dict. dispatch_tool must treat that as "no
    # arguments supplied" and hand text back, never raise AttributeError.
    for tool_name in ("read_file", "list_files", "write_file", "run_tests"):
        result = dispatch_tool(tool_name, bad_args, worktree=worktree, target=_target())
        assert isinstance(result, str)
        assert "object" in result.lower()


@pytest.mark.parametrize("raw_arguments", ["[]", "null", "5", '"x"'])
def test_run_agent_survives_valid_json_non_dict_tool_arguments(worktree, raw_arguments):
    # The model emitted syntactically valid JSON that isn't an object. The
    # loop must not crash: it should feed a tool result back and continue.
    call = SimpleNamespace(id="1", function=SimpleNamespace(name="list_files", arguments=raw_arguments))
    responses = [
        _message(tool_calls=[call]),
        _message(content="done after malformed args"),
    ]

    def provider_call(**kwargs):
        return responses.pop(0)

    result = run_agent(worktree=worktree, target=_target(), request=_request(), provider_call=provider_call)

    assert result["stopped"] == "model-finished"
    assert result["iterations"] == 2


def test_run_agent_survives_syntactically_invalid_json_tool_arguments(worktree):
    call = SimpleNamespace(id="1", function=SimpleNamespace(name="list_files", arguments="{nope"))
    responses = [
        _message(tool_calls=[call]),
        _message(content="done after bad json"),
    ]

    def provider_call(**kwargs):
        return responses.pop(0)

    result = run_agent(worktree=worktree, target=_target(), request=_request(), provider_call=provider_call)

    assert result["stopped"] == "model-finished"
    assert result["iterations"] == 2


# --- I1: run_tests must ignore any model-supplied kwargs ---


def test_dispatch_tool_run_tests_ignores_model_supplied_arguments(worktree):
    hostile_args = {"timeout_seconds": 0, "cmd": "rm -rf /", "extra": 1}
    result = dispatch_tool("run_tests", hostile_args, worktree=worktree, target=_target())
    assert "PASSED" in result
    assert "tests passed" in result
    assert "rm -rf" not in result


# --- I3: a provider response with missing/None usage must not crash ---
#
# F3 (final whole-branch review): the original assertion here was
# `result["tokens"] == 0`, which pinned the very bug the review flagged --
# a provider that never reports `usage` left `budget.tokens` permanently at
# 0, so the token ceiling in Budget.tick could never trip for that provider
# (only iteration/wall-clock still bounded the loop). The fix makes `_tokens`
# fall back to a small chars/4 estimate of the response instead of a bare 0
# when usage is missing/None, so the ceiling stays live. That estimate is
# necessarily nonzero for non-empty content ("done" -> 1), so these two tests
# are updated from `== 0` to `> 0` -- the invariant they protect ("must not
# crash on missing usage") is unchanged and still asserted via
# `stopped == "model-finished"` completing normally; only the stale exact
# value is corrected. See test_run_agent_tokens_estimate_can_trip_the_ceiling
# below for the new behavior's actual point: the ceiling is live again.


def test_run_agent_tokens_estimate_a_nonzero_value_when_usage_is_none(worktree):
    message = SimpleNamespace(content="done", tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    def provider_call(**kwargs):
        return response

    result = run_agent(worktree=worktree, target=_target(), request=_request(), provider_call=provider_call)

    assert result["tokens"] > 0
    assert result["stopped"] == "model-finished"


def test_run_agent_tokens_estimate_a_nonzero_value_when_usage_is_absent(worktree):
    message = SimpleNamespace(content="done", tool_calls=None)
    # No `usage` attribute at all -- getattr(response, "usage", None) path.
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def provider_call(**kwargs):
        return response

    result = run_agent(worktree=worktree, target=_target(), request=_request(), provider_call=provider_call)

    assert result["tokens"] > 0
    assert result["stopped"] == "model-finished"


def test_run_agent_tokens_estimate_can_trip_the_ceiling(worktree):
    # The actual point of F3: a provider that NEVER reports usage must not be
    # able to run forever just because the token ceiling silently reads 0
    # every tick. A single large response from such a provider now trips the
    # ceiling via the chars/4 estimate.
    huge_content = "n" * 4000  # ~1000 estimated tokens
    message = SimpleNamespace(content=huge_content, tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])  # no usage attr

    def provider_call(**kwargs):
        return response

    with pytest.raises(CeilingExceeded, match="tokens"):
        run_agent(worktree=worktree, target=_target(agent_max_tokens=100),
                  request=_request(), provider_call=provider_call)


def test_run_agent_tokens_still_prefer_reported_usage_over_the_estimate(worktree):
    # When a provider DOES report usage, the estimate must not override it --
    # regardless of how long the response content is.
    message = SimpleNamespace(content="n" * 4000, tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(total_tokens=7))

    def provider_call(**kwargs):
        return response

    result = run_agent(worktree=worktree, target=_target(), request=_request(), provider_call=provider_call)

    assert result["tokens"] == 7


# --- M1: the untrusted request must be wrapped in an explicit delimited block ---


def test_build_messages_delimits_the_untrusted_request_block():
    body = build_messages(_request(), _target())[1]["content"]
    # There must be a clearly-tagged region wrapping the untrusted payload,
    # not just free text splicing.
    assert "<untrusted" in body.lower()
    assert body.lower().count("untrusted") >= 2  # an opening and a closing marker


# --- Task 6: self-check and CLI entrypoint ---


import subprocess

from devflow_delegation.agent_runner import changed_paths, main, self_check


def _git(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def git_worktree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(["git", "init", "--initial-branch", "main"], tmp_path)
    _git(["git", "config", "user.email", "t@example.test"], tmp_path)
    _git(["git", "config", "user.name", "T"], tmp_path)
    _git(["git", "add", "src/app.py"], tmp_path)
    _git(["git", "commit", "-m", "seed"], tmp_path)
    return tmp_path


def test_changed_paths_sees_modified_and_new_files(git_worktree):
    (git_worktree / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (git_worktree / "src" / "new.py").write_text("y = 1\n", encoding="utf-8")
    assert set(changed_paths(git_worktree)) == {"src/app.py", "src/new.py"}


def test_self_check_passes_for_a_clean_scoped_change(git_worktree):
    (git_worktree / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    self_check(git_worktree, _target(), known_values=("sk-live-abc",))


def test_self_check_rejects_an_empty_diff(git_worktree):
    with pytest.raises(RuntimeError, match="no meaningful diff"):
        self_check(git_worktree, _target(), known_values=())


def test_self_check_rejects_a_change_outside_allowed_globs(git_worktree):
    (git_worktree / "sneaky.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="out-of-scope"):
        self_check(git_worktree, _target(), known_values=())


def test_self_check_rejects_a_leaked_credential(git_worktree):
    # allowed_globs validates WHERE the agent wrote, never WHAT. This is the check
    # that stops a secret in an allowed path from reaching a real PR.
    (git_worktree / "src" / "app.py").write_text("TOKEN = 'sk-live-abc'\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="secret"):
        self_check(git_worktree, _target(), known_values=("sk-live-abc",))


def test_self_check_rejects_too_many_files(git_worktree):
    for index in range(5):
        (git_worktree / "src" / f"f{index}.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="files"):
        self_check(git_worktree, _target(agent_max_files=3), known_values=())


def test_main_exits_nonzero_when_the_request_path_is_missing(monkeypatch, git_worktree):
    monkeypatch.delenv("DDP_REQUEST_PATH", raising=False)
    monkeypatch.chdir(git_worktree)
    assert main([]) == 1


# --- environ-scrub ordering: a regression test for a defect found in the brief.
#
# The brief's reference `main()` read `os.environ.get("PATH", "")` AFTER
# `os.environ.clear()`, which always observes an empty environ -- PATH (and
# every other allow-listed var: SYSTEMROOT, TEMP, HOME, ...) would be wiped,
# not merely scrubbed. That breaks every subprocess call made afterwards
# (git in self_check, run_tests in the agent loop). The fix snapshots and
# scrubs the environment BEFORE clearing it. This test drives `main()` for
# real (with run_agent/self_check swapped for lightweight fakes so no LLM or
# network call happens) and asserts PATH is still populated inside run_agent.
def test_main_preserves_path_when_scrubbing_the_environment(monkeypatch, git_worktree, tmp_path):
    # main() calls os.environ.clear() for real -- correct in production, where
    # main() only ever runs as a freshly-spawned child process (the executor's
    # implementation_command), so clearing affects a throwaway copy of the
    # environment that dies with the process. Here it runs in-process inside
    # the shared pytest worker, so without an explicit full snapshot/restore
    # the clear() would leak into every test that runs afterward in this
    # session (monkeypatch's own env teardown only restores the handful of
    # keys IT touched via setenv/delenv, not a wholesale clear() of every var
    # it never knew about). Save and restore the entire environ by hand.
    saved_environ = dict(os.environ)
    try:
        _run_main_preserves_path_when_scrubbing_the_environment(monkeypatch, git_worktree, tmp_path)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)


def _run_main_preserves_path_when_scrubbing_the_environment(monkeypatch, git_worktree, tmp_path):
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps({
            "request_id": "dwr_test",
            "request": {
                "target": {"repo": "fixture"},
                "title": "t", "problem_statement": "p", "acceptance_criteria": [],
            },
        }),
        encoding="utf-8",
    )
    allowlist_path = tmp_path / "allowlist.json"
    allowlist_path.write_text(
        json.dumps({
            "version": "1",
            "targets": {
                "fixture": {
                    "repo": "fixture", "checkout_path": "/unused",
                    "allowed_globs": ["src/**"],
                    "executor_enabled": True,
                    "implementation_command": ["python", "-m", "devflow_delegation.agent_runner"],
                    "github_repo": "org/fixture",
                    "max_autonomous_action": "create_pr",
                    "synthetic_fixture": True,
                }
            },
        }),
        encoding="utf-8",
    )

    monkeypatch.setenv("DDP_REQUEST_PATH", str(request_path))
    monkeypatch.setenv("PATH", "C:/fake/real/path")
    monkeypatch.chdir(git_worktree)

    from devflow_delegation import agent_runner

    monkeypatch.setattr("events.paths.devflow_allowlist_path", lambda: allowlist_path)

    captured = {}

    def fake_run_agent(**kwargs):
        captured["path"] = os.environ.get("PATH", "")
        return {"iterations": 1, "tokens": 1, "stopped": "model-finished"}

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
    monkeypatch.setattr(agent_runner, "self_check", lambda *a, **k: None)

    assert agent_runner.main([]) == 0
    assert captured["path"] == "C:/fake/real/path"


def _write_request_and_allowlist(tmp_path, *, agent_model="test/model"):
    """Shared fixture-file setup for the main()-level tests below."""
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps({
            "request_id": "dwr_test",
            "request": {
                "target": {"repo": "fixture"},
                "title": "t", "problem_statement": "p", "acceptance_criteria": [],
            },
        }),
        encoding="utf-8",
    )
    allowlist_path = tmp_path / "allowlist.json"
    allowlist_path.write_text(
        json.dumps({
            "version": "1",
            "targets": {
                "fixture": {
                    "repo": "fixture", "checkout_path": "/unused",
                    "allowed_globs": ["src/**"],
                    "executor_enabled": True,
                    "implementation_command": ["python", "-m", "devflow_delegation.agent_runner"],
                    "github_repo": "org/fixture",
                    "max_autonomous_action": "create_pr",
                    "synthetic_fixture": True,
                    "agent_model": agent_model,
                }
            },
        }),
        encoding="utf-8",
    )
    return request_path, allowlist_path


# --- F1: the env scrub had no test asserting a secret is actually GONE from
# what run_agent observes. The only test driving main() to success checked
# ONLY that PATH survived (see test_main_preserves_path_when_scrubbing_the_
# environment above) -- delete the scrub lines in main() and every test still
# passed. This seeds a secret-shaped var and asserts it is absent from the
# environment run_agent (and therefore the eventual provider call) observes,
# while PATH is still present.


def test_main_scrubs_a_secret_shaped_env_var_before_running_the_agent(monkeypatch, git_worktree, tmp_path):
    saved_environ = dict(os.environ)
    try:
        _run_main_scrubs_a_secret_shaped_env_var(monkeypatch, git_worktree, tmp_path)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)


def _run_main_scrubs_a_secret_shaped_env_var(monkeypatch, git_worktree, tmp_path):
    request_path, allowlist_path = _write_request_and_allowlist(tmp_path)

    # A secret-shaped value assembled at runtime -- never a literal secret in
    # this file (the repo's own pre-commit gitleaks hook flags those).
    fake_secret = "fk" + "-" + "test" + "-" + "0123456789abcdef"
    monkeypatch.setenv("DDP_REQUEST_PATH", str(request_path))
    monkeypatch.setenv("PATH", "C:/fake/real/path")
    monkeypatch.setenv("FAKE_TEST_API_KEY", fake_secret)
    monkeypatch.chdir(git_worktree)

    from devflow_delegation import agent_runner

    monkeypatch.setattr("events.paths.devflow_allowlist_path", lambda: allowlist_path)

    observed = {}

    def fake_run_agent(**kwargs):
        observed.update(os.environ)
        return {"iterations": 1, "tokens": 1, "stopped": "model-finished"}

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
    monkeypatch.setattr(agent_runner, "self_check", lambda *a, **k: None)

    assert agent_runner.main([]) == 0

    # The whole point of the scrub: run_agent (and the real provider call it
    # would make) must never see the raw credential value or its var name.
    assert "FAKE_TEST_API_KEY" not in observed
    assert fake_secret not in observed.values()
    # But the scrub must not be a blunt full wipe -- PATH must survive.
    assert observed.get("PATH") == "C:/fake/real/path"


# --- F2: known_values capture ordering. `known = secret_values(dict(os.environ))`
# must run BEFORE os.environ.clear(); if it ran after, `known` would always be
# `()` and self_check's exact-match arm would go permanently dark (the regex
# arm would still catch classic credential SHAPES like "sk-...", masking the
# gap for anything that doesn't match one of those shapes). This drives
# main() with self_check NOT stubbed, so the capture-before-scrub wire is
# proven end to end rather than just at the unit level.


def test_main_self_check_catches_a_leaked_env_secret_end_to_end(monkeypatch, git_worktree, tmp_path_factory):
    saved_environ = dict(os.environ)
    try:
        _run_main_self_check_catches_a_leaked_env_secret(monkeypatch, git_worktree, tmp_path_factory, leak=True)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)


def test_main_self_check_passes_when_the_agents_diff_is_clean(monkeypatch, git_worktree, tmp_path_factory):
    saved_environ = dict(os.environ)
    try:
        _run_main_self_check_catches_a_leaked_env_secret(monkeypatch, git_worktree, tmp_path_factory, leak=False)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)


def _run_main_self_check_catches_a_leaked_env_secret(monkeypatch, git_worktree, tmp_path_factory, *, leak):
    # request.json/allowlist.json must live OUTSIDE git_worktree: git_worktree
    # IS this test's `tmp_path` (the fixture returns it directly), and
    # self_check runs for real here, so any file written under it becomes
    # part of the scanned diff. A separate tmp_path_factory dir keeps the
    # worktree clean of anything but what the "agent" itself wrote.
    meta_dir = tmp_path_factory.mktemp("main_self_check_meta")
    request_path, allowlist_path = _write_request_and_allowlist(meta_dir)

    # A value that is both secret-shaped (its env var name has "KEY" in it,
    # so secret_values() collects it) AND long enough to clear scan_for_secrets'
    # exact-match floor. Assembled at runtime, not a literal.
    fake_secret = "fk" + "-" + "leak" + "-" + "0123456789abcdef"
    monkeypatch.setenv("DDP_REQUEST_PATH", str(request_path))
    # PATH is deliberately left as the real, ambient value here (unlike the
    # other main()-level tests in this file): self_check runs for real below
    # and shells out to `git`, which needs a real PATH to resolve. Faking
    # PATH would make git fail and raise RuntimeError for the WRONG reason,
    # making the leak=True assertion pass even if the secret-scan never ran.
    monkeypatch.setenv("FAKE_TEST_API_KEY", fake_secret)
    monkeypatch.chdir(git_worktree)

    from devflow_delegation import agent_runner

    monkeypatch.setattr("events.paths.devflow_allowlist_path", lambda: allowlist_path)
    # This target's agent_model ("test/model") never resolves to a real
    # provider anyway (see _resolve_agent_credentials tests), but the real
    # hermes_cli.providers.get_provider still does a full models.dev lookup
    # (and persists a disk cache under HERMES_HOME) to determine that. The
    # per-test HERMES_HOME tempdir (see tests/conftest.py's autouse
    # _hermetic_environment) is itself nested under this test's tmp_path --
    # the same directory git_worktree uses as its repo root -- so that cache
    # write would land inside the worktree self_check is about to scan. Stub
    # it out: credential resolution isn't what this test is about.
    monkeypatch.setattr("hermes_cli.providers.get_provider", lambda name: None)

    def fake_run_agent(**kwargs):
        # Stand in for "the agent wrote a scoped file" -- with or without the
        # leaked secret value inside it, depending on the scenario.
        content = f"TOKEN = '{fake_secret}'\n" if leak else "x = 2\n"
        (git_worktree / "src" / "app.py").write_text(content, encoding="utf-8")
        return {"iterations": 1, "tokens": 1, "stopped": "model-finished"}

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
    # self_check is deliberately NOT stubbed here -- that's the point.

    exit_code = agent_runner.main([])
    assert exit_code == (1 if leak else 0)


# --- F4: the live provider call is likely broken by the scrub. call_llm(model=
# target.agent_model, ...) with no provider= resolves to auxiliary_client's
# "auto" chain, which reads HERMES_HOME + *_API_KEY env vars -- exactly what
# the scrub removes. main() must resolve provider/api_key/base_url from the
# PRE-SCRUB environment (via hermes_cli.providers, the provider-identity
# source of truth) and forward them explicitly, so the call doesn't depend on
# ambient auto-detection.


def test_main_forwards_the_resolved_provider_credential_to_call_llm(monkeypatch, git_worktree, tmp_path):
    saved_environ = dict(os.environ)
    try:
        _run_main_forwards_the_resolved_provider_credential(monkeypatch, git_worktree, tmp_path)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)


def _run_main_forwards_the_resolved_provider_credential(monkeypatch, git_worktree, tmp_path):
    request_path, allowlist_path = _write_request_and_allowlist(
        tmp_path, agent_model="fakevendor/fake-model-1")

    fake_key = "fk" + "-" + "live" + "-" + "0123456789abcdef"
    monkeypatch.setenv("DDP_REQUEST_PATH", str(request_path))
    monkeypatch.setenv("PATH", "C:/fake/real/path")
    monkeypatch.setenv("FAKEVENDOR_API_KEY", fake_key)
    monkeypatch.setenv("FAKEVENDOR_BASE_URL", "https://fake.example.test/v1")
    monkeypatch.chdir(git_worktree)

    from devflow_delegation import agent_runner

    monkeypatch.setattr("events.paths.devflow_allowlist_path", lambda: allowlist_path)

    # A fake provider registry entry -- avoids depending on which real
    # providers hermes_cli.providers happens to know about, and proves the
    # wiring generically rather than pinning to today's real registry.
    fake_provider = SimpleNamespace(
        id="fakevendor",
        api_key_env_vars=("FAKEVENDOR_API_KEY",),
        base_url_env_var="FAKEVENDOR_BASE_URL",
    )
    monkeypatch.setattr(
        "hermes_cli.providers.get_provider",
        lambda name: fake_provider if name == "fakevendor" else None,
    )

    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="done", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(total_tokens=1))

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    monkeypatch.setattr(agent_runner, "self_check", lambda *a, **k: None)

    assert agent_runner.main([]) == 0

    # Forwarded explicitly, not left to ambient auto-detection.
    assert captured.get("provider") == "fakevendor"
    assert captured.get("api_key") == fake_key
    assert captured.get("base_url") == "https://fake.example.test/v1"
    # The model string itself is untouched by this fix.
    assert captured.get("model") == "fakevendor/fake-model-1"

    # And captured BEFORE the scrub: by the time call_llm actually runs (which
    # just happened, above), the raw var is already gone from the child
    # environment -- the value only survived in the captured kwarg.
    assert "FAKEVENDOR_API_KEY" not in os.environ


def test_resolve_agent_credentials_returns_empty_for_an_unknown_provider_prefix():
    from devflow_delegation.agent_runner import _resolve_agent_credentials

    # "test/model" (the default fixture agent_model used throughout this
    # file) has no matching entry in hermes_cli.providers -- this must not
    # raise, and must not invent a provider.
    assert _resolve_agent_credentials("test/model", {"TEST_API_KEY": "x"}) == {}


def test_resolve_agent_credentials_forwards_only_whats_present(monkeypatch):
    from devflow_delegation.agent_runner import _resolve_agent_credentials

    fake_provider = SimpleNamespace(
        id="fakevendor", api_key_env_vars=("FAKEVENDOR_API_KEY",), base_url_env_var="",
    )
    monkeypatch.setattr(
        "hermes_cli.providers.get_provider",
        lambda name: fake_provider if name == "fakevendor" else None,
    )

    # No env vars set at all -- provider is still identified, but neither
    # api_key nor base_url are invented out of thin air.
    result = _resolve_agent_credentials("fakevendor/some-model", {})
    assert result == {"provider": "fakevendor"}


# --- F5: the failure path printed unscanned exception text. The executor
# captures stderr into its ExecutorError, which lands in the ledger and
# notification surface -- a provider SDK error echoing a credential would put
# it in the control plane. main()'s except handlers must redact before
# printing.


def test_main_redacts_a_leaked_secret_from_an_exception_message(monkeypatch, git_worktree, tmp_path, capsys):
    saved_environ = dict(os.environ)
    try:
        _run_main_redacts_a_leaked_secret_from_an_exception(monkeypatch, git_worktree, tmp_path, capsys)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)


def _run_main_redacts_a_leaked_secret_from_an_exception(monkeypatch, git_worktree, tmp_path, capsys):
    request_path, allowlist_path = _write_request_and_allowlist(tmp_path)

    fake_secret = "fk" + "-" + "boom" + "-" + "0123456789abcdef"
    monkeypatch.setenv("DDP_REQUEST_PATH", str(request_path))
    monkeypatch.setenv("PATH", "C:/fake/real/path")
    monkeypatch.setenv("FAKE_TEST_API_KEY", fake_secret)
    monkeypatch.chdir(git_worktree)

    from devflow_delegation import agent_runner

    monkeypatch.setattr("events.paths.devflow_allowlist_path", lambda: allowlist_path)

    def fake_run_agent(**kwargs):
        # Simulate a provider SDK exception that happens to echo the secret
        # value back (e.g. an auth error quoting a request header).
        raise RuntimeError(f"upstream 401: token {fake_secret} was rejected, retry later")

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)

    assert agent_runner.main([]) == 1

    err = capsys.readouterr().err
    assert fake_secret not in err
    assert "[REDACTED]" in err
    # Not a blanket suppression -- the surrounding, non-secret context survives.
    assert "upstream 401" in err
    assert "retry later" in err


# --- Task 7: end-to-end -- the real executor drives a runner-shaped
# implementation_command to VALIDATED in shadow mode. No executor change was
# made for the agent runner; this proves implementation_command was already
# the full integration surface. Uses a stub runner script (same contract as
# agent_runner.main: reads DDP_REQUEST_PATH, writes inside allowed_globs,
# prints an observable summary) so the wiring is proven without a provider.


def test_executor_drives_an_agent_style_runner_to_validated_in_shadow(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(["git", "init", "--initial-branch", "main"], repo)
    _git(["git", "config", "user.email", "t@example.test"], repo)
    _git(["git", "config", "user.name", "T"], repo)
    _git(["git", "add", "src/app.py"], repo)
    _git(["git", "commit", "-m", "seed"], repo)

    # A stand-in for the agent loop: same contract (reads DDP_REQUEST_PATH, writes
    # inside allowed_globs, prints an observable summary) without a provider.
    runner = tmp_path / "stub_runner.py"
    runner.write_text(
        "import json,os,pathlib\n"
        "p=pathlib.Path(os.environ['DDP_REQUEST_PATH'])\n"
        "rid=json.loads(p.read_text())['request_id']\n"
        "pathlib.Path('src/fix.py').write_text('y = 2\\n')\n"
        "print(f'agent completed: iterations=2 tokens=10 stopped=model-finished {rid}')\n",
        encoding="utf-8",
    )

    ledger = DelegationLedger(tmp_path / "devflow" / "ledger.db")
    request = parse_request({
        "schema_version": "3.0", "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": "agent:e2e:v1",
        "source": {"agent": "operator", "kind": "explicit", "finding_id": "e2e"},
        "kind": "task", "title": "Agent end to end",
        "problem_statement": "Prove the executor drives the runner.",
        "evidence": [{"kind": "test", "summary": "e2e"}],
        "target": {"repo": "fixture", "subsystem": "src"},
        "severity": "low", "priority": "P3", "confidence": 1.0,
        "acceptance_criteria": ["a scoped file exists"], "safety_notes": [],
    })
    ledger.insert_request(request)
    transition(ledger, None, request.request_id, "TRIAGED", actor="operator")
    ledger.record_human_decision(request.request_id, "operator", "approve", "e2e",
                                 f"tok-{request.request_id}")
    transition(ledger, None, request.request_id, "PLANNED", actor="operator")

    target = TargetConfig(
        repo="fixture", checkout_path=str(repo), default_branch="main", remote="origin",
        allowed_globs=("src/**",), denied_globs=("**/.env",),
        worktree_base=str(tmp_path / "worktrees"),
        test_commands=(("python", "-c", "print('tests passed')"),),
        required_checks=("test",), command_timeout_seconds=120,
        risk_ceiling="low", max_autonomous_action="create_pr",
        executor_enabled=True, synthetic_fixture=True,
        implementation_command=("python", str(runner)),
        github_repo="example/fixture", live_gateway_imports=False,
    )

    result = run_executor_tick(ledger, Allowlist(version="t", targets={"fixture": target}), None)

    assert result == {"processed": 1, "errors": 0, "skipped": 0}
    assert ledger.get_request(request.request_id)["state"] == "VALIDATED"
    kinds = {a["kind"] for a in ledger.artifacts_for(request.request_id)}
    assert "shadow" in kinds
    assert "pr" not in kinds and "pr_number" not in kinds
