"""HER-96 security regressions for Code-profile worktree admission."""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LANE = REPO_ROOT / "scripts" / "factory_lane.py"
HOOK = REPO_ROOT / "scripts" / "factory_admission_hook.py"


def run_hook(registry: Path, payload: dict, *, agent="code-a", profile="code-a"):
    return subprocess.run(
        [sys.executable, str(HOOK), "--registry", str(registry),
         "--agent", agent, "--profile", profile, "--only-mutating",
         "--require-owned-git"],
        input=json.dumps(payload), text=True, capture_output=True, timeout=10,
    )


def decision(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def payload(tool, args, cwd, session="s1"):
    return {"hook_event_name": "pre_tool_call", "tool_name": tool,
            "tool_input": args, "session_id": session, "cwd": str(cwd)}


def init_repo(path: Path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


def admit(registry: Path, repo: Path, *, session="s1", agent="code-a",
          profile="code-a", pid=None, start=None):
    args = [sys.executable, str(LANE), "--registry", str(registry), "admit", "HER-96",
            "--mode", "owner", "--hard", "--agent", agent, "--profile", profile,
            "--session", session, "--worktree", str(repo),
            "--owner-pid", str(pid or os.getpid())]
    if start:
        args += ["--owner-start-time", start]
    return subprocess.run(args, text=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("command", [
    "pwd", "git worktree list --porcelain", "git branch --show-current",
    "git rev-parse HEAD",
    "gh pr list --limit 2",
    "gh pr view 70602 --json number,title", "claude --version", "claude auth status",
])
def test_readonly_terminal_discovery_is_explicitly_allowed_preclaim(tmp_path, command):
    outside = tmp_path / "outside"
    outside.mkdir()
    registry = tmp_path / "missing-registry"
    result = run_hook(registry, payload("terminal", {"command": command}, outside))
    assert decision(result) == {"decision": "allow"}
    assert not registry.exists()


@pytest.mark.parametrize("command", [
    "touch x", "git status --short", "git -C /tmp status --short",
    "git commit -m x", "git worktree add /tmp/x", "gh pr create",
    "env git status", "sudo git status", "sh -c 'git status'", "bash -c 'git status'",
    "eval 'git status'", "git status | cat", "git status < input",
    "git status && touch x", "git status > out", "git status $(echo x)",
    'git -C "$WT" status', "git status;", "git diff", "git diff --stat",
    "git diff --no-ext-diff --no-textconv", "git log -1 --oneline",
    "git show --stat HEAD", "git diff --output=out",
    "git diff *.py", "git status 'unterminated", "curl https://example.test",
    "python -c 'print(1)'", "git --unknown-global status", "git -c alias.x=status x",
])
def test_mutating_or_ambiguous_terminal_is_blocked_preclaim(tmp_path, command):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload("terminal", {"command": command}, outside))
    assert decision(result)["decision"] == "block"


def test_execute_code_and_file_tools_are_always_mutating(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    for tool, args in [
        ("execute_code", {"code": "print('read only')"}),
        ("write_file", {"path": "x", "content": "x"}),
        ("edit_file", {"path": "x", "old": "a", "new": "b"}),
        ("apply_patch", {"changes": [{"path": "x"}]}),
    ]:
        assert decision(run_hook(tmp_path / "missing", payload(tool, args, outside)))["decision"] == "block"


def test_owned_workdir_replaces_non_git_cwd_fallback(tmp_path):
    repo, outside, registry = tmp_path / "repo", tmp_path / "outside", tmp_path / "registry"
    init_repo(repo)
    outside.mkdir()
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload("terminal", {"command": "touch x", "workdir": str(repo)}, outside))
    assert decision(result) == {"decision": "allow"}


def test_owned_worker_can_run_post_commit_diff_tree_verification(tmp_path):
    repo, outside, registry = (
        tmp_path / "repo", tmp_path / "outside", tmp_path / "registry"
    )
    init_repo(repo)
    outside.mkdir()
    assert admit(registry, repo).returncode == 0
    command = (
        "git status --porcelain=v1; git rev-parse HEAD; git rev-parse HEAD^; "
        "git diff-tree --no-commit-id --name-status -r HEAD; "
        "git show -s --format=%s HEAD"
    )

    result = run_hook(
        registry,
        payload(
            "terminal",
            {"command": command, "workdir": str(repo)},
            outside,
        ),
    )

    assert decision(result) == {"decision": "allow"}


def test_diff_tree_verification_still_requires_owner(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)

    result = run_hook(
        tmp_path / "missing",
        payload(
            "terminal",
            {"command": "git diff-tree --name-status -r HEAD", "workdir": str(repo)},
            repo,
        ),
    )

    assert decision(result)["decision"] == "block"


def test_owned_worker_can_run_standard_git_and_pr_preflight(tmp_path):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    command = (
        "pwd -P; git status --porcelain=v1; git branch --show-current; "
        "git rev-parse HEAD; git worktree list --porcelain; git remote -v; "
        "gh pr list --state open --json "
        "number,headRefName,headRepositoryOwner,url 2>/dev/null || true"
    )

    result = run_hook(
        registry,
        payload("terminal", {"command": command, "workdir": str(repo)}, repo),
    )

    assert decision(result) == {"decision": "allow"}


def test_owned_worker_can_run_multiline_standard_preflight(tmp_path):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    command = "\n".join((
        "set -e",
        "pwd -P",
        "git branch --show-current",
        "git rev-parse HEAD",
        "git status --porcelain=v1",
        "git worktree list --porcelain",
        "gh pr list --state open --head canary/example --json number,url",
    ))

    result = run_hook(
        registry,
        payload("terminal", {"command": command, "workdir": str(repo)}, repo),
    )

    assert decision(result) == {"decision": "allow"}


def test_newline_separated_foreign_git_target_is_blocked(tmp_path):
    repo, foreign, registry = (
        tmp_path / "repo", tmp_path / "foreign", tmp_path / "registry"
    )
    init_repo(repo)
    init_repo(foreign)
    assert admit(registry, repo).returncode == 0

    result = run_hook(
        registry,
        payload(
            "terminal",
            {
                "command": "git status\ngit -C ../foreign status",
                "workdir": str(repo),
            },
            repo,
        ),
    )

    assert decision(result)["decision"] == "block"


@pytest.mark.parametrize("separator", [";\n", "&&\n"])
def test_coalesced_shell_control_run_cannot_hide_foreign_git(tmp_path, separator):
    repo, foreign, registry = (
        tmp_path / "repo", tmp_path / "foreign", tmp_path / "registry"
    )
    init_repo(repo)
    init_repo(foreign)
    assert admit(registry, repo).returncode == 0
    command = (
        ": ${p:=..} ${x:=$p/foreign} ${y:=i}"
        f"{separator}"
        "g${y}t -C $x commit --allow-empty -m bypass"
    )

    result = run_hook(
        registry,
        payload("terminal", {"command": command, "workdir": str(repo)}, repo),
    )

    assert decision(result)["decision"] == "block"


def test_git_remote_still_requires_owner(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)

    result = run_hook(
        tmp_path / "missing",
        payload(
            "terminal",
            {"command": "git remote -v", "workdir": str(repo)},
            repo,
        ),
    )

    assert decision(result)["decision"] == "block"


def test_devnull_is_exempt_only_as_a_redirection_target(tmp_path):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0

    redirected = run_hook(
        registry,
        payload(
            "terminal",
            {"command": "gh pr list 2>/dev/null || true", "workdir": str(repo)},
            repo,
        ),
    )
    direct_target = run_hook(
        registry,
        payload(
            "terminal",
            {"command": "rm /dev/null", "workdir": str(repo)},
            repo,
        ),
    )

    assert decision(redirected) == {"decision": "allow"}
    assert decision(direct_target)["decision"] == "block"


def test_all_effective_targets_must_be_owned(tmp_path):
    repo, other, outside, registry = (tmp_path / name for name in ("repo", "other", "outside", "registry"))
    init_repo(repo)
    init_repo(other)
    outside.mkdir()
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload("apply_patch", {"changes": [
        {"path": str(repo / "a.py")}, {"path": str(other / "b.py")}]}, outside))
    assert decision(result)["decision"] == "block"


@pytest.mark.parametrize("compact", [False, True])
def test_git_global_c_target_is_checked_before_foreign_commit(tmp_path, compact):
    local, foreign, registry = (tmp_path / name for name in ("local", "foreign", "registry"))
    init_repo(local)
    init_repo(foreign)
    assert admit(registry, local).returncode == 0
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=foreign, text=True, capture_output=True, check=True,
    ).stdout.strip()
    target = "../foreign"
    command = (
        f"git -C{target} commit --allow-empty -m bypass"
        if compact else f"git -C {target} commit --allow-empty -m bypass"
    )
    result = run_hook(
        registry,
        payload("terminal", {"command": command, "workdir": str(local)}, local),
    )
    verdict = decision(result)
    if verdict["decision"] == "allow" and not compact:
        subprocess.run(command, cwd=local, shell=True, check=True, capture_output=True, text=True)
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=foreign, text=True, capture_output=True, check=True,
    ).stdout.strip()
    assert verdict["decision"] == "block"
    assert after == before


