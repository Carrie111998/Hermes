"""Local Code A/B canary: two lanes, real spawn path, real hook, shared registry.

This is the hermetic replay of the 2026-07-30 incident, with both fleet
profiles running the *actual* controller spawn path (``_default_spawn`` →
owner claim → session seed → gate file → reaper) against the repo-shipped
admission hook, in one shared temp registry:

* Code A on its own canary issue, Code B on a distinct one (no lane collision);
* both owners exist simultaneously with exact identity (issue lane, profile,
  deterministic session, worker PID + start time, workspace realpath);
* each worker performs an admitted mutation + git commit in its own worktree;
* each worker's attempt to mutate the *other* lane's worktree is refused;
* each worker completes its card through the exact lifecycle gate;
* on worker exit, each reaper releases exactly its own lane's owner.

Everything lives under tmp_path: HERMES_HOME, kanban DB, registry, worktrees.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "factory_admission_hook.py"

PROBE_TEMPLATE = """
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

from agent import shell_hooks
from hermes_cli import plugins
import model_tools
import tools.kanban_tools  # noqa: F401

plugins._plugin_manager = plugins.PluginManager()
shell_hooks.reset_for_tests()
config = yaml.safe_load(
    (Path(os.environ['HERMES_HOME']) / 'config.yaml').read_text()
)
shell_hooks.register_from_config(config, accept_hooks=True)
session = os.environ['HERMES_SESSION_ID']
workspace = Path(os.environ['HERMES_KANBAN_WORKSPACE'])
foreign = Path(os.environ['PROBE_FOREIGN_WORKSPACE'])
issue = os.environ['PROBE_ISSUE']
owner_path = (
    Path(os.environ['PROBE_REGISTRY']) / 'locks' / issue / 'owner.json'
)
proof = {
    'session': session,
    'pid': os.getpid(),
    'owner': json.loads(owner_path.read_text()),
}

def hook_decision(tool, tool_input):
    payload = {
        'hook_event_name': 'pre_tool_call', 'tool_name': tool,
        'tool_input': tool_input, 'session_id': session,
        'cwd': str(workspace),
    }
    profile = os.environ['HERMES_PROFILE']
    result = subprocess.run(
        [sys.executable, os.environ['PROBE_HOOK'],
         '--registry', os.environ['PROBE_REGISTRY'],
         '--agent', profile, '--profile', profile,
         '--only-mutating', '--require-owned-git'],
        input=json.dumps(payload), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)

own_target = workspace / 'canary-mutation.txt'
proof['own_write_decision'] = hook_decision(
    'write_file', {'path': str(own_target), 'content': 'admitted\\n'},
)
if proof['own_write_decision'].get('decision') == 'allow':
    own_target.write_text('admitted\\n')

for label, command in (
    ('own_add', 'git add canary-mutation.txt'),
    ('own_commit', 'git commit -m canary-admitted'),
):
    proof[label] = hook_decision(
        'terminal', {'command': command, 'workdir': str(workspace)},
    )
    if proof[label].get('decision') == 'allow':
        subprocess.run(command.split(), cwd=workspace, check=True,
                       capture_output=True, text=True)
proof['head_after_commit'] = subprocess.run(
    ['git', 'rev-parse', 'HEAD'], cwd=workspace,
    capture_output=True, text=True,
).stdout.strip()

# Foreign-lane refusal, on both mutation surfaces the worker would use.
proof['foreign_write_decision'] = hook_decision(
    'write_file',
    {'path': str(foreign / 'intrusion.txt'), 'content': 'forbidden\\n'},
)
proof['foreign_commit_decision'] = hook_decision(
    'terminal',
    {'command': 'git add intrusion.txt', 'workdir': str(foreign)},
)
# The in-process pipeline must refuse it too (fail-closed shell hook bridge).
proof['foreign_write'] = json.loads(model_tools.handle_function_call(
    'write_file',
    {'path': str(foreign / 'intrusion.txt'), 'content': 'forbidden\\n'},
    session_id=session,
))

proof['complete'] = json.loads(model_tools.handle_function_call(
    'kanban_complete', {'summary': 'canary lane proven'}, session_id=session,
))

