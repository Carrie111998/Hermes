"""Tests for the memory/skill write-approval gate (tools/write_approval.py)
and the shared slash-command handlers (hermes_cli/write_approval_commands.py).

Covers the boolean write_approval gate (off by default = write freely; on =
require approval) for both subsystems, the foreground-vs-background staging
split, pending store CRUD, and the list/approve/reject/diff/approval
subcommand dispatch.
"""

import json
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

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


def test_load_on_disk_store_honors_configured_char_limits(hermes_home, monkeypatch):
    """load_on_disk_store() must read memory.memory_char_limit /
    user_char_limit from config so approvals applied without a live agent
    enforce the SAME caps as the live agent (agent_init.py). Falls back to
    defaults when config can't be loaded.
    """
    from tools.memory_tool import load_on_disk_store

    # Config override path: helper picks up the configured limits.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"memory_char_limit": 999, "user_char_limit": 444}},
    )
    store = load_on_disk_store()
    assert store.memory_char_limit == 999
    assert store.user_char_limit == 444

    # Failure path: config raises → defaults, never blows up.
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    fallback = load_on_disk_store()
    assert fallback.memory_char_limit == 2200
    assert fallback.user_char_limit == 1375


# ---------------------------------------------------------------------------
# Skill gate
# ---------------------------------------------------------------------------

_SKILL = (
    "---\nname: test-skill\ndescription: A test skill\nversion: 1.0.0\n---\n"
    "# Test\nbody\n"
)


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


def _background_skill_patch_result(
    hermes_home, tmp_path, *, reset_skill=True, new_string="improved body"
):
    """Run one real background-review patch attempt against a temp skill."""
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import set_current_write_origin, reset_current_write_origin

    _set_approval("skills", True)
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    if reset_skill:
        skill_md.write_text(_SKILL, encoding="utf-8")

    token = set_current_write_origin("background_review")
    try:
        with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]), \
             patch("tools.skill_manager_tool._background_review_preflight", return_value=None):
            result = json.loads(skill_manage(
                action="patch",
                name="test-skill",
                old_string="body",
                new_string=new_string,
                task_id="task-123",
                session_id="session-456",
            ))
    finally:
        reset_current_write_origin(token)
    return result, skill_md


def _stage_background_skill_patch(hermes_home, tmp_path):
    """Stage one real background-review patch against an existing temp skill."""
    from tools import write_approval as wa

    result, skill_md = _background_skill_patch_result(hermes_home, tmp_path)
    assert result["success"] is True
    assert result["staged"] is True
    record = wa.get_pending(wa.SKILLS, result["pending_id"])
    assert record is not None
    return result, record, skill_md


def test_background_skill_patch_stages_frozen_refinement_candidate(
    hermes_home, tmp_path
):
    from tools import write_approval as wa

    result, record, skill_md = _stage_background_skill_patch(hermes_home, tmp_path)
    candidate = record["refinement_candidate"]

    assert result["candidate_id"] == candidate["id"]
    assert len(candidate["id"]) == 32
    assert candidate["schema_version"] == 1
    assert candidate["target"]["skill"] == "test-skill"
    assert candidate["target"]["file"] == "SKILL.md"
    assert Path(candidate["target"]["root"]) == tmp_path / "test-skill"
    assert candidate["evidence"] == {
        "origin": "background_review",
        "task_id": "task-123",
        "session_id": "session-456",
    }
    assert candidate["base"]["state"] == "present"
    assert candidate["proposed"]["state"] == "present"
    assert candidate["base"]["sha256"] != candidate["proposed"]["sha256"]
    assert "-body" in candidate["diff"]
    assert "+improved body" in candidate["diff"]
    assert skill_md.read_text(encoding="utf-8") == _SKILL

    frozen = wa.skill_pending_diff(record)
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    rendered = handle_pending_subcommand(
        wa.SKILLS, ["diff", result["pending_id"]]
    )
    assert candidate["id"] in rendered
    assert "session-456" in rendered
    assert candidate["base"]["sha256"] in rendered

    skill_md.write_text(_SKILL.replace("body", "unrelated drift"), encoding="utf-8")
    assert wa.skill_pending_diff(record) == frozen