@pytest.mark.parametrize("command", [
    "git -C ../foreign -C nested commit --allow-empty -m bypass",
    "git -C../foreign -Cnested commit --allow-empty -m bypass",
    "git --no-pager -C../foreign commit --allow-empty -m bypass",
])
def test_every_git_global_c_target_is_checked(tmp_path, command):
    local, foreign, nested, registry = (
        tmp_path / "local", tmp_path / "foreign", tmp_path / "foreign" / "nested",
        tmp_path / "registry",
    )
    init_repo(local)
    init_repo(foreign)
    init_repo(nested)
    assert admit(registry, local).returncode == 0
    result = run_hook(
        registry, payload("terminal", {"command": command, "workdir": str(local)}, local),
    )
    assert decision(result)["decision"] == "block"


def _v4a_update(path: str, old: str, new: str) -> str:
    return f"*** Update File: {path}\n@@\n-{old}\n+{new}\n"


def test_real_patch_handler_blocks_mixed_owned_and_foreign_v4a(tmp_path, monkeypatch):
    from agent import shell_hooks
    from hermes_cli import plugins
    import model_tools

    local, foreign, registry = (tmp_path / name for name in ("local", "foreign", "registry"))
    init_repo(local)
    init_repo(foreign)
    assert admit(registry, local).returncode == 0
    (local / "owned.txt").write_text("owned\n")
    (foreign / "foreign.txt").write_text("foreign\n")
    monkeypatch.chdir(local)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    plugins._plugin_manager = plugins.PluginManager()
    shell_hooks.reset_for_tests()
    command = " ".join([
        sys.executable, str(HOOK), "--registry", str(registry), "--agent", "code-a",
        "--profile", "code-a", "--only-mutating", "--require-owned-git",
    ])
    cfg = {"hooks": {"pre_tool_call": [{
        "command": command,
        "matcher": ".*",
        "fail_closed": True,
    }]}}
    assert len(shell_hooks.register_from_config(cfg, accept_hooks=True)) == 1
    patch_body = (
        "*** Begin Patch\n"
        + _v4a_update("owned.txt", "owned", "changed")
        + "*** Add File: added.txt\n+new\n"
        + "*** Delete File: ../foreign/foreign.txt\n"
        + "*** Move File: README.md -> ../foreign/moved.md\n"
        + "*** End Patch\n"
    )
    result = json.loads(model_tools.handle_function_call(
        "patch", {"mode": "patch", "patch": patch_body}, session_id="s1",
    ))
    assert "error" in result
    assert (local / "owned.txt").read_text() == "owned\n"
    assert not (local / "added.txt").exists()
    assert (foreign / "foreign.txt").read_text() == "foreign\n"
    assert not (foreign / "moved.md").exists()


def test_shared_v4a_parser_extracts_every_operation_path():
    from acp_adapter.edit_approval import extract_v4a_patch_paths

    patch_body = (
        "*** Begin Patch\n"
        "*** Update File: src/update.py\n@@\n-old\n+new\n"
        "*** Add File: src/add.py\n+new\n"
        "*** Delete File: src/delete.py\n"
        "*** Move File: src/old.py -> src/new.py\n"
        "*** Update File: src/rename.py\n"
        "*** Move to: src/renamed.py\n"
        "@@\n-old\n+new\n"
        "*** End Patch\n"
    )
    assert extract_v4a_patch_paths(patch_body) == [
        "src/update.py", "src/add.py", "src/delete.py", "src/old.py", "src/new.py",
        "src/rename.py", "src/renamed.py",
    ]
    assert extract_v4a_patch_paths(
        "***Begin Patch\n***Add File: compact.txt\n+new\n***End Patch\n"
    ) == ["compact.txt"]


@pytest.mark.parametrize("patch_body", [
    "*** Begin Patch\n*** Update File:\n@@\n-a\n+b\n*** End Patch\n",
    "*** Begin Patch\n*** Add File: /absolute.txt\n+x\n*** End Patch\n",
    "*** Begin Patch\n*** Update File: a.txt\n*** Move to:\n@@\n-a\n+b\n*** End Patch\n",
    "*** Begin Patch\n*** Move to: b.txt\n*** End Patch\n",
    "*** Begin Patch\n*** Mystery File: a.txt\n*** End Patch\n",
])
def test_malformed_or_ambiguous_v4a_blocks_fail_closed(tmp_path, patch_body):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload(
        "patch", {"mode": "patch", "patch": patch_body}, repo,
    ))
    assert decision(result)["decision"] == "block"


def test_v4a_move_to_checks_source_and_destination(tmp_path):
    local, foreign, registry = (tmp_path / name for name in ("local", "foreign", "registry"))
    init_repo(local)
    init_repo(foreign)
    assert admit(registry, local).returncode == 0
    patch_body = (
        "*** Begin Patch\n"
        "*** Update File: README.md\n"
        "*** Move to: ../foreign/moved.md\n"
        "@@\n-base\n+moved\n"
        "*** End Patch\n"
    )
    result = run_hook(registry, payload(
        "patch", {"mode": "patch", "patch": patch_body}, local,
    ))
    assert decision(result)["decision"] == "block"


