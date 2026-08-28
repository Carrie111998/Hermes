"""Hermetic tests for the public ``hermes qualification`` command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_qualification(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real CLI while inheriting the fixture-provided Hermes home."""
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=Path(__file__).parents[2],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _fixture_home_snapshot() -> tuple[str, ...]:
    home = Path(os.environ["HERMES_HOME"])
    if not home.exists():
        return ()
    return tuple(
        sorted(
            str(path.relative_to(home))
            for path in home.rglob("*")
            if path.is_file() or path.is_symlink()
        )
    )


@pytest.mark.parametrize("scenario", ["clean", "existing"])
def test_qualification_json_is_canonical_and_private_state_free(scenario: str) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification("qualification", "--scenario", scenario, "--json")
    after = _fixture_home_snapshot()

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    expected = {
        "schema_version": 1,
        "command": "qualification",
        "scenario": scenario,
        "network_accessed": False,
        "private_state_accessed": False,
    }
    assert (
        result.stdout
        == json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
    )
    assert json.loads(result.stdout) == expected
    assert after == before


@pytest.mark.parametrize(
    "args",
    [
        ("qualification", "--scenario", "clean"),
        ("qualification", "--json"),
        ("qualification", "--scenario", "unknown", "--json"),
    ],
)
def test_qualification_requires_exact_flags_and_choices(args: tuple[str, ...]) -> None:
    result = _run_qualification(*args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("qualification", "--sc", "clean", "--json"),
        ("qualification", "--scenario", "clean", "--j"),
        ("qualification", "--help"),
    ],
)
def test_qualification_rejects_abbreviated_and_help_flags(
    args: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(*args)
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


def test_qualification_rejects_leading_global_option_before_startup() -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(
        "--profile",
        "x",
        "qualification",
        "--scenario",
        "clean",
        "--json",
    )
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


def test_qualification_rejects_profile_after_missing_model_value() -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(
        "--model",
        "--profile",
        "x",
        "qualification",
        "--scenario",
        "clean",
        "--json",
    )
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize(
    "args",
    [
        ("--profile", "--model", "x", "qualification", "--scenario", "clean", "--json"),
        ("-p", "--provider", "x", "qualification", "--help"),
        ("--profile", "--mo", "x", "qualification", "--json"),
    ],
)
def test_qualification_rejects_invalid_profile_identifier_prefix(
    args: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(*args)
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize(
    "args",
    [
        ("--profile", "INVALID!", "qualification", "--scenario", "clean", "--json"),
        ("-p", "INVALID!", "qualification", "--help"),
    ],
)
def test_qualification_rejects_invalid_profile_value_prefix(
    args: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(*args)
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize(
    "args",
    [
        ("--profile", "-1", "qualification", "--scenario", "clean", "--json"),
        ("-p", "-.5", "qualification", "--help"),
        ("--profile", "-", "qualification", "--json"),
    ],
)
def test_qualification_rejects_negative_profile_value_prefix(
    args: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(*args)
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize(
    "args",
    [
        ("--mo", "--profile", "-1", "qualification", "--scenario", "clean", "--json"),
        ("--pro", "--profile", "INVALID!", "qualification", "--help"),
    ],
)
def test_qualification_rejects_abbreviated_value_option_before_bad_profile(
    args: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(*args)
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize(
    "prefix",
    [
        ("--model", "qualification", "qualification", "--help"),
        ("--pro", "qualification", "qualification", "--reasoning"),
    ],
)
def test_qualification_rejects_repeated_qualification_after_global_value(
    prefix: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(*prefix)
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize(
    "prefix",
    [
        ("--mo", "x"),
        ("-mx",),
        ("--ver",),
        ("-wV",),
    ],
)
def test_qualification_rejects_parser_accepted_global_abbreviations(
    prefix: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(
        *prefix,
        "qualification",
        "--scenario",
        "clean",
        "--json",
    )
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize("option", ["--model", "--pr", "--pro"])
def test_global_option_value_named_qualification_is_not_misclassified(
    option: str,
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            option,
            "qualification",
            "--version",
        ))
        is False
    )


@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "qualification"),
        ("-p", "qualification"),
        ("--profile=qualification",),
    ],
)
def test_profile_value_named_qualification_is_not_misclassified(
    profile_args: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((*profile_args, "--version"))
        is False
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("--model", "x", "profile", "show", "qualification"),
        ("--model", "x", "config", "set", "qualification", "value"),
        (
            "--model",
            "x",
            "mcp",
            "add",
            "demo",
            "--command",
            "echo",
            "--args",
            "qualification",
        ),
        (
            "--model",
            "x",
            "mcp",
            "add",
            "demo",
            "--command",
            "echo",
            "--args",
            "--mo",
            "--profile",
            "INVALID!",
            "qualification",
        ),
    ],
)
def test_non_qualification_command_data_is_not_misclassified(
    argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is False


@pytest.mark.parametrize(
    "command_argv",
    [
        ("config", "set", "qualification", "value"),
        ("profile", "show", "qualification"),
        ("sessions", "list", "qualification"),
        ("gateway", "run", "qualification"),
        ("auth", "status", "qualification"),
        ("doctor", "qualification"),
        ("status", "qualification"),
        ("verify", "qualification"),
        ("skills", "list", "qualification"),
        ("plugins", "list", "qualification"),
        ("project", "list", "qualification"),
        ("worktree", "list", "qualification"),
        ("completion", "bash", "qualification"),
        ("logs", "list", "qualification"),
        ("memory", "list", "qualification"),
    ],
)
def test_repeated_profile_does_not_reclassify_nested_command_data(
    command_argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            "--profile",
            "a",
            "--profile",
            "b",
            *command_argv,
        ))
        is False
    )


