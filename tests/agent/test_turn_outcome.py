"""Tests for agent/turn_outcome.py — end-of-turn outcome evaluation (Layer 0).

Pins the Layer 0 decisions from patch.md to behavior:

  - signal-gated trigger: no signal (all used skills verified clean, no
    residue, no ``run: always``) ⇒ no aux call, nothing recorded
  - down-only override: a mechanical verifier FAIL wins over an eval that
    claims success
  - pass-is-not-success: a verifier PASS never confirms success; the eval's
    semantic failure is still recorded
  - weak pass: eval success at low confidence over unverified residue is not
    recorded (must not clear ``needs_review`` on its own)
  - dumb-recorder attribution: mechanical FAILs always land on their skill;
    empty ``failure_points`` writes nothing
  - reason corpus: verifier reason and eval reason both surface
  - best-effort: a broken aux call never breaks the turn

The verifier path is the REAL one — real SKILL.md frontmatter, real subprocess
against a temp skill dir. Only the aux model call is injected (a seam); there
is no live network anywhere.
"""

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def turn_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a clean skills/ dir per test."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import tools.skill_usage as mod

    importlib.reload(mod)
    monkeypatch.setattr(mod, "_prune_builtins_enabled", lambda: False)
    return home


def _verify_script(success: bool, reason: str) -> str:
    """Body of a verifier script that prints valid structured JSON on stdout."""
    payload = json.dumps({"success": success, "reason": reason})
    return "print(" + repr(payload) + ")\n"


def _write_skill_with_verify(skills_dir: Path, name: str, script_body: str) -> Path:
    d = skills_dir / name
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "scripts" / "verify.py").write_text(script_body, encoding="utf-8")
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill
metadata:
  hermes:
    verify:
      run: scripts/verify.py
---

# body
""",
        encoding="utf-8",
    )
    return d


def _write_plain_skill(skills_dir: Path, name: str) -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill
---

# body
""",
        encoding="utf-8",
    )
    return d


def _eval(**kwargs):
    """Shortcut for the injected aux-eval seam."""
    return lambda _prompt: kwargs


def test_no_signal_skips_aux_and_writes_nothing(turn_env):
    """All used skills verified clean, no residue, run=auto ⇒ no aux, no write."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record, set_verify_enabled

    d = _write_skill_with_verify(
        turn_env / "skills", "golden", _verify_script(True, "ok")
    )
    set_verify_enabled("golden", True)

    called = []
    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"golden": d},
        outcome_config={"enabled": True, "run": "auto"},
        _aux_eval=lambda p: called.append(p) or _eval(
            task_succeeded=True, confidence=0.9, failure_points=[], reason="ok"
        ),
    )
    assert outcome is None
    assert called == []
    assert get_record("golden").get("recent_outcomes") == []


def test_down_only_verifier_fail_wins_over_llm_success(turn_env):
    """A mechanical FAIL is recorded even when the eval claims success."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record, set_verify_enabled

    d = _write_skill_with_verify(
        turn_env / "skills",
        "bad",
        _verify_script(False, "commit message 'fix stuff' has no type prefix"),
    )
    set_verify_enabled("bad", True)

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"bad": d},
        outcome_config={"enabled": True},
        _aux_eval=_eval(
            task_succeeded=True, confidence=0.95, failure_points=[], reason="looks fine"
        ),
    )
    assert outcome is not None
    assert outcome.task_succeeded is False
    assert outcome.confidence == 1.0
    assert outcome.failure_points == ["bad"]
    assert get_record("bad")["recent_outcomes"] == [False]