def test_git_status_fsmonitor_never_executes_preclaim(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    marker = tmp_path / "fsmonitor-ran"
    helper = tmp_path / "fsmonitor.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\nprintf '0\\n'\n")
    helper.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(helper)], cwd=repo, check=True)
    result = run_hook(tmp_path / "missing", payload(
        "terminal", {"command": "git status --short", "workdir": str(repo)}, repo,
    ))
    verdict = decision(result)
    if verdict["decision"] == "allow":
        subprocess.run(["git", "status", "--short"], cwd=repo, check=False,
                       capture_output=True, text=True)
    assert verdict["decision"] == "block"
    assert not marker.exists()


def test_git_diff_helpers_never_execute_preclaim(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    marker = tmp_path / "external-ran"
    helper = tmp_path / "external.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    helper.chmod(0o755)
    subprocess.run(["git", "config", "diff.external", str(helper)], cwd=repo, check=True)
    (repo / "README.md").write_text("changed\n")
    result = run_hook(tmp_path / "missing", payload(
        "terminal", {"command": "git diff", "workdir": str(repo)}, repo,
    ))
    verdict = decision(result)
    if verdict["decision"] == "allow":
        subprocess.run(["git", "diff"], cwd=repo, check=False, capture_output=True, text=True)
    assert verdict["decision"] == "block"
    assert not marker.exists()


def test_git_attribute_textconv_never_executes_preclaim(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    marker = tmp_path / "textconv-ran"
    helper = tmp_path / "textconv.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\ncat \"$1\"\n")
    helper.chmod(0o755)
    (repo / ".gitattributes").write_text("*.dat diff=owned\n")
    (repo / "sample.dat").write_text("base\n")
    subprocess.run(["git", "add", ".gitattributes", "sample.dat"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "driver base"], cwd=repo, check=True)
    subprocess.run(["git", "config", "diff.owned.textconv", str(helper)], cwd=repo, check=True)
    (repo / "sample.dat").write_text("changed\n")
    result = run_hook(tmp_path / "missing", payload(
        "terminal", {"command": "git diff", "workdir": str(repo)}, repo,
    ))
    verdict = decision(result)
    if verdict["decision"] == "allow":
        subprocess.run(["git", "diff"], cwd=repo, check=False, capture_output=True, text=True)
    assert verdict["decision"] == "block"
    assert not marker.exists()


def test_git_attribute_external_driver_never_executes_preclaim(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    marker = tmp_path / "driver-ran"
    helper = tmp_path / "driver.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    helper.chmod(0o755)
    (repo / ".gitattributes").write_text("*.dat diff=owned\n")
    (repo / "sample.dat").write_text("base\n")
    subprocess.run(["git", "add", ".gitattributes", "sample.dat"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "driver base"], cwd=repo, check=True)
    subprocess.run(["git", "config", "diff.owned.command", str(helper)], cwd=repo, check=True)
    (repo / "sample.dat").write_text("changed\n")
    result = run_hook(tmp_path / "missing", payload(
        "terminal", {"command": "git diff", "workdir": str(repo)}, repo,
    ))
    verdict = decision(result)
    if verdict["decision"] == "allow":
        subprocess.run(["git", "diff"], cwd=repo, check=False, capture_output=True, text=True)
    assert verdict["decision"] == "block"
    assert not marker.exists()


def test_unknown_git_global_options_block_even_in_owned_worktree(tmp_path):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    for command in (
        "git --unknown-global status",
        "git -c alias.x=status x",
        "git --git-dir=.git status",
        "git --work-tree=. status",
        "env git status",
        "sh -c 'git unknown-alias'",
    ):
        result = run_hook(registry, payload(
            "terminal", {"command": command, "workdir": str(repo)}, repo,
        ))
        assert decision(result)["decision"] == "block"


def test_git_alias_and_inline_config_cannot_execute_preclaim(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    marker = tmp_path / "alias-ran"
    helper = tmp_path / "alias.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n")
    helper.chmod(0o755)
    subprocess.run(
        ["git", "config", "alias.inspect", f"!{helper}"], cwd=repo, check=True,
    )
    for command in (
        "git inspect",
        f"git -c alias.inspect=!{helper} inspect",
        f"git -c diff.external={helper} diff",
    ):
        result = run_hook(tmp_path / "missing", payload(
            "terminal", {"command": command, "workdir": str(repo)}, repo,
        ))
        assert decision(result)["decision"] == "block"
    assert not marker.exists()


@pytest.mark.parametrize("escape_kind", ["alias", "external"])
def test_owned_git_rejects_alias_and_external_subcommand_before_real_handler(
        tmp_path, monkeypatch, escape_kind):
    from agent import shell_hooks
    from hermes_cli import plugins
    import model_tools

    local, foreign, registry = (tmp_path / name for name in ("local", "foreign", "registry"))
    init_repo(local)
    init_repo(foreign)
    assert admit(registry, local).returncode == 0
    marker = foreign / f"{escape_kind}-ran"
    helper = tmp_path / f"{escape_kind}-helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n")
    helper.chmod(0o755)
    command_name = f"escape-{escape_kind}"
    if escape_kind == "alias":
        subprocess.run(
            ["git", "config", f"alias.{command_name}", f"!{helper}"],
            cwd=local, check=True,
        )
    else:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        external = bin_dir / f"git-{command_name}"
        external.symlink_to(helper)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    monkeypatch.chdir(local)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    plugins._plugin_manager = plugins.PluginManager()
    shell_hooks.reset_for_tests()
    hook_command = " ".join([
        sys.executable, str(HOOK), "--registry", str(registry), "--agent", "code-a",
        "--profile", "code-a", "--only-mutating", "--require-owned-git",
    ])
    cfg = {"hooks": {"pre_tool_call": [{
        "command": hook_command, "matcher": ".*", "fail_closed": True,
    }]}}
    assert len(shell_hooks.register_from_config(cfg, accept_hooks=True)) == 1
    called = False
    real_dispatch = model_tools.registry.dispatch

    def spy_dispatch(name, args, **kwargs):
        nonlocal called
        if name == "terminal":
            called = True
        return real_dispatch(name, args, **kwargs)

    monkeypatch.setattr(model_tools.registry, "dispatch", spy_dispatch)
    result = json.loads(model_tools.handle_function_call(
        "terminal", {"command": f"git {command_name}", "workdir": str(local)},
        session_id="s1",
    ))
    assert "error" in result
    assert called is False
    assert not marker.exists()


@pytest.mark.parametrize(("tool_name", "tool_input"), [
    ("cronjob", {"action": "create", "name": "escape", "schedule": "0 0 * * *",
                 "workdir": "/tmp/foreign", "no_agent": True, "script": "touch escaped"}),
    ("computer_use", {"action": "type", "app": "Terminal", "text": "touch escaped"}),
    ("project_create", {"name": "escape"}),
    ("skill_manage", {"action": "create", "name": "escape", "content": "unsafe"}),
])
def test_strict_all_tools_matcher_blocks_unbounded_mutator_before_handler(
        tmp_path, monkeypatch, tool_name, tool_input):
    from agent import shell_hooks
    from hermes_cli import plugins
    import model_tools

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    plugins._plugin_manager = plugins.PluginManager()
    shell_hooks.reset_for_tests()
    hook_command = " ".join([
        sys.executable, str(HOOK), "--registry", str(tmp_path / "missing"),
        "--agent", "code-a", "--profile", "code-a", "--only-mutating",
        "--require-owned-git",
    ])
    cfg = {"hooks": {"pre_tool_call": [{
        "command": hook_command, "matcher": ".*", "fail_closed": True,
    }]}}
    assert len(shell_hooks.register_from_config(cfg, accept_hooks=True)) == 1
    called = False
    resolve_calls = []
    real_resolve = plugins.resolve_pre_tool_block

    def spy_resolve(*args, **kwargs):
        resolve_calls.append(args[0])
        return real_resolve(*args, **kwargs)

    def spy_dispatch(name, args, **kwargs):
        nonlocal called
        called = True
        return {"name": name, "spy": "executed"}

    monkeypatch.setattr(plugins, "resolve_pre_tool_block", spy_resolve)
    monkeypatch.setattr(model_tools.registry, "dispatch", spy_dispatch)
    result = json.loads(model_tools.handle_function_call(
        tool_name, tool_input, session_id="s1",
    ))
    assert "error" in result
    assert resolve_calls == [tool_name]
    assert called is False


def test_runtime_tool_catalog_is_explicit_and_unknown_tools_fail_closed():
    import model_tools
    from scripts import factory_admission_hook as admission_hook

    exposed = set(model_tools.get_all_tool_names())
    assert exposed <= admission_hook._KNOWN_RUNTIME_TOOLS
    assert admission_hook._strict_tool_classification("future_mutator", {}) == (
        "unbounded_mutation"
    )


@pytest.mark.parametrize(("tool_name", "tool_input", "expected"), [
    ("cronjob", {"action": "list"}, "observation"),
    ("cronjob", {"action": "create"}, "unbounded_mutation"),
    ("cronjob", {"action": "future"}, "unbounded_mutation"),
    ("computer_use", {"action": "capture", "mode": "som"}, "observation"),
    ("computer_use", {"action": "list_apps"}, "observation"),
    ("computer_use", {"action": "list_windows"}, "observation"),
    ("computer_use", {"action": "type", "text": "touch escaped"}, "unbounded_mutation"),
    ("computer_use", {"action": "focus_app", "app": "Terminal"}, "unbounded_mutation"),
    ("process", {"action": "poll", "session_id": "p1"}, "observation"),
    ("process", {"action": "wait", "session_id": "p1"}, "unbounded_mutation"),
    ("terminal", {"command": "touch x"}, "worktree_mutation"),
    ("terminal", {}, "unbounded_mutation"),
    ("write_file", {"path": "x", "content": "x"}, "worktree_mutation"),
    ("write_file", {"content": "x"}, "unbounded_mutation"),
    ("skill_view", {"name": "code"}, "observation"),
    ("skill_manage", {"action": "create", "name": "x"}, "unbounded_mutation"),
    ("project_create", {"name": "x"}, "unbounded_mutation"),
    ("delegate_task", {"prompt": "mutate"}, "unbounded_mutation"),
    ("web_search", {"query": "HER-96"}, "observation"),
    ("browser_snapshot", {}, "observation"),
    ("browser_click", {"ref": "e1"}, "unbounded_mutation"),
    ("session_search", {"query": "HER-96", "limit": 3}, "observation"),
    ("todo", {}, "observation"),
    ("todo", {"todos": []}, "unbounded_mutation"),
    ("clarify", {"question": "Continue?"}, "observation"),
    ("clarify", {"question": 42}, "unbounded_mutation"),
])
def test_strict_action_classifier_is_fail_closed(tool_name, tool_input, expected):
    from scripts import factory_admission_hook as admission_hook

    assert admission_hook._strict_tool_classification(tool_name, tool_input) == expected


@pytest.mark.parametrize(("tool_name", "tool_input"), [
    ("cronjob", {"action": "list"}),
    ("computer_use", {"action": "capture", "mode": "ax", "max_elements": 100}),
    ("computer_use", {"action": "list_apps"}),
    ("computer_use", {"action": "list_windows"}),
    ("read_file", {"path": "/tmp/example", "offset": 1, "limit": 5}),
    ("session_search", {"query": "HER-96", "limit": 3}),
    ("skill_view", {"name": "code"}),
    ("skills_list", {}),
    ("todo", {}),
    ("clarify", {"question": "Continue?"}),
])
def test_strict_valid_observations_are_allowed_without_claim(
        tmp_path, tool_name, tool_input):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload(tool_name, tool_input, outside))
    assert decision(result) == {"decision": "allow"}