@pytest.mark.parametrize(
    "prefix",
    [
        ("--profile", "a", "--profile", "b"),
        ("--model", "x", "--profile", "a", "--profile", "b"),
        ("--profile", "a", "--model", "x", "--profile", "b"),
        ("--profile", "INVALID!", "--profile", "b"),
        ("-p", "-1", "--profile=b"),
    ],
)
@pytest.mark.parametrize(
    "command_argv",
    [
        ("config", "set", "qualification", "value"),
        ("profile", "show", "qualification"),
        ("sessions", "list", "qualification"),
    ],
)
def test_repeated_profile_command_data_is_false_across_selector_placements(
    prefix: tuple[str, ...], command_argv: tuple[str, ...]
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification(prefix + command_argv) is False
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            (
                "--model",
                "mcp",
                "--provider",
                "add",
                "--args",
                "qualification",
                "--scenario",
                "clean",
                "--json",
            ),
            True,
        ),
        (
            (
                "--model",
                "x",
                "mcp",
                "add",
                "demo",
                "--command",
                "echo",
                "--args",
                "--mo",
                "--profile",
                "INVALID!",
                "qualification",
            ),
            False,
        ),
        (
            (
                "--model",
                "x",
                "mcp",
                "add",
                "demo",
                "--command",
                "echo",
                "--arg",
                "--mo",
                "--profile",
                "INVALID!",
                "qualification",
            ),
            False,
        ),
        (
            (
                "--model",
                "x",
                "mcp",
                "add",
                "demo",
                "--command",
                "echo",
                "--ar",
                "--mo",
                "--profile",
                "INVALID!",
                "qualification",
            ),
            False,
        ),
    ],
)
def test_mcp_passthrough_boundary_requires_a_real_top_level_command(
    argv: tuple[str, ...], expected: bool
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is expected


def test_qualification_probe_does_not_rescan_repeated_global_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.qualification_cmd as qualification_cmd

    calls = 0
    original = qualification_cmd._inside_mcp_add_args

    def counting_probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(qualification_cmd, "_inside_mcp_add_args", counting_probe)

    argv = ("--version",) * 8 + ("qualification", "--scenario", "clean", "--json")
    assert qualification_cmd.has_leading_global_option_before_qualification(argv)
    assert calls <= len(argv)


def test_qualification_probe_does_not_rescan_after_non_mcp_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.qualification_cmd as qualification_cmd

    parse_calls = 0
    original = qualification_cmd._parse_qualification_probe

    def counting_probe(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(qualification_cmd, "_parse_qualification_probe", counting_probe)

    argv = ("--model", "x", "chat") + ("--version",) * 12 + ("qualification",)
    assert (
        qualification_cmd.has_leading_global_option_before_qualification(argv) is False
    )
    assert parse_calls <= 3


@pytest.mark.parametrize(
    "argv",
    [
        (
            "--mo",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--pro",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--rea",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--reason",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        ("--mo", "-p", "x", "value", "qualification", "--scenario", "clean", "--json"),
        (
            "--mo",
            "--profile=dev",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
    ],
)
def test_qualification_rejects_profile_selector_as_pending_global_value(
    argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is True


@pytest.mark.parametrize(
    "args",
    [
        (
            "--mo",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--pro",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--rea",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--reason",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        ("--mo", "-p", "x", "value", "qualification", "--scenario", "clean", "--json"),
        (
            "--mo",
            "--profile=dev",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
    ],
)
def test_qualification_rejects_pending_profile_selector_before_bootstrap(
    args: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(*args)
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize(
    "argv",
    [
        ("--mo", "qualification", "--version"),
        ("--pro", "qualification", "--version"),
        ("--rea", "qualification", "--version"),
        ("--reason", "qualification", "--version"),
        ("--mo", "--profile", "qualification", "--version"),
        ("--pro", "--profile", "qualification", "--version"),
        ("--rea", "--profile", "qualification", "--version"),
        ("--reason", "--profile", "qualification", "--version"),
        ("--mo", "--profile", "x", "qualification", "--version"),
        ("--mo", "--profile", "dev", "qualification", "--version"),
        ("--mo", "--profile=dev", "qualification", "--version"),
        ("--pro", "--profile=qualification", "qualification", "--version"),
        ("--reason", "--profile", "dev", "qualification", "--version"),
        ("--mo", "-p", "qualification", "--version"),
        ("--mo", "--profile=qualification", "--version"),
        ("--profile", "dev", "--version"),
        ("--profile=dev", "--version"),
    ],
)
def test_qualification_preserves_valid_global_value_discriminators(
    argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is False


def test_qualification_rejects_literal_command_after_pending_profile_value() -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            "--mo",
            "--profile",
            "x",
            "value",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            "--profile",
            "dev",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ))
        is True
    )


@pytest.mark.parametrize(
    "argv",
    [
        (
            "--mo",
            "--profile",
            "a",
            "--profile",
            "b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--mo",
            "--profile=a",
            "--profile=b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--mo",
            "-p",
            "a",
            "-p",
            "b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
    ],
)
def test_qualification_rejects_repeated_profile_selectors_after_global_value(
    argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        (
            "--profile",
            "a",
            "--profile",
            "b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--profile=a",
            "--profile=b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "-p",
            "a",
            "-p",
            "b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
    ],
)
def test_qualification_rejects_repeated_profiles_after_first_override(
    argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        (
            "--profile",
            "a",
            "--profile",
            "b",
            "--model",
            "qualification",
            "--version",
        ),
        (
            "--profile=a",
            "--profile=b",
            "--provider",
            "qualification",
            "--version",
        ),
        (
            "-p",
            "a",
            "-p",
            "b",
            "--reasoning",
            "qualification",
            "--version",
        ),
        (
            "--profile",
            "a",
            "--profile",
            "qualification",
            "--version",
        ),
        (
            "--profile",
            "INVALID!",
            "--profile",
            "qualification",
            "--version",
        ),
        (
            "-p",
            "-1",
            "-p",
            "qualification",
            "--version",
        ),
        (
            "--profile=a",
            "--profile=qualification",
            "--version",
        ),
    ],
)
def test_qualification_preserves_repeated_profile_data_values(
    argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is False


@pytest.mark.parametrize(
    "first_profile",
    [
        ("--profile", "a"),
        ("-p", "a"),
        ("--profile", "INVALID!"),
        ("--profile", "-1"),
    ],
)
@pytest.mark.parametrize(
    "residual_profile",
    [
        ("--profile", "qualification"),
        ("-p", "qualification"),
        ("--profile=qualification",),
    ],
)
@pytest.mark.parametrize(
    "value_flag",
    [
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "-s",
        "--skills",
        "--usage-file",
        "--in",
        "-z",
        "--oneshot",
    ],
)
def test_qualification_preserves_residual_profile_data_after_value_flag(
    first_profile: tuple[str, ...],
    residual_profile: tuple[str, ...],
    value_flag: str,
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification(
            first_profile + (value_flag,) + residual_profile + ("--version",)
        )
        is False
    )


def test_qualification_rejects_command_after_residual_profile_value_flag() -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            "--profile",
            "a",
            "--model",
            "--profile",
            "b",
            "qualification",
            "--help",
        ))
        is True
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            (
                "--profile",
                "a",
                "--profile",
                "qualification",
                "--",
                "--version",
            ),
            False,
        ),
        (
            (
                "--profile",
                "a",
                "-p",
                "qualification",
                "--",
                "--version",
            ),
            False,
        ),
        (
            (
                "--profile",
                "a",
                "--profile=qualification",
                "--",
                "--version",
            ),
            False,
        ),
        (
            (
                "--profile",
                "a",
                "--profile",
                "b",
                "--",
                "qualification",
                "--help",
            ),
            True,
        ),
        (
            (
                "--profile",
                "a",
                "-p",
                "b",
                "--",
                "qualification",
                "--help",
            ),
            True,
        ),
        (
            (
                "--profile",
                "a",
                "--profile=b",
                "--",
                "qualification",
                "--help",
            ),
            True,
        ),
        (
            (
                "--profile",
                "a",
                "--profile",
                "b",
                "--",
                "config",
                "qualification",
            ),
            False,
        ),
        (
            (
                "--profile",
                "a",
                "-p",
                "b",
                "--",
                "config",
                "qualification",
            ),
            False,
        ),
        (
            (
                "--profile",
                "a",
                "--profile=b",
                "--",
                "config",
                "qualification",
            ),
            False,
        ),
        (("--model", "--", "qualification", "--help"), True),
        (("--model", "--", "config", "qualification"), False),
        (("--mo", "--", "qualification", "--help"), True),
        (("--mo", "--", "config", "qualification"), False),
        (("--bogus", "--", "qualification", "--help"), True),
        (("--bogus", "--", "config", "qualification"), False),
        (("--profile", "--", "qualification", "--help"), True),
        (("--profile", "--", "config", "qualification"), False),
        (
            ("--model", "--profile", "--", "qualification", "--help"),
            True,
        ),
        (("--model", "--profile", "--", "config", "qualification"), False),
        (("--model", "x", "--", "qualification"), True),
        (("--model", "x", "config", "--", "qualification"), False),
        (
            ("--profile", "a", "--profile", "b", "config", "--", "qualification"),
            False,
        ),
        (
            ("--profile", "a", "-p", "b", "config", "--", "qualification"),
            False,
        ),
        (
            ("--profile", "a", "--profile=b", "config", "--", "qualification"),
            False,
        ),
        (("--model", "x", "profile", "show", "--", "qualification"), False),
        (
            (
                "--model",
                "x",
                "mcp",
                "add",
                "demo",
                "--command",
                "echo",
                "--args",
                "--",
                "qualification",
            ),
            False,
        ),
    ],
)
def test_qualification_probe_honors_double_dash_command_boundary(
    argv: tuple[str, ...], expected: bool
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    try:
        actual: object = has_leading_global_option_before_qualification(argv)
    except SystemExit as exc:
        actual = f"SystemExit({exc.code})"
    assert actual is expected


@pytest.mark.parametrize(
    "option",
    ["--p", "--r", "--s", "--t", "--c", "--i", "--ig", "--ignore"],
)
@pytest.mark.parametrize(
    ("argv_suffix", "expected"),
    [
        (("config", "--", "qualification"), False),
        (("--", "qualification"), True),
        (("--profile", "a", "config", "--", "qualification"), False),
        (
            ("--profile", "a", "--profile", "b", "config", "--", "qualification"),
            False,
        ),
    ],
)
def test_qualification_probe_normalizes_ambiguous_global_prefixes(
    option: str,
    argv_suffix: tuple[str, ...],
    expected: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    argv = (option, *argv_suffix)
    try:
        actual: object = has_leading_global_option_before_qualification(argv)
    except SystemExit as exc:
        actual = f"SystemExit({exc.code})"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert actual is expected


@pytest.mark.parametrize(
    "option",
    [
        "--c",
        "--i",
        "--ig",
        "--ign",
        "--igno",
        "--ignor",
        "--ignore",
        "--ignore-",
        "--p",
        "--r",
        "--re",
        "--s",
        "--t",
    ],
)
@pytest.mark.parametrize(
    ("argv_suffix", "expected"),
    [
        (("--model", "qualification", "--version"), False),
        (("config", "set", "qualification", "value"), False),
        (("plugins", "list", "qualification"), False),
        (("qualification", "--scenario", "clean", "--json"), True),
    ],
)
def test_qualification_parse_error_recovery_tracks_first_value_or_command(
    option: str,
    argv_suffix: tuple[str, ...],
    expected: bool,
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((option, *argv_suffix))
        is expected
    )


@pytest.mark.parametrize(
    "option",
    [
        "--c",
        "--i",
        "--ig",
        "--ign",
        "--igno",
        "--ignor",
        "--ignore",
        "--ignore-",
        "--p",
        "--r",
        "--re",
        "--s",
        "--t",
    ],
)
@pytest.mark.parametrize("value_flag", ["--model", "--provider", "--reasoning"])
def test_qualification_parse_error_recovery_preserves_global_value_data(
    option: str,
    value_flag: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            option,
            value_flag,
            "qualification",
            "--version",
        ))
        is False
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "option",
    ["--p", "--r", "--s", "--t", "--c", "--i", "--ig", "--ignore"],
)
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "INVALID!"),
        ("-p", "-1"),
        ("--profile", "-"),
        ("--profile=INVALID!",),
    ],
)
def test_qualification_probe_normalizes_ambiguous_prefix_before_bad_profile(
    option: str,
    profile_args: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    argv = (option, *profile_args, "qualification", "--scenario", "clean", "--json")
    try:
        actual: object = has_leading_global_option_before_qualification(argv)
    except SystemExit as exc:
        actual = f"SystemExit({exc.code})"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert actual is True


def test_qualification_preserves_repeated_profile_command_boundary() -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            "--profile",
            "a",
            "--profile",
            "b",
            "qualification",
            "--help",
        ))
        is True
    )


