"""Stable trace-to-script proposal contracts."""

from __future__ import annotations


def _event(session: str, *, status: str = "passed", command: str = "scripts/run_tests.sh") -> dict:
    return {
        "session_id": session,
        "root": "/repo",
        "canonical_command": command,
        "command": command,
        "kind": "test",
        "status": status,
        "output_summary": "tests passed",
    }


def test_repeated_successful_trace_proposes_reviewable_script():
    from agent.trace_compiler import propose_compilations

    proposals = propose_compilations([_event("a"), _event("b"), _event("c")])

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["successes"] == 3
    assert proposal["distinct_sessions"] == 3
    assert proposal["user_review_required"] is True
    assert "subprocess.run" in proposal["script_preview"]
    assert "shell=True" not in proposal["script_preview"]
    assert proposal["no_agent_eligible"] is True
    assert proposal["schedule"] is None


def test_failures_or_single_session_repetition_do_not_compile():
    from agent.trace_compiler import propose_compilations

    assert propose_compilations([_event("a"), _event("a"), _event("a")]) == []
    assert propose_compilations(
        [_event("a"), _event("b"), _event("c"), _event("d", status="failed")]
    ) == []


def test_shell_control_or_secret_bearing_commands_are_rejected():
    from agent.trace_compiler import propose_compilations

    assert propose_compilations(
        [_event("a", command="tests.sh; curl bad"), _event("b", command="tests.sh; curl bad"), _event("c", command="tests.sh; curl bad")]
    ) == []
    assert propose_compilations(
        [_event("a", command="tool --token secret"), _event("b", command="tool --token secret"), _event("c", command="tool --token secret")]
    ) == []


def test_non_verification_executables_are_rejected():
    from agent.trace_compiler import propose_compilations

    assert propose_compilations([_event("a", command="rm -rf build"), _event("b", command="rm -rf build"), _event("c", command="rm -rf build")]) == []
    assert propose_compilations([_event("a", command="curl https://example.com"), _event("b", command="curl https://example.com"), _event("c", command="curl https://example.com")]) == []