@pytest.mark.parametrize(("tool_name", "tool_input"), [
    ("cronjob", {"action": "list", "unexpected": True}),
    ("computer_use", {"action": "capture", "max_elements": 0}),
    ("read_file", {"path": "/tmp/example", "limit": 0}),
    ("session_search", {"query": "HER-96", "limit": 11}),
    ("skill_view", {"name": "code", "unexpected": True}),
    ("todo", {"merge": False}),
])
def test_strict_ambiguous_observation_payloads_are_mutating_by_default(
        tmp_path, tool_name, tool_input):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload(tool_name, tool_input, outside))
    assert decision(result)["decision"] == "block"


@pytest.mark.parametrize("tool_name", [
    "execute_code", "project_create", "skill_manage", "delegate_task", "browser_click",
])
def test_unbounded_mutations_remain_blocked_after_worktree_claim(tmp_path, tool_name):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload(tool_name, {"action": "mutate"}, repo))
    assert decision(result)["decision"] == "block"


@pytest.mark.parametrize(("tool_name", "tool_input"), [
    ("kanban_show", {}),
    ("kanban_heartbeat", {"note": "working"}),
    ("kanban_complete", {"summary": "done", "metadata": {"tests_run": 1}}),
    ("kanban_block", {"reason": "needs review", "kind": "needs_input"}),
])
def test_exact_current_worker_lifecycle_is_admitted(
        tmp_path, monkeypatch, tool_name, tool_input):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    task_id = "t_exact"
    run_id = "17"
    session = f"kanban-{task_id}-run-{run_id}"
    assert admit(registry, repo, session=session).returncode == 0
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))

    result = run_hook(
        registry, payload(tool_name, tool_input, repo, session=session),
    )

    assert decision(result) == {"decision": "allow"}


@pytest.mark.parametrize("handoff_field", ["summary", "result"])
def test_exact_worker_completion_prose_cannot_create_scratch_attachment(
        tmp_path, monkeypatch, handoff_field):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="HER-118 — exact completion prose",
            assignee="code-a",
        )
        task = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, task_id, workspace)
        task = kb.claim_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
    assert task is not None
    assert run is not None
    workspace.rmdir()
    init_repo(workspace)

    registry = tmp_path / "registry"
    session = f"kanban-{task_id}-run-{run.id}"
    assert admit(
        registry,
        workspace,
        session=session,
        agent="code-a",
        profile="code-a",
    ).returncode == 0
    artifact = workspace / "exfiltrated.txt"
    artifact.write_text("exact-owner prose artifact bypass\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_EXACT_OWNER", "1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.get_current_board())
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    args = {handoff_field: f"Deliverable is at {artifact}"}

    hook_result = run_hook(
        registry,
        payload("kanban_complete", args, workspace, session=session),
        agent="code-a",
        profile="code-a",
    )
    assert decision(hook_result) == {"decision": "allow"}
    assert json.loads(kt._handle_complete(args))["ok"] is True

    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "done"
        assert kb.list_attachments(conn, task_id) == []
        completed = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "completed"
        ][-1]
        closed_run = kb.latest_run(conn, task_id)
    assert "artifacts" not in completed.payload
    assert closed_run is not None
    assert closed_run.summary == args[handoff_field]
    assert not kb.task_attachments_dir(task_id).exists()