@pytest.mark.parametrize(
    "argv",
    [
        (
            "--profile",
            "a",
            "--profile",
            "qualification",
            "qualification",
            "--help",
        ),
        (
            "--profile=a",
            "--profile=qualification",
            "qualification",
            "--help",
        ),
        (
            "-p",
            "a",
            "-p",
            "qualification",
            "qualification",
            "--help",
        ),
    ],
)
def test_qualification_skips_profile_value_before_command_boundary(
    argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        (
            "--profile",
            "INVALID!",
            "--mo",
            "--profile",
            "b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "-p",
            "-1",
            "--mo",
            "--profile=b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--profile=INVALID!",
            "--mo",
            "--profile=b",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
    ],
)
def test_qualification_preserves_first_malformed_profile_selector_state(
    argv: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is True


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ("--mo", "--profile", "INVALID!", "config", "qualification"),
            False,
        ),
        (
            (
                "--mo",
                "--profile",
                "INVALID!",
                "--model",
                "qualification",
                "--version",
            ),
            False,
        ),
        (("--mo", "--profile", "INVALID!", "qualification"), True),
    ],
)
def test_qualification_malformed_profile_after_value_prefix_tracks_boundary(
    argv: tuple[str, ...],
    expected: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is expected
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    ("first_value_flag", "second_value_flag"),
    [
        (first, second)
        for first in (
            "--model",
            "--provider",
            "--reasoning",
            "--toolsets",
            "--resume",
            "--skills",
            "--usage-file",
        )
        for second in (
            "--model",
            "--provider",
            "--reasoning",
            "--toolsets",
            "--resume",
            "--skills",
            "--usage-file",
        )
    ],
)
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (("qualification",), True),
        (("config", "qualification"), False),
        (("plugins", "list", "qualification"), False),
        (("--model", "qualification", "--version"), False),
        (("--provider", "qualification", "--version"), False),
        (("--reasoning", "qualification", "--version"), False),
        (("config", "set", "qualification", "value"), False),
    ],
)
def test_qualification_malformed_profile_recovery_preserves_value_prefix_chain(
    first_value_flag: str,
    second_value_flag: str,
    tail: tuple[str, ...],
    expected: bool,
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    argv = (
        first_value_flag,
        second_value_flag,
        "--profile",
        "INVALID!",
        *tail,
    )
    assert has_leading_global_option_before_qualification(argv) is expected


@pytest.mark.parametrize(
    "first_value_flag",
    [
        "--model",
        "--provider",
        "--reasoning",
        "--toolsets",
        "--resume",
        "--skills",
        "--usage-file",
        "--oneshot",
        "--in",
        "-m",
        "-r",
        "-s",
        "-t",
        "-z",
        "--continue",
    ],
)
@pytest.mark.parametrize("second_value_flag", ["--model", "--mo", "--pr", "--co"])
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (("qualification", "--profile", "INVALID!", "qualification"), True),
        (
            ("qualification", "--profile", "INVALID!", "config", "qualification"),
            False,
        ),
        (
            (
                "qualification",
                "--profile",
                "INVALID!",
                "--model",
                "qualification",
                "--version",
            ),
            False,
        ),
        (
            (
                "qualification",
                "--profile",
                "INVALID!",
                "config",
                "set",
                "qualification",
                "value",
            ),
            False,
        ),
    ],
)
def test_qualification_malformed_profile_recovery_tracks_option_like_value_chain(
    first_value_flag: str,
    second_value_flag: str,
    tail: tuple[str, ...],
    expected: bool,
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            first_value_flag,
            second_value_flag,
            *tail,
        ))
        is expected
    )


