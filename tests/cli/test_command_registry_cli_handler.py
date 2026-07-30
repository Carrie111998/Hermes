"""Tests for registry-driven classic-CLI dispatch (``CommandDef.cli_handler``).

``cli_handler`` names a ``HermesCLI`` method that ``process_command`` resolves
with ``getattr``, replacing a per-command ``elif canonical == "x"`` branch. These
tests pin the contract that makes that safe: the named method must exist, must
accept the raw command string, and must not also still have a dispatch branch.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from cli import HermesCLI
from hermes_cli.commands import COMMAND_REGISTRY

MIGRATED = [cmd for cmd in COMMAND_REGISTRY if cmd.cli_handler]


def _dispatched_in_elif_chain() -> set[str]:
    """Command names the elif chain still branches on.

    Only positive tests against ``canonical`` count -- ``canonical == "x"`` and
    ``canonical in {...}``. ``not in`` is excluded because the pending-``/resume``
    guard uses it for bookkeeping, not dispatch, and a bare string mention is
    excluded because the fallback section uses names like ``"tools"`` as plain
    dict keys.
    """

    tree = ast.parse(textwrap.dedent(inspect.getsource(HermesCLI.process_command)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "canonical"):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                names.add(comparator.value)
            elif isinstance(op, ast.In) and isinstance(
                comparator, (ast.Set, ast.Tuple, ast.List)
            ):
                names.update(
                    elt.value
                    for elt in comparator.elts
                    if isinstance(elt, ast.Constant)
                )
    return names


def test_migration_is_not_empty():
    """Guard against the whole suite passing vacuously if the field is dropped."""

    assert len(MIGRATED) > 20


@pytest.mark.parametrize("cmd", MIGRATED, ids=lambda c: c.name)
class TestCliHandlerContract:
    def test_handler_exists_and_is_callable(self, cmd):
        handler = getattr(HermesCLI, cmd.cli_handler, None)
        assert callable(handler), (
            f"/{cmd.name} names cli_handler={cmd.cli_handler!r}, which is not a "
            "callable attribute of HermesCLI (check the name, or that the mixin "
            "providing it is still in the MRO)."
        )

    def test_handler_accepts_the_raw_command_string(self, cmd):
        """Dispatch calls ``handler(cmd_original)`` — one positional argument."""

        sig = inspect.signature(getattr(HermesCLI, cmd.cli_handler))
        params = [p for p in sig.parameters.values() if p.name != "self"]
        positional = [
            p
            for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        assert positional, (
            f"/{cmd.name} handler {cmd.cli_handler} takes no positional argument, "
            "but registry dispatch passes the raw command string."
        )
        required = [p for p in positional if p.default is inspect.Parameter.empty]
        assert len(required) <= 1, (
            f"/{cmd.name} handler {cmd.cli_handler} requires {len(required)} "
            "positional arguments; dispatch supplies exactly one."
        )

    def test_no_leftover_dispatch_branch(self, cmd):
        """A migrated command must not also be matched in the elif chain."""

        branched = _dispatched_in_elif_chain()
        stale = [n for n in (cmd.name, *cmd.aliases) if n in branched]
        assert not stale, (
            f"/{cmd.name} is dispatched via cli_handler but {stale} still appears "
            "in process_command — the leftover branch is unreachable dead code."
        )


class TestCliHandlerDispatch:
    def _cli(self):
        cli = object.__new__(HermesCLI)
        cli._pending_resume_sessions = None
        return cli

    def test_dispatch_invokes_the_registered_handler(self):
        cli = self._cli()
        seen = []
        cli._handle_tools_command = seen.append

        assert cli.process_command("/tools list") is True
        assert seen == ["/tools list"]

    def test_dispatch_preserves_argument_case(self):
        """Only the command word is lowercased; arguments reach the handler intact."""

        cli = self._cli()
        seen = []
        cli._handle_personality_command = seen.append

        cli.process_command("/PERSONALITY Zen Master")
        assert seen == ["/PERSONALITY Zen Master"]

    def test_dispatch_resolves_aliases_to_the_canonical_handler(self):
        cli = self._cli()
        seen = []
        cli._confirm_and_reload_mcp = seen.append

        cli.process_command("/reload_mcp")
        assert seen == ["/reload_mcp"]

    def test_unmigrated_command_does_not_take_the_registry_path(self):
        """/help has no cli_handler, so it must still reach the elif chain."""

        cli = self._cli()
        called = []
        cli.show_help = lambda: called.append(True)

        assert cli.process_command("/help") is True
        assert called == [True]
