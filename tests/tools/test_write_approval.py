"""Tests for the memory/skill write-approval gate (tools/write_approval.py)
and the shared slash-command handlers (hermes_cli/write_approval_commands.py).

Covers the boolean write_approval gate (off by default = write freely; on =
require approval) for both subsystems, the foreground-vs-background staging
split, pending store CRUD, and the list/approve/reject/diff/approval
subcommand dispatch.
"""

import json
import multiprocessing
import os
import tempfile
import shutil
from pathlib import Path

import pytest


def _approve_pending_worker(home: str, pending_id: str, queue) -> None:
    os.environ["HERMES_HOME"] = home
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store

    queue.put(
        handle_pending_subcommand(
            wa.MEMORY,
            ["approve", pending_id],
            memory_store=load_on_disk_store(),
        )
    )


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


def test_stage_write_creates_linked_learning_candidate(hermes_home):
    from agent import learning_ledger
    from tools import write_approval as wa

    record = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "prefers concise answers"},
        summary="prefers concise answers",
        origin="background_review",
        metadata={
            "source": {"session_id": "session-1", "trust": "user_explicit"},
            "evidence": {
                "status": "captured",
                "source_trust": "user_explicit",
                "excerpt": "Keep it concise",
                "hypothesis": "Avoid repeated verbosity corrections",
                "risk": "low",
                "confidence": "high",
            },
        },
    )

    candidate = learning_ledger.get_candidate(record["candidate_id"])
    assert record["candidate_id"] == record["id"]
    assert record["ledger_recorded"] is True
    assert candidate is not None
    assert candidate["pending_relpath"] == f"pending/memory/{record['id']}.json"
    assert candidate["evidence"]["excerpt"] == "Keep it concise"


def test_claim_pending_has_one_winner_and_can_restore(hermes_home):
    from tools import write_approval as wa

    record = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "one"},
        summary="one",
        origin="foreground",
    )

    first = wa.claim_pending("memory", record["id"])
    second = wa.claim_pending("memory", record["id"])

    assert first is not None
    assert second is None
    assert wa.get_pending("memory", record["id"]) is None
    assert wa.release_claim("memory", first, restore=True) is True
    assert wa.get_pending("memory", record["id"]) is not None


def test_approve_transitions_candidate_and_removes_pending(hermes_home):
    from agent import learning_ledger
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa

    record = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "approved learning"},
        summary="approved learning",
        origin="background_review",
    )
    store = MemoryStore(); store.load_from_disk()

    out = handle_pending_subcommand(wa.MEMORY, ["approve", record["id"]], memory_store=store)

    assert "Approved 1" in out
    assert wa.get_pending("memory", record["id"]) is None
    assert learning_ledger.get_candidate(record["id"])["status"] == "active"
    assert [event["event"] for event in learning_ledger.list_events(candidate_id=record["id"])][-2:] == [
        "candidate_apply_started",
        "candidate_activated",
    ]


def test_reject_preserves_reason_in_ledger(hermes_home):
    from agent import learning_ledger
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        "skills",
        {"action": "delete", "name": "example"},
        summary="delete example",
        origin="background_review",
    )

    out = handle_pending_subcommand(
        wa.SKILLS,
        ["reject", record["id"], "too", "risky"],
    )

    assert "Rejected" in out
    candidate = learning_ledger.get_candidate(record["id"])
    assert candidate["status"] == "rejected"
    rejection = next(
        event
        for event in learning_ledger.list_events(candidate_id=record["id"])
        if event["event"] == "candidate_rejected"
    )
    assert rejection["detail"]["reason"] == "too risky"


def test_background_write_stages_when_approval_config_is_on(hermes_home, monkeypatch):
    from tools import write_approval as wa
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        reset_current_write_origin,
        set_current_write_origin,
    )

    monkeypatch.setattr(wa, "write_approval_enabled", lambda subsystem: True)
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        decision = wa.evaluate_gate(wa.MEMORY)
    finally:
        reset_current_write_origin(token)

    assert decision.stage is True
    assert decision.allow is False


def test_equivalent_rejected_background_proposal_is_suppressed(hermes_home):
    from agent import learning_ledger
    from tools import write_approval as wa

    payload = {"action": "add", "target": "user", "content": "stable preference"}
    first = wa.stage_write(
        "memory", payload, summary="stable preference", origin="background_review"
    )
    assert learning_ledger.transition_candidate(
        first["id"],
        from_status="pending",
        to_status="rejected",
        event="candidate_rejected",
        detail={"reason": "not durable"},
    ) is not None
    assert wa.discard_pending("memory", first["id"])

    duplicate = wa.stage_write(
        "memory", payload, summary="same words, same mutation", origin="background_review"
    )

    assert duplicate["suppressed"] is True
    assert duplicate["candidate_id"] == first["id"]
    assert wa.pending_count("memory") == 0