@pytest.mark.parametrize(
    "first_value_flag",
    [
        "--model",
        "--provider",
        "--reasoning",
        "--toolsets",
        "--resume",
        "--skills",
        "--usage-file",
        "--oneshot",
        "--in",
        "-m",
        "-r",
        "-s",
        "-t",
        "-z",
    ],
)
@pytest.mark.parametrize(
    "second_value_flag",
    [
        "--model",
        "--provider",
        "--reasoning",
        "--toolsets",
        "--resume",
        "--skills",
        "--usage-file",
        "--oneshot",
        "--in",
        "-m",
        "-r",
        "-s",
        "-t",
        "-z",
        "--continue",
        "--mo",
        "--pr",
        "--co",
    ],
)
@pytest.mark.parametrize(
    "tail",
    [
        ("--profile", "valid", "qualification"),
        (
            "--profile",
            "valid",
            "--model",
            "qualification",
            "--version",
        ),
    ],
)
def test_qualification_valid_profile_value_chain_keeps_q_data_or_command(
    first_value_flag: str,
    second_value_flag: str,
    tail: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            first_value_flag,
            second_value_flag,
            *tail,
        ))
        is False
    )


@pytest.mark.parametrize(
    ("value_prefix", "expected_immediate_q"),
    [
        (("--model",), True),
        (("--model", "--model"), False),
        (("--model", "--model", "--model"), True),
        (("--model", "--model", "--model", "--model"), False),
        (("--model", "--model", "--model", "--model", "--model"), True),
        (("--model", "--provider", "--reasoning"), True),
        (("--model", "--provider", "--pr"), False),
        (("--model", "--mo", "--provider"), True),
        (("--mo", "--model", "--model"), False),
    ],
)
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "valid"),
        ("--profile=valid",),
        ("-p", "valid"),
    ],
)
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (("qualification",), None),
        (("config", "qualification"), "opposite"),
        (("--model", "qualification", "--version"), False),
    ],
)
def test_qualification_profile_probe_preserves_arbitrary_preparse_chain_state(
    value_prefix: tuple[str, ...],
    expected_immediate_q: bool,
    profile_args: tuple[str, ...],
    tail: tuple[str, ...],
    expected: bool | str | None,
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    expected_result = (
        expected_immediate_q
        if expected is None
        else not expected_immediate_q
        if expected == "opposite"
        else expected
    )
    assert (
        has_leading_global_option_before_qualification(
            value_prefix + profile_args + tail
        )
        is expected_result
    )


@pytest.mark.parametrize(
    "value_prefix",
    [
        ("--model",),
        ("--model", "--model", "--model"),
        ("--model", "--provider", "--reasoning"),
    ],
)
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "INVALID!"),
        ("-p", "-1"),
        ("--profile=INVALID!",),
    ],
)
def test_qualification_profile_probe_keeps_malformed_chain_command_boundary(
    value_prefix: tuple[str, ...], profile_args: tuple[str, ...]
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification(
            value_prefix + profile_args + ("qualification",)
        )
        is True
    )
    assert (
        has_leading_global_option_before_qualification(
            value_prefix + profile_args + ("config", "qualification")
        )
        is False
    )


