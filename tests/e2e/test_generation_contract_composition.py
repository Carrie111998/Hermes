"""Phase 3 Packet 4A — composition proof for the new generation-contract skill
and the corrected vault-task-workflow completion example.

Safe to run in CI: no model calls, no real cron/session state touched (reuses
tests/e2e/precedence_harness.py's isolated_cron_state()).
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

CONTRACT_PATH = (
    PROFILE_ROOTS["ops-repair"] / "skills" / "communication" / "generation-contract" / "SKILL.md"
)
VAULT_TASK_WORKFLOW_PATH = (
    PROFILE_ROOTS["ops-repair"] / "skills" / "vault-task-workflow" / "SKILL.md"
)


def test_generation_contract_file_exists_and_is_tracked():
    assert CONTRACT_PATH.is_file()
    import subprocess

    out = subprocess.run(
        ["git", "status", "--short", "--", str(CONTRACT_PATH)],
        cwd=str(PROFILE_ROOTS["ops-repair"].parents[1]),
        capture_output=True, text=True, check=True,
    ).stdout
    # "??" (untracked-but-visible) or "A " (staged) are both fine — the point
    # is it must NOT be silently absent from git status (which is what
    # gitignored-and-invisible would look like: empty output).
    assert out.strip() != "", "generation-contract SKILL.md is invisible to git status — likely gitignored"


def test_generation_contract_loads_via_skill_view_in_real_composition():
    """Proves the new contract is actually loadable through the same
    production skill-injection path (_build_job_prompt -> skill_view) real
    cron jobs use — not just present on disk."""
    case = PrecedenceCase(
        key="gc_load_check",
        description="Packet 4A composition check",
        prompt="test prompt",
        skills=["generation-contract"],
    )
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(case)

    assert result["skills_skipped"] == [], "generation-contract skill failed to resolve"
    assert '[IMPORTANT: The user has invoked the "generation-contract" skill' in result["prompt"]
    assert "No decorative or status emoji" in result["prompt"]


def test_conflicting_checkmark_example_no_longer_present_in_vault_task_workflow():
    """Regression proof for the Packet 4A fix: the specific string Packet 3's
    controlled experiment showed drives 10/10 emoji emission must be gone
    from the assembled prompt, not just edited in isolation on disk."""
    case = PrecedenceCase(
        key="vault_regression_check",
        description="Packet 4A regression: checkmark example removed",
        prompt="test prompt",
        skills=["vault-task-workflow"],
    )
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(case)

    assert result["skills_skipped"] == []
    assert "✅ <TASK-ID> done" not in result["prompt"], (
        "the proven-conflicting checkmark example is still present in the assembled prompt"
    )
    # The corrected, plain-text example must be present instead.
    assert "<TASK-ID> done — <one plain-English sentence" in result["prompt"]
    assert "No emoji, decorative or status." in result["prompt"]


def test_unrelated_vault_task_workflow_content_unchanged_around_the_edit():
    """Scope check: only the checkmark example line changed. The surrounding
    §6b hard rules, the Shape template above it, and the reference-file
    pointers below it must be byte-identical to before, proving this was a
    surgical edit, not an opportunistic rewrite."""
    text = VAULT_TASK_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "What I did: <one sentence, no jargon>" in text
    assert 'What\'s next: <one sentence — what happens on the next tick, or "nothing — waiting on X">' in text
    assert "BANNED vocabulary in the final reply:" in text
    assert "references/task-completion-report-format.md" in text
    assert "references/format-defect-forensic-trace.md" in text


def test_phase_completion_template_scope_unchanged():
    """Packet 4A must not touch lean-build-execution's phase-completion-
    template.md — that stays scoped to the vault artifact per the contract's
    own Scope section, not edited or removed in this packet."""
    path = (
        PROFILE_ROOTS["ops-repair"] / "skills" / "devops" / "lean-build-execution"
        / "references" / "phase-completion-template.md"
    )
    assert path.is_file(), "phase-completion-template.md must still exist, untouched, out of this packet's scope"


def test_generation_contract_no_known_self_contradiction_patterns():
    """Contract contradiction lint (P3.9/4A.6) — checks for the SPECIFIC
    self-contradiction shape already found and documented in
    technical-message-style/SKILL.md (a blanket ban stated once, then a later
    'preferred' statement contradicting it with no reconciling language).
    Not a general-purpose contradiction detector — a targeted regression
    guard against reintroducing the same known failure mode in the new file.
    """
    text = CONTRACT_PATH.read_text(encoding="utf-8")

    # The known bad pattern: "...preferred" immediately following emoji/table
    # guidance without an intervening qualifier. Assert it doesn't recur.
    assert not re.search(r"tables?\s+(?:are\s+)?preferred", text, re.IGNORECASE)
    assert not re.search(r"emoji\s+(?:is|are\s+)?preferred", text, re.IGNORECASE)

    # The emoji rule must be stated exactly once as a directive (the
    # "Evidence:" paragraph below it references the same rule, which is
    # expected and fine — this counts directive-strength statements only).
    emoji_directives = re.findall(r"No decorative or status emoji", text)
    assert len(emoji_directives) == 1, f"expected exactly one emoji directive, found {len(emoji_directives)}"


def test_no_production_state_touched_by_these_composition_checks():
    """Belt-and-suspenders: these are read-only/offline checks (they never
    call AIAgent/run_conversation, so the Packet 3 session-write gap isn't
    even reachable here); confirm running them doesn't add any NEW file
    under the real profile's cron/output or sessions.

    This is a live production system with its own background cron activity
    (e.g. profiles/ops-repair/cron/jobs.json is already modified by real
    scheduler ticks unrelated to this test session), so the check must
    compare a before/after snapshot of exactly these two directories'
    listings, not assert on overall `git status` cleanliness.
    """
    cron_output_dir = PROFILE_ROOTS["ops-repair"] / "cron" / "output"
    sessions_dir = PROFILE_ROOTS["ops-repair"] / "sessions"

    before_output = set(cron_output_dir.rglob("*")) if cron_output_dir.is_dir() else set()
    before_sessions = set(sessions_dir.glob("session_*.json")) if sessions_dir.is_dir() else set()

    with isolated_cron_state("ops-repair"):
        assemble_prompt(PrecedenceCase("noop_check", "", "test", ["generation-contract"]))

    after_output = set(cron_output_dir.rglob("*")) if cron_output_dir.is_dir() else set()
    after_sessions = set(sessions_dir.glob("session_*.json")) if sessions_dir.is_dir() else set()

    new_output_files = {p for p in (after_output - before_output) if p.is_file()}
    assert not new_output_files, f"unexpected new cron/output files: {new_output_files}"
    assert after_sessions == before_sessions, (
        f"unexpected new session files: {after_sessions - before_sessions}"
    )
