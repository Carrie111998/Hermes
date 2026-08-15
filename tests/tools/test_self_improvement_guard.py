"""Regression tests: governed PGF runs cannot mutate SKILL.md / references / memory.

The operating contract says a governed run must NOT autonomously mutate the
skill library or memory store unless an explicit auditable SELF_IMPROVEMENT
authorization is present. A bare process env var alone is NOT sufficient for a
governed profile (Claude review governance finding). These tests prove the
structural guards enforced by `tools/self_improvement_guard` at the three choke
points (fork spawn, skill write gate, memory write entrypoint).
"""

from __future__ import annotations

import os
from unittest import mock


def _cfg(gov: bool, enabled: bool) -> dict:
    return {"governance": {"governed": gov}, "self_improvement": {"enabled": enabled}}


def _patch_profile_config(cfg: dict):
    return mock.patch.object(
        __import__("tools.self_improvement_guard", fromlist=["_profile_config"]),
        "_profile_config",
        return_value=cfg,
    )


def _patch(cfg: dict):
    return _patch_profile_config(cfg)


def test_governed_no_auth_is_not_authorized():
    os.environ.pop("HERMES_SELF_IMPROVEMENT", None)
    with _patch(_cfg(True, False)):
        from tools.self_improvement_guard import self_improvement_authorized

        assert self_improvement_authorized() is False


def test_governed_with_res_config_auth_is_authorized():
    os.environ.pop("HERMES_SELF_IMPROVEMENT", None)
    with _patch(_cfg(True, True)):
        from tools.self_improvement_guard import self_improvement_authorized

        assert self_improvement_authorized() is True


def test_governed_env_optin_does_NOT_authorize_with_disabled_config():
    """Governance fix: for a governed run a bare env var must NOT silently
    authorise SKILL.md/memory mutation. The operator must set the versioned,
    auditable `self_improvement.enabled: true` config flag."""
    os.environ["HERMES_SELF_IMPROVEMENT"] = "1"
    with _patch(_cfg(True, False)):
        from tools.self_improvement_guard import self_improvement_authorized

        assert self_improvement_authorized() is False


def test_non_governed_profile_keeps_autonomous_behaviour():
    # No governance marker -> normal Hermes self-improvement continues.
    with _patch(_cfg(False, False)):
        from tools.self_improvement_guard import self_improvement_authorized, spawn_allowed

        assert self_improvement_authorized() is True
        assert spawn_allowed() is True


def test_governed_spawn_is_refused():
    """The autonomous review fork must not even start for a governed run."""
    os.environ.pop("HERMES_SELF_IMPROVEMENT", None)
    with _patch(_cfg(True, False)):
        from tools.self_improvement_guard import spawn_allowed

        assert spawn_allowed() is False


def test_skill_write_guard_refuses_review_origin_when_governed():
    """A background-review-origin skill write must be refused (fail closed)."""
    from tools import skill_manager_tool as smt

    os.environ.pop("HERMES_SELF_IMPROVEMENT", None)
    with _patch(_cfg(True, False)):
        with mock.patch.object(
            __import__("tools.skill_provenance", fromlist=["is_background_review"]),
            "is_background_review",
            return_value=True,
        ):
            result = smt._apply_skill_write_gate("patch", "some-skill")
            assert result is not None
            import json

            parsed = json.loads(result)
            assert parsed["success"] is False
            assert "SELF_IMPROVEMENT" in parsed["error"] or "Self-improvement" in parsed["error"]


def test_skill_write_guard_allows_foreground_governed_write():
    """A foreground operator write is NOT affected by the autonomy guard."""
    from tools.self_improvement_guard import guard_skill_write

    assert guard_skill_write("patch", "some-skill") is None