def test_down_only_blocks_eval_blaming_unverified_sibling(turn_env):
    """Down-only covers attribution too: a mechanical FAIL on skill A forecloses
    the turn, so the eval's ``failure_points`` must not pin blame on an
    unrelated, unverified skill B that also ran. Only A gets bump_outcome(False);
    B's record stays untouched even though the eval named it."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record, set_verify_enabled

    da = _write_skill_with_verify(
        turn_env / "skills", "mechfail", _verify_script(False, "verifier says no")
    )
    set_verify_enabled("mechfail", True)
    db = _write_plain_skill(turn_env / "skills", "unverified_sibling")

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"mechfail": da, "unverified_sibling": db},
        outcome_config={"enabled": True},
        _aux_eval=_eval(
            task_succeeded=False,
            confidence=0.9,
            failure_points=["mechfail", "unverified_sibling"],
            reason="wrong change committed",
        ),
    )
    assert outcome is not None
    assert outcome.task_succeeded is False
    assert outcome.failure_points == ["mechfail"]
    assert get_record("mechfail")["recent_outcomes"] == [False]
    assert get_record("unverified_sibling").get("recent_outcomes") == []


def test_pass_is_not_success_when_eval_flags_semantics(turn_env):
    """Verifier PASS never confirms success; the eval's semantic fail is recorded.

    ``run: always`` here because under ``run: auto`` a clean verifier-backed
    turn has no residue to trigger the eval — the semantic-fail-over-pass
    case is exactly when the eval must still run.
    """
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record, set_verify_enabled

    d = _write_skill_with_verify(
        turn_env / "skills", "rel", _verify_script(True, "ok")
    )
    set_verify_enabled("rel", True)

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"rel": d},
        outcome_config={"enabled": True, "run": "always"},
        _aux_eval=_eval(
            task_succeeded=False,
            confidence=0.8,
            failure_points=["rel"],
            reason="commit describes the wrong change",
        ),
    )
    assert outcome.task_succeeded is False
    assert outcome.failure_points == ["rel"]
    assert get_record("rel")["recent_outcomes"] == [False]


def test_weak_pass_low_confidence_not_recorded(turn_env):
    """Unverified residue + low-confidence eval success ⇒ nothing written."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record

    d = _write_plain_skill(turn_env / "skills", "open")

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"open": d},
        outcome_config={"enabled": True},
        _aux_eval=_eval(
            task_succeeded=True, confidence=0.4, failure_points=[], reason="probably fine"
        ),
    )
    assert outcome is not None
    assert outcome.task_succeeded is True
    assert outcome.confidence == 0.4
    assert get_record("open").get("recent_outcomes") == []


def test_empty_failure_points_no_sidecar_write(turn_env):
    """A turn-level failure with no attributable skill writes nothing."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record

    d = _write_plain_skill(turn_env / "skills", "mystery")

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"mystery": d},
        outcome_config={"enabled": True},
        _aux_eval=_eval(
            task_succeeded=False,
            confidence=0.7,
            failure_points=[],
            reason="turn failed but no skill to blame",
        ),
    )
    assert outcome is not None
    assert outcome.task_succeeded is False
    assert outcome.failure_points == []
    assert get_record("mystery").get("recent_outcomes") == []


def test_reason_corpus_merges_verifier_and_eval(turn_env):
    """Both the mechanical reason and the semantic reason surface together."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import set_verify_enabled

    d = _write_skill_with_verify(
        turn_env / "skills",
        "cc",
        _verify_script(False, "commit message 'fix stuff' has no type prefix"),
    )
    set_verify_enabled("cc", True)

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"cc": d},
        outcome_config={"enabled": True},
        _aux_eval=_eval(
            task_succeeded=False,
            confidence=0.9,
            failure_points=[],
            reason="message also describes the wrong change",
        ),
    )
    assert "verifier (cc)" in outcome.reason
    assert "no type prefix" in outcome.reason
    assert "wrong change" in outcome.reason


def test_aux_raise_falls_back_to_mechanical(turn_env):
    """A broken aux call never breaks the turn; mechanical verdict still lands."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record, set_verify_enabled

    d = _write_skill_with_verify(
        turn_env / "skills", "cc", _verify_script(False, "verifier said no")
    )
    set_verify_enabled("cc", True)

    def _boom(_prompt):
        raise RuntimeError("aux provider down")

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"cc": d},
        outcome_config={"enabled": True},
        _aux_eval=_boom,
    )
    assert outcome is not None
    assert outcome.task_succeeded is False
    assert outcome.failure_points == ["cc"]
    assert get_record("cc")["recent_outcomes"] == [False]


def test_file_mutation_failure_forces_fail(turn_env):
    """The existing per-turn file-mutation state forces a fail down-only."""
    from agent.turn_outcome import evaluate_turn_outcome

    d = _write_plain_skill(turn_env / "skills", "open")
    fm = {"src/foo.py": {"tool": "write_file", "error_preview": "permission denied"}}

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"open": d},
        outcome_config={"enabled": True},
        file_mutation_state=fm,
        _aux_eval=_eval(
            task_succeeded=True, confidence=0.95, failure_points=[], reason="all good"
        ),
    )
    assert outcome is not None
    assert outcome.task_succeeded is False
    assert "file-mutation" in outcome.reason


def test_disabled_config_is_inert(turn_env):
    """With the feature disabled the verifier never even runs."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record, set_verify_enabled

    d = _write_skill_with_verify(
        turn_env / "skills", "cc", _verify_script(False, "verifier said no")
    )
    set_verify_enabled("cc", True)

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"cc": d},
        outcome_config={"enabled": False},
        _aux_eval=_eval(task_succeeded=False, confidence=0.9, failure_points=["cc"], reason="x"),
    )
    assert outcome is None
    assert get_record("cc").get("recent_outcomes") == []


