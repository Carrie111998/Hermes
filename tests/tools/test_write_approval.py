"""Tests for the memory/skill write-approval gate (tools/write_approval.py)
and the shared slash-command handlers (hermes_cli/write_approval_commands.py).

Covers the boolean write_approval gate (off by default = write freely; on =
require approval) for both subsystems, the foreground-vs-background staging
split, pending store CRUD, and the list/approve/reject/diff/approval
subcommand dispatch.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import shutil
import time
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_wa_test_")
    home = os.path.join(d, ".hermes")
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(d, ignore_errors=True)


def _set_approval(subsystem, enabled):
    import hermes_cli.config as cfg
    c = cfg.load_config()
    c.setdefault(subsystem, {})["write_approval"] = enabled
    cfg.save_config(c)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def test_default_gate_is_off(hermes_home):
    from tools import write_approval as wa
    # Default: gate off → writes flow freely.
    assert wa.write_approval_enabled("memory") is False
    assert wa.write_approval_enabled("skills") is False


def test_invalid_subsystem_is_off(hermes_home):
    from tools import write_approval as wa
    assert wa.write_approval_enabled("bogus") is False


def test_normalize_enabled_coerces_values():
    from tools import write_approval as wa
    # Real bools pass through.
    assert wa._normalize_enabled(True) is True
    assert wa._normalize_enabled(False) is False
    # Truthy strings → True (incl. legacy 'approve').
    assert wa._normalize_enabled("on") is True
    assert wa._normalize_enabled("approve") is True
    assert wa._normalize_enabled("true") is True
    # Everything else → False (gate off is the safe default).
    assert wa._normalize_enabled("off") is False
    assert wa._normalize_enabled("garbage") is False
    assert wa._normalize_enabled(None) is False


# ---------------------------------------------------------------------------
# Memory gate
# ---------------------------------------------------------------------------

def test_memory_gate_off_allows_write(hermes_home):
    # Default (gate off) → write straight through, no staging.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "user", "save me", store=store))
    assert r["success"] is True
    assert r["entry_count"] == 1
    assert wa.pending_count("memory") == 0


def test_cli_memory_approve_without_live_agent_uses_fresh_store(hermes_home, capsys):
    """#46783: ``/memory approve`` from a context with no live agent (e.g. the
    Desktop GUI) passed ``memory_store=None`` into the shared handler, which
    returned "memory store unavailable" and applied nothing. The CLI handler must
    fall back to a freshly loaded on-disk store, like the gateway path does."""
    import json
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    _set_approval("memory", True)
    staging = MemoryStore(); staging.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "remember the launch date", store=staging))
    assert r.get("pending_id"), r
    assert wa.pending_count("memory") == 1

    # Bare CLI handler with no live agent → store resolves to None pre-fix.
    handler = CLICommandsMixin.__new__(CLICommandsMixin)
    handler.agent = None
    handler._handle_memory_command("/memory approve all")

    out = capsys.readouterr().out
    assert "memory store unavailable" not in out, out
    assert "Approved 1" in out, out
    assert wa.pending_count("memory") == 0
    # The approved write landed in a freshly loaded on-disk store (MEMORY.md).
    reloaded = MemoryStore(); reloaded.load_from_disk()
    assert any("remember the launch date" in e for e in reloaded.memory_entries)


def test_load_on_disk_store_honors_configured_limits_and_permissions(hermes_home, monkeypatch):
    """Fresh approval stores must match the live agent's limits and target gates."""
    from tools.memory_tool import load_on_disk_store

    # Config override path: helper picks up configured limits and store flags.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "memory": {
                "memory_char_limit": 999,
                "user_char_limit": 444,
                "memory_enabled": False,
                "user_profile_enabled": True,
            }
        },
    )
    store = load_on_disk_store()
    assert store.memory_char_limit == 999
    assert store.user_char_limit == 444
    assert store.memory_enabled is False
    assert store.user_profile_enabled is True

    # Failure path: config raises → defaults, never blows up.
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    fallback = load_on_disk_store()
    assert fallback.memory_char_limit == 2200
    assert fallback.user_char_limit == 1375
    assert fallback.memory_enabled is True
    assert fallback.user_profile_enabled is True


# ---------------------------------------------------------------------------
# Skill gate
# ---------------------------------------------------------------------------

_SKILL = (
    "---\nname: test-skill\ndescription: A test skill\nversion: 1.0.0\n---\n"
    "# Test\nbody\n"
)