@pytest.mark.parametrize("chain_length", list(range(1, 9)))
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (("qualification",), True),
        (("config", "qualification"), False),
        (("--model", "qualification", "--version"), False),
    ],
)
def test_qualification_inline_malformed_profile_survives_any_chain_length(
    chain_length: int, tail: tuple[str, ...], expected: bool
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    argv = ("--model",) * chain_length + ("--profile=INVALID!",) + tail
    assert has_leading_global_option_before_qualification(argv) is expected


@pytest.mark.parametrize(
    "value_prefix",
    [
        ("--model", "--provider"),
        ("--model", "--mo"),
        ("--mo", "--model"),
        ("--continue", "--model"),
        ("--model", "--continue", "--provider"),
    ],
)
def test_qualification_inline_malformed_profile_mixed_value_chain(
    value_prefix: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification(
            value_prefix + ("--profile=INVALID!", "qualification")
        )
        is True
    )
    assert (
        has_leading_global_option_before_qualification(
            value_prefix + ("--profile=INVALID!", "config", "qualification")
        )
        is False
    )


@pytest.mark.parametrize(
    ("value_option", "profile_args", "command_value"),
    [
        ("--mo", ("--profile", "valid"), "config"),
        ("--pr", ("--profile=valid",), "plugins"),
        ("--continue", ("-p", "valid"), "config"),
        ("--co", ("-p", "valid"), "config"),
        ("--rea", ("--profile=valid",), "config"),
    ],
)
def test_qualification_profile_strip_preserves_pending_value_command_boundary(
    value_option: str,
    profile_args: tuple[str, ...],
    command_value: str,
) -> None:
    """A global value may be spelled like a command after profile stripping."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            command_value,
            "qualification",
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            command_value,
            "set",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "args",
    [
        (
            "--mo",
            "--profile",
            "valid",
            "config",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
        (
            "--pr",
            "--profile=valid",
            "plugins",
            "qualification",
            "--scenario",
            "existing",
            "--json",
        ),
        (
            "--co",
            "-p",
            "valid",
            "config",
            "qualification",
            "--scenario",
            "clean",
            "--json",
        ),
    ],
)
def test_qualification_rejects_post_profile_value_prefix_before_bootstrap(
    args: tuple[str, ...],
) -> None:
    before = _fixture_home_snapshot()
    result = _run_qualification(*args)
    after = _fixture_home_snapshot()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr
    assert after == before


@pytest.mark.parametrize("value_option", ["--model", "--provider", "--reasoning"])
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "valid"),
        ("--profile=valid",),
        ("-p", "valid"),
    ],
)
def test_qualification_profile_strip_keeps_exact_consumed_profile_data_boundary(
    value_option: str,
    profile_args: tuple[str, ...],
) -> None:
    """Exact required flags consume an option-looking profile selector."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            "config",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize("value_option", ["--mo", "--pr", "--co"])
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "qualification"),
        ("--profile=qualification",),
        ("-p", "qualification"),
    ],
)
def test_qualification_profile_value_named_qualification_stays_data_after_option(
    value_option: str,
    profile_args: tuple[str, ...],
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            "--version",
        ))
        is False
    )


@pytest.mark.parametrize(
    ("value_option", "profile_args", "command_value"),
    [
        ("--model", ("--profile", "valid"), "config"),
        ("--mo", ("--profile=valid",), "config"),
        ("--provider", ("-p", "valid"), "plugins"),
        ("--pr", ("--profile", "valid"), "plugins"),
        ("--continue", ("--profile=valid",), "config"),
        ("--co", ("-p", "valid"), "config"),
    ],
)
def test_qualification_profile_strip_preserves_option_like_command_value_matrix(
    value_option: str,
    profile_args: tuple[str, ...],
    command_value: str,
) -> None:
    """A stripped profile must not make nested command data look top-level."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            command_value,
            "set",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    ("value_chain", "command_value"),
    [
        (("--model", "--provider"), "config"),
        (("--model", "--model"), "config"),
        (("--model", "--pr"), "plugins"),
        (("--model", "--mo"), "config"),
    ],
)
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "valid"),
        ("--profile=valid",),
        ("-p", "valid"),
    ],
)
def test_qualification_profile_strip_recovers_full_option_like_value_chain(
    value_chain: tuple[str, ...],
    command_value: str,
    profile_args: tuple[str, ...],
) -> None:
    """A complete exact/abbreviated prefix can leave a later command value."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            *value_chain,
            *profile_args,
            command_value,
            "qualification",
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            *value_chain,
            *profile_args,
            "qualification",
        ))
        is False
    )
    assert (
        has_leading_global_option_before_qualification((
            *value_chain,
            *profile_args,
            command_value,
            "set",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "value_chain",
    [
        ("--mo", "--provider"),
        ("--pr", "--model"),
        ("--continue", "--model"),
        ("--co", "--provider"),
    ],
)
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "valid"),
        ("--profile=valid",),
        ("-p", "valid"),
    ],
)
def test_qualification_profile_strip_keeps_unstripped_option_like_chain_data(
    value_chain: tuple[str, ...],
    profile_args: tuple[str, ...],
) -> None:
    """A later exact value flag can own the profile pair as data."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            *value_chain,
            *profile_args,
            "config",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "value_option",
    [
        "--model",
        "--provider",
        "--reasoning",
        "--toolsets",
        "--resume",
        "--skills",
        "--usage-file",
        "--oneshot",
        "--in",
        "-m",
        "-r",
        "-s",
        "-t",
        "-z",
    ],
)
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "valid"),
        ("--profile=valid",),
        ("-p", "valid"),
    ],
)
def test_qualification_exact_value_profile_keeps_mcp_action_as_data(
    value_option: str,
    profile_args: tuple[str, ...],
) -> None:
    """Exact pre-parser values leave MCP action tokens as nested data."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            "mcp",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "value_option",
    [
        "--model",
        "--provider",
        "--reasoning",
        "--toolsets",
        "--resume",
        "--skills",
        "--usage-file",
        "--oneshot",
        "--in",
        "-m",
        "-r",
        "-s",
        "-t",
        "-z",
    ],
)
@pytest.mark.parametrize(
    "profile_form",
    [
        "--profile",
        "--profile=",
        "-p",
    ],
)
@pytest.mark.parametrize(
    "command_value",
    [
        "mcp",
        "config",
        "plugins",
        "profile",
        "sessions",
        "gateway",
        "auth",
        "doctor",
        "status",
        "verify",
    ],
)
def test_qualification_exact_value_profile_keeps_builtin_command_data(
    value_option: str,
    profile_form: str,
    command_value: str,
) -> None:
    """An exact required value can leave any built-in command as data."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    profile_args = (
        (f"--profile={command_value}",)
        if profile_form == "--profile="
        else (profile_form, command_value)
    )
    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "value_option",
    ["--mo", "--pr", "--co", "--continue"],
)
@pytest.mark.parametrize(
    "profile_form",
    [
        "--profile",
        "--profile=",
        "-p",
    ],
)
@pytest.mark.parametrize(
    "command_value",
    [
        "mcp",
        "config",
        "plugins",
        "profile",
        "sessions",
        "gateway",
        "auth",
        "doctor",
        "status",
        "verify",
    ],
)
def test_qualification_abbreviated_value_profile_keeps_builtin_command_data(
    value_option: str,
    profile_form: str,
    command_value: str,
) -> None:
    """Accepted global abbreviations keep profile command values as data."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    profile_args = (
        (f"--profile={command_value}",)
        if profile_form == "--profile="
        else (profile_form, command_value)
    )
    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "value_option",
    ["--mo", "--pr", "--co", "--continue"],
)
@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "valid"),
        ("--profile=valid",),
        ("-p", "valid"),
    ],
)
@pytest.mark.parametrize(
    ("mcp_tail", "expected"),
    [
        (("mcp", "qualification"), True),
        (("mcp", "list", "qualification"), False),
        (("mcp", "add", "qualification"), False),
        (
            (
                "mcp",
                "add",
                "demo",
                "--command",
                "echo",
                "--args",
                "qualification",
            ),
            False,
        ),
    ],
)
def test_qualification_profile_strip_preserves_mcp_command_boundary_matrix(
    value_option: str,
    profile_args: tuple[str, ...],
    mcp_tail: tuple[str, ...],
    expected: bool,
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            *mcp_tail,
        ))
        is expected
    )


