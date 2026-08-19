"""Regression: ``hermes send`` must not pay for the whole CLI's import graph.

Why this exists
---------------
``hermes_cli/send_cmd.py`` carries a deliberate comment -- "Import lazily so
``hermes send --help`` stays fast and does not pull in the full tool registry /
gateway config stack" -- and it is correct *in its own file*: importing
``hermes_cli.send_cmd`` alone costs only 12 modules over the bare-interpreter
floor.

It was defeated UPSTREAM of itself by ``hermes_cli/main.py``, which at module
scope imported all ~38 ``build_*_parser`` subcommand modules, then loaded
``hermes_cli.config`` / ``env_loader`` and called ``setup_logging()`` (which
starts a QueueListener *thread*) -- all before argparse had any idea which
subcommand was being run. Measured on PRECISION 2026-08-19:

    python -X importtime -m hermes_cli.main send --help  ->  444 modules

This matters far beyond raw speed. Windows Task Scheduler's default
``<Priority>7</Priority>`` is BelowNormal and is inherited by children, so this
import work *starves*: 388.2s cold / 458.0s warm at Priority 7 versus
8.8s / 4.0s at Priority 5, both exiting 0. Raising task priority is a saturated
lever on this box, so cutting the import graph is the remaining one. Full
analysis: GBrain ``sessions/2026-08-19-hermes-send-task-context-starvation``.

The fix is ``hermes_cli/_fast_send.py``: a pre-argparse fast path, in the same
spirit as the ``_try_termux_fast_cli_launch()`` hook that already exists at the
top of ``main()`` and the ``_BUILTIN_SUBCOMMANDS`` plugin-discovery
short-circuit that already exists below it.

This test runs SUBPROCESSES on purpose: ``sys.modules`` is process-global, so a
sibling test that legitimately imports the gateway stack would mask the
regression entirely.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The measured baseline before the fast path existed. Kept as a literal so the
# assertion message can quote the real number rather than a moving target.
BASELINE_MODULES = 444

# Generous ceiling: the observed post-fix cost is ~120 modules, and the bare
# interpreter floor on this box is ~70 (13 sys.path entries, 8 sys.meta_path
# finders including four _EditableFinder instances). Anything at or above this
# means an upstream module-scope import crept back in.
MAX_SEND_HELP_MODULES = 200

# Module roots that ``hermes send`` provably cannot need to print its own
# --help. Each was observed in the 444-module baseline. Matching is on the
# dotted prefix, so "agent.lsp" also catches "agent.lsp.client".
FORBIDDEN_PREFIXES = (
    "hermes_logging",             # starts a QueueListener THREAD at import time
    "agent.lsp",                  # language-server client stack
    "agent.secret_sources",       # bitwarden / 1password providers
    "hermes_cli.kanban",
    "hermes_cli.journey",
    "hermes_cli.secrets_cli",
    "hermes_cli.pets",
    "hermes_cli.projects_db",
    "hermes_cli.fallback_cmd",
    "hermes_cli.moa_cmd",
    "hermes_cli.overrides_cmd",
    "hermes_cli.model_setup_flows",
    "hermes_cli.subcommands.gateway",
    "hermes_cli.subcommands.cron",
    "hermes_cli.subcommands.tools",
    "hermes_cli.subcommands.doctor",
)

# Subcommands whose parsers must still be reachable -- proves the fast path is
# scoped to ``send`` and did not amputate the real parser.
_FULL_STACK_COMMANDS = ("gateway", "cron", "tools")


def _child_env() -> dict:
    env = dict(os.environ)
    # Keep the child off the lazy-install path so a slow/absent optional dep can
    # never turn this into a pip run. See tools/lazy_deps.py.
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    # ``-m hermes_cli.main`` resolves ``hermes_cli`` from sys.path; without
    # pinning REPO_ROOT first the child can import from whatever checkout the
    # editable install points at -- i.e. silently test a different tree than the
    # one under test.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + os.pathsep + existing if existing else str(REPO_ROOT)
    )
    return env


def _run_importtime(argv: list[str]) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run ``python -X importtime -m hermes_cli.main ARGV`` and return imports.

    Returns the completed process plus the list of module names, in import
    order, parsed out of the ``-X importtime`` report on stderr.
    """
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "hermes_cli.main", *argv],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=900,
    )
    modules = []
    for line in proc.stderr.splitlines():
        if not line.startswith("import time:"):
            continue
        if "cumulative" in line:  # the header row
            continue
        # ``import time:   self [us] | cumulative | imported package``
        parts = line.split("|")
        if len(parts) < 3:
            continue
        modules.append(parts[-1].strip())
    return proc, modules


def _offenders(modules: list[str]) -> list[str]:
    hits = set()
    for mod in modules:
        for prefix in FORBIDDEN_PREFIXES:
            if mod == prefix or mod.startswith(prefix + "."):
                hits.add(mod)
    return sorted(hits)


