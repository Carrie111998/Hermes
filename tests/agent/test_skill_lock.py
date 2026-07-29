"""Process-level contracts for the skill-library locking protocol."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest


def _attempt_lock(home: str, skill: str | None, exclusive_namespace: bool, queue) -> None:
    """Child-process helper; must remain module-level for spawn platforms."""
    os.environ["HERMES_HOME"] = home
    from agent.skill_lock import skill_write_lock, skills_namespace_lock

    try:
        with skills_namespace_lock(exclusive=exclusive_namespace, timeout=0.25):
            if skill is None:
                queue.put("acquired")
            else:
                with skill_write_lock(Path(home) / "skills" / skill, timeout=0.25):
                    queue.put("acquired")
    except TimeoutError:
        queue.put("timed-out")


@pytest.mark.skipif(os.name == "nt", reason="Windows uses the portable lock-file fallback")
def test_same_skill_is_exclusive_but_independent_skills_are_concurrent(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    skills = home / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "beta").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from agent.skill_lock import skill_write_lock, skills_namespace_lock

    context = multiprocessing.get_context("spawn")
    with skills_namespace_lock(exclusive=False):
        with skill_write_lock(skills / "alpha"):
            queue = context.Queue()
            blocked = context.Process(
                target=_attempt_lock, args=(str(home), "alpha", False, queue)
            )
            allowed = context.Process(
                target=_attempt_lock, args=(str(home), "beta", False, queue)
            )
            blocked.start()
            allowed.start()
            outcomes = {queue.get(timeout=5), queue.get(timeout=5)}
            blocked.join(timeout=5)
            allowed.join(timeout=5)

    assert outcomes == {"acquired", "timed-out"}
    assert blocked.exitcode == 0
    assert allowed.exitcode == 0


@pytest.mark.skipif(os.name == "nt", reason="Windows uses the portable lock-file fallback")
def test_structural_lock_waits_for_inflight_content_transaction(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "skills" / "alpha").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from agent.skill_lock import skills_namespace_lock

    context = multiprocessing.get_context("spawn")
    with skills_namespace_lock(exclusive=False):
        queue = context.Queue()
        writer = context.Process(
            target=_attempt_lock, args=(str(home), None, True, queue)
        )
        writer.start()
        assert queue.get(timeout=5) == "timed-out"
        writer.join(timeout=5)
    assert writer.exitcode == 0