@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile", "INVALID!"),
        ("-p", "INVALID!"),
        ("--profile", "-1"),
        ("-p", "-.5"),
        ("--profile", "-"),
    ],
)
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (("qualification",), True),
        (("config", "qualification"), False),
        (("--model", "qualification", "--version"), False),
    ],
)
def test_qualification_malformed_profile_value_chain_supports_selector_forms(
    profile_args: tuple[str, ...],
    tail: tuple[str, ...],
    expected: bool,
) -> None:
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            "--model",
            "--provider",
            *profile_args,
            *tail,
        ))
        is expected
    )


@pytest.mark.parametrize(
    "value_option",
    [
        "--model",
        "--provider",
        "--reasoning",
        "--toolsets",
        "--resume",
        "--skills",
        "--usage-file",
        "--oneshot",
        "--in",
        "-m",
        "-r",
        "-s",
        "-t",
        "-z",
    ],
)
@pytest.mark.parametrize(
    "profile_form",
    [
        "--profile",
        "--profile=",
        "-p",
    ],
)
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ((), False),
        (("qualification",), True),
        (("qualification", "--help"), True),
        (("config", "qualification"), False),
    ],
)
def test_qualification_exact_consumed_profile_qualification_value_boundary(
    value_option: str,
    profile_form: str,
    tail: tuple[str, ...],
    expected: bool,
) -> None:
    """A consumed profile value named qualification is data, then later q is a command."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    profile_args = (
        (f"--profile=qualification",)
        if profile_form == "--profile="
        else (profile_form, "qualification")
    )
    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            *tail,
        ))
        is expected
    )


@pytest.mark.parametrize("value_option", ["--mo", "--pr", "--co", "--continue"])
@pytest.mark.parametrize(
    "profile_form",
    [
        "--profile",
        "--profile=",
        "-p",
    ],
)
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ((), False),
        (("qualification",), False),
        (("qualification", "qualification"), True),
        (("config", "qualification"), True),
    ],
)
def test_qualification_abbreviated_profile_qualification_value_stays_data(
    value_option: str,
    profile_form: str,
    tail: tuple[str, ...],
    expected: bool,
) -> None:
    """Accepted global options continue to own a q value after profile stripping."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    profile_args = (
        (f"--profile=qualification",)
        if profile_form == "--profile="
        else (profile_form, "qualification")
    )
    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            *tail,
        ))
        is expected
    )


@pytest.mark.parametrize(
    "value_option",
    [
        "--model",
        "--provider",
        "--reasoning",
        "--toolsets",
        "--resume",
        "--skills",
        "--usage-file",
        "--oneshot",
        "--in",
        "-m",
        "-r",
        "-s",
        "-t",
        "-z",
    ],
)
@pytest.mark.parametrize(
    "profile_form",
    [
        "--profile",
        "--profile=",
        "-p",
    ],
)
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (("mcp", "qualification"), False),
        (("--profile", "valid", "qualification"), True),
    ],
)
def test_qualification_consumed_profile_q_pair_preserves_following_boundary(
    value_option: str,
    profile_form: str,
    tail: tuple[str, ...],
    expected: bool,
) -> None:
    """A consumed q-valued profile pair remains data across later boundaries."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    profile_args = (
        ("--profile=qualification",)
        if profile_form == "--profile="
        else (profile_form, "qualification")
    )
    assert (
        has_leading_global_option_before_qualification((
            value_option,
            *profile_args,
            *tail,
        ))
        is expected
    )


@pytest.mark.parametrize(
    "cluster",
    ["-Vm", "-Vc", "-Vz", "-Vs", "-Vt", "-Vr", "-wm"],
)
def test_qualification_parser_accepted_short_cluster_value_boundary(
    cluster: str,
) -> None:
    """An accepted short cluster consumes one q before the command boundary."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((cluster, "qualification"))
        is False
    )
    assert (
        has_leading_global_option_before_qualification((
            cluster,
            "qualification",
            "qualification",
        ))
        is True
    )


@pytest.mark.parametrize("cluster", ["-Vx", "-wx", "-wq"])
def test_qualification_rejected_short_cluster_does_not_trigger(
    cluster: str,
) -> None:
    """Unknown cluster members must not be treated as accepted value flags."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            cluster,
            "qualification",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("--profile", "-Vx", "qualification"),
        ("-p", "-Vx", "qualification"),
        ("--model", "--profile", "-Vx", "qualification"),
        ("-Vm", "--profile", "-Vx", "qualification"),
    ],
)
def test_qualification_malformed_profile_recovery_keeps_rejected_cluster_data(
    argv: tuple[str, ...],
) -> None:
    """A rejected profile value is not a rejected qualification command."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert has_leading_global_option_before_qualification(argv) is True


@pytest.mark.parametrize("first_selector", ["--profile", "-p"])
@pytest.mark.parametrize("second_selector", ["--profile", "-p"])
@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (("qualification",), True),
        (("config", "qualification"), False),
    ],
)
def test_qualification_recovery_preserves_malformed_repeated_profile_boundary(
    first_selector: str,
    second_selector: str,
    tail: tuple[str, ...],
    expected: bool,
) -> None:
    """A malformed selector value is not reinterpreted as a later override."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            first_selector,
            second_selector,
            *tail,
        ))
        is expected
    )


@pytest.mark.parametrize(
    "value_flag",
    [
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "-s",
        "--skills",
        "--usage-file",
        "--in",
        "-z",
        "--oneshot",
    ],
)
def test_qualification_exact_value_consumes_first_profile_selector(
    value_flag: str,
) -> None:
    """An exact required value owns the first option-looking profile token."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_flag,
            "--profile",
            "--profile",
            "qualification",
        ))
        is False
    )
    assert (
        has_leading_global_option_before_qualification((
            value_flag,
            "--profile",
            "--profile",
            "config",
            "qualification",
        ))
        is True
    )


@pytest.mark.parametrize(
    "prefix",
    [
        ("--mo",),
        ("--continue",),
        ("-Vm",),
        ("--model", "x"),
    ],
)
def test_qualification_non_exact_value_prefix_keeps_profile_pair_boundary(
    prefix: tuple[str, ...],
) -> None:
    """Only exact required flags consume the first profile selector token."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification(
            prefix + ("--profile", "--profile", "qualification")
        )
        is True
    )


@pytest.mark.parametrize(
    "value_flag",
    [
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "-s",
        "--skills",
        "--usage-file",
        "--in",
        "-z",
        "--oneshot",
    ],
)
def test_qualification_exact_value_preserves_malformed_second_profile_boundary(
    value_flag: str,
) -> None:
    """A malformed residual profile value still leaves q as the command."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_flag,
            "--profile",
            "--profile",
            "INVALID!",
            "qualification",
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            value_flag,
            "--profile",
            "--profile",
            "INVALID!",
            "config",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize("selector", ["--profile", "-p"])
