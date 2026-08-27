"""Behavior contracts for cross-process skill mutation authority."""

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


_SKILL = (
    "---\nname: test-skill\ndescription: A test skill\nversion: 1.0.0\n---\n"
    "# Test\nbody\n"
)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _set_skill_approval(enabled: bool) -> None:
    import hermes_cli.config as config

    current = config.load_config()
    current.setdefault("skills", {})["write_approval"] = enabled
    config.save_config(current)


def _stage_rewrite():
    from tools import write_approval as approval
    from tools.skill_manager_tool import _create_skill, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    _set_skill_approval(True)
    proposed = _SKILL.replace("body", "proposed rewrite C")
    staged = json.loads(
        skill_manage(action="patch", name="test-skill", content=proposed)
    )
    assert staged["staged"] is True, staged
    record = approval.get_pending(approval.SKILLS, staged["pending_id"])
    assert record is not None
    return created, proposed, staged, record


def _wait_for_paths(*paths: Path) -> None:
    deadline = time.monotonic() + 10
    while not all(path.exists() for path in paths):
        if time.monotonic() >= deadline:
            missing = [str(path) for path in paths if not path.exists()]
            raise AssertionError(f"timed out waiting for child barriers: {missing}")
        time.sleep(0.01)


def test_scan_rejection_settles_under_the_original_lease(
    hermes_home, monkeypatch
):
    """Rollback uses the publication lease; there is no second admission."""
    from tools import skill_manager_tool as manager

    created, _proposed, _staged, record = _stage_rewrite()
    real_lease = manager._skill_mutation_lease
    lease_calls = []

    @contextlib.contextmanager
    def _counted_lease(identity):
        lease_calls.append(identity)
        with real_lease(identity) as admitted:
            yield admitted

    monkeypatch.setattr(manager, "_skill_mutation_lease", _counted_lease)
    monkeypatch.setattr(manager, "_security_scan_skill", lambda _path: "blocked")

    result = json.loads(
        manager.apply_skill_pending(
            record["payload"], precondition=record["precondition"]
        )
    )

    assert result["success"] is False
    assert result["settlement"] == "restored"
    assert len(lease_calls) == 1
    assert Path(created["skill_md"]).read_text(encoding="utf-8") == _SKILL


def test_rejected_supporting_file_restores_the_complete_skill_tree(
    hermes_home, monkeypatch
):
    """Rollback removes directories created solely for the rejected file."""
    from tools import skill_manager_tool as manager

    created = manager._create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    skill_dir = Path(created["skill_md"]).parent
    monkeypatch.setattr(manager, "_security_scan_skill", lambda _path: "blocked")

    result = manager._write_file(
        "test-skill",
        "references/nested/rejected.md",
        "rejected bytes",
    )

    assert result["success"] is False
    assert result["settlement"] == "restored"
    assert not (skill_dir / "references").exists()