def test_skill_approval_preserves_newer_edit_when_pending_rewrite_is_stale(
    hermes_home,
):
    """Approving an old proposal must not overwrite a newer live edit."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, _edit_skill, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created

    _set_approval("skills", True)
    stale_rewrite = _SKILL.replace("body", "stale pending rewrite")
    staged = json.loads(
        skill_manage(action="patch", name="test-skill", content=stale_rewrite)
    )
    assert staged["staged"] is True, staged
    pending_id = staged["pending_id"]

    # The Desktop editor writes through the same direct edit path while the
    # older agent proposal remains queued for later review.
    newer_edit = _SKILL.replace("body", "newer Desktop edit")
    edited = _edit_skill("test-skill", newer_edit)
    assert edited["success"] is True, edited

    result = handle_pending_subcommand(wa.SKILLS, ["approve", pending_id])

    skill_md = Path(created["skill_md"])
    assert skill_md.read_text(encoding="utf-8") == newer_edit, result
    assert wa.get_pending(wa.SKILLS, pending_id) is not None


def test_legacy_pending_skill_write_without_precondition_fails_closed(hermes_home):
    """Old queue records are reviewable but never safe to replay blindly."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    legacy_rewrite = _SKILL.replace("body", "unverifiable legacy rewrite")
    record = wa.stage_write(
        wa.SKILLS,
        {"action": "patch", "name": "test-skill", "content": legacy_rewrite},
        summary="legacy rewrite",
        origin="background_review",
    )

    result = handle_pending_subcommand(wa.SKILLS, ["approve", record["id"]])

    assert result is not None
    assert "Approved 0" in result
    assert "cannot be safely applied" in result
    assert Path(created["skill_md"]).read_text(encoding="utf-8") == _SKILL
    assert wa.get_pending(wa.SKILLS, record["id"]) is not None


