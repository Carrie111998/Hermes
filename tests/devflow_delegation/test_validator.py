import sys

from devflow_delegation.allowlist import TargetConfig
from devflow_delegation.validator import validate_worktree


def _target(**overrides):
    values = dict(
        repo="fixture",
        checkout_path="/fixture",
        # Headroom, not a timing assertion: every test using this default
        # asserts on exit codes and output, never on duration. 5s stopped
        # being headroom once validation moved to ``run_text_capture`` —
        # capturing into temp files means CREATE_NO_WINDOW, and on Windows
        # that allocates a fresh hidden console (a conhost spawn) per child,
        # measured at ~1.2s on top of an interpreter start that is already
        # ~1.8s on a loaded box. Trivial `python -c` commands were landing at
        # 3.8s idle and tipping past 5s under batch load, so these tests
        # flaked as "timed_out". The one test that genuinely exercises the
        # timeout path overrides this with its own 1s budget below.
        command_timeout_seconds=30,
        test_commands=((sys.executable, "-c", "print('tests passed')"),),
        required_checks=("test",),
    )
    values.update(overrides)
    return TargetConfig(**values)


def test_validation_passes_with_command_output(tmp_path):
    result = validate_worktree(tmp_path, _target())

    assert result.passed
    assert len(result.commands) == 1
    assert result.commands[0].status == "passed"
    assert "tests passed" in result.commands[0].stdout


def test_zero_exit_without_observable_output_is_not_a_green(tmp_path):
    result = validate_worktree(tmp_path, _target(test_commands=((sys.executable, "-c", "pass"),)))

    assert not result.passed
    assert result.commands[0].returncode == 0
    assert result.commands[0].status == "failed"


def test_nonzero_exit_fails_validation(tmp_path):
    result = validate_worktree(
        tmp_path,
        _target(test_commands=((sys.executable, "-c", "import sys; print('failure'); sys.exit(7)"),)),
    )

    assert not result.passed
    assert result.commands[0].returncode == 7
    assert result.commands[0].status == "failed"


def test_commands_stop_at_first_failure(tmp_path):
    result = validate_worktree(
        tmp_path,
        _target(test_commands=(
            (sys.executable, "-c", "print('first failed'); import sys; sys.exit(1)"),
            (sys.executable, "-c", "print('must not run')"),
        )),
    )

    assert not result.passed
    assert len(result.commands) == 1


def test_no_configured_commands_is_invalid_not_green(tmp_path):
    result = validate_worktree(tmp_path, _target(test_commands=()))

    assert not result.passed
    assert result.commands[0].status == "invalid"
    assert result.commands[0].evidence == "validation:no-commands"


def test_missing_worktree_is_invalid_not_green(tmp_path):
    missing = tmp_path / "missing"
    result = validate_worktree(missing, _target())

    assert not result.passed
    assert result.commands[0].status == "invalid"
    assert result.commands[0].evidence == "validation:invalid-worktree"


def test_timeout_is_reported(tmp_path):
    result = validate_worktree(
        tmp_path,
        _target(
            command_timeout_seconds=1,
            test_commands=((sys.executable, "-c", "import time; time.sleep(2)"),),
        ),
    )

    assert not result.passed
    assert result.commands[0].status == "timed_out"
    assert result.commands[0].evidence == "validation:test:timeout"


def test_validation_runs_in_the_requested_worktree(tmp_path):
    marker = tmp_path / "marker.txt"
    marker.write_text("expected", encoding="utf-8")
    command = (
        sys.executable, "-c",
        "from pathlib import Path; assert Path('marker.txt').read_text() == 'expected'; print('cwd verified')",
    )

    result = validate_worktree(tmp_path, _target(test_commands=(command,)))

    assert result.passed
    assert "cwd verified" in result.commands[0].stdout


def test_string_command_is_rejected_without_invoking_a_shell(tmp_path):
    result = validate_worktree(tmp_path, _target(test_commands=("python -c \"print('unsafe')\"",)))

    assert not result.passed
    assert result.commands[0].status == "invalid"