def test_high_confidence_eval_success_records_pass(turn_env):
    """A confirmed success (run=always) is recorded, so recovery is possible."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import bump_outcome, get_record, set_verify_enabled

    d = _write_skill_with_verify(
        turn_env / "skills", "golden", _verify_script(True, "ok")
    )
    set_verify_enabled("golden", True)
    for _ in range(3):
        bump_outcome("golden", False)

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"golden": d},
        outcome_config={"enabled": True, "run": "always"},
        _aux_eval=_eval(
            task_succeeded=True, confidence=0.9, failure_points=[], reason="held up"
        ),
    )
    assert outcome is not None
    assert outcome.task_succeeded is True
    assert get_record("golden")["recent_outcomes"][-1] is True


def test_interrupted_turn_is_not_a_work_failure(turn_env):
    """User-stopped turns produce no outcome and no writes."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record

    d = _write_plain_skill(turn_env / "skills", "open")

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"open": d},
        outcome_config={"enabled": True},
        interrupted=True,
        _aux_eval=_eval(task_succeeded=False, confidence=0.9, failure_points=["open"], reason="x"),
    )
    assert outcome is None
    assert get_record("open").get("recent_outcomes") == []


def test_infra_failure_reports_outcome_without_blaming_a_skill(turn_env):
    """An infra-failed turn yields an outcome but no sidecar attribution."""
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import get_record

    d = _write_plain_skill(turn_env / "skills", "open")

    outcome = evaluate_turn_outcome(
        skills_used_this_turn={"open": d},
        outcome_config={"enabled": True},
        failed=True,
        exit_reason="session_persistence_failed",
    )
    assert outcome is not None
    assert outcome.task_succeeded is False
    assert outcome.failure_points == []
    assert "session_persistence_failed" in outcome.reason
    assert get_record("open").get("recent_outcomes") == []


def test_verifier_runs_in_agent_cwd_not_process_cwd(turn_env):
    """Verifiers run against the agent's working directory — the same resolver
    the system prompt advertises — not the backend process's cwd. A gateway
    session pinned to its worktree must verify that tree, or a passing check
    certifies the wrong directory."""
    from agent.runtime_cwd import clear_session_cwd, set_session_cwd
    from agent.turn_outcome import evaluate_turn_outcome
    from tools.skill_usage import set_verify_enabled

    session_cwd = turn_env.parent / "session-cwd"
    session_cwd.mkdir()
    d = _write_skill_with_verify(
        turn_env / "skills", "cwdcheck", _verify_script(True, "ok")
    )
    set_verify_enabled("cwdcheck", True)
    # Overwrite the script to drop a sentinel into whatever cwd it runs in.
    (d / "scripts" / "verify.py").write_text(
        "from pathlib import Path\n"
        "Path('ran-here').write_text('ran')\n"
        + _verify_script(True, "ok"),
        encoding="utf-8",
    )
    set_session_cwd(str(session_cwd))
    try:
        evaluate_turn_outcome(
            skills_used_this_turn={"cwdcheck": d},
            outcome_config={"enabled": True},
            _aux_eval=_eval(task_succeeded=True, confidence=0.9, failure_points=[], reason="ok"),
        )
    finally:
        clear_session_cwd()
    assert (session_cwd / "ran-here").exists()
    assert not (Path.cwd() / "ran-here").exists()
