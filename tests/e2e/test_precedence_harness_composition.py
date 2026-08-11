"""Self-test for the Phase 3 (Packets 2 & 3) precedence-experiment harness.

Safe to run in CI: no model calls, no real cron/session state touched.
Confirms the harness's isolation context actually redirects job/output
storage while still resolving real skill content, that assemble_prompt()
faithfully reproduces production's skill-injection markers for a known-good
case, and that AIAgent's session-transcript persistence (a write path with
no constructor opt-out, found live during the first Packet 3 run — see
precedence_harness.py's module docstring) is correctly redirected before any
future live call is authorized.
"""

import sys
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.e2e.precedence_harness import (  # noqa: E402
    CASES,
    PROFILE_ROOTS,
    _resolve_experiment_runtime,
    assemble_prompt,
    isolated_cron_state,
    pin_hermes_home_env,
)


def test_isolated_cron_state_redirects_job_storage(tmp_path):
    import cron.jobs as jobs_mod

    with isolated_cron_state("ops-repair") as tmp:
        assert jobs_mod.HERMES_DIR == tmp
        assert str(jobs_mod.JOBS_FILE).startswith(str(tmp))
        assert str(jobs_mod.OUTPUT_DIR).startswith(str(tmp))
        # Never points at a real profile's cron state.
        assert "profiles/ops-repair/cron" not in str(jobs_mod.JOBS_FILE)


def test_isolated_cron_state_resolves_real_skills_dir():
    import tools.skills_tool as skills_mod

    with isolated_cron_state("ops-repair"):
        assert skills_mod.SKILLS_DIR == PROFILE_ROOTS["ops-repair"] / "skills"
        assert skills_mod.SKILLS_DIR.is_dir()


def test_isolated_cron_state_restores_originals_after_exit():
    import cron.jobs as jobs_mod
    import tools.skills_tool as skills_mod

    before_skills_dir = skills_mod.SKILLS_DIR
    before_hermes_dir = jobs_mod.HERMES_DIR

    with isolated_cron_state("ops-repair"):
        pass

    assert skills_mod.SKILLS_DIR == before_skills_dir
    assert jobs_mod.HERMES_DIR == before_hermes_dir


def test_isolated_cron_state_neutralizes_bump_use_side_effect():
    """tools/skill_usage.py:291-292 writes a real counters file on every
    bump_use() call — the harness must no-op this so composition-proof runs
    never mutate live skill-usage tracking state."""
    import tools.skill_usage as usage_mod

    with isolated_cron_state("ops-repair"):
        # Must not raise and must not touch disk; if it were the real
        # implementation this would write to the real profile's usage file.
        usage_mod.bump_use("technical-message-style")


def test_assemble_prompt_real_job_shape_includes_all_requested_skills():
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(CASES["real_job_shape"])

    assert result["skills_skipped"] == []
    for skill_name in CASES["real_job_shape"].skills:
        marker = f'[IMPORTANT: The user has invoked the "{skill_name}" skill'
        assert marker in result["prompt"], f"missing injection marker for {skill_name!r}"

    # The cron_hint safety instruction (scheduler.py:790-800) must always be
    # present — it's what tells the model not to call send_message itself.
    assert "do NOT use send_message" in result["prompt"]


def test_assemble_prompt_flags_unresolvable_skill_instead_of_silently_dropping():
    from tests.e2e.precedence_harness import PrecedenceCase

    bogus = PrecedenceCase(
        key="bogus_skill_probe",
        description="Composition-integrity check: an unresolvable skill name must surface, not vanish silently.",
        prompt="test",
        skills=["this-skill-does-not-exist-phase3-probe"],
    )
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(bogus)

    assert "this-skill-does-not-exist-phase3-probe" in result["skills_skipped"]


@pytest.mark.parametrize("case_key", list(CASES))
def test_all_cases_assemble_without_error(case_key):
    """Smoke test (Packet 2's required self-test): every defined case in the
    P3.3 matrix assembles to a non-empty prompt against the real ops-repair
    skill tree, with no exception and no silently-skipped skill."""
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(CASES[case_key])

    assert result["prompt_chars"] > 0
    assert result["skills_skipped"] == [], (
        f"case {case_key!r} references a skill that doesn't resolve in the "
        f"ops-repair skill tree: {result['skills_skipped']}"
    )


def test_session_persistence_redirected_to_tmp_not_real_profile():
    """Packet 3 harness-safety regression test.

    Proves AIAgent's session-transcript write path (run_agent.py:1676-1679,
    self.logs_dir = get_hermes_home() / "sessions", no constructor opt-out)
    lands in the disposable tmp root when run_agent.get_hermes_home is
    patched the way run_live_case() patches it — and that the REAL profile's
    sessions/ directory gets zero new files. No model call is made:
    _save_session_log() is called directly with a synthetic message, exactly
    mirroring what run_conversation() would trigger, without the network
    round trip.
    """
    import run_agent
    from run_agent import AIAgent
    from toolsets import get_toolset_names

    real_sessions_dir = PROFILE_ROOTS["ops-repair"] / "sessions"
    before = set(real_sessions_dir.glob("session_*.json")) if real_sessions_dir.is_dir() else set()

    pin_hermes_home_env("ops-repair")
    rt = _resolve_experiment_runtime("ops-repair")

    with isolated_cron_state("ops-repair") as tmp:
        with mock_patch.object(run_agent, "get_hermes_home", lambda: tmp):
            agent = AIAgent(
                model=rt["model"],
                api_key=rt["api_key"],
                base_url=rt["base_url"],
                provider=rt["provider"],
                api_mode=rt["api_mode"],
                enabled_toolsets=[],
                disabled_toolsets=get_toolset_names(),
                skip_memory=True,
                skip_context_files=True,
                quiet_mode=True,
                platform="cron",
                load_soul_identity=False,
            )
            assert str(agent.logs_dir).startswith(str(tmp)), (
                f"session logs_dir not redirected: {agent.logs_dir}"
            )
            assert not str(agent.logs_dir).startswith(str(PROFILE_ROOTS["ops-repair"])), (
                "session logs_dir points at the REAL profile — redirect failed"
            )

            agent._save_session_log(messages=[
                {"role": "user", "content": "synthetic harness self-test message, no API call made"},
            ])

        assert agent.session_log_file.exists(), "expected the session file to land under tmp"
        assert str(agent.session_log_file).startswith(str(tmp))

    after = set(real_sessions_dir.glob("session_*.json")) if real_sessions_dir.is_dir() else set()
    assert after == before, (
        f"real profile sessions/ directory gained files: {after - before}"
    )
