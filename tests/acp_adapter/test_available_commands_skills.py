"""ACP available_commands includes skills/bundles; slash expands skill bodies.

Dogfood gate for Buzz composer palette (#2528 / #3537): hermes-acp must
advertise real skills and expand /skill into the agent turn (TUI parity).
"""

from __future__ import annotations

from acp_adapter.server import HermesACPAgent


def test_available_commands_static_prefix():
    cmds = HermesACPAgent._available_commands()
    names = [c.name for c in cmds]
    assert names[:9] == [
        "help",
        "model",
        "tools",
        "context",
        "reset",
        "compress",
        "steer",
        "queue",
        "version",
    ]


def test_available_commands_includes_skills_and_unique():
    cmds = HermesACPAgent._available_commands()
    names = [c.name for c in cmds]
    assert len(names) == len({n.lower() for n in names})
    # Live install has mesh skill library
    assert "lead-architect" in names
    assert any(n.startswith("mesh") for n in names)


def test_expand_unknown_returns_none():
    assert HermesACPAgent._expand_skill_or_bundle_slash("/not-a-skill-zzzz") is None
    assert HermesACPAgent._expand_skill_or_bundle_slash("nope") is None
    assert HermesACPAgent._expand_skill_or_bundle_slash("") is None


def test_expand_skill_includes_body_and_instruction():
    msg = HermesACPAgent._expand_skill_or_bundle_slash(
        "/lead-architect smoke-instruction-token"
    )
    assert isinstance(msg, str)
    assert "IMPORTANT" in msg
    assert "smoke-instruction-token" in msg
    assert len(msg) > 500


def test_expand_bundle_returns_string_message():
    msg = HermesACPAgent._expand_skill_or_bundle_slash("/mesh orient-token")
    assert isinstance(msg, str)
    assert "IMPORTANT" in msg or "bundle" in msg.lower()
    assert "orient-token" in msg
    assert len(msg) > 500


def test_expand_skill_underscore_alias():
    msg = HermesACPAgent._expand_skill_or_bundle_slash("/lead_architect x")
    assert isinstance(msg, str)
    assert "IMPORTANT" in msg