@pytest.mark.parametrize("failure", ["snapshot", "restore"])
def test_scan_rejection_reports_indeterminate_settlement_distinctly(
    hermes_home, monkeypatch, failure
):
    """Rollback failure cannot masquerade as an ordinary settled rejection."""
    from tools import skill_mutation_authority as authority
    from tools import skill_manager_tool as manager
    from tools import write_approval as approval

    created, proposed, staged, record = _stage_rewrite()

    def _reject_and_break_settlement(_path):
        if failure == "snapshot":
            def _snapshot_failure(_target):
                raise OSError("deterministic rollback snapshot failure")

            monkeypatch.setattr(authority, "snapshot_path_state", _snapshot_failure)
        else:
            def _restore_failure(*_args, **_kwargs):
                raise OSError("deterministic rollback restore failure")

            monkeypatch.setattr(authority, "atomic_write_text", _restore_failure)
        return "blocked by deterministic security scan"

    monkeypatch.setattr(manager, "_security_scan_skill", _reject_and_break_settlement)
    result = json.loads(
        manager.apply_skill_pending(
            record["payload"], precondition=record["precondition"]
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "security_scan_settlement_failed"
    assert result["settlement"] == "failed"
    assert "terminal filesystem state" in result["error"]
    assert Path(created["skill_md"]).read_text(encoding="utf-8") == proposed
    assert approval.get_pending(approval.SKILLS, staged["pending_id"]) is not None


def test_independent_profiles_share_one_external_target_lease(tmp_path):
    """Different HERMES_HOME values cannot lease the same target twice."""
    repo = Path(__file__).resolve().parents[2]
    target = tmp_path / "external-vault" / "shared-skill"
    target.mkdir(parents=True)
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    acquired = tmp_path / "first-acquired"
    release = tmp_path / "release-first"

    holder_code = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"os.environ['HERMES_HOME'] = {str(home_a)!r}\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "from tools import skill_mutation_authority as authority\n"
        f"target = Path({str(target)!r})\n"
        f"acquired = Path({str(acquired)!r})\n"
        f"release = Path({str(release)!r})\n"
        "with authority.skill_mutation_lease(target) as admitted:\n"
        "    assert admitted\n"
        "    acquired.write_text('held', encoding='utf-8')\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not release.exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n"
    )
    contender_code = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"os.environ['HERMES_HOME'] = {str(home_b)!r}\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "from tools import skill_mutation_authority as authority\n"
        "authority.SKILL_MUTATION_LOCK_TIMEOUT_SECONDS = 0.1\n"
        "authority.SKILL_MUTATION_LOCK_POLL_SECONDS = 0.01\n"
        f"with authority.skill_mutation_lease(Path({str(target)!r})) as admitted:\n"
        "    print(json.dumps({'admitted': admitted}), flush=True)\n"
    )

    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    _wait_for_paths(acquired)
    blocked = subprocess.run(
        [sys.executable, "-c", contender_code],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert blocked.returncode == 0, blocked.stdout + blocked.stderr
    assert json.loads(blocked.stdout)["admitted"] is False

    release.write_text("release", encoding="utf-8")
    holder_stdout, holder_stderr = holder.communicate(timeout=10)
    assert holder.returncode == 0, holder_stdout + holder_stderr

    admitted = subprocess.run(
        [sys.executable, "-c", contender_code],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert admitted.returncode == 0, admitted.stdout + admitted.stderr
    assert json.loads(admitted.stdout)["admitted"] is True


def test_independent_same_name_creates_serialize_across_categories(tmp_path):
    """Two categories cannot concurrently publish the same logical name."""
    repo = Path(__file__).resolve().parents[2]
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    ready_a = tmp_path / "ready-a"
    ready_b = tmp_path / "ready-b"
    release = tmp_path / "release-create-race"
    result_a = tmp_path / "result-a.json"
    result_b = tmp_path / "result-b.json"

    def _creator_code(category: str, ready: Path, output: Path) -> str:
        return (
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            f"os.environ['HERMES_HOME'] = {str(hermes_home)!r}\n"
            f"sys.path.insert(0, {str(repo)!r})\n"
            "from tools import skill_manager_tool as manager\n"
            "real_find = manager._find_skill\n"
            "first = True\n"
            "def synchronized_find(name):\n"
            "    global first\n"
            "    result = real_find(name)\n"
            "    if first:\n"
            "        first = False\n"
            "        assert result is None\n"
            f"        Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
            "        deadline = time.monotonic() + 10\n"
            f"        while not Path({str(release)!r}).exists():\n"
            "            assert time.monotonic() < deadline\n"
            "            time.sleep(0.01)\n"
            "    return result\n"
            "manager._find_skill = synchronized_find\n"
            f"content = {_SKILL!r}\n"
            f"result = manager._create_skill('test-skill', content, {category!r})\n"
            f"Path({str(output)!r}).write_text(json.dumps(result), encoding='utf-8')\n"
        )

    first = subprocess.Popen(
        [sys.executable, "-c", _creator_code("category-a", ready_a, result_a)],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    second = subprocess.Popen(
        [sys.executable, "-c", _creator_code("category-b", ready_b, result_b)],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    _wait_for_paths(ready_a, ready_b)
    release.write_text("go", encoding="utf-8")
    first_stdout, first_stderr = first.communicate(timeout=20)
    second_stdout, second_stderr = second.communicate(timeout=20)
    assert first.returncode == 0, first_stdout + first_stderr
    assert second.returncode == 0, second_stdout + second_stderr

    results = [
        json.loads(result_a.read_text(encoding="utf-8")),
        json.loads(result_b.read_text(encoding="utf-8")),
    ]
    assert sum(result["success"] is True for result in results) == 1
    created = list((hermes_home / "skills").glob("*/test-skill/SKILL.md"))
    assert len(created) == 1