Path(os.environ['PROBE_RESULT']).write_text(json.dumps(proof))
release = Path(os.environ['PROBE_RELEASE'])
for _ in range(600):
    if release.exists():
        break
    time.sleep(0.025)
else:
    raise TimeoutError('parent never released the canary worker')
""".lstrip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


def _profile(root: Path, registry: Path, name: str) -> Path:
    registry.mkdir(parents=True, exist_ok=True)
    profile = root / "profiles" / name
    profile.mkdir(parents=True)
    command = " ".join([
        sys.executable, str(HOOK),
        "--registry", str(registry),
        "--agent", name, "--profile", name,
        "--only-mutating", "--require-owned-git",
    ])
    profile.joinpath("config.yaml").write_text(
        "hooks:\n"
        "  pre_tool_call:\n"
        "    - matcher: '.*'\n"
        "      fail_closed: true\n"
        f"      command: {json.dumps(command)}\n",
        encoding="utf-8",
    )
    return profile


@pytest.fixture(autouse=True)
def _isolate_kanban_spawn_environment(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles as profile_module

    def test_home() -> Path:
        return Path(
            os.environ.get("HERMES_KANBAN_HOME")
            or os.environ.get("HERMES_HOME")
            or Path.home() / ".hermes"
        )

    def resolve_test_profile(profile_name: str) -> str:
        profile = test_home() / "profiles" / profile_name
        if not profile.is_dir():
            raise FileNotFoundError(profile)
        return str(profile)

    monkeypatch.setattr(kb, "kanban_home", test_home)
    monkeypatch.setattr(profile_module, "resolve_profile_env", resolve_test_profile)


def _wait_for(predicate, *, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def test_code_a_and_code_b_canary_lanes_run_without_collision(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(root / "kanban-test.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv(
        "HERMES_KANBAN_WORKSPACES_ROOT", str(root / "kanban" / "workspaces")
    )
    root.mkdir()
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")

    lanes = {
        "hermes-code-a": {"issue": "HER-9901"},
        "hermes-code-b": {"issue": "HER-9902"},
    }
    for name, lane in lanes.items():
        _profile(root, registry, name)
        workspace = tmp_path / f"ws-{name}"
        _init_repo(workspace)
        lane["workspace"] = workspace

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    probe = tmp_path / "worker_probe.py"
    probe.write_text(PROBE_TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(probe)],
    )
    # The canary drives the real controller spawn path with a deterministic
    # probe worker, which is not a Hermes launcher; the launcher attestation
    # has its own dedicated wrapper-chain coverage in
    # test_kanban_code_owner_bootstrap.py.
    monkeypatch.setattr(kb, "_attest_worker_admission_hook_armed", lambda **kw: None)

    names = list(lanes)
    with kb.connect() as conn:
        for name in names:
            lane = lanes[name]
            task_id = kb.create_task(
                conn,
                title=f"{lane['issue']} — canary {name}",
                body=f"Canary lane for {name}",
                assignee=name,
                workspace_kind="dir",
                workspace_path=str(lane["workspace"]),
            )
            lane["task"] = kb.claim_task(conn, task_id)
            assert lane["task"] is not None

    monkeypatch.setenv("PROBE_REGISTRY", str(registry))
    monkeypatch.setenv("PROBE_HOOK", str(HOOK))
    for name in names:
        lane = lanes[name]
        other = lanes[names[1] if name == names[0] else names[0]]
        lane["result"] = tmp_path / f"result-{name}.json"
        lane["release"] = tmp_path / f"release-{name}"
        monkeypatch.setenv("PROBE_ISSUE", lane["issue"])
        monkeypatch.setenv("PROBE_FOREIGN_WORKSPACE", str(other["workspace"]))
        monkeypatch.setenv("PROBE_RESULT", str(lane["result"]))
        monkeypatch.setenv("PROBE_RELEASE", str(lane["release"]))
        lane["pid"] = kb._default_spawn(lane["task"], str(lane["workspace"]))
        assert lane["pid"], f"spawn failed for {name}"
        _wait_for(
            lane["result"].exists, timeout=30,
            message=f"canary worker {name} produced no proof "
            f"(log: {(root / 'kanban' / 'logs').glob('*.log')})",
        )

    # Both lanes must hold their exact owner simultaneously — no collision,
    # no cross-lane takeover.
    owners = {}
    for name in names:
        lane = lanes[name]
        owner_path = registry / "locks" / lane["issue"] / "owner.json"
        assert owner_path.is_file(), f"{name} lane has no live owner"
        owners[name] = json.loads(owner_path.read_text())

    for name in names:
        lane, owner = lanes[name], owners[name]
        task = lane["task"]
        expected_session = f"kanban-{task.id}-run-{task.current_run_id}"
        proof = json.loads(lane["result"].read_text())

        assert owner["profile"] == name
        assert owner["agent"] == name
        assert owner["session_id"] == expected_session
        assert owner["pid"] == lane["pid"] == proof["pid"]
        assert owner["worktree"] == str(lane["workspace"].resolve())
        assert proof["session"] == expected_session

        # Own-lane mutation admitted by the real hook, then executed for real.
        assert proof["own_write_decision"] == {"decision": "allow"}
        assert (lane["workspace"] / "canary-mutation.txt").read_text() == "admitted\n"
        assert proof["own_add"] == {"decision": "allow"}
        assert proof["own_commit"] == {"decision": "allow"}
        head = subprocess.run(
            ["git", "log", "-1", "--format=%s"], cwd=lane["workspace"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert head == "canary-admitted"

        # Foreign-lane mutation refused before execution, on every surface.
        assert proof["foreign_write_decision"].get("decision") == "block"
        assert proof["foreign_commit_decision"].get("decision") == "block"
        assert "error" in proof["foreign_write"], proof["foreign_write"]
        assert not (lane["workspace"] / "intrusion.txt").exists()

        # Exact worker lifecycle completion went through.
        assert "error" not in proof["complete"], proof["complete"]

    for name in names:
        assert not (lanes[name]["workspace"] / "intrusion.txt").exists()

    # Let the workers exit; each reaper must release exactly its lane's owner.
    for name in names:
        lanes[name]["release"].write_text("release\n", encoding="utf-8")
    for name in names:
        lane = lanes[name]
        owner_path = registry / "locks" / lane["issue"] / "owner.json"
        _wait_for(
            lambda p=owner_path: not p.exists(), timeout=30,
            message=f"reaper never released the {name} lane owner",
        )

    with kb.connect() as conn:
        for name in names:
            task = kb.get_task(conn, lanes[name]["task"].id)
            assert task.status == "done", (
                f"{name} canary card should complete, got {task.status}"
            )


def test_code_a_and_code_b_canary_with_real_attestation_and_launcher(
    monkeypatch, tmp_path,
):
    """R4-B8d: one full A/B canary that does NOT stub the attestation.

    The other canary drives the controller path with a deterministic probe
    worker, which is not a Hermes launcher, so it stubs the armed check. This
    one closes that gap: both lanes go through the genuine
    ``_attest_worker_admission_hook_armed`` against a real wrapper chain
    (bash -> sh -> console script), a real nonce and a real trusted-launcher
    check, so no lane can open its gate without a verdict from its own install.
    """
    from hermes_cli import kanban_db as kb

    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    monkeypatch.setenv("HERMES_HOME", str(root))

    # A launcher chain shaped exactly like the packaged install.
    bindir = tmp_path / "install" / "venv" / "bin"
    bindir.mkdir(parents=True)
    console = bindir / "hermes"
    console.write_text(
        "#!/bin/sh\n"
        "'''exec' \"%s\" \"$0\" \"$@\"\n"
        "' '''\n"
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from hermes_cli.main import main\n"
        "sys.exit(main())\n" % sys.executable,
        encoding="utf-8",
    )
    console.chmod(0o755)
    outer_dir = tmp_path / "install" / "bin"
    outer_dir.mkdir(parents=True)
    launcher = outer_dir / "hermes"
    launcher.write_text(
        "#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\n"
        f'exec "{console}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    verdicts = []
    real_attest = kb._attest_worker_admission_hook_armed

    def traced_attest(**kwargs):
        verdicts.append(kwargs["profile_home"].name)
        return real_attest(**kwargs)

    monkeypatch.setattr(kb, "_attest_worker_admission_hook_armed", traced_attest)

    for lane in ("hermes-code-a", "hermes-code-b"):
        profile = root / "profiles" / lane
        profile.mkdir(parents=True)
        command = " ".join([
            sys.executable, str(HOOK), "--registry", str(registry),
            "--agent", lane, "--profile", lane,
            "--only-mutating", "--require-owned-git",
        ])
        profile.joinpath("config.yaml").write_text(
            "hooks:\n  pre_tool_call:\n    - matcher: '.*'\n"
            "      fail_closed: true\n"
            f"      command: {json.dumps(command)}\n",
            encoding="utf-8",
        )
        root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")

        workspace = tmp_path / f"ws-{lane}"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)

        env = dict(os.environ)
        env["HERMES_HOME"] = str(profile)
        # The genuine armed check, through the genuine wrapper chain.
        kb._attest_worker_admission_hook_armed(
            profile_home=profile, env=env, worker_argv=[str(launcher)],
            cwd=str(workspace), workspace=str(workspace),
        )

        # A lane whose profile drops the factory hook must be refused, so the
        # pass above is a real verdict and not a vacuous one.
        profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="admission hook|attestation"):
            kb._attest_worker_admission_hook_armed(
                profile_home=profile, env=env, worker_argv=[str(launcher)],
                cwd=str(workspace), workspace=str(workspace),
            )

    # Both lanes went through the genuine attestation, twice each (the armed
    # profile and its disarmed control).
    assert verdicts == [
        "hermes-code-a", "hermes-code-a", "hermes-code-b", "hermes-code-b",
    ], verdicts


def test_code_a_and_code_b_canary_module_fallback_hostile_cwd(monkeypatch, tmp_path):
    """R5-B1 #4: both lanes on the module fallback, with a hostile cwd.

    The console-script canary cannot exercise this: only ``python -m`` puts the
    worker's own worktree on ``sys.path``. Each lane plants a fake
    ``hermes_cli.main`` that would emit a perfectly-shaped forged verdict for
    the fresh nonce; neither may run, and neither lane may be admitted on it.
    """
    from hermes_cli import kanban_db as kb

    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    monkeypatch.setenv("HERMES_HOME", str(root))

    for lane in ("hermes-code-a", "hermes-code-b"):
        profile = root / "profiles" / lane
        profile.mkdir(parents=True)
        command = " ".join([
            sys.executable, str(HOOK), "--registry", str(registry),
            "--agent", lane, "--profile", lane,
            "--only-mutating", "--require-owned-git",
        ])
        profile.joinpath("config.yaml").write_text(
            "hooks:\n  pre_tool_call:\n    - matcher: '.*'\n"
            "      fail_closed: true\n"
            f"      command: {json.dumps(command)}\n",
            encoding="utf-8",
        )
        root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")

        workspace = tmp_path / f"ws-{lane}"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        executed = tmp_path / f"{lane}-fake-executed"
        package = workspace / "hermes_cli"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "main.py").write_text(
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(executed)!r}).write_text('ran')\n"
            "print('HERMES_FACTORY_ATTEST ' + json.dumps({\n"
            "    'nonce': sys.argv[-1], 'armed': ['forged --require-owned-git'],\n"
            f"    'tree': {str(REPO_ROOT)!r}, 'safe_path': True, 'error': None,\n"
            "}))\n",
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["HERMES_HOME"] = str(profile)
        with pytest.raises(RuntimeError):
            kb._attest_worker_admission_hook_armed(
                profile_home=profile, env=env,
                worker_argv=[sys.executable, "-m", "hermes_cli.main"],
                cwd=str(workspace), workspace=str(workspace),
            )
        assert not executed.exists(), (
            f"lane {lane}: the planted module ran — attestation imported "
            "attacker-controlled code"
        )
