"""Phase 3 Packet 4B — composition/consolidation proof.

Safe to run in CI: no model calls, no real cron/session state touched (reuses
tests/e2e/precedence_harness.py's isolated_cron_state()). Structural/semantic
assertions, not whole-file snapshots — files here carry a lot of unrelated
prose (e.g. technical-message-style's behavioral Pitfalls bullets) that must
stay free to change without breaking these tests.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.e2e.precedence_harness import (  # noqa: E402
    PROFILE_ROOTS,
    PrecedenceCase,
    assemble_prompt,
    isolated_cron_state,
)

OPS = PROFILE_ROOTS["ops-repair"]
FANTASY = PROFILE_ROOTS["fantasy"]

GENERATION_CONTRACT = OPS / "skills" / "communication" / "generation-contract" / "SKILL.md"
SOUL_OPS = OPS / "SOUL.md"
RESPONSE_MODES_OPS = OPS / "skills" / "communication" / "response-modes" / "SKILL.md"
TMS_OPS = OPS / "skills" / "communication" / "technical-message-style" / "SKILL.md"
TMS_FANTASY = FANTASY / "skills" / "communication" / "technical-message-style" / "SKILL.md"
VAULT_TASK_WORKFLOW = OPS / "skills" / "vault-task-workflow" / "SKILL.md"
PHASE_TEMPLATE_OPS = (
    OPS / "skills" / "devops" / "lean-build-execution" / "references" / "phase-completion-template.md"
)
PHASE_TEMPLATE_FANTASY = (
    FANTASY / "skills" / "devops" / "structured-phase-builds" / "references" / "phase-completion-template.md"
)
VERIFY_SCRIPT_OPS = OPS / "skills" / "communication" / "technical-message-style" / "scripts" / "verify-fantasy-output.py"
VERIFY_SCRIPT_FANTASY = FANTASY / "skills" / "fantasy-response-format" / "scripts" / "verify-fantasy-output.py"

_TABLES_PREFERRED_RE = re.compile(r"tables?\s+(?:are\s+)?preferred", re.IGNORECASE)


# 1. generation-contract loads via skill_view() in real composition
def test_generation_contract_loads_in_real_composition():
    case = PrecedenceCase("gc_4b_check", "", "test prompt", ["generation-contract"])
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(case)
    assert result["skills_skipped"] == []
    assert '[IMPORTANT: The user has invoked the "generation-contract" skill' in result["prompt"]


# 2. exactly one active canonical no-status-emoji directive for Telegram operational output
def test_exactly_one_canonical_no_emoji_directive():
    gc_text = GENERATION_CONTRACT.read_text(encoding="utf-8")
    directives = re.findall(r"No decorative or status emoji", gc_text)
    assert len(directives) == 1, f"expected exactly one canonical emoji directive, found {len(directives)}"

    # Subordinate files must not restate it as their own independent rule —
    # they may only reference/point to it.
    for path, label in [(SOUL_OPS, "SOUL.md"), (RESPONSE_MODES_OPS, "response-modes"), (TMS_OPS, "technical-message-style")]:
        text = path.read_text(encoding="utf-8")
        assert "No decorative or status emoji" not in text, (
            f"{label} restates the canonical emoji directive instead of pointing to it"
        )


# 3. the proven ✅ <TASK-ID> done conflicting example remains absent
def test_checkmark_example_still_absent_from_vault_task_workflow_composition():
    case = PrecedenceCase("vault_4b_check", "", "test prompt", ["vault-task-workflow"])
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(case)
    assert "✅ <TASK-ID> done" not in result["prompt"]
    assert "<TASK-ID> done — <one plain-English sentence" in result["prompt"]


# 4. technical-message-style no longer contains its table-policy contradiction
def test_technical_message_style_table_contradiction_resolved():
    for path, label in [(TMS_OPS, "ops-repair"), (TMS_FANTASY, "fantasy")]:
        text = path.read_text(encoding="utf-8")
        assert not _TABLES_PREFERRED_RE.search(text), (
            f"{label} technical-message-style still contains the 'tables preferred' contradiction"
        )


# 5. response-modes delegates presentation to generation-contract
def test_response_modes_completion_mode_delegates_to_generation_contract():
    text = RESPONSE_MODES_OPS.read_text(encoding="utf-8")
    assert "## Mode: completion" in text
    assert "## Known gap: completion mode" not in text
    # Must not restate the 3-line template as its own independent block.
    assert "✅ <ID> done" not in text
    assert "generation-contract" in text.split("## Mode: completion", 1)[1].split("##", 1)[0]


# 6. SOUL does not independently compete on Telegram table/emoji/completion formatting
def test_soul_no_longer_independently_competes_on_formatting():
    text = SOUL_OPS.read_text(encoding="utf-8")
    assert "NO decorative headings" not in text  # old Response-Modes quick-ref block, removed
    assert "Field: Branch" not in text  # old duplicated normative example, removed
    assert "generation-contract" in text  # pointer present
    # The 🔧 signature exception is SPECIALIZED, not a duplicate — must survive.
    assert "🔧" in text


# 7. vault artifact templates remain outside Telegram-generation authority (byte-unchanged)
def test_phase_completion_templates_untouched():
    import hashlib

    for path in (PHASE_TEMPLATE_OPS, PHASE_TEMPLATE_FANTASY):
        assert path.is_file()
    ops_hash = hashlib.sha256(PHASE_TEMPLATE_OPS.read_bytes()).hexdigest()
    fantasy_hash = hashlib.sha256(PHASE_TEMPLATE_FANTASY.read_bytes()).hexdigest()
    # Both were byte-identical to each other before this packet (Phase 3 investigation) and
    # neither was touched by any 4B edit — still identical to each other confirms both are
    # untouched (a change to only one would break this).
    assert ops_hash == fantasy_hash


# 8. deployment states remain reserved, not redefined as casual task states
def test_deployment_vocabulary_still_reserved_in_generation_contract():
    text = GENERATION_CONTRACT.read_text(encoding="utf-8")
    assert "EDITED" in text and "VERIFIED" in text  # still named, as reserved-not-reused
    completion_section = text.split("## Completion shape", 1)[1].split("## Evidence discipline", 1)[0]
    assert "reused" in completion_section.lower() or "reserved" in completion_section.lower()


# 9. dormant verify-fantasy-output.py is marked, not represented as active enforcement
def test_verify_fantasy_output_marked_dormant_both_copies():
    for path in (VERIFY_SCRIPT_OPS, VERIFY_SCRIPT_FANTASY):
        assert path.is_file(), f"missing: {path}"
        text = path.read_text(encoding="utf-8")
        assert "DORMANT" in text
        assert "not invoked by any cron job, test, CI step, or hook" in text


# 10. real Task Orchestration job composition contains the pointer, no contradictory instruction
def test_real_job_composition_has_no_contradictory_instruction():
    case = PrecedenceCase(
        "real_job_4b_check", "", "test prompt",
        ["vault-task-workflow", "lean-build-execution", "technical-message-style"],
    )
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(case)
    prompt = result["prompt"]
    assert result["skills_skipped"] == []
    assert re.search(r"aligns with `generation-contract`'s canonical\s+Completion-shape section", prompt)
    assert not _TABLES_PREFERRED_RE.search(prompt), (
        "the removed Pitfalls contradiction reappeared via technical-message-style's injected content"
    )


def test_no_production_state_touched_by_these_composition_checks():
    cron_output_dir = OPS / "cron" / "output"
    sessions_dir = OPS / "sessions"

    before_output = set(cron_output_dir.rglob("*")) if cron_output_dir.is_dir() else set()
    before_sessions = set(sessions_dir.glob("session_*.json")) if sessions_dir.is_dir() else set()

    with isolated_cron_state("ops-repair"):
        assemble_prompt(PrecedenceCase("noop_4b_check", "", "test", ["generation-contract"]))

    after_output = set(cron_output_dir.rglob("*")) if cron_output_dir.is_dir() else set()
    after_sessions = set(sessions_dir.glob("session_*.json")) if sessions_dir.is_dir() else set()

    new_output_files = {p for p in (after_output - before_output) if p.is_file()}
    assert not new_output_files, f"unexpected new cron/output files: {new_output_files}"
    assert after_sessions == before_sessions