@pytest.mark.parametrize("tool_name", [
    "kanban_show", "kanban_heartbeat", "kanban_complete", "kanban_block",
])
@pytest.mark.parametrize("override", [
    {"task_id": "t_exact"},
    {"task_id": "t_foreign"},
    {"board": "other"},
    {"db_path": "/tmp/other.db"},
    {"workspace": "/tmp/other"},
    {"profile": "other"},
    {"session_id": "other"},
    {"run_id": 18},
    {"claim_lock": "other"},
])
def test_worker_lifecycle_rejects_all_identity_and_routing_overrides(
        tmp_path, monkeypatch, tool_name, override):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    session = "kanban-t_exact-run-17"
    assert admit(registry, repo, session=session).returncode == 0
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_exact")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
    base = {
        "kanban_show": {},
        "kanban_heartbeat": {"note": "working"},
        "kanban_complete": {"summary": "done"},
        "kanban_block": {"reason": "blocked"},
    }[tool_name]

    result = run_hook(
        registry,
        payload(tool_name, {**base, **override}, repo, session=session),
    )

    assert decision(result)["decision"] == "block"


@pytest.mark.parametrize("run_id", [None, "", "invalid", "0", "-1"])
@pytest.mark.parametrize("tool_name", [
    "kanban_heartbeat", "kanban_complete", "kanban_block",
])
def test_worker_lifecycle_mutations_require_positive_run_id(
        tmp_path, monkeypatch, run_id, tool_name):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    session = "kanban-t_exact-run-17"
    assert admit(registry, repo, session=session).returncode == 0
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_exact")
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
    args = {
        "kanban_heartbeat": {"note": "working"},
        "kanban_complete": {"summary": "done"},
        "kanban_block": {"reason": "blocked"},
    }[tool_name]

    result = run_hook(
        registry, payload(tool_name, args, repo, session=session),
    )

    assert decision(result)["decision"] == "block"


@pytest.mark.parametrize("tool_name", [
    "kanban_comment", "kanban_create", "kanban_list", "kanban_link",
    "kanban_unblock", "kanban_attach", "kanban_attach_url",
    "kanban_attachments",
])
def test_all_other_kanban_tools_remain_blocked_after_exact_claim(
        tmp_path, monkeypatch, tool_name):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    session = "kanban-t_exact-run-17"
    assert admit(registry, repo, session=session).returncode == 0
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_exact")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))

    result = run_hook(
        registry, payload(tool_name, {}, repo, session=session),
    )

    assert decision(result)["decision"] == "block"


@pytest.mark.parametrize("action", ["write", "submit", "kill", "close", "wait"])
def test_process_mutations_and_wait_block_without_process_owner_proof(tmp_path, action):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    args = {"action": action, "session_id": "proc_foreign"}
    if action in {"write", "submit"}:
        args["data"] = "payload"
    result = run_hook(registry, payload("process", args, repo))
    assert decision(result)["decision"] == "block"


@pytest.mark.parametrize("args", [
    {"action": "list"},
    {"action": "poll", "session_id": "proc_x"},
    {"action": "log", "session_id": "proc_x", "offset": 0, "limit": 200},
])
def test_process_instant_observation_is_explicitly_readonly(tmp_path, args):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload("process", args, outside))
    assert decision(result) == {"decision": "allow"}


@pytest.mark.parametrize("args", [
    {"action": "poll"},
    {"action": "log", "session_id": "proc_x", "limit": 0},
    {"action": "log", "session_id": "proc_x", "limit": 1000000},
    {"action": "unknown"},
])
def test_process_ambiguous_observation_blocks(tmp_path, args):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload("process", args, outside))
    assert decision(result)["decision"] == "block"


def test_real_process_schema_dispatch_is_blocked_before_handler(tmp_path, monkeypatch):
    from agent import shell_hooks
    from hermes_cli import plugins
    import model_tools

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    plugins._plugin_manager = plugins.PluginManager()
    shell_hooks.reset_for_tests()
    command = " ".join([
        sys.executable, str(HOOK), "--registry", str(tmp_path / "missing"),
        "--agent", "code-a", "--profile", "code-a", "--only-mutating",
        "--require-owned-git",
    ])
    cfg = {"hooks": {"pre_tool_call": [{
        "command": command, "matcher": ".*", "fail_closed": True,
    }]}}
    assert "process" in model_tools.get_all_tool_names()
    assert len(shell_hooks.register_from_config(cfg, accept_hooks=True)) == 1
    called = False
    real_dispatch = model_tools.registry.dispatch

    def spy_dispatch(name, args, **kwargs):
        nonlocal called
        if name == "process":
            called = True
        return real_dispatch(name, args, **kwargs)

    monkeypatch.setattr(model_tools.registry, "dispatch", spy_dispatch)
    result = json.loads(model_tools.handle_function_call(
        "process", {"action": "kill", "session_id": "proc_foreign"}, session_id="s1",
    ))
    assert "error" in result
    assert called is False


@pytest.mark.parametrize("command", [
    # PoC S-B1 exacts : substitution de commande indirecte derrière une
    # commande externe readonly, sous owner valide.
    "C='rm -rf ~/.hermes/hermes-agent'; echo $($C)",
    "C='rm -rf ~/.hermes/hermes-agent'; echo $(eval \"$C\")",
    "C='git -C ~/.hermes/hermes-agent reset --hard'; printf $(sh -c \"$C\")",
    # Variantes de la même classe : exécution dynamique dans le corps.
    "C='rm -rf x'; echo `$C`",
    "echo $(sh -c 'touch pwned')",
    "printf $(date)",
    "echo $(echo $(touch pwned))",
])
def test_indirect_command_substitution_blocked_even_with_owner(tmp_path, command):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload("terminal", {"command": command}, repo))
    assert decision(result)["decision"] == "block", command


@pytest.mark.parametrize("command", [
    # Contrôles sûrs : corps littéraux de la grammaire readonly, variables
    # simples (valeur, pas exécution), mutation littérale dans le worktree.
    "echo $(pwd)",
    "echo $(git rev-parse HEAD)",
    "echo $HOME",
    "touch marker.txt",
])
def test_safe_substitution_bodies_and_literals_stay_admitted(tmp_path, command):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload("terminal", {"command": command}, repo))
    assert decision(result) == {"decision": "allow"}, command