def test_qualification_repeated_profile_invalid_value_is_nested_data(
    selector: str,
) -> None:
    """Without an exact value prefix, the first selector owns invalid data."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            selector,
            selector,
            "INVALID!",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "value_flag",
    [
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "-s",
        "--skills",
        "--usage-file",
        "--in",
        "-z",
        "--oneshot",
    ],
)
@pytest.mark.parametrize(
    "third_profile_value",
    ["--profile", "-p", "--profile=bad"],
)
def test_qualification_exact_value_preserves_third_malformed_profile_boundary(
    value_flag: str,
    third_profile_value: str,
) -> None:
    """A third option-looking selector value cannot swallow the command."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_flag,
            "--profile",
            "--profile",
            third_profile_value,
            "qualification",
        ))
        is True
    )


@pytest.mark.parametrize(
    "value_flag",
    [
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "-s",
        "--skills",
        "--usage-file",
        "--in",
        "-z",
        "--oneshot",
    ],
)
def test_qualification_exact_value_preserves_third_profile_data_controls(
    value_flag: str,
) -> None:
    """Malformed profile data and nested commands remain distinct."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            value_flag,
            "--profile",
            "--profile",
            "INVALID!",
            "qualification",
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            value_flag,
            "--profile",
            "--profile",
            "--profile",
            "config",
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "value_flag",
    [
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "-s",
        "--skills",
        "--usage-file",
        "--in",
        "-z",
        "--oneshot",
    ],
)
@pytest.mark.parametrize(
    "profile_chain",
    [
        ("--profile", "--profile", "--profile", "--profile"),
        ("--profile", "-p", "--profile=bad", "-p"),
    ],
)
def test_qualification_preserves_arbitrary_malformed_profile_chain_boundary(
    value_flag: str,
    profile_chain: tuple[str, ...],
) -> None:
    """A malformed selector must not reopen profile parsing downstream."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    prefix = (value_flag, *profile_chain)
    assert (
        has_leading_global_option_before_qualification(prefix + ("qualification",))
        is True
    )
    assert (
        has_leading_global_option_before_qualification(
            prefix + ("config", "qualification")
        )
        is False
    )


@pytest.mark.parametrize(
    "value_flag",
    [
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "-s",
        "--skills",
        "--usage-file",
        "--in",
        "-z",
        "--oneshot",
    ],
)
@pytest.mark.parametrize("later_selector", ["--profile", "-p", "--profile=also-bad"])
def test_qualification_exact_value_inline_profile_stops_later_selector_recovery(
    value_flag: str,
    later_selector: str,
) -> None:
    """An inline selector after a consumed profile token closes recovery."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    prefix = (value_flag, "--profile", "--profile=bad", later_selector)
    assert (
        has_leading_global_option_before_qualification(prefix + ("qualification",))
        is True
    )
    assert (
        has_leading_global_option_before_qualification(
            prefix + ("config", "qualification")
        )
        is False
    )


@pytest.mark.parametrize(
    "value_flag",
    [
        "-m",
        "--model",
        "--provider",
        "--reasoning",
        "-t",
        "--toolsets",
        "-r",
        "--resume",
        "-s",
        "--skills",
        "--usage-file",
        "--in",
        "-z",
        "--oneshot",
    ],
)
@pytest.mark.parametrize("selector", ["--profile", "-p"])
@pytest.mark.parametrize("invalid_value", ["INVALID!", "-1", "-"])
@pytest.mark.parametrize("later_selector", ["--profile", "-p", "--profile=bad"])
def test_qualification_malformed_profile_selector_stops_all_later_recovery(
    value_flag: str,
    selector: str,
    invalid_value: str,
    later_selector: str,
) -> None:
    """An invalid selector attempt closes profile parsing for all later forms."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    prefix = (value_flag, "--profile", selector, invalid_value, later_selector)
    assert (
        has_leading_global_option_before_qualification(prefix + ("qualification",))
        is True
    )
    assert (
        has_leading_global_option_before_qualification(
            prefix + ("config", "qualification")
        )
        is False
    )


@pytest.mark.parametrize("selector", ["--profile", "-p"])
@pytest.mark.parametrize("invalid_value", ["INVALID!", "-1", "-"])
@pytest.mark.parametrize("later_selector", ["--profile", "-p", "--profile=bad"])
def test_qualification_bare_malformed_profile_selector_stops_all_later_recovery(
    selector: str,
    invalid_value: str,
    later_selector: str,
) -> None:
    """The first bare malformed selector owns the recovery boundary."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    prefix = (selector, invalid_value, later_selector)
    assert (
        has_leading_global_option_before_qualification(prefix + ("qualification",))
        is True
    )
    assert (
        has_leading_global_option_before_qualification(
            prefix + ("config", "qualification")
        )
        is False
    )


@pytest.mark.parametrize(
    "profile_args",
    [
        ("--profile=INVALID!",),
        ("--profile=-1",),
        ("--profile=-",),
    ],
)
@pytest.mark.parametrize("later_selector", ["--profile", "-p", "--profile=bad"])
def test_qualification_inline_malformed_profile_selector_stops_later_recovery(
    profile_args: tuple[str, ...],
    later_selector: str,
) -> None:
    """Inline malformed selectors keep later profile tokens opaque."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    prefix = (*profile_args, later_selector)
    assert (
        has_leading_global_option_before_qualification(prefix + ("qualification",))
        is True
    )
    assert (
        has_leading_global_option_before_qualification(
            prefix + ("config", "qualification")
        )
        is False
    )


@pytest.mark.parametrize("selector", ["--profile", "-p"])
@pytest.mark.parametrize("invalid_value", ["INVALID!", "-1", "-"])
@pytest.mark.parametrize("version_flag", ["--version", "-V"])
def test_qualification_residual_profile_version_carveout_stops_at_first_command(
    selector: str,
    invalid_value: str,
    version_flag: str,
) -> None:
    """A later profile/version pair is data only before the first command."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            selector,
            invalid_value,
            "qualification",
            selector,
            "qualification",
            version_flag,
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            selector,
            invalid_value,
            "config",
            "qualification",
            selector,
            "qualification",
            version_flag,
        ))
        is False
    )


@pytest.mark.parametrize("selector", ["--profile", "-p"])
@pytest.mark.parametrize("invalid_value", ["INVALID!", "-1", "-"])
@pytest.mark.parametrize("version_flag", ["--version", "-V"])
@pytest.mark.parametrize("marker", ["--"])
def test_qualification_residual_profile_version_carveout_respects_separator(
    selector: str,
    invalid_value: str,
    version_flag: str,
    marker: str,
) -> None:
    """A first command after ``--`` remains authoritative over later data."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            selector,
            invalid_value,
            marker,
            "qualification",
            selector,
            "qualification",
            version_flag,
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            selector,
            invalid_value,
            marker,
            "config",
            "qualification",
            selector,
            "qualification",
            version_flag,
        ))
        is False
    )