def test_pending_list_surfaces_evidence_risk_and_hypothesis(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    wa.stage_write(
        "skills",
        {"action": "patch", "name": "demo", "old_string": "a", "new_string": "b"},
        summary="patch demo",
        origin="background_review",
        metadata={
            "evidence": {
                "status": "captured",
                "source_trust": "review_observed",
                "excerpt": "The previous procedure failed",
                "hypothesis": "This patch prevents the same failure",
                "risk": "medium",
                "confidence": "medium",
            }
        },
    )

    out = handle_pending_subcommand("skills", ["pending"])

    assert "risk=medium" in out
    assert "The previous procedure failed" in out
    assert "This patch prevents the same failure" in out


def test_approve_all_skips_high_risk_candidate(hermes_home):
    from agent import learning_ledger
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        "skills",
        {"action": "delete", "name": "dangerous"},
        summary="delete dangerous",
        origin="background_review",
    )

    out = handle_pending_subcommand("skills", ["approve", "all"])

    assert "requires explicit approval" in out
    assert wa.get_pending("skills", record["id"]) is not None
    assert learning_ledger.get_candidate(record["id"])["status"] == "pending"


def test_history_surfaces_rejection_reason(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "temporary detail"},
        summary="temporary detail",
        origin="background_review",
    )
    handle_pending_subcommand("memory", ["reject", record["id"], "temporary"])

    out = handle_pending_subcommand("memory", ["history"])

    assert record["id"] in out
    assert "rejected" in out
    assert "temporary" in out


def test_skill_eval_compares_baseline_and_records_outcome(hermes_home):
    from pathlib import Path

    from agent import learning_ledger
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    skill_dir = Path(hermes_home) / "skills" / "demo"
    (skill_dir / "evals").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Retry the command.\n", encoding="utf-8")
    (skill_dir / "evals" / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [{"name": "verified retry", "must_contain": ["retry", "verify"]}],
            }
        ),
        encoding="utf-8",
    )
    record = wa.stage_write(
        "skills",
        {
            "action": "patch",
            "name": "demo",
            "old_string": "Retry the command.",
            "new_string": "Retry the command, then verify the result.",
        },
        summary="add verification",
        origin="background_review",
    )

    out = handle_pending_subcommand("skills", ["eval", record["id"]])

    assert "improved" in out
    assert (Path(hermes_home) / "learning" / "snapshots" / record["id"] / "baseline.txt").exists()
    assert learning_ledger.list_events(candidate_id=record["id"])[-1]["event"] == "outcome_verification_succeeded"


def test_stage_failure_is_not_reported_as_staged(hermes_home, monkeypatch):
    from tools import write_approval as wa

    monkeypatch.setattr(wa, "_atomic_pending_json_write", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    result = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "x"},
        summary="x",
        origin="foreground",
    )

    assert result["success"] is False
    assert result["staged"] is False


def test_pending_public_apis_reject_path_traversal(hermes_home):
    import pytest

    from tools import write_approval as wa

    with pytest.raises(ValueError):
        wa.get_pending("memory", "../other-profile")
    with pytest.raises(ValueError):
        wa.list_pending("../memory")


def test_untrusted_background_review_context_forces_staging_when_gate_off(hermes_home, monkeypatch):
    from agent.learning_context import learning_metadata_scope
    from tools import write_approval as wa
    from tools.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin

    monkeypatch.setattr(wa, "write_approval_enabled", lambda subsystem: False)
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        with learning_metadata_scope(
            {"evidence": {"status": "captured", "source_trust": "user_supplied_unverified", "risk": "medium"}}
        ):
            decision = wa.evaluate_gate("memory")
    finally:
        reset_current_write_origin(token)

    assert decision.stage is True


def test_approval_rejects_tampered_replay_payload(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store

    record = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "reviewed"},
        summary="reviewed",
        origin="foreground",
    )
    path = wa._pending_dir("memory") / f"{record['id']}.json"
    body = json.loads(path.read_text())
    body["payload"]["content"] = "tampered"
    path.write_text(json.dumps(body))

    out = handle_pending_subcommand(
        "memory", ["approve", record["id"]], memory_store=load_on_disk_store()
    )

    assert "stale review" in out
    assert wa.get_pending("memory", record["id"]) is not None