@pytest.mark.parametrize("override", [
    {"session": "other"}, {"agent": "code-b"}, {"profile": "code-b"},
])
def test_owner_identity_mismatch_blocks(tmp_path, override):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    values = {"session": "s1", "agent": "code-a", "profile": "code-a"}
    values.update(override)
    assert admit(registry, repo, **values).returncode == 0
    result = run_hook(registry, payload("write_file", {"path": str(repo / "x")}, repo))
    assert decision(result)["decision"] == "block"


def test_dead_pid_start_mismatch_and_pending_owner_block(tmp_path):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    owner_path = registry / "locks" / "HER-96" / "owner.json"
    owner = json.loads(owner_path.read_text())
    for update in ({"pid": 99999999}, {"process_start_time": "definitely-wrong"},
                   {"state": "bootstrap_pending"}):
        modified = dict(owner)
        modified.update(update)
        owner_path.write_text(json.dumps(modified))
        result = run_hook(registry, payload("write_file", {"path": str(repo / "x")}, repo))
        assert decision(result)["decision"] == "block"


def test_non_strict_only_mutating_hook_keeps_legacy_unknown_tool_behavior(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = subprocess.run(
        [sys.executable, str(HOOK), "--registry", str(tmp_path / "missing"),
         "--agent", "legacy", "--profile", "legacy", "--only-mutating"],
        input=json.dumps(payload("future_tool", {}, outside)),
        text=True, capture_output=True, timeout=10,
    )
    assert decision(result) == {"decision": "allow"}


def test_fail_closed_bridge_and_legacy_compatibility(tmp_path, monkeypatch):
    from agent import shell_hooks

    outcomes = [
        {"returncode": None, "stdout": "", "stderr": "", "timed_out": True,
         "elapsed_seconds": 1.0, "error": None},
        {"returncode": None, "stdout": "", "stderr": "", "timed_out": False,
         "elapsed_seconds": 0.0, "error": "command not found"},
        {"returncode": 0, "stdout": "invalid", "stderr": "", "timed_out": False,
         "elapsed_seconds": 0.0, "error": None},
        {"returncode": 1, "stdout": "", "stderr": "boom", "timed_out": False,
         "elapsed_seconds": 0.0, "error": None},
    ]
    for outcome in outcomes:
        monkeypatch.setattr(shell_hooks, "_spawn", lambda *_a, value=outcome: value)
        required = shell_hooks.ShellHookSpec("pre_tool_call", "gate", fail_closed=True)
        legacy = shell_hooks.ShellHookSpec("pre_tool_call", "gate")
        assert shell_hooks._make_callback(required)(tool_name="terminal")["action"] == "block"
        assert shell_hooks._make_callback(legacy)(tool_name="terminal") is None


def write_policy(path: Path, repo: Path, parent: Path, **overrides):
    spec = {"repo": str(repo), "base_ref": "HEAD", "worktrees_parent": str(parent),
            "branch_prefix": "fix"}
    spec.update(overrides)
    path.write_text(json.dumps({"profiles": {"code-a": spec}}))


def bootstrap(registry, policy, *, key="HER-96", session="s1", profile="code-a"):
    return subprocess.run([
        sys.executable, str(LANE), "--registry", str(registry), "bootstrap", key,
        "--policy", str(policy), "--profile", profile, "--agent", "code-a",
        "--session", session, "--owner-pid", str(os.getpid()),
    ], text=True, capture_output=True, timeout=20)


def test_bootstrap_success_creates_and_activates_deterministic_worktree(tmp_path):
    repo, parent, registry, policy = (tmp_path / n for n in ("repo", "worktrees", "registry", "policy.json"))
    init_repo(repo)
    parent.mkdir()
    write_policy(policy, repo, parent)
    result = bootstrap(registry, policy)
    assert result.returncode == 0, result.stderr
    target = parent / "code-a-her-96"
    assert subprocess.run(["git", "-C", str(target), "rev-parse", "--show-toplevel"],
                          text=True, capture_output=True).stdout.strip() == str(target)
    owner = json.loads((registry / "locks" / "HER-96" / "owner.json").read_text())
    assert owner["state"] == "active"
    assert owner["agent"] == "code-a" and owner["profile"] == "code-a"
    assert owner["session_id"] == "s1" and owner["pid"] == os.getpid()
    assert owner["worktree"] == str(target)


def test_concurrent_bootstrap_collision_has_one_winner(tmp_path):
    repo, parent, registry, policy = (tmp_path / n for n in ("repo", "worktrees", "registry", "policy.json"))
    init_repo(repo)
    parent.mkdir()
    write_policy(policy, repo, parent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: bootstrap(registry, policy), range(2)))
    assert sorted(r.returncode for r in results) == [0, 1]
    assert (parent / "code-a-her-96").is_dir()


@pytest.mark.parametrize("collision", ["target", "branch"])
def test_bootstrap_refuses_existing_target_or_branch(tmp_path, collision):
    repo, parent, registry, policy = (tmp_path / n for n in ("repo", "worktrees", "registry", "policy.json"))
    init_repo(repo)
    parent.mkdir()
    write_policy(policy, repo, parent)
    if collision == "target":
        (parent / "code-a-her-96").mkdir()
    else:
        subprocess.run(["git", "branch", "fix/her-96"], cwd=repo, check=True)
    result = bootstrap(registry, policy)
    assert result.returncode == 1
    assert not (registry / "locks" / "HER-96" / "owner.json").exists()


def test_bootstrap_refuses_parent_symlink_and_policy_escape(tmp_path):
    repo, actual, registry = tmp_path / "repo", tmp_path / "actual", tmp_path / "registry"
    init_repo(repo)
    actual.mkdir()
    symlink = tmp_path / "worktrees"
    symlink.symlink_to(actual, target_is_directory=True)
    policy = tmp_path / "policy.json"
    write_policy(policy, repo, symlink)
    assert bootstrap(registry, policy).returncode == 1
    policy.write_text(json.dumps({"profiles": {"../escape": {
        "repo": str(repo), "base_ref": "HEAD", "worktrees_parent": str(actual),
        "branch_prefix": "fix"}}}))
    assert bootstrap(registry, policy, profile="../escape").returncode == 1


def test_bootstrap_git_add_failure_never_leaves_active_owner(tmp_path, monkeypatch):
    import scripts.factory_lane as factory_lane

    repo, parent, registry, policy = (tmp_path / n for n in ("repo", "worktrees", "registry", "policy.json"))
    init_repo(repo)
    parent.mkdir()
    write_policy(policy, repo, parent)
    real_run = factory_lane.subprocess.run

    def fail_add(argv, *args, **kwargs):
        if isinstance(argv, list) and "worktree" in argv and "add" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "injected add failure")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(factory_lane.subprocess, "run", fail_add)
    root = factory_lane._safe_registry_root(str(registry))
    with pytest.raises(factory_lane.RegistryError):
        factory_lane.cmd_bootstrap(root, "HER-96", str(policy), "code-a", "code-a", "s1",
                                   os.getpid(), None)
    owner_path = registry / "locks" / "HER-96" / "owner.json"
    assert not owner_path.exists() or json.loads(owner_path.read_text()).get("state") != "active"


