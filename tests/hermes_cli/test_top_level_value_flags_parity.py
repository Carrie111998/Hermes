"""Top-level value-flag sets must match the real parser (#93530).

``_TOP_LEVEL_VALUE_FLAGS`` (used by ``_first_positional_argv``) and the
``value_flags`` set inside ``_apply_profile_override`` are hand-maintained
copies of "which top-level options consume a value". AGENTS.md bans
hand-written flag sets for exactly this reason: they drift. ``--reasoning``
drifted — a value-taking top-level option absent from both sets, so every
``hermes --reasoning high <cmd> …`` invocation mis-classified ``high`` as
the first positional and forced eager plugin CLI discovery.

These tests derive the expected set from ``build_top_level_parser()`` so
any future drift fails CI instead of silently degrading startup.
"""

import pytest

from hermes_cli._parser import build_top_level_parser
from hermes_cli.main import _TOP_LEVEL_VALUE_FLAGS, _first_positional_argv


def _parser_value_flags() -> set:
    """Option strings of top-level actions that consume a value.

    An action consumes a value when its nargs is not zero-valued: Store /
    Append with nargs=None take exactly one; '?' takes an optional value
    and is deliberately treated as value-taking by the scanners (safe-side,
    same rationale as the ``-c/--continue`` comment in main.py). Boolean
    flags in this parser expose nargs == 0.
    """
    out = build_top_level_parser()
    parser = out[0] if isinstance(out, tuple) else out
    flags = set()
    for action in parser._actions:
        if not action.option_strings:
            continue
        nargs = getattr(action, "nargs", 0)
        if nargs == 0:
            continue
        flags.update(action.option_strings)
    return flags


def test_top_level_value_flags_cover_every_value_taking_parser_option():
    derived = _parser_value_flags()
    missing = derived - _TOP_LEVEL_VALUE_FLAGS - {"-p", "--profile"}
    assert not missing, (
        f"top-level value flags missing from _TOP_LEVEL_VALUE_FLAGS: {sorted(missing)} "
        "(they will be misread as positionals and force eager plugin discovery)"
    )


def test_reasoning_is_classified_as_value_flag():
    assert "--reasoning" in _TOP_LEVEL_VALUE_FLAGS


def test_first_positional_skips_reasoning_value(monkeypatch):
    monkeypatch.setattr("sys.argv", ["hermes", "--reasoning", "high", "chat", "hello"])
    assert _first_positional_argv() == "chat"


@pytest.mark.parametrize("argv", [
    ["hermes", "--reasoning", "ultra", "--provider", "openai", "-z", "prompt here"],
    ["hermes", "-m", "x", "--reasoning=low", "chat"],
])
def test_reasoning_forms_do_not_shadow_the_subcommand(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", argv)
    first = _first_positional_argv()
    assert first not in {"high", "ultra", "low"}, (
        f"a reasoning level leaked through as the positional: {first!r}"
    )