@pytest.mark.parametrize("selector", ["--profile", "-p"])
@pytest.mark.parametrize("invalid_value", ["INVALID!", "-1", "-"])
@pytest.mark.parametrize("version_flag", ["--version", "-V"])
def test_qualification_residual_profile_version_pair_before_first_command_remains_data(
    selector: str,
    invalid_value: str,
    version_flag: str,
) -> None:
    """The pre-existing residual profile/version interpretation is retained."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            selector,
            invalid_value,
            selector,
            "qualification",
            version_flag,
        ))
        is False
    )


@pytest.mark.parametrize("malformed_profile", ["--profile=INVALID!", "--profile=-1"])
@pytest.mark.parametrize("later_selector", ["--profile", "-p", "--profile=valid"])
@pytest.mark.parametrize("version_flag", ["--version", "-V"])
def test_qualification_residual_profile_version_carveout_handles_inline_profiles(
    malformed_profile: str,
    later_selector: str,
    version_flag: str,
) -> None:
    """Inline malformed selectors obey the same first-command boundary."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            malformed_profile,
            "qualification",
            later_selector,
            "qualification",
            version_flag,
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            malformed_profile,
            "config",
            "qualification",
            later_selector,
            "qualification",
            version_flag,
        ))
        is False
    )


@pytest.mark.parametrize(
    "malformed_profile",
    [
        ("--profile", "INVALID!"),
        ("-p", "-1"),
        ("--profile=INVALID!",),
    ],
)
@pytest.mark.parametrize("first_later_selector", ["--profile", "-p", "--profile=valid"])
@pytest.mark.parametrize(
    "second_later_selector", ["--profile", "-p", "--profile=valid"]
)
@pytest.mark.parametrize("version_flag", ["--version", "--ver", "-V"])
def test_qualification_residual_profile_version_triple_keeps_earlier_command(
    malformed_profile: tuple[str, ...],
    first_later_selector: str,
    second_later_selector: str,
    version_flag: str,
) -> None:
    """A later triple cannot retroactively hide an earlier qualification."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            *malformed_profile,
            first_later_selector,
            "qualification",
            second_later_selector,
            "qualification",
            version_flag,
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            *malformed_profile,
            "config",
            "qualification",
            first_later_selector,
            "qualification",
            second_later_selector,
            "qualification",
            version_flag,
        ))
        is False
    )


@pytest.mark.parametrize(
    "malformed_profile",
    [
        ("--profile", "INVALID!"),
        ("-p", "-1"),
        ("--profile=INVALID!",),
    ],
)
@pytest.mark.parametrize("later_selector", ["--profile", "-p", "--profile=valid"])
@pytest.mark.parametrize("version_flag", ["--version", "--ver", "-V"])
def test_qualification_residual_profile_version_immediate_pair_remains_data(
    malformed_profile: tuple[str, ...],
    later_selector: str,
    version_flag: str,
) -> None:
    """The immediate residual profile/version pair remains data."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            *malformed_profile,
            later_selector,
            "qualification",
            version_flag,
        ))
        is False
    )


@pytest.mark.parametrize(
    "malformed_profile",
    [
        ("--profile", "INVALID!"),
        ("-p", "-1"),
        ("--profile=INVALID!",),
    ],
)
@pytest.mark.parametrize("residual_selector", ["--profile", "-p", "--profile=valid"])
@pytest.mark.parametrize("version_flag", ["--version", "--ver", "-V"])
@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ((), False),
        (("qualification",), True),
        (("config", "qualification"), False),
        (("--", "qualification"), True),
        (("--", "config", "qualification"), False),
    ],
)
def test_qualification_residual_profile_version_consumes_only_immediate_triple(
    malformed_profile: tuple[str, ...],
    residual_selector: str,
    version_flag: str,
    suffix: tuple[str, ...],
    expected: bool,
) -> None:
    """A suffix is classified after, rather than hidden by, residual data."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            *malformed_profile,
            residual_selector,
            "qualification",
            version_flag,
            *suffix,
        ))
        is expected
    )


@pytest.mark.parametrize(
    "malformed_profile",
    [
        ("--profile", "INVALID!"),
        ("-p", "-1"),
        ("--profile=INVALID!",),
    ],
)
@pytest.mark.parametrize("residual_selector", ["--profile", "-p", "--profile=valid"])
@pytest.mark.parametrize("suffix_selector", ["--profile", "-p", "--profile=valid"])
@pytest.mark.parametrize("version_flag", ["--version", "--ver", "-V"])
def test_qualification_residual_profile_version_suffix_selector_is_a_command(
    malformed_profile: tuple[str, ...],
    residual_selector: str,
    suffix_selector: str,
    version_flag: str,
) -> None:
    """A separate suffix selector exposes its value as the next command."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            *malformed_profile,
            residual_selector,
            "qualification",
            version_flag,
            suffix_selector,
            "qualification",
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            *malformed_profile,
            residual_selector,
            "qualification",
            version_flag,
            "config",
            "qualification",
            suffix_selector,
            "qualification",
        ))
        is False
    )


@pytest.mark.parametrize(
    "malformed_profile",
    [
        ("--profile", "INVALID!"),
        ("-p", "-1"),
        ("--profile=INVALID!",),
    ],
)
@pytest.mark.parametrize("residual_selector", ["--profile", "-p", "--profile=valid"])
@pytest.mark.parametrize("suffix_selector", ["--profile", "-p", "--profile=valid"])
@pytest.mark.parametrize("pending_option", ["--continue", "--co"])
@pytest.mark.parametrize("version_flag", ["--version", "--ver", "-V"])
def test_qualification_residual_suffix_profile_ignores_optional_global_value(
    malformed_profile: tuple[str, ...],
    residual_selector: str,
    suffix_selector: str,
    pending_option: str,
    version_flag: str,
) -> None:
    """A pending optional option cannot consume a residual profile value."""
    from hermes_cli.qualification_cmd import (
        has_leading_global_option_before_qualification,
    )

    assert (
        has_leading_global_option_before_qualification((
            *malformed_profile,
            residual_selector,
            "qualification",
            version_flag,
            pending_option,
            suffix_selector,
            "qualification",
        ))
        is True
    )
    assert (
        has_leading_global_option_before_qualification((
            *malformed_profile,
            "config",
            "qualification",
            version_flag,
            pending_option,
            suffix_selector,
            "qualification",
        ))
        is False
    )