def test_real_dispatch_path_blocks_before_terminal_execution(tmp_path, monkeypatch):
    from agent import shell_hooks
    from hermes_cli import plugins
    import model_tools

    registry, outside = tmp_path / "registry", tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    plugins._plugin_manager = plugins.PluginManager()
    shell_hooks.reset_for_tests()
    command = " ".join([
        sys.executable, str(HOOK), "--registry", str(registry), "--agent", "code-a",
        "--profile", "code-a", "--only-mutating", "--require-owned-git",
    ])
    cfg = {"hooks": {"pre_tool_call": [{"command": command,
           "matcher": ".*",
           "fail_closed": True}]}}
    assert len(shell_hooks.register_from_config(cfg, accept_hooks=True)) == 1
    result = json.loads(model_tools.handle_function_call(
        "terminal", {"command": "touch escaped", "workdir": str(outside)}, session_id="s1"))
    assert "error" in result
    assert not (outside / "escaped").exists()


def test_documented_config_uses_command_matcher_and_fail_closed_only():
    text = (REPO_ROOT / "docs" / "ai-factory-worktree-admission.md").read_text()
    gate_blocks = [block for block in re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
                   if "factory_admission_hook.py" in block]
    assert gate_blocks
    assert all("command:" in block and 'matcher: ".*"' in block and "fail_closed: true" in block
               and "--only-mutating" in block
               and "script:" not in block and "args:" not in block for block in gate_blocks)
    code_blocks = [block for block in gate_blocks if "hermes-code-a" in block]
    assert code_blocks and all("--require-owned-git" in block for block in code_blocks)


# ---------------------------------------------------------------------------
# R2-B1 — quote-state desynchronization must not hide active substitutions
# ---------------------------------------------------------------------------

# A single quote inside a double-quoted region is LITERAL to the shell. A
# scanner that flips into single-quote state there never recovers, so every
# later ``$(...)``/backtick becomes invisible and the command is judged safe.
QUOTE_DESYNC_PAYLOADS = [
    'git rev-parse "it\'s $(touch pwned)"',
    'gh pr list --search "it\'s $(touch pwned)"',
    'git rev-parse "\'" `touch pwned`',
    'echo "it\'s $(touch pwned)"',
    'echo "a\'b" $(sh -c \'touch pwned\')',
    # Same class, other shapes.
    'git rev-parse "don\'t" $(eval "rm -rf x")',
    'printf "it\'s" `sh -c "touch pwned"`',
]


def test_quote_desync_does_not_hide_variable_expansion_either():
    """A ``'`` inside double quotes must not blind the scanner to ``$VAR``.

    ``echo "it's ${HOME}"`` is not itself dangerous — ``echo`` never executes
    its argument — so this asserts the scanner's quote state directly rather
    than an end-to-end verdict, which is where the desync actually mattered.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import factory_admission_hook as hook

    for command in [
        'echo "it\'s ${HOME}"',
        'echo "it\'s $HOME"',
        'git rev-parse "don\'t" "$WT"',
    ]:
        assert hook._has_active_shell_expansion(command) is True, command
    # ...while a genuinely literal single-quoted dollar stays data.
    assert hook._has_active_shell_expansion("echo '$HOME'") is False


@pytest.mark.parametrize("command", QUOTE_DESYNC_PAYLOADS)
def test_quote_desync_substitution_blocked_preclaim(tmp_path, command):
    """Pre-claim: no readonly early-return may precede substitution inspection."""
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload("terminal", {"command": command}, outside))
    assert decision(result)["decision"] == "block", command


@pytest.mark.parametrize("command", QUOTE_DESYNC_PAYLOADS)
def test_quote_desync_substitution_blocked_with_valid_owner(tmp_path, command):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload("terminal", {"command": command}, repo))
    assert decision(result)["decision"] == "block", command


@pytest.mark.parametrize("command", [
    # Literal single quotes inside double quotes, with NO active substitution,
    # stay ordinary readonly discovery commands.
    'git rev-parse "it\'s-a-ref"',
    "git rev-parse 'HEAD'",
    'gh pr list --search "it\'s mine"',
])
def test_literal_quotes_without_substitution_stay_readonly(tmp_path, command):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload("terminal", {"command": command}, outside))
    assert decision(result) == {"decision": "allow"}, command


def test_shell_scanner_is_the_single_source_for_all_three_consumers():
    """Coherence: masking, body extraction and expansion detection must all
    derive from one scan, so they can never disagree about quote state."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import factory_admission_hook as hook

    corpus = [
        *QUOTE_DESYNC_PAYLOADS,
        "pwd", "git rev-parse HEAD", "echo $(pwd)", "echo `pwd`",
        "echo '$(not-active)'", 'echo "$(active)"', "echo \\$(escaped)",
        'echo "\\$(escaped-in-dq)"', "echo $(echo $(nested))",
        "echo $HOME", 'echo "${HOME}"', "echo 'single' \"double\"",
        "git status 'unterminated", 'git status "unterminated',
        "echo $(unbalanced", "echo `unbalanced", "echo trailing\\",
        'echo "a\'b"', "echo \"it's\"", "true",
    ]
    for command in corpus:
        scan = hook._scan_shell_command(command)
        expansion = hook._has_active_shell_expansion(command)
        masked = hook._mask_active_command_substitutions(command)
        bodies = hook._command_substitution_bodies(command)
        if scan is None:
            # Ambiguous input: every consumer must fail closed together.
            assert expansion is True, command
            assert masked is None, command
            assert bodies is None, command
            continue
        assert expansion == scan["has_active_expansion"], command
        assert masked == scan["masked"], command
        assert bodies == scan["bodies"], command
        # A recorded substitution body implies an active expansion.
        if bodies:
            assert expansion is True, command