def test_approve_all_applies_safe_skill_write_and_preserves_conflict(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, _edit_skill, skill_manage

    safe_initial = _SKILL.replace("test-skill", "safe-skill")
    conflict_initial = _SKILL.replace("test-skill", "conflict-skill")
    safe_created = _create_skill("safe-skill", safe_initial)
    conflict_created = _create_skill("conflict-skill", conflict_initial)
    assert safe_created["success"] is True, safe_created
    assert conflict_created["success"] is True, conflict_created

    _set_approval("skills", True)
    safe_rewrite = safe_initial.replace("body", "safe queued rewrite")
    conflict_rewrite = conflict_initial.replace("body", "stale queued rewrite")
    safe_staged = json.loads(
        skill_manage(action="patch", name="safe-skill", content=safe_rewrite)
    )
    conflict_staged = json.loads(
        skill_manage(action="patch", name="conflict-skill", content=conflict_rewrite)
    )

    newer_edit = conflict_initial.replace("body", "newer Desktop edit")
    edited = _edit_skill("conflict-skill", newer_edit)
    assert edited["success"] is True, edited

    result = handle_pending_subcommand(wa.SKILLS, ["approve", "all"])

    assert result is not None
    assert "Approved 1" in result
    assert conflict_staged["pending_id"] in result
    assert Path(safe_created["skill_md"]).read_text(encoding="utf-8") == safe_rewrite
    assert Path(conflict_created["skill_md"]).read_text(encoding="utf-8") == newer_edit
    assert wa.get_pending(wa.SKILLS, safe_staged["pending_id"]) is None
    assert wa.get_pending(wa.SKILLS, conflict_staged["pending_id"]) is not None


@pytest.mark.parametrize(
    ("action", "staged_kwargs"),
    [
        ("write_file", {"file_content": "stale replacement"}),
        ("remove_file", {}),
        ("patch", {"old_string": "original file", "new_string": "stale patch"}),
    ],
)
def test_pending_supporting_file_mutation_preserves_newer_file(
    hermes_home,
    action,
    staged_kwargs,
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, _write_file, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    initial_file = _write_file("test-skill", "references/note.md", "original file")
    assert initial_file["success"] is True, initial_file

    _set_approval("skills", True)
    staged = json.loads(
        skill_manage(
            action=action,
            name="test-skill",
            file_path="references/note.md",
            **staged_kwargs,
        )
    )
    newer_file = _write_file("test-skill", "references/note.md", "newer file")
    assert newer_file["success"] is True, newer_file

    result = handle_pending_subcommand(wa.SKILLS, ["approve", staged["pending_id"]])

    target = Path(created["skill_md"]).parent / "references" / "note.md"
    assert result is not None
    assert "Approved 0" in result
    assert target.read_text(encoding="utf-8") == "newer file"
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


def test_pending_delete_conflicts_when_skill_tree_changes(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, _write_file, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    _set_approval("skills", True)
    staged = json.loads(
        skill_manage(action="delete", name="test-skill", absorbed_into="")
    )
    newer_file = _write_file("test-skill", "references/note.md", "newer file")
    assert newer_file["success"] is True, newer_file

    result = handle_pending_subcommand(wa.SKILLS, ["approve", staged["pending_id"]])

    assert result is not None
    assert "Approved 0" in result
    assert Path(created["skill_md"]).exists()
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


def test_pending_create_conflicts_when_skill_is_created_before_approval(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, skill_manage

    _set_approval("skills", True)
    stale_create = _SKILL.replace("body", "stale proposed skill")
    staged = json.loads(
        skill_manage(action="create", name="test-skill", content=stale_create)
    )
    newer_skill = _SKILL.replace("body", "newer created skill")
    created = _create_skill("test-skill", newer_skill)
    assert created["success"] is True, created

    result = handle_pending_subcommand(wa.SKILLS, ["approve", staged["pending_id"]])

    assert result is not None
    assert "Approved 0" in result
    assert Path(created["skill_md"]).read_text(encoding="utf-8") == newer_skill
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


def test_pending_full_rewrite_diff_compares_proposal_with_current_skill(hermes_home):
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    rewrite = _SKILL.replace("body", "proposed rewrite")
    diff = wa.skill_pending_diff(
        {
            "payload": {
                "action": "patch",
                "name": "test-skill",
                "content": rewrite,
            }
        }
    )

    assert "-body" in diff
    assert "+proposed rewrite" in diff


class _Gate:
    """Deterministic two-thread barrier for check→publish / publish→rollback."""

    def __init__(self):
        self.arrived = threading.Event()
        self.release = threading.Event()

    def hold(self):
        self.arrived.set()
        assert self.release.wait(timeout=10), "timed out waiting to resume mutation"


def test_pending_approval_fails_closed_when_mutation_lock_cannot_open(
    hermes_home, monkeypatch
):
    """Missing mutation authority must preserve both live bytes and proposal."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import skill_mutation_authority as authority
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    _set_approval("skills", True)
    proposed = _SKILL.replace("body", "proposed rewrite C")
    staged = json.loads(
        skill_manage(action="patch", name="test-skill", content=proposed)
    )
    assert staged["staged"] is True, staged

    def _deny_lock_open(*_args, **_kwargs):
        raise PermissionError("deterministic lock-open refusal")

    monkeypatch.setattr(authority, "open", _deny_lock_open, raising=False)
    result = handle_pending_subcommand(
        wa.SKILLS, ["approve", staged["pending_id"]]
    )

    assert result is not None
    assert "Approved 0" in result
    assert "exclusive mutation authority" in result
    assert Path(created["skill_md"]).read_text(encoding="utf-8") == _SKILL
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


def test_pending_nested_supporting_file_lock_refusal_preserves_entire_tree(
    hermes_home, monkeypatch
):
    """Lock refusal cannot create parents before supporting-file authority."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import skill_mutation_authority as authority
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    skill_dir = Path(created["skill_md"]).parent
    original_tree = authority.snapshot_path_state(skill_dir)

    _set_approval("skills", True)
    staged = json.loads(
        skill_manage(
            action="write_file",
            name="test-skill",
            file_path="references/nested/proposed.md",
            file_content="proposed supporting bytes",
        )
    )
    assert staged["staged"] is True, staged

    def _deny_lock_open(*_args, **_kwargs):
        raise PermissionError("deterministic supporting-file lock refusal")

    monkeypatch.setattr(authority, "open", _deny_lock_open, raising=False)
    result = handle_pending_subcommand(
        wa.SKILLS, ["approve", staged["pending_id"]]
    )

    assert result is not None
    assert "Approved 0" in result
    assert "exclusive mutation authority" in result
    assert authority.snapshot_path_state(skill_dir) == original_tree
    assert not (skill_dir / "references").exists()
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


def test_check_publish_race_preserves_desktop_edit(hermes_home, monkeypatch):
    """A Desktop write that lands after the approval check, before publish,
    must survive. Proof of A cannot authorize replacing B."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, _edit_skill, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    _set_approval("skills", True)
    stale_rewrite = _SKILL.replace("body", "stale pending rewrite")
    staged = json.loads(
        skill_manage(action="patch", name="test-skill", content=stale_rewrite)
    )
    assert staged["staged"] is True, staged

    gate = _Gate()
    monkeypatch.setattr(
        "tools.skill_mutation_authority._after_pending_precondition_check",
        gate.hold,
    )

    newer_edit = _SKILL.replace("body", "newer Desktop edit")
    results = []

    def _approve():
        results.append(
            handle_pending_subcommand(wa.SKILLS, ["approve", staged["pending_id"]])
        )

    worker = threading.Thread(target=_approve)
    worker.start()
    assert gate.arrived.wait(timeout=10)
    edited = _edit_skill("test-skill", newer_edit)
    assert edited["success"] is True, edited
    gate.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    result = results[0]
    skill_md = Path(created["skill_md"])
    assert skill_md.read_text(encoding="utf-8") == newer_edit, result
    assert result is not None
    assert "Approved 0" in result
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


def test_publish_rollback_race_preserves_desktop_edit(hermes_home, monkeypatch):
    """A newer Desktop write waits for rejected C to settle, then survives."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, _edit_skill, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    _set_approval("skills", True)
    proposed = _SKILL.replace("body", "proposed rewrite C")
    staged = json.loads(
        skill_manage(action="patch", name="test-skill", content=proposed)
    )
    assert staged["staged"] is True, staged

    gate = _Gate()
    approve_thread = []

    def _scan(_skill_dir):
        if threading.current_thread() is approve_thread[0]:
            gate.hold()
            return "blocked"
        return None

    monkeypatch.setattr("tools.skill_manager_tool._security_scan_skill", _scan)

    newer_edit = _SKILL.replace("body", "newer Desktop edit")
    results = []

    def _approve():
        approve_thread.append(threading.current_thread())
        results.append(
            handle_pending_subcommand(wa.SKILLS, ["approve", staged["pending_id"]])
        )

    worker = threading.Thread(target=_approve)
    worker.start()
    assert gate.arrived.wait(timeout=10)
    writer_started = threading.Event()
    writer_results = []

    def _write_newer_edit():
        writer_started.set()
        writer_results.append(_edit_skill("test-skill", newer_edit))

    writer = threading.Thread(target=_write_newer_edit)
    writer.start()
    assert writer_started.wait(timeout=10)
    gate.release.set()
    worker.join(timeout=10)
    writer.join(timeout=10)
    assert not worker.is_alive()
    assert not writer.is_alive()
    assert writer_results[0]["success"] is True, writer_results[0]

    result = results[0]
    skill_md = Path(created["skill_md"])
    assert skill_md.read_text(encoding="utf-8") == newer_edit, result
    assert result is not None
    assert "Approved 0" in result
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX independent-process harness")
def test_check_publish_race_independent_process_preserves_newer_edit(
    hermes_home, monkeypatch
):
    """Same check→publish invariant across a real second process.

    /skills approve and the Desktop backend do not share one Python lock.
    """
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    _set_approval("skills", True)
    stale_rewrite = _SKILL.replace("body", "stale pending rewrite")
    staged = json.loads(
        skill_manage(action="patch", name="test-skill", content=stale_rewrite)
    )
    assert staged["staged"] is True, staged

    gate = _Gate()
    monkeypatch.setattr(
        "tools.skill_mutation_authority._after_pending_precondition_check",
        gate.hold,
    )

    newer_edit = _SKILL.replace("body", "newer Desktop edit")
    results = []

    def _approve():
        results.append(
            handle_pending_subcommand(wa.SKILLS, ["approve", staged["pending_id"]])
        )

    worker = threading.Thread(target=_approve)
    worker.start()
    assert gate.arrived.wait(timeout=10)

    repo = str(Path(__file__).resolve().parents[2])
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                f"os.environ['HERMES_HOME'] = {hermes_home!r}\n"
                f"sys.path.insert(0, {repo!r})\n"
                "from tools.skill_manager_tool import _edit_skill\n"
                f"result = _edit_skill('test-skill', {newer_edit!r})\n"
                "assert result.get('success') is True, result\n"
            ),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
    )
    gate.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert child.returncode == 0, child.stdout + child.stderr

    result = results[0]
    skill_md = Path(created["skill_md"])
    assert skill_md.read_text(encoding="utf-8") == newer_edit, result
    assert result is not None
    assert "Approved 0" in result
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX independent-process harness")
def test_publish_rollback_race_independent_process_preserves_newer_edit(
    hermes_home, monkeypatch
):
    """A child-process write waits for rejected C to settle, then survives."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_manager_tool import _create_skill, skill_manage

    created = _create_skill("test-skill", _SKILL)
    assert created["success"] is True, created
    _set_approval("skills", True)
    proposed = _SKILL.replace("body", "proposed rewrite C")
    staged = json.loads(
        skill_manage(action="patch", name="test-skill", content=proposed)
    )
    assert staged["staged"] is True, staged

    gate = _Gate()
    approve_thread = []

    def _scan(_skill_dir):
        if threading.current_thread() is approve_thread[0]:
            gate.hold()
            return "blocked"
        return None

    monkeypatch.setattr("tools.skill_manager_tool._security_scan_skill", _scan)

    newer_edit = _SKILL.replace("body", "newer Desktop edit")
    results = []

    def _approve():
        approve_thread.append(threading.current_thread())
        results.append(
            handle_pending_subcommand(wa.SKILLS, ["approve", staged["pending_id"]])
        )

    worker = threading.Thread(target=_approve)
    worker.start()
    assert gate.arrived.wait(timeout=10)

    repo = str(Path(__file__).resolve().parents[2])
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                f"os.environ['HERMES_HOME'] = {hermes_home!r}\n"
                f"sys.path.insert(0, {repo!r})\n"
                "print('STARTED', flush=True)\n"
                "from tools.skill_manager_tool import _edit_skill\n"
                f"result = _edit_skill('test-skill', {newer_edit!r})\n"
                "assert result.get('success') is True, result\n"
            ),
        ],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "STARTED"
    gate.release.set()
    worker.join(timeout=10)
    stdout, stderr = child.communicate(timeout=20)
    assert not worker.is_alive()
    assert child.returncode == 0, stdout + stderr

    result = results[0]
    skill_md = Path(created["skill_md"])
    assert skill_md.read_text(encoding="utf-8") == newer_edit, result
    assert result is not None
    assert "Approved 0" in result
    assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is not None


def test_direct_targeted_patches_serialize_the_full_read_modify_write(
    hermes_home, tmp_path
):
    """Independent direct patches must derive from the latest locked image."""
    from tools.skill_manager_tool import _create_skill

    initial = _SKILL.replace("body", "alpha: one\nbeta: two")
    created = _create_skill("test-skill", initial)
    assert created["success"] is True, created
    _set_approval("skills", False)

    repo = str(Path(__file__).resolve().parents[2])
    first_acquired = tmp_path / "first-acquired"
    second_entered = tmp_path / "second-entered"
    child_code = """
import contextlib
import json
import os
from pathlib import Path
import sys
import time

role, hermes_home, repo, first_marker, second_marker, old, new = sys.argv[1:]
os.environ["HERMES_HOME"] = hermes_home
sys.path.insert(0, repo)

from tools import skill_manager_tool as manager

real_lease = manager._skill_mutation_lease
first_marker = Path(first_marker)
second_marker = Path(second_marker)

@contextlib.contextmanager
def coordinated_lease(identity):
    if role == "second":
        second_marker.write_text("entered", encoding="utf-8")
    with real_lease(identity) as admitted:
        if role == "first" and admitted:
            first_marker.write_text("acquired", encoding="utf-8")
            deadline = time.monotonic() + 10
            while not second_marker.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("second writer never reached mutation authority")
                time.sleep(0.01)
        yield admitted

manager._skill_mutation_lease = coordinated_lease
result = json.loads(
    manager.skill_manage(
        action="patch",
        name="test-skill",
        old_string=old,
        new_string=new,
    )
)
assert result.get("success") is True, result
print(json.dumps(result), flush=True)
"""

    common = [
        hermes_home,
        repo,
        str(first_acquired),
        str(second_entered),
    ]
    first = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            "first",
            *common,
            "alpha: one",
            "alpha: ONE",
        ],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    deadline = time.monotonic() + 10
    while not first_acquired.exists():
        if first.poll() is not None:
            stdout, stderr = first.communicate()
            pytest.fail(f"first writer exited early: {stdout}{stderr}")
        if time.monotonic() >= deadline:
            first.kill()
            stdout, stderr = first.communicate()
            pytest.fail(f"first writer never acquired authority: {stdout}{stderr}")
        time.sleep(0.01)

    second = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            "second",
            *common,
            "beta: two",
            "beta: TWO",
        ],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_stdout, first_stderr = first.communicate(timeout=20)
    second_stdout, second_stderr = second.communicate(timeout=20)

    assert first.returncode == 0, first_stdout + first_stderr
    assert second.returncode == 0, second_stdout + second_stderr
    final_content = Path(created["skill_md"]).read_text(encoding="utf-8")
    assert "alpha: ONE" in final_content
    assert "beta: TWO" in final_content


# ---------------------------------------------------------------------------
# Pending store CRUD
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared command handler
# ---------------------------------------------------------------------------


def test_handle_approve_all(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    wa.stage_write("memory", {"action": "add", "target": "user", "content": "a"},
                   summary="a", origin="foreground")
    wa.stage_write("memory", {"action": "add", "target": "user", "content": "b"},
                   summary="b", origin="foreground")
    out = handle_pending_subcommand(wa.MEMORY, ["approve", "all"], memory_store=store)
    assert "Approved 2" in out
    assert wa.pending_count("memory") == 0
    assert len(store.user_entries) == 2


def test_handle_approval_on(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.MEMORY, ["approval", "on"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is True
    assert "on" in out


def test_handle_approval_off(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.SKILLS, ["approval", "off"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is False
    assert "off" in out


# ---------------------------------------------------------------------------
# Inline (interactive CLI) approval path — regression for the bug where the
# per-thread approval callback was never passed to prompt_dangerous_approval,
# so every gated foreground memory write was silently denied.
# ---------------------------------------------------------------------------

@pytest.fixture
def approval_callback_cleanup():
    yield
    from tools.terminal_tool import set_approval_callback
    set_approval_callback(None)


def test_memory_inline_approve_writes(hermes_home, approval_callback_cleanup):
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)

    calls = []
    def approve_cb(command, description, **kw):
        calls.append((command, description))
        return "once"
    set_approval_callback(approve_cb)

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "approved fact", store=store))
    assert r["success"] is True
    assert r.get("staged") is None  # real write, not staged
    assert store.memory_entries == ["approved fact"]
    assert wa.pending_count("memory") == 0
    # The registered callback must actually be invoked (not the input() path).
    assert len(calls) == 1
    assert "approved fact" in calls[0][0]


def test_memory_inline_deny_blocks(hermes_home, approval_callback_cleanup):
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)
    set_approval_callback(lambda command, description, **kw: "deny")

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "denied fact", store=store))
    assert r["success"] is False
    assert "denied" in r["error"].lower()
    assert store.memory_entries == []
    assert wa.pending_count("memory") == 0  # denied, not staged


def test_memory_invalid_params_rejected_before_staging(hermes_home):
    # Param validation must run BEFORE the gate so a broken write is rejected
    # immediately instead of staged and failing at approve time.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    _set_approval("memory", True)
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", None, store=store))
    assert r["success"] is False
    assert wa.pending_count("memory") == 0


class TestSkillGist:
    """skill_gist builds a heuristic one-line summary for a pending skill write.

    Pure, no model call — every branch is verifiable from the function source.
    """

    def test_create_with_frontmatter_description(self):
        from tools import write_approval as wa
        content = "---\ndescription: My cool skill\n---\nprint('hi')\n"
        assert (
            wa.skill_gist("create", "demo", content=content)
            == f"create 'demo' — My cool skill ({len(content)} chars)"
        )

    def test_edit_without_description_uses_size_only(self):
        from tools import write_approval as wa
        content = "no frontmatter here"
        assert (
            wa.skill_gist("edit", "demo", content=content)
            == f"rewrite 'demo' ({len(content)} chars)"
        )

    def test_patch_with_full_content_is_described_as_rewrite(self):
        from tools import write_approval as wa
        content = "---\ndescription: Updated skill.\n---\n# Updated\n"
        assert (
            wa.skill_gist("patch", "demo", content=content)
            == f"rewrite 'demo' — Updated skill. ({len(content)} chars)"
        )


    def test_file_actions_and_unknown_fallback(self):
        from tools import write_approval as wa
        assert wa.skill_gist("write_file", "demo", file_path="a.py") == "write a.py in 'demo'"
        assert wa.skill_gist("remove_file", "demo", file_path="a.py") == "remove a.py from 'demo'"
        assert wa.skill_gist("delete", "demo") == "delete skill 'demo'"
        assert wa.skill_gist("unknown", "demo") == "unknown 'demo'"