def test_approval_rejects_changed_memory_target(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store

    store = load_on_disk_store()
    assert store.add("memory", "old fact")["success"]
    record = wa.stage_write(
        "memory",
        {"action": "replace", "target": "memory", "old_text": "old fact", "content": "new fact"},
        summary="replace fact",
        origin="foreground",
        metadata={"precondition": {"old_texts": ["old fact"]}},
    )
    assert store.remove("memory", "old fact")["success"]

    out = handle_pending_subcommand("memory", ["approve", record["id"]], memory_store=store)

    assert "memory target changed" in out
    assert wa.get_pending("memory", record["id"]) is not None


def test_two_process_approval_applies_exactly_once(hermes_home):
    from agent import learning_ledger
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "one durable entry"},
        summary="concurrent approval",
        origin="background_review",
    )
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_approve_pending_worker,
            args=(hermes_home, record["id"], queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    results = [queue.get(timeout=2) for _ in processes]
    assert sum("Approved 1" in result for result in results) == 1, results
    assert load_on_disk_store()._entries_for("memory") == ["one durable entry"]
    assert wa.get_pending(wa.MEMORY, record["id"]) is None
    candidate = learning_ledger.get_candidate(record["id"])
    assert candidate is not None
    assert candidate["status"] == "active"


def test_interrupted_claim_requires_explicit_reconciliation(hermes_home):
    from agent import learning_ledger
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "recoverable"},
        summary="reconcile interrupted approval",
        origin="background_review",
    )
    claim = wa.claim_pending(wa.MEMORY, record["id"])
    assert claim is not None
    assert learning_ledger.transition_candidate(
        record["id"],
        from_status="pending",
        to_status="applying",
        event="candidate_apply_started",
        detail={"claim_id": claim["_claim_id"]},
    ) is not None

    listing = handle_pending_subcommand(wa.MEMORY, ["reconcile"])
    assert record["id"] in listing
    restored = handle_pending_subcommand(
        wa.MEMORY, ["reconcile", record["id"], "restore"]
    )

    assert restored is not None
    assert "Restored" in restored
    assert wa.get_pending(wa.MEMORY, record["id"]) is not None
    candidate = learning_ledger.get_candidate(record["id"])
    assert candidate is not None
    assert candidate["status"] == "pending"
    assert wa.list_claims(wa.MEMORY) == []


def test_staging_fails_closed_when_ledger_write_fails(hermes_home, monkeypatch):
    from agent import learning_ledger
    from tools import write_approval as wa

    monkeypatch.setattr(
        learning_ledger,
        "create_candidate",
        lambda _candidate: (_ for _ in ()).throw(OSError("ledger unavailable")),
    )
    result = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "must not orphan"},
        summary="ledger failure",
        origin="foreground",
    )

    assert result["success"] is False
    assert result["staged"] is False
    assert wa.pending_count(wa.MEMORY) == 0


def test_pending_envelope_id_must_match_filename(hermes_home):
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "original"},
        summary="tamper envelope",
        origin="foreground",
    )
    path = Path(hermes_home) / "pending" / "memory" / f"{record['id']}.json"
    body = json.loads(path.read_text())
    body["id"] = "different-id"
    path.write_text(json.dumps(body))

    assert wa.get_pending(wa.MEMORY, record["id"]) is None
    assert wa.list_pending(wa.MEMORY) == []
    assert wa.claim_pending(wa.MEMORY, record["id"]) is None


def test_pending_subsystem_directory_must_not_be_symlink(hermes_home):
    from tools import write_approval as wa

    outside = Path(hermes_home).parent / "outside"
    outside.mkdir()
    pending = Path(hermes_home) / "pending"
    pending.mkdir()
    (pending / "memory").symlink_to(outside, target_is_directory=True)

    result = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "escape"},
        summary="symlink escape",
        origin="foreground",
    )
    assert result["success"] is False
    assert result["staged"] is False
    assert list(outside.iterdir()) == []


def test_staging_rejects_preexisting_pending_record_symlink(hermes_home, monkeypatch):
    from types import SimpleNamespace
    from tools import write_approval as wa

    pending_id = "fixed-pending-id"
    outside = Path(hermes_home).parent / "outside.json"
    outside.write_text("unchanged")
    pending_dir = Path(hermes_home) / "pending" / wa.MEMORY
    pending_dir.mkdir(parents=True)
    (pending_dir / f"{pending_id}.json").symlink_to(outside)
    monkeypatch.setattr(wa.uuid, "uuid4", lambda: SimpleNamespace(hex=pending_id))

    result = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "must stay local"},
        summary="symlink target",
        origin="foreground",
    )

    assert result["success"] is False
    assert result["staged"] is False
    assert outside.read_text() == "unchanged"


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


    def test_file_actions_and_unknown_fallback(self):
        from tools import write_approval as wa
        assert wa.skill_gist("write_file", "demo", file_path="a.py") == "write a.py in 'demo'"
        assert wa.skill_gist("remove_file", "demo", file_path="a.py") == "remove a.py from 'demo'"
        assert wa.skill_gist("delete", "demo") == "delete skill 'demo'"
        assert wa.skill_gist("unknown", "demo") == "unknown 'demo'"