def test_real_shell_oracle_confirms_blocked_payloads_would_have_executed(tmp_path):
    """Prove the payloads are real (harmless temp-dir sentinels only).

    Each command is rewritten to touch a sentinel inside an isolated temp
    directory. The real shell creates it — so the hook's BLOCK is what stands
    between an admitted worker and host mutation. Nothing destructive, no
    network, no path outside ``tmp_path``.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    for index, template in enumerate([
        'git rev-parse "it\'s $(touch {s})"',
        'echo "it\'s $(touch {s})"',
        'git rev-parse "\'" `touch {s}`',
        'echo "a\'b" $(sh -c \'touch {s}\')',
    ]):
        sandbox = tmp_path / f"oracle{index}"
        sandbox.mkdir()
        sentinel = sandbox / "sentinel"
        command = template.format(s=sentinel.name)
        # The hook must refuse it...
        assert decision(run_hook(
            tmp_path / "missing", payload("terminal", {"command": command}, outside),
        ))["decision"] == "block", command
        # ...and the real shell proves the payload was live.
        subprocess.run(
            ["sh", "-c", command], cwd=sandbox, capture_output=True, timeout=10,
        )
        assert sentinel.exists(), (
            f"oracle did not execute the payload, test is not proving anything: {command}"
        )


# ---------------------------------------------------------------------------
# R3-B3 — shell path expansions must not escape ownership resolution
# ---------------------------------------------------------------------------

# Bash expands these to paths the hook never resolves: the hook treats a
# literal ``~/x`` as relative to the workspace while the shell rewrites it to
# ``$HOME/x``. Same class for globs, braces and process substitution.
PATH_EXPANSION_PAYLOADS = [
    "touch ~/pwned",
    "git -C ~/other commit -am x",
    "cd ~/other && git commit -am x",
    "touch ~user/pwned",
    "git -C ~ status --short",
    "cp file ~/pwned",
    "touch pwned*",
    "rm -f build/*.o",
    "touch {a,b}",
    "cat <(touch pwned)",
    "tee >(touch pwned)",
    "touch a?b",
    "touch [ab]c",
]


@pytest.mark.parametrize("command", PATH_EXPANSION_PAYLOADS)
def test_path_expansion_is_unresolved_at_helper_level(command):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import factory_admission_hook as hook

    assert hook._terminal_has_unresolved_dynamic_target(command) is True, command


@pytest.mark.parametrize("command", PATH_EXPANSION_PAYLOADS)
def test_path_expansion_blocked_with_valid_owner(tmp_path, command):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload("terminal", {"command": command}, repo))
    assert decision(result)["decision"] == "block", command


@pytest.mark.parametrize("command", PATH_EXPANSION_PAYLOADS)
def test_path_expansion_blocked_preclaim(tmp_path, command):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload("terminal", {"command": command}, outside))
    assert decision(result)["decision"] == "block", command


@pytest.mark.parametrize("command", [
    # Literal, ownership-resolvable targets keep working.
    "touch pwned",
    "touch sub/file.txt",
    "git commit -am x",
    "git -C . status --short",
    "cd sub && git commit -am x",
    # A literal tilde that cannot expand: quoted, so it is ordinary data.
    "touch './~literal'",
    # Home-anchored discovery reads stay readonly through the normal grammar.
    "pwd",
])
def test_literal_targets_are_not_treated_as_expansion(tmp_path, command):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    (repo / "sub").mkdir()
    assert admit(registry, repo).returncode == 0
    result = run_hook(registry, payload("terminal", {"command": command}, repo))
    assert decision(result)["decision"] == "allow", command


# ---------------------------------------------------------------------------
# R4-B3 / R4-B4 / R4-B8a — path expansion in EVERY payload field, not just
# terminal `command`
# ---------------------------------------------------------------------------

EXPANDING_PATHS = ["~/pwned", "~", "~user/pwned", "build/*.o", "{a,b}", "a?b", "[ab]c"]


@pytest.mark.parametrize("bad", EXPANDING_PATHS)
def test_file_tool_path_payloads_reject_expansion(tmp_path, bad):
    """File tools call ``expanduser``; the hook must not see a workspace-local
    literal where the tool will write under ``$HOME``."""
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    for tool, args in [
        ("write_file", {"path": bad, "content": "x"}),
        ("patch", {"path": bad, "mode": "replace", "old_string": "a", "new_string": "b"}),
        ("edit_file", {"path": bad, "old": "a", "new": "b"}),
        ("create_file", {"path": bad, "content": "x"}),
        ("delete_file", {"path": bad}),
        ("str_replace_editor", {"path": bad, "old_str": "a", "new_str": "b"}),
    ]:
        result = run_hook(registry, payload(tool, args, repo))
        assert decision(result)["decision"] == "block", (tool, bad)


@pytest.mark.parametrize("bad", EXPANDING_PATHS)
def test_nested_apply_patch_change_paths_reject_expansion(tmp_path, bad):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    for args in [
        {"changes": [{"path": bad}]},
        {"changes": [{"path": "inside.txt"}, {"path": bad}]},
        {"changes": [{"file_path": bad}]},
        {"changes": [{"target_path": bad}]},
    ]:
        result = run_hook(registry, payload("apply_patch", args, repo))
        assert decision(result)["decision"] == "block", (args, bad)


@pytest.mark.parametrize("bad", EXPANDING_PATHS)
def test_move_file_source_and_destination_reject_expansion(tmp_path, bad):
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    for args in [
        {"path": bad, "target_path": "inside.txt"},
        {"path": "inside.txt", "target_path": bad},
        {"source_path": bad, "destination_path": "inside.txt"},
        {"file_path": "inside.txt", "destination": bad},
    ]:
        result = run_hook(registry, payload("move_file", args, repo))
        assert decision(result)["decision"] == "block", (args, bad)


@pytest.mark.parametrize("bad", EXPANDING_PATHS)
def test_terminal_workdir_rejects_expansion(tmp_path, bad):
    """R4-B4: the local backend expands ``workdir`` before running the command."""
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    assert admit(registry, repo).returncode == 0
    result = run_hook(
        registry, payload("terminal", {"command": "touch pwned", "workdir": bad}, repo),
    )
    assert decision(result)["decision"] == "block", bad


@pytest.mark.parametrize("command", [
    "git -C ~/x rev-parse HEAD",
    "git -C ~ rev-parse HEAD",
    "git -C build/* rev-parse HEAD",
])
def test_preclaim_readonly_rejects_tilde_and_glob_reads(tmp_path, command):
    """R4-B8a: no tilde read bypass on the pre-claim discovery grammar."""
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run_hook(tmp_path / "missing", payload("terminal", {"command": command}, outside))
    assert decision(result)["decision"] == "block", command


def test_literal_owned_payload_paths_stay_admitted(tmp_path):
    """Controls: ordinary literal targets inside the owned worktree still pass."""
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    init_repo(repo)
    (repo / "sub").mkdir()
    assert admit(registry, repo).returncode == 0
    for tool, args in [
        ("write_file", {"path": "inside.txt", "content": "x"}),
        ("write_file", {"path": str(repo / "sub" / "deep.txt"), "content": "x"}),
        ("apply_patch", {"changes": [{"path": "a.txt"}, {"path": "sub/b.txt"}]}),
        ("move_file", {"path": "a.txt", "target_path": "sub/b.txt"}),
        ("terminal", {"command": "touch pwned", "workdir": str(repo)}),
        ("terminal", {"command": "touch pwned", "workdir": "sub"}),
    ]:
        result = run_hook(registry, payload(tool, args, repo))
        assert decision(result)["decision"] == "allow", (tool, args)


def test_hook_path_key_table_covers_every_admitted_mutating_tool_schema():
    """R4-B3: the hook's proven target must be the path the tool resolves.

    Contract, not snapshot: every path-shaped parameter of every mutating tool
    the strict gate admits must appear in the hook's path-key table. A key the
    hook does not inspect is a key a worker can steer the tool through.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import factory_admission_hook as hook
    import model_tools

    schemas = {}
    for schema in model_tools.get_tool_definitions():
        function = schema.get("function", schema)
        name = function.get("name")
        if name:
            schemas[name] = function.get("parameters", {}).get("properties", {}) or {}

    covered = set(hook._PAYLOAD_PATH_KEYS)
    uncovered = {}
    for tool in sorted(hook._WORKTREE_MUTATION_TOOLS):
        for param in schemas.get(tool, {}):
            lowered = param.lower()
            looks_like_path = (
                lowered.endswith("path") or lowered.endswith("paths")
                or lowered in {"source", "destination", "src", "dst", "file", "files"}
            )
            if looks_like_path and param not in covered and param != "workdir":
                uncovered.setdefault(tool, []).append(param)
    assert not uncovered, (
        "these mutating-tool path parameters are invisible to the hook's "
        f"target validation: {uncovered}"
    )
