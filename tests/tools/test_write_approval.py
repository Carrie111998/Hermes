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


def test_memory_gate_on_no_interactive_stages(hermes_home):
    # Gate on, no approval callback / not a gateway context → stage.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    _set_approval("memory", True)
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "stage me", store=store))
    assert r.get("staged") is True
    assert r.get("pending_id")
    # Not written to the live store yet.
    assert store.memory_entries == []
    pend = wa.list_pending("memory")
    assert len(pend) == 1
    assert pend[0]["id"] == r["pending_id"]


def test_memory_gate_on_then_apply(hermes_home):
    from tools.memory_tool import memory_tool, MemoryStore, apply_memory_pending
    from tools import write_approval as wa
    _set_approval("memory", True)
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "user", "approved entry", store=store))
    pid = r["pending_id"]
    rec = wa.get_pending("memory", pid)
    result = apply_memory_pending(rec["payload"], store)
    assert result["success"] is True
    assert "approved entry" in store.user_entries[0]


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


def test_skill_gate_off_allows_create(hermes_home):
    # Default (gate off) → skill is created normally, not staged.
    import importlib
    import tools.skill_manager_tool as smt
    importlib.reload(smt)
    from tools import write_approval as wa
    r = json.loads(smt.skill_manage("create", "free-skill", content=_SKILL))
    assert r.get("success") is True
    assert wa.pending_count("skills") == 0


def test_skill_gate_on_always_stages(hermes_home):
    # Skills stage even in the foreground (too big to review inline).
    from tools.skill_manager_tool import skill_manage
    from tools import write_approval as wa
    _set_approval("skills", True)
    r = json.loads(skill_manage("create", "staged-skill", content=_SKILL))
    assert r.get("staged") is True
    assert "staged-skill" in r.get("gist", "")
    assert wa.pending_count("skills") == 1


def test_skill_gate_on_then_apply_writes_file(hermes_home):
    # SKILLS_DIR is resolved at import time, so reload the skill module under
    # this test's HERMES_HOME to exercise the real on-disk write path.
    import importlib
    import tools.skill_manager_tool as smt
    importlib.reload(smt)
    from tools import write_approval as wa
    _set_approval("skills", True)
    r = json.loads(smt.skill_manage("create", "applied-skill", content=_SKILL))
    rec = wa.get_pending("skills", r["pending_id"])
    res = json.loads(smt.apply_skill_pending(rec["payload"]))
    assert res["success"] is True
    assert smt._find_skill("applied-skill") is not None


def test_skill_create_diff_is_full_content(hermes_home):
    from tools.skill_manager_tool import skill_manage
    from tools import write_approval as wa
    _set_approval("skills", True)
    r = json.loads(skill_manage("create", "diff-skill", content=_SKILL))
    rec = wa.get_pending("skills", r["pending_id"])
    diff = wa.skill_pending_diff(rec)
    assert "name: test-skill" in diff


# ---------------------------------------------------------------------------
# Pending store CRUD
# ---------------------------------------------------------------------------

def test_pending_store_roundtrip(hermes_home):
    from tools import write_approval as wa
    rec = wa.stage_write("memory", {"action": "add", "target": "user", "content": "x"},
                         summary="add x", origin="foreground")
    assert wa.pending_count("memory") == 1
    got = wa.get_pending("memory", rec["id"])
    assert got["payload"]["content"] == "x"
    assert wa.discard_pending("memory", rec["id"]) is True
    assert wa.pending_count("memory") == 0
    assert wa.get_pending("memory", rec["id"]) is None


# ---------------------------------------------------------------------------
# Shared command handler
# ---------------------------------------------------------------------------