def test_pending_candidate_relink_is_not_listed(hermes_home):
    from tools import write_approval as wa

    first = wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": "first"}, summary="first", origin="foreground")
    second = wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": "second"}, summary="second", origin="foreground")
    path = Path(hermes_home) / "pending" / "memory" / f"{first['id']}.json"
    envelope = json.loads(path.read_text())
    envelope["candidate_id"] = second["id"]
    envelope["payload"] = second["payload"]
    path.write_text(json.dumps(envelope))

    assert wa.get_pending(wa.MEMORY, first["id"]) is None
    assert all(item["id"] != first["id"] for item in wa.list_pending(wa.MEMORY))


def test_legacy_pending_record_without_candidate_id_is_migratable(hermes_home):
    from tools import write_approval as wa

    pending_id = "legacy123"
    path = Path(hermes_home) / "pending" / "memory" / f"{pending_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "id": pending_id,
                "subsystem": wa.MEMORY,
                "action": "add",
                "summary": "private legacy detail that must not persist",
                "payload": {"action": "add", "target": "memory", "content": "legacy"},
            }
        )
    )

    record = wa.get_pending(wa.MEMORY, pending_id)

    assert record is not None
    assert record["candidate_id"] == pending_id
    candidate = wa.ensure_candidate_for_record(record)
    assert candidate is not None
    assert candidate["candidate_id"] == pending_id
    assert candidate["proposal"]["summary"] == "memory add candidate"
    assert "private legacy" not in json.dumps(candidate)


def test_ambiguous_apply_exception_requires_reconciliation_without_replay(hermes_home, monkeypatch):
    from agent import learning_ledger
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "exactly once"},
        summary="ambiguous application",
        origin="foreground",
    )
    side_effects = []

    def mutate_then_raise(payload, store):
        side_effects.append(payload["content"])
        raise RuntimeError("receipt lost after mutation")

    monkeypatch.setattr("tools.memory_tool.apply_memory_pending", mutate_then_raise)
    store = MemoryStore()

    first = handle_pending_subcommand(
        wa.MEMORY, ["approve", record["id"]], memory_store=store
    )
    second = handle_pending_subcommand(
        wa.MEMORY, ["approve", record["id"]], memory_store=store
    )

    assert first is not None and "needs reconciliation" in first
    assert second is not None and "No pending" in second
    assert side_effects == ["exactly once"]
    candidate = learning_ledger.get_candidate(record["id"])
    assert candidate is not None and candidate["status"] == "applying"
    assert len(wa.list_claims(wa.MEMORY)) == 1


def test_memory_substring_precondition_preserves_existing_semantics(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, memory_tool

    store = MemoryStore()
    assert store.add("memory", "User prefers concise technical answers")["success"]
    _set_approval(wa.MEMORY, True)
    staged = json.loads(memory_tool(action="replace", target="memory", old_text="concise technical", content="User prefers concise verified answers", store=store))
    assert staged["staged"] is True
    result = handle_pending_subcommand(wa.MEMORY, ["approve", staged["pending_id"]], memory_store=store)

    assert result is not None and "Approved 1" in result
    assert store.memory_entries == ["User prefers concise verified answers"]


def test_long_lived_ledger_does_not_copy_raw_memory_summary(hermes_home):
    from agent import learning_ledger
    from tools import write_approval as wa

    secret_text = "private family detail that belongs only in pending replay"
    record = wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": secret_text}, summary=f"add to memory: {secret_text}", origin="foreground")
    candidate = learning_ledger.get_candidate(record["id"])

    assert candidate is not None
    assert secret_text not in json.dumps(candidate)


def test_invalid_skill_path_is_rejected_before_staging_or_diff(hermes_home, tmp_path):
    from tools import write_approval as wa
    from tools.skill_manager_tool import skill_manage

    skill_dir = Path(hermes_home) / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("must not be disclosed")
    _set_approval(wa.SKILLS, True)
    result = json.loads(skill_manage(action="write_file", name="demo", file_path=str(secret), file_content="x"))

    assert result["success"] is False
    assert wa.pending_count(wa.SKILLS) == 0


def test_explicit_rejection_discards_orphan_pending_record(hermes_home, monkeypatch):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": "orphan"}, summary="orphan", origin="foreground")
    monkeypatch.setattr(wa, "ensure_candidate_for_record", lambda _record: None)
    result = handle_pending_subcommand(wa.MEMORY, ["reject", record["id"]])

    assert result == f"Rejected pending memory write '{record['id']}'."
    assert wa.get_pending(wa.MEMORY, record["id"]) is None