@pytest.mark.timeout(900)
def test_send_help_does_not_import_the_whole_cli():
    """``send --help`` must stay near the interpreter floor, not near 444."""
    proc, modules = _run_importtime(["send", "--help"])

    assert proc.returncode == 0, (
        f"`send --help` exited {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )
    # Behaviour first: this must still actually be send's help, not a stub.
    assert "send" in proc.stdout, f"no send help printed:\n{proc.stdout}"
    assert "--to" in proc.stdout, (
        "send --help no longer documents --to; the fast path must build the "
        f"real send parser via register_send_subparser.\nstdout:\n{proc.stdout}"
    )

    # Guard against measuring the wrong checkout: an editable install can point
    # ``hermes_cli`` at a different tree, which would make this pass against
    # code that is not the code under test.
    assert modules, f"no importtime report parsed.\nstderr:\n{proc.stderr[-3000:]}"

    offenders = _offenders(modules)
    assert not offenders, (
        f"`hermes send --help` imported {len(offenders)} module(s) it cannot "
        f"need: {offenders}\n"
        "These are pulled in UPSTREAM of send_cmd.py's own lazy imports, by "
        "hermes_cli/main.py module scope. The _fast_send pre-argparse path in "
        "hermes_cli/_fast_send.py must fire before them."
    )

    assert len(modules) < MAX_SEND_HELP_MODULES, (
        f"`hermes send --help` imported {len(modules)} modules "
        f"(ceiling {MAX_SEND_HELP_MODULES}, pre-fix baseline "
        f"{BASELINE_MODULES}). Something re-introduced an eager module-scope "
        "import upstream of the _fast_send fast path."
    )


@pytest.mark.timeout(900)
@pytest.mark.parametrize("command", _FULL_STACK_COMMANDS)
def test_other_subcommands_still_get_the_full_parser(command):
    """The fast path must be scoped to ``send`` only.

    ``gateway run`` and interactive chat legitimately need the full stack; this
    proves we did not amputate the shared parser while making send cheap.
    """
    proc, modules = _run_importtime([command, "--help"])

    assert proc.returncode == 0, (
        f"`{command} --help` exited {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )
    assert command in proc.stdout, (
        f"`{command} --help` printed no help:\n{proc.stdout}"
    )
    # These commands SHOULD still pay the full price -- if this ever drops to
    # the send-path floor, the fast path has over-triggered.
    assert len(modules) > MAX_SEND_HELP_MODULES, (
        f"`{command} --help` imported only {len(modules)} modules. The send "
        "fast path appears to be firing for a command it must not handle."
    )


@pytest.mark.timeout(900)
def test_top_level_help_still_lists_every_subcommand():
    """``hermes --help`` must be unaffected -- it needs every subparser."""
    proc, _ = _run_importtime(["--help"])
    assert proc.returncode == 0, (
        f"`--help` exited {proc.returncode}.\nstderr tail:\n{proc.stderr[-3000:]}"
    )
    for command in ("send", "gateway", "cron", "chat"):
        assert command in proc.stdout, (
            f"top-level --help no longer lists {command!r}:\n{proc.stdout}"
        )


# argv forms whose stdout/stderr/exit-code must be byte-identical with the fast
# path on and off. ``--nope`` is the interesting one: the fast path's stand-in
# parser knows only ``send``, so its own usage banner would read
# "usage: hermes {send} ..." instead of the full command list. _make_parser
# raises instead of printing, which aborts the fast path and lets the real
# parser emit the canonical message.
_PARITY_ARGV = [
    ["send", "--help"],
    ["send", "--list"],
    ["send", "--list", "telegram"],
    ["send", "--list", "--json"],
    ["send", "hello"],              # missing --to  -> usage error, exit 2
    ["send", "--to", "telegram"],   # missing body  -> usage error, exit 2
    ["send", "--nope"],             # unrecognized  -> must fall back
]


def _run_plain(argv: list[str], *, fast: bool) -> subprocess.CompletedProcess:
    env = _child_env()
    if not fast:
        env["HERMES_NO_FAST_SEND"] = "1"
    else:
        env.pop("HERMES_NO_FAST_SEND", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *argv],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )


@pytest.mark.timeout(900)
@pytest.mark.parametrize("argv", _PARITY_ARGV, ids=lambda a: " ".join(a[1:]) or "bare")
def test_fast_path_output_is_identical_to_slow_path(argv):
    """The fast path must be invisible: same stdout, stderr, and exit code."""
    fast = _run_plain(argv, fast=True)
    slow = _run_plain(argv, fast=False)

    assert fast.returncode == slow.returncode, (
        f"exit code diverged for {argv}: fast={fast.returncode} "
        f"slow={slow.returncode}"
    )
    assert fast.stdout == slow.stdout, (
        f"stdout diverged for {argv}\n--- fast ---\n{fast.stdout}\n"
        f"--- slow ---\n{slow.stdout}"
    )
    assert fast.stderr == slow.stderr, (
        f"stderr diverged for {argv}\n--- fast ---\n{fast.stderr}\n"
        f"--- slow ---\n{slow.stderr}"
    )


@pytest.mark.timeout(900)
def test_fast_send_can_be_disabled_by_env():
    """``HERMES_NO_FAST_SEND=1`` restores the original full-parser path.

    An escape hatch matters here: if the fast path ever mis-parses an argument
    the slow path accepted, an operator needs a one-env-var way to get the old
    behaviour back without a rollback.
    """
    env = _child_env()
    env["HERMES_NO_FAST_SEND"] = "1"
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "hermes_cli.main", "send", "--help"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, (
        f"disabled fast path exited {proc.returncode}.\n"
        f"stderr tail:\n{proc.stderr[-3000:]}"
    )
    assert "--to" in proc.stdout, (
        f"send --help broken with the fast path disabled:\n{proc.stdout}"
    )
    modules = [
        line.split("|")[-1].strip()
        for line in proc.stderr.splitlines()
        if line.startswith("import time:") and "cumulative" not in line
    ]
    assert len(modules) > MAX_SEND_HELP_MODULES, (
        f"HERMES_NO_FAST_SEND=1 still only imported {len(modules)} modules; "
        "the escape hatch is not actually restoring the full-parser path."
    )