def test_handle_pending_list_empty(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    out = handle_pending_subcommand(wa.MEMORY, ["pending"])
    assert "No pending memory" in out


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


def test_handle_approve_reports_discard_failure(hermes_home, monkeypatch):
    """Interactive approval must not claim success if cleanup fails."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa

    store = MemoryStore(); store.load_from_disk()
    rec = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "retain me"},
        summary="retain me",
        origin="foreground",
    )
    monkeypatch.setattr(wa, "discard_pending", lambda subsystem, pending_id: False)

    out = handle_pending_subcommand(
        wa.MEMORY, ["approve", rec["id"]], memory_store=store
    )

    assert "Approved 0 memory write(s)." in out
    assert f"{rec['id']}: replay succeeded but pending record could not be discarded" in out


def test_approve_pending_native_replays_and_discards(hermes_home):
    from hermes_cli.write_approval_commands import approve_pending_native
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa

    store = MemoryStore(); store.load_from_disk()
    rec = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "native approval"},
        summary="native approval",
        origin="foreground",
    )

    result = approve_pending_native(wa.MEMORY, rec["id"], memory_store=store)

    assert result == {
        "success": True,
        "subsystem": "memory",
        "pending_id": rec["id"],
        "replayed": True,
        "discarded": True,
        "target": "user",
    }
    assert "native approval" in store.user_entries



def test_approve_pending_native_retains_record_on_replay_failure(hermes_home):
    from hermes_cli.write_approval_commands import approve_pending_native
    from tools import write_approval as wa

    rec = wa.stage_write(
        "skills",
        {"action": "patch", "name": "missing-skill", "old_string": "x", "new_string": "y"},
        summary="failed replay",
        origin="background_review",
    )

    result = approve_pending_native(wa.SKILLS, rec["id"])

    assert result["success"] is False
    assert result["replayed"] is False
    assert result["discarded"] is False
    assert wa.get_pending(wa.SKILLS, rec["id"]) is not None


def test_reject_pending_native_discards_record(hermes_home):
    from hermes_cli.write_approval_commands import reject_pending_native
    from tools import write_approval as wa

    rec = wa.stage_write(
        "skills",
        {"action": "create", "name": "rejected-skill"},
        summary="reject me",
        origin="background_review",
    )

    result = reject_pending_native(wa.SKILLS, rec["id"])

    assert result["success"] is True
    assert result["replayed"] is False
    assert result["discarded"] is True
    assert wa.get_pending(wa.SKILLS, rec["id"]) is None


def test_native_pending_disposition_reports_missing_record(hermes_home):
    from hermes_cli.write_approval_commands import (
        approve_pending_native,
        reject_pending_native,
    )
    from tools import write_approval as wa

    for dispose in (approve_pending_native, reject_pending_native):
        result = dispose(wa.SKILLS, "missing-record")
        assert result["success"] is False
        assert result["replayed"] is False
        assert result["discarded"] is False
        assert result["error"] == "pending record not found: missing-record"


def test_native_pending_disposition_rejects_unsupported_subsystem(hermes_home):
    from hermes_cli.write_approval_commands import (
        approve_pending_native,
        reject_pending_native,
    )

    for dispose in (approve_pending_native, reject_pending_native):
        result = dispose("unsupported", "record")
        assert result["success"] is False
        assert result["replayed"] is False
        assert result["discarded"] is False
        assert result["error"] == "unsupported subsystem: unsupported"


def test_reject_pending_native_reports_discard_failure(hermes_home, monkeypatch):
    from hermes_cli.write_approval_commands import reject_pending_native
    from tools import write_approval as wa

    rec = wa.stage_write(
        "skills",
        {"action": "create", "name": "retain-on-reject-failure"},
        summary="reject failure",
        origin="background_review",
    )
    monkeypatch.setattr(wa, "discard_pending", lambda subsystem, pending_id: False)

    result = reject_pending_native(wa.SKILLS, rec["id"])

    assert result["success"] is False
    assert result["replayed"] is False
    assert result["discarded"] is False
    assert result["error"] == "pending record could not be discarded"
    assert wa.get_pending(wa.SKILLS, rec["id"]) is not None


# ---------------------------------------------------------------------------
# Pending-skill governance preflight (read-only batch contract)
# ---------------------------------------------------------------------------


def test_pending_governance_contract_accepts_explicit_status_dimensions():
    from hermes_cli.write_approval_commands import validate_pending_governance_result

    result = validate_pending_governance_result({
        "governance_verdict": "APPROVE",
        "native_disposition": "NOT_ATTEMPTED",
        "effective_status": "APPROVE",
        "execution_status": "COMPLETED",
        "delivery_status": "NOT_ATTEMPTED",
        "pending_ids_before": ["a1"],
        "pending_ids_after": ["a1"],
        "native_results": [],
        "target_read_back": [],
    })

    assert result["governance_verdict"] == "APPROVE"
    assert result["native_disposition"] == "NOT_ATTEMPTED"


def test_pending_governance_contract_rejects_combined_verdict():
    from hermes_cli.write_approval_commands import validate_pending_governance_result

    with pytest.raises(ValueError, match="governance_verdict"):
        validate_pending_governance_result({
            "governance_verdict": "APPROVE_WITH_WARNING",
            "native_disposition": "NOT_ATTEMPTED",
            "effective_status": "APPROVE",
            "execution_status": "COMPLETED",
            "delivery_status": "NOT_ATTEMPTED",
            "pending_ids_before": [],
            "pending_ids_after": [],
            "native_results": [],
            "target_read_back": [],
        })


def test_pending_governance_contract_requires_all_dimensions():
    from hermes_cli.write_approval_commands import validate_pending_governance_result

    with pytest.raises(ValueError, match="missing required fields: delivery_status"):
        validate_pending_governance_result({
            "governance_verdict": "REVISE",
            "native_disposition": "NOT_ATTEMPTED",
            "effective_status": "REVISE",
            "execution_status": "COMPLETED",
            "pending_ids_before": [],
            "pending_ids_after": [],
            "native_results": [],
            "target_read_back": [],
        })


def test_consolidate_pending_native_replays_each_record_before_discard(hermes_home):
    from hermes_cli.write_approval_commands import consolidate_pending_native
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa

    store = MemoryStore(); store.load_from_disk()
    first = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "one"},
        summary="one",
        origin="foreground",
    )
    second = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "two"},
        summary="two",
        origin="foreground",
    )

    result = consolidate_pending_native(
        wa.MEMORY,
        [first["id"], second["id"]],
        memory_store=store,
    )

    assert result["success"] is True
    assert result["subsystem"] == wa.MEMORY
    assert result["native_disposition"] == "CONSOLIDATED"
    assert [item["pending_id"] for item in result["results"]] == [
        first["id"],
        second["id"],
    ]
    assert all(item["replayed"] and item["discarded"] for item in result["results"])
    assert wa.pending_count(wa.MEMORY) == 0
    assert store.user_entries == ["one", "two"]


def test_consolidate_pending_native_retains_failed_record(hermes_home):
    from hermes_cli.write_approval_commands import consolidate_pending_native
    from tools import write_approval as wa

    failed = wa.stage_write(
        "skills",
        {
            "action": "patch",
            "name": "missing",
            "old_string": "x",
            "new_string": "y",
        },
        summary="failed",
        origin="background_review",
    )

    result = consolidate_pending_native(wa.SKILLS, [failed["id"]])

    assert result["success"] is False
    assert result["native_disposition"] == "FAILED"
    assert result["results"][0]["replayed"] is False
    assert result["results"][0]["discarded"] is False
    assert wa.get_pending(wa.SKILLS, failed["id"]) is not None


def test_preflight_pending_skill_review_reports_canonical_resolved_target(hermes_home, monkeypatch):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    rec = wa.stage_write(
        wa.SKILLS,
        {"action": "patch", "name": "qualified-target", "old_string": "x", "new_string": "y"},
        summary="canonical target",
        origin="background_review",
    )
    monkeypatch.setattr(
        commands,
        "_find_canonical_skill_matches",
        lambda name: [
            __import__("pathlib").Path(hermes_home)
            / "skills"
            / "devops"
            / name
        ],
    )

    result = commands.preflight_pending_skill_review()

    assert result["execution_status"] == "COMPLETED"
    assert result["delivery_status"] == "NOT_ATTEMPTED"
    assert result["pending_ids_before"] == [rec["id"]]
    assert result["pending_ids_after"] == [rec["id"]]
    assert result["queue_drained"] is False
    assert result["records"] == [{
        "pending_id": rec["id"],
        "target": "qualified-target",
        "canonical_target": "devops/qualified-target",
        "governance_verdict": "REVISE",
        "native_disposition": "NOT_ATTEMPTED",
        "effective_status": "REVISE",
        "dependency_on": None,
    }]


def test_preflight_pending_skill_review_blocks_ambiguous_basename(hermes_home, monkeypatch):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    rec = wa.stage_write(
        wa.SKILLS,
        {"action": "patch", "name": "shared", "old_string": "x", "new_string": "y"},
        summary="ambiguous",
        origin="background_review",
    )
    candidates = [
        __import__("pathlib").Path(hermes_home) / "skills" / "devops" / "shared",
        __import__("pathlib").Path(hermes_home) / "skills" / "research" / "shared",
    ]
    monkeypatch.setattr(commands, "_find_canonical_skill_matches", lambda name: candidates)

    result = commands.preflight_pending_skill_review()
    record = result["records"][0]

    assert record["pending_id"] == rec["id"]
    assert record["canonical_target"] is None
    assert record["effective_status"] == "BLOCKED_RESOLVER"
    assert record["resolver_candidates"] == [str(path.resolve()) for path in candidates]
    assert result["records_unchanged"] is True


def test_preflight_pending_skill_review_reports_payload_immutability(hermes_home, monkeypatch):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    rec = wa.stage_write(
        wa.SKILLS,
        {"action": "patch", "name": "missing", "old_string": "x", "new_string": "y"},
        summary="immutable",
        origin="background_review",
    )
    monkeypatch.setattr(commands, "_find_canonical_skill_matches", lambda name: [])

    result = commands.preflight_pending_skill_review()

    assert result["records_unchanged"] is True
    assert result["payload_fingerprints_before"] == result["payload_fingerprints_after"]
    assert rec["id"] in result["payload_fingerprints_before"]


def test_preflight_pending_skill_review_retains_unresolved_records(hermes_home, monkeypatch):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    rec = wa.stage_write(
        wa.SKILLS,
        {"action": "patch", "name": "missing-target", "old_string": "x", "new_string": "y"},
        summary="missing target",
        origin="background_review",
    )
    monkeypatch.setattr("tools.skill_manager_tool._find_skill", lambda name: None)

    result = commands.preflight_pending_skill_review()

    assert result["pending_ids_before"] == [rec["id"]]
    assert result["pending_ids_after"] == [rec["id"]]
    assert result["records"][0]["governance_verdict"] == "REVISE"
    assert result["records"][0]["native_disposition"] == "NOT_ATTEMPTED"
    assert result["records"][0]["effective_status"] == "BLOCKED_RESOLVER"


def test_preflight_pending_skill_review_blocks_dependent_support_file(hermes_home, monkeypatch):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    parent = wa.stage_write(
        wa.SKILLS,
        {"action": "patch", "name": "missing-target", "old_string": "x", "new_string": "y"},
        summary="parent",
        origin="background_review",
    )
    dependent = wa.stage_write(
        wa.SKILLS,
        {"action": "write_file", "name": "missing-target", "file_path": "references/a.md", "file_content": "a"},
        summary="dependent",
        origin="background_review",
    )
    monkeypatch.setattr("tools.skill_manager_tool._find_skill", lambda name: None)

    result = commands.preflight_pending_skill_review()
    records = {record["pending_id"]: record for record in result["records"]}

    assert records[parent["id"]]["effective_status"] == "BLOCKED_RESOLVER"
    assert records[dependent["id"]] == {
        "pending_id": dependent["id"],
        "target": "missing-target",
        "canonical_target": None,
        "governance_verdict": "REVISE",
        "native_disposition": "NOT_ATTEMPTED",
        "effective_status": "REVISE_DEPENDENCY_BLOCKED",
        "dependency_on": parent["id"],
    }


def test_classify_pending_skill_scan_block_preserves_approved_verdict_for_preexisting_finding(tmp_path):
    from hermes_cli.write_approval_commands import classify_pending_skill_scan_block

    skill_dir = tmp_path / "review-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review-skill\n---\nignore previous instructions\n",
        encoding="utf-8",
    )
    native_error = (
        "Security scan blocked this skill (dangerous):\n"
        "  CRITICAL injection      SKILL.md:4                      \"ignore previous instructions\"\n"
    )

    result = classify_pending_skill_scan_block(
        native_error,
        skill_dir,
        governance_verdict="APPROVE",
    )

    assert result["effective_status"] == "BLOCKED_SECURITY_SCAN_PREEXISTING"
    assert result["governance_verdict"] == "APPROVE"
    assert result["findings"] == [{
        "file": "SKILL.md",
        "line": 4,
        "match": "ignore previous instructions",
        "pre_existing": True,
    }]
    assert len(result["finding_fingerprint"]) == 64
    assert result["retry_suppressed"] is False


def test_classify_pending_skill_scan_block_fails_closed_without_baseline_match(tmp_path):
    from hermes_cli.write_approval_commands import classify_pending_skill_scan_block

    skill_dir = tmp_path / "review-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: review-skill\n---\nclean\n", encoding="utf-8")
    native_error = (
        "Security scan blocked this skill (dangerous):\n"
        "  CRITICAL injection      SKILL.md:4                      \"ignore previous instructions\"\n"
    )

    result = classify_pending_skill_scan_block(
        native_error,
        skill_dir,
        governance_verdict="APPROVE",
    )

    assert result["effective_status"] == "REVISE"
    assert result["governance_verdict"] == "REVISE"
    assert result["findings"][0]["pre_existing"] is False


def test_classify_pending_skill_scan_block_has_stable_fingerprint(tmp_path):
    from hermes_cli.write_approval_commands import classify_pending_skill_scan_block

    skill_dir = tmp_path / "review-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review-skill\n---\nignore previous instructions\n",
        encoding="utf-8",
    )
    native_error = (
        "Security scan blocked this skill (dangerous):\n"
        "  CRITICAL injection      SKILL.md:4                      \"ignore previous instructions\"\n"
    )

    first = classify_pending_skill_scan_block(native_error, skill_dir, governance_verdict="APPROVE")
    second = classify_pending_skill_scan_block(native_error, skill_dir, governance_verdict="APPROVE")

    assert len(first["finding_fingerprint"]) == 64
    assert first["finding_fingerprint"] == second["finding_fingerprint"]


def test_classify_pending_skill_scan_block_suppresses_unchanged_retry(tmp_path):
    from hermes_cli.write_approval_commands import classify_pending_skill_scan_block

    skill_dir = tmp_path / "review-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review-skill\n---\nignore previous instructions\n",
        encoding="utf-8",
    )
    native_error = (
        "Security scan blocked this skill (dangerous):\n"
        "  CRITICAL injection      SKILL.md:4                      \"ignore previous instructions\"\n"
    )
    seen = set()

    first = classify_pending_skill_scan_block(
        native_error,
        skill_dir,
        governance_verdict="APPROVE",
        seen_scan_fingerprints=seen,
    )
    second = classify_pending_skill_scan_block(
        native_error,
        skill_dir,
        governance_verdict="APPROVE",
        seen_scan_fingerprints=seen,
    )

    assert first["retry_suppressed"] is False
    assert second["retry_suppressed"] is True
    assert first["finding_fingerprint"] in seen


def test_preflight_pending_skill_review_reports_final_queue_delta(hermes_home, monkeypatch):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    initial = {
        "id": "initial", "payload": {"action": "patch", "name": "target"},
    }
    arriving = {
        "id": "arriving", "payload": {"action": "patch", "name": "target"},
    }
    inventories = iter([[initial], [initial, arriving]])
    monkeypatch.setattr(wa, "list_pending", lambda subsystem: next(inventories))
    monkeypatch.setattr(
        "tools.skill_manager_tool._find_skill",
        lambda name: {"path": __import__("pathlib").Path(hermes_home) / "skills" / name},
    )

    result = commands.preflight_pending_skill_review()

    assert result["pending_ids_before"] == ["initial"]
    assert result["pending_ids_after"] == ["initial", "arriving"]
    assert result["new_pending_ids"] == ["arriving"]
    assert result["queue_drained"] is False


def test_preflight_pending_skill_review_only_claims_drained_for_empty_final_inventory(hermes_home):
    from hermes_cli import write_approval_commands as commands

    result = commands.preflight_pending_skill_review()

    assert result["pending_ids_before"] == []
    assert result["pending_ids_after"] == []
    assert result["new_pending_ids"] == []
    assert result["queue_drained"] is True
    assert result["governance_status"] == "COMPLETED"


def test_preflight_pending_skill_review_reports_partial_governance_when_records_remain(hermes_home):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    wa.stage_write(
        wa.SKILLS,
        {"action": "patch", "name": "missing-target", "old_string": "x", "new_string": "y"},
        summary="retained blocker",
        origin="background_review",
    )

    result = commands.preflight_pending_skill_review()

    assert result["execution_status"] == "COMPLETED"
    assert result["governance_status"] == "PARTIAL"
    assert result["queue_drained"] is False
    assert result["records"][0]["effective_status"] == "BLOCKED_RESOLVER"


def test_handle_reject(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    rec = wa.stage_write("skills", {"action": "create", "name": "s"},
                         summary="create s", origin="background_review")
    out = handle_pending_subcommand(wa.SKILLS, ["reject", rec["id"]])
    assert "Rejected" in out
    assert wa.pending_count("skills") == 0


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


def test_finalize_pending_skill_manifest_computes_deltas(hermes_home):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    initial = {"pending_ids_before": ["p1", "p2"]}
    res = commands.finalize_pending_skill_manifest(initial)
    assert res["queue_drained"] is True
    assert res["pending_ids_after"] == []
    assert res["new_pending_ids"] == []


def test_format_executive_summary_digest():
    from hermes_cli.write_approval_commands import format_executive_summary_digest

    gov_res = {
        "execution_status": "COMPLETED",
        "governance_status": "PARTIAL",
        "records": [
            {"pending_id": "123456789", "target": "test-skill", "effective_status": "REVISE"}
        ]
    }
    digest = format_executive_summary_digest(gov_res)
    assert "Pending Skills Governance Digest" in digest
    assert "COMPLETED" in digest
    assert "12345678" in digest
    assert "test-skill" in digest


def test_write_internal_governance_artifact(tmp_path):
    from hermes_cli.write_approval_commands import write_internal_governance_artifact

    gov_res = {"execution_status": "COMPLETED", "governance_status": "COMPLETED", "records": []}
    path = write_internal_governance_artifact(gov_res, output_dir=tmp_path)
    assert path.exists()
    assert "pending_governance_" in path.name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["execution_status"] == "COMPLETED"


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


def test_handle_mode_alias_still_works(hermes_home):
    # 'mode' is kept as a back-compat alias for 'approval'.
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.MEMORY, ["mode", "on"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is True
    assert "on" in out


def test_handle_approval_invalid(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    out = handle_pending_subcommand(wa.MEMORY, ["approval", "bogus"],
                                    set_mode_fn=lambda enabled: None)
    assert "Invalid value" in out


def test_handle_unknown_subcommand_returns_none(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    # An unrecognized /skills subcommand (e.g. 'search') must return None so
    # the CLI falls through to the skills hub.
    out = handle_pending_subcommand(wa.SKILLS, ["search", "foo"])
    assert out is None


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


def test_memory_inline_callback_error_stages(hermes_home, approval_callback_cleanup):
    # If the prompt machinery fails, fall back to staging — never drop silently.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)
    def broken_cb(command, description, **kw):
        raise RuntimeError("boom")
    set_approval_callback(broken_cb)

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "fallback fact", store=store))
    assert r.get("staged") is True
    assert wa.pending_count("memory") == 1


def test_gateway_context_stages_not_prompts(hermes_home, monkeypatch):
    # A gateway session has no per-thread CLI callback; the dangerous-command
    # /approve round-trip lives in the pending-queue machinery which the gate
    # does not use. The gate must stage, never attempt an inline prompt
    # (which would hit the input() fallback and silently deny).
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    _set_approval("memory", True)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "gateway fact", store=store))
    assert r.get("staged") is True
    assert store.memory_entries == []
    assert wa.pending_count("memory") == 1


def test_skills_never_prompt_inline_even_with_callback(hermes_home, approval_callback_cleanup):
    # Skills always stage — even when an interactive callback is registered.
    from tools.skill_manager_tool import skill_manage
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("skills", True)

    calls = []
    set_approval_callback(lambda c, d, **kw: calls.append(1) or "once")

    r = json.loads(skill_manage(
        action="create", name="test-inline-skill",
        content="---\nname: test-inline-skill\ndescription: x\n---\nbody\n"))
    assert r.get("staged") is True
    assert calls == []  # never prompted
    assert wa.pending_count("skills") == 1


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

    def test_large_content_reports_kb(self):
        from tools import write_approval as wa
        content = "x" * 2048  # >= 1024 bytes -> KB rounding
        assert wa.skill_gist("create", "big", content=content) == "create 'big' (3 KB)"

    def test_create_without_content_falls_through(self):
        from tools import write_approval as wa
        assert wa.skill_gist("create", "demo") == "create 'demo'"

    def test_patch_counts_lines(self):
        from tools import write_approval as wa
        assert (
            wa.skill_gist("patch", "demo", file_path="SKILL.md",
                          old_string="a\nb", new_string="x\ny\nz")
            == "patch 'demo' SKILL.md (+3/-2 lines)"
        )

    def test_patch_defaults_target_and_empty_strings(self):
        from tools import write_approval as wa
        assert wa.skill_gist("patch", "demo") == "patch 'demo' SKILL.md (+0/-0 lines)"

    def test_file_actions_and_unknown_fallback(self):
        from tools import write_approval as wa
        assert wa.skill_gist("write_file", "demo", file_path="a.py") == "write a.py in 'demo'"
        assert wa.skill_gist("remove_file", "demo", file_path="a.py") == "remove a.py from 'demo'"
        assert wa.skill_gist("delete", "demo") == "delete skill 'demo'"
        assert wa.skill_gist("unknown", "demo") == "unknown 'demo'"


def test_format_executive_summary_digest_sanitization():
    from hermes_cli.write_approval_commands import format_executive_summary_digest
    governance_result = {
        "execution_status": "SUCCESS",
        "governance_status": "PARTIAL",
        "delivery_status": "SUCCESS",
        "pending_ids_before": ["01b97723", "1ff8ddcb"],
        "pending_ids_after": ["01b97723"],
        "approved_ids": ["1ff8ddcb"],
        "records": [
            {
                "pending_id": "1ff8ddcb",
                "target": "demo-skill",
                "effective_status": "APPLIED",
                "old_string": "FORBIDDEN_OLD_STRING",
                "new_string": "FORBIDDEN_NEW_STRING",
                "diff": "FORBIDDEN_DIFF_CONTENT",
                "findings": ["FORBIDDEN_STACK_TRACE"]
            }
        ]
    }
    digest = format_executive_summary_digest(governance_result)
    assert "Pending Skills Governance Digest" in digest
    assert "SUCCESS" in digest
    assert "PARTIAL" in digest
    assert "[1ff8ddcb]" in digest
    # Verify sanitized boundary: raw json/diff/payload fields must NOT leak into human digest
    assert "FORBIDDEN_OLD_STRING" not in digest
    assert "FORBIDDEN_NEW_STRING" not in digest
    assert "FORBIDDEN_DIFF_CONTENT" not in digest
    assert "FORBIDDEN_STACK_TRACE" not in digest
    assert '"pending_ids_before"' not in digest
    assert '"records"' not in digest