def test_refinement_approval_rejects_stale_base_before_snapshot(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, _record, skill_md = _stage_background_skill_patch(hermes_home, tmp_path)
    skill_md.write_text(_SKILL.replace("body", "newer user edit"), encoding="utf-8")
    snapshots = []
    monkeypatch.setattr(
        "agent.curator_backup.snapshot_skills",
        lambda **kwargs: snapshots.append(kwargs) or Path("unused"),
    )

    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        out = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    assert "base changed" in out.lower()
    assert snapshots == []
    assert wa.pending_count(wa.SKILLS) == 1
    assert "newer user edit" in skill_md.read_text(encoding="utf-8")


def test_refinement_approval_requires_snapshot_and_keeps_pending_on_failure(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, _record, skill_md = _stage_background_skill_patch(hermes_home, tmp_path)
    snapshot_calls = []
    monkeypatch.setattr(
        "agent.curator_backup.snapshot_skills",
        lambda **kwargs: snapshot_calls.append(kwargs) or None,
    )

    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        out = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )
        immediate_retry = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    assert "snapshot" in out.lower()
    assert "failed" in out.lower()
    assert "cooldown" in immediate_retry.lower()
    assert len(snapshot_calls) == 1
    assert wa.pending_count(wa.SKILLS) == 1
    assert skill_md.read_text(encoding="utf-8") == _SKILL


def test_refinement_approval_snapshots_applies_and_verifies_result(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, record, skill_md = _stage_background_skill_patch(hermes_home, tmp_path)
    candidate_id = record["refinement_candidate"]["id"]
    snapshot = Path(hermes_home) / "skills" / ".curator_backups" / "snap-123"
    calls = []

    def _snapshot(*, reason, **kwargs):
        calls.append(reason)
        return snapshot

    monkeypatch.setattr("agent.curator_backup.snapshot_skills", _snapshot)

    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        out = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    assert calls == [f"pre-refinement {candidate_id}"]
    assert "Approved 1" in out
    assert "snap-123" in out
    assert wa.pending_count(wa.SKILLS) == 0
    assert "improved body" in skill_md.read_text(encoding="utf-8")


def test_refinement_approval_creates_real_curator_snapshot(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    skills_root = Path(hermes_home) / "skills"
    result, record, skill_md = _stage_background_skill_patch(
        hermes_home, skills_root
    )

    with patch("tools.skill_manager_tool.SKILLS_DIR", skills_root), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_root]):
        out = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    backups = skills_root / ".curator_backups"
    manifests = list(backups.glob("*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["reason"] == (
        f"pre-refinement {record['refinement_candidate']['id']}"
    )
    assert manifest["skill_files"] == 1
    assert manifests[0].parent.name in out
    assert "improved body" in skill_md.read_text(encoding="utf-8")
    assert wa.pending_count(wa.SKILLS) == 0


def test_refinement_guard_allows_only_one_active_candidate_per_skill(
    hermes_home, tmp_path
):
    from tools import write_approval as wa

    _stage_background_skill_patch(hermes_home, tmp_path)
    second, _skill_md = _background_skill_patch_result(
        hermes_home, tmp_path, reset_skill=False
    )

    assert second["success"] is False
    assert "active refinement candidate" in second["error"].lower()
    assert wa.pending_count(wa.SKILLS) == 1


def test_rejected_refinement_candidate_enters_cooldown(
    hermes_home, tmp_path
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, _record, _skill_md = _stage_background_skill_patch(
        hermes_home, tmp_path
    )
    rejected = handle_pending_subcommand(
        wa.SKILLS, ["reject", result["pending_id"]]
    )
    assert "Rejected" in rejected

    retry, _skill_md = _background_skill_patch_result(
        hermes_home,
        tmp_path,
        reset_skill=False,
        new_string="differently improved body",
    )
    assert retry["success"] is False
    assert "cooldown" in retry["error"].lower()
    assert wa.pending_count(wa.SKILLS) == 0


def test_exact_duplicate_remains_blocked_after_cooldown(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    now = [1_000_000.0]
    monkeypatch.setattr(wa.time, "time", lambda: now[0])
    result, _record, _skill_md = _stage_background_skill_patch(
        hermes_home, tmp_path
    )
    handle_pending_subcommand(wa.SKILLS, ["reject", result["pending_id"]])
    now[0] += wa.REFINEMENT_COOLDOWN_SECONDS + 1

    duplicate, _skill_md = _background_skill_patch_result(
        hermes_home, tmp_path, reset_skill=False
    )
    assert duplicate["success"] is False
    assert "duplicate refinement candidate" in duplicate["error"].lower()


def test_refinement_guard_caps_same_candidate_at_three_attempts_per_window(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    now = [1_000_000.0]
    monkeypatch.setattr(wa.time, "time", lambda: now[0])

    for attempt in range(wa.REFINEMENT_MAX_ATTEMPTS):
        result, _skill_md = _background_skill_patch_result(
            hermes_home,
            tmp_path,
            new_string=f"improved body variant {attempt}",
        )
        assert result["success"] is True
        handle_pending_subcommand(
            wa.SKILLS, ["reject", result["pending_id"]]
        )
        now[0] += wa.REFINEMENT_COOLDOWN_SECONDS + 1

    blocked, _skill_md = _background_skill_patch_result(
        hermes_home, tmp_path, reset_skill=False
    )
    assert blocked["success"] is False
    assert "attempt limit" in blocked["error"].lower()
    assert wa.pending_count(wa.SKILLS) == 0


def test_corrupt_refinement_guard_blocks_new_candidate(hermes_home, tmp_path):
    from tools import write_approval as wa

    guard = Path(hermes_home) / "pending" / "refinement_guard.json"
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text("not valid json", encoding="utf-8")

    result, _skill_md = _background_skill_patch_result(hermes_home, tmp_path)
    assert result["success"] is False
    assert "loop guard" in result["error"].lower()
    assert wa.pending_count(wa.SKILLS) == 0


def test_semantically_corrupt_refinement_guard_blocks_new_candidate(
    hermes_home, tmp_path
):
    from tools import write_approval as wa

    guard = Path(hermes_home) / "pending" / "refinement_guard.json"
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(json.dumps({
        "schema_version": 1,
        "fingerprints": {},
        "skills": {"test-skill": {"attempts": "corrupt", "failures": []}},
    }), encoding="utf-8")

    result, _skill_md = _background_skill_patch_result(hermes_home, tmp_path)
    assert result["success"] is False
    assert "loop guard" in result["error"].lower()
    assert wa.pending_count(wa.SKILLS) == 0


def test_refinement_candidate_is_bound_to_exact_skill_root(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    result, _record, skill_a = _stage_background_skill_patch(
        hermes_home, root_a
    )
    skill_b_dir = root_b / "test-skill"
    skill_b_dir.mkdir(parents=True)
    skill_b = skill_b_dir / "SKILL.md"
    skill_b.write_text(_SKILL, encoding="utf-8")
    snapshots = []
    monkeypatch.setattr(
        "agent.curator_backup.snapshot_skills",
        lambda **kwargs: snapshots.append(kwargs) or Path("unused"),
    )

    with patch("tools.skill_manager_tool.SKILLS_DIR", root_b), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[root_b, root_a]):
        out = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    assert "target root changed" in out.lower()
    assert snapshots == []
    assert skill_a.read_text(encoding="utf-8") == _SKILL
    assert skill_b.read_text(encoding="utf-8") == _SKILL
    assert wa.pending_count(wa.SKILLS) == 1


def test_refinement_apply_rechecks_base_inside_skill_writer(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, _record, skill_md = _stage_background_skill_patch(
        hermes_home, tmp_path
    )

    def _snapshot_then_user_edit(**kwargs):
        skill_md.write_text(
            _SKILL.replace("body", "concurrent user edit"), encoding="utf-8"
        )
        return Path("snap")

    monkeypatch.setattr(
        "agent.curator_backup.snapshot_skills", _snapshot_then_user_edit
    )
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        out = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    assert "changed during apply" in out.lower()
    assert "concurrent user edit" in skill_md.read_text(encoding="utf-8")
    assert wa.pending_count(wa.SKILLS) == 1


def test_parallel_refinement_approvals_apply_only_once(
    hermes_home, tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, _record, skill_md = _stage_background_skill_patch(
        hermes_home, tmp_path
    )
    entered = threading.Event()
    release = threading.Event()
    apply_calls = []

    def _apply_once(payload, refinement_candidate=None):
        apply_calls.append(payload)
        entered.set()
        release.wait(timeout=2)
        skill_md.write_text(_SKILL.replace("body", "improved body"), encoding="utf-8")
        return json.dumps({"success": True})

    monkeypatch.setattr(
        "agent.curator_backup.snapshot_skills", lambda **kwargs: Path("snap")
    )
    monkeypatch.setattr("tools.skill_manager_tool.apply_skill_pending", _apply_once)

    def _approve():
        return handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]), \
         ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_approve)
        ready = entered.wait(timeout=2)
        assert ready, first.result(timeout=1)
        second = pool.submit(_approve)
        time.sleep(0.05)
        release.set()
        outputs = [first.result(timeout=2), second.result(timeout=2)]

    assert len(apply_calls) == 1
    assert sum("Approved 1" in output for output in outputs) == 1
    assert wa.pending_count(wa.SKILLS) == 0


def test_applied_candidate_with_guard_failure_is_retained_and_not_retried(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, _record, skill_md = _stage_background_skill_patch(
        hermes_home, tmp_path
    )
    original_record_outcome = wa.record_refinement_candidate_outcome
    snapshots = []

    def _record_outcome(record, outcome):
        if outcome == "applied":
            return False, "simulated guard write failure"
        return original_record_outcome(record, outcome)

    monkeypatch.setattr(wa, "record_refinement_candidate_outcome", _record_outcome)
    monkeypatch.setattr(
        "agent.curator_backup.snapshot_skills",
        lambda **kwargs: snapshots.append(kwargs) or Path("snap"),
    )

    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        first = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )
        second = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    retained = wa.get_pending(wa.SKILLS, result["pending_id"])
    assert "retained" in first.lower()
    assert "must not be retried" in second.lower()
    assert retained["refinement_apply_state"] == "applied_guard_error"
    assert len(snapshots) == 1
    assert "improved body" in skill_md.read_text(encoding="utf-8")


def test_tampered_refinement_candidate_can_still_be_rejected(
    hermes_home, tmp_path
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, record, _skill_md = _stage_background_skill_patch(
        hermes_home, tmp_path
    )
    record["refinement_candidate"]["diff"] += "\nforged"
    wa.replace_pending_record(wa.SKILLS, record)

    out = handle_pending_subcommand(
        wa.SKILLS, ["reject", result["pending_id"]]
    )
    assert "Rejected" in out
    assert wa.pending_count(wa.SKILLS) == 0


def test_refinement_candidate_integrity_tampering_blocks_apply(
    hermes_home, tmp_path, monkeypatch
):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    result, record, skill_md = _stage_background_skill_patch(hermes_home, tmp_path)
    record["refinement_candidate"]["diff"] += "\nforged"
    wa.replace_pending_record(wa.SKILLS, record)
    snapshots = []
    monkeypatch.setattr(
        "agent.curator_backup.snapshot_skills",
        lambda **kwargs: snapshots.append(kwargs) or Path("unused"),
    )

    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        out = handle_pending_subcommand(
            wa.SKILLS, ["approve", result["pending_id"]]
        )

    assert "integrity" in out.lower()
    assert snapshots == []
    assert wa.pending_count(wa.SKILLS) == 1
    assert skill_md.read_text(encoding="utf-8") == _SKILL


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


    def test_file_actions_and_unknown_fallback(self):
        from tools import write_approval as wa
        assert wa.skill_gist("write_file", "demo", file_path="a.py") == "write a.py in 'demo'"
        assert wa.skill_gist("remove_file", "demo", file_path="a.py") == "remove a.py from 'demo'"
        assert wa.skill_gist("delete", "demo") == "delete skill 'demo'"
        assert wa.skill_gist("unknown", "demo") == "unknown 'demo'"
