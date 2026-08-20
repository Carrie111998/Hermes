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

# Ceiling for the send path. The fast path deliberately pays main.py's full early
# bootstrap -- .env, the config.yaml redact/force_ipv4 bridge, and setup_logging --
# so `send` keeps the same logging and network semantics as the slow path. What it
# skips is model_setup_flows and the ~38 build_*_parser subcommand modules.
# Set with headroom over the observed cost; at or above this means one of those
# crept back in.
MAX_SEND_HELP_MODULES = 330

# Modules the fast path MUST still import. Skipping setup_logging would cost
# `send` its agent.log records; skipping the bridge that precedes it would ignore
# security.redact_secrets and network.force_ipv4 from config.yaml. An earlier
# revision did exactly that, so this is a regression guard with real history.
REQUIRED_PREFIXES = (
    "hermes_logging",   # setup_logging() -> agent.log + errors.log
    "agent.redact",     # imported by hermes_logging; snapshots HERMES_REDACT_SECRETS
)

# Module roots that ``hermes send`` provably cannot need to print its own
# --help. Each was observed in the 444-module baseline. Matching is on the
# dotted prefix, so "agent.lsp" also catches "agent.lsp.client".
FORBIDDEN_PREFIXES = (
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


def _missing_required(modules: list[str]) -> list[str]:
    """REQUIRED_PREFIXES with no matching import in ``modules``."""
    gone = []
    for prefix in REQUIRED_PREFIXES:
        if not any(m == prefix or m.startswith(prefix + ".") for m in modules):
            gone.append(prefix)
    return gone


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

    missing = _missing_required(modules)
    assert not missing, (
        f"`hermes send --help` did NOT import {missing}. The fast path must fire "
        "AFTER main.py's early bootstrap so `send` keeps setup_logging (agent.log) "
        "and the config.yaml bridge that sets HERMES_REDACT_SECRETS and "
        "network.force_ipv4. Moving the call site earlier saves modules but "
        "silently drops logging and IPv4 forcing -- that regression already "
        "happened once."
    )

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


@pytest.mark.timeout(900)
@pytest.mark.parametrize("fast", [True, False], ids=["fast-path", "slow-path"])
def test_send_still_writes_agent_log(tmp_path, fast):
    """A fast-path ``send`` must still produce ``agent.log`` records.

    This is the behavioural half of REQUIRED_PREFIXES: importing
    ``hermes_logging`` proves the module loaded, not that ``setup_logging()``
    ran and wired a file handler. The first cut of the fast path fired before
    ``setup_logging()`` and silently cost ``send`` its logging entirely.

    ``--to notaplatform`` fails at target resolution, before any adapter or
    network call, so this exercises the error-logging path without sending
    anything. The child env is also scrubbed of credentials and HERMES_HOME is
    redirected at ``tmp_path``, so a real delivery is impossible even if the
    platform name were valid.
    """
    env = _child_env()
    env["HERMES_HOME"] = str(tmp_path)
    if fast:
        env.pop("HERMES_NO_FAST_SEND", None)
    else:
        env["HERMES_NO_FAST_SEND"] = "1"
    # Belt and braces: no inherited credential can turn this into a real send.
    for key in list(env):
        upper = key.upper()
        if any(m in upper for m in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "HOME_CHANNEL")):
            if upper != "HERMES_HOME":
                env.pop(key, None)

    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "send", "--to", "notaplatform", "probe"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )

    # Delivery-level failure, not a crash and not a usage error.
    assert proc.returncode == 1, (
        f"expected exit 1 for an unknown platform, got {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "notaplatform" in proc.stderr, proc.stderr

    log = tmp_path / "logs" / "agent.log"
    assert log.exists(), (
        f"no agent.log under {tmp_path}. The fast path must run AFTER "
        "main.py's setup_logging() call, or `hermes send` loses file logging. "
        f"Files present: {sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file())}"
    )
    assert log.stat().st_size > 0, "agent.log was created but is empty"


# ---------------------------------------------------------------------------
# The DELIVERY path: `send --list`
# ---------------------------------------------------------------------------
#
# `send --help` measures the CLI scaffolding above send. It stops before
# `cmd_send` runs, so it says nothing about what send costs to actually *do*
# something. `send --list` is the cheapest argv that exercises the real path:
# `send_cmd._load_hermes_env()` (dotenv + the config.yaml -> env bridge) and
# `gateway.channel_directory` (read side). Measured on PRECISION 2026-08-19,
# with the fast path already in place:
#
#     python -X importtime -m hermes_cli.main send --list  ->  378 modules
#
# 246 of those 378 were send's OWN dependency tree -- everything `--list`
# imported that `--help` did not. Three causes, all now fixed:
#
#   * `gateway/__init__.py` eagerly re-exported from .config / .session /
#     .delivery, so `import gateway.channel_directory` paid 294 modules
#     (requests, urllib3, agent.context_engine) for a module that is one.
#     Now PEP 562 `__getattr__`.
#   * `gateway/channel_directory.py` imported `get_hermes_home` from
#     `hermes_cli.config` (126 modules) rather than `hermes_constants` (11),
#     which is where it is actually defined -- plus `asyncio` and
#     `utils.atomic_json_write` at module scope for the write-side only.
#   * `send_cmd._load_hermes_env()` imported `hermes_cli.config` twice, for
#     that same re-exported `get_hermes_home` and for `_expand_env_vars` --
#     eight lines of `re` that now live in `hermes_cli/_env_expand.py`.
#
# Post-fix: 171 modules, a 39-module delta over `send --help`.
#
# WHY THE ASSERTION IS RELATIVE. It bounds `--list` minus `--help`, not the
# absolute count. The absolute number is a function of how much of main.py's
# bootstrap runs before the fast path fires, which is a separate design
# decision with its own tradeoffs (logging, force_ipv4) and is guarded above.
# The delta isolates the thing this section owns: send's own delivery tree.
LIST_BASELINE_DELTA = 246
MAX_LIST_DELTA = 80

# Prefixes that must not appear in the `--list` DELTA. Each was in the 246 and
# is provably unnecessary to read `~/.hermes/channel_directory.json`. Checked
# against the delta rather than the whole import set so this stays a statement
# about the delivery path and not about main.py's bootstrap.
LIST_DELTA_FORBIDDEN_PREFIXES = (
    "gateway.config",        # -> agent.secret_scope; enum + loader, not needed to read
    "gateway.session",       # -> agent.turn_context
    "gateway.delivery",
    "gateway.dead_targets",
    "agent.secret_scope",
    "agent.turn_context",
    "agent.context_engine",
    "requests",              # 17 modules, + urllib3/idna/charset_normalizer/certifi
    "urllib3",
    "asyncio",               # build/refresh side only -- 31 modules on the read path
    "hermes_cli.config",     # 126 modules for get_hermes_home + _expand_env_vars
)


def _delta_offenders(delta: set[str]) -> list[str]:
    hits = set()
    for mod in delta:
        for prefix in LIST_DELTA_FORBIDDEN_PREFIXES:
            if mod == prefix or mod.startswith(prefix + "."):
                hits.add(mod)
    return sorted(hits)


@pytest.mark.timeout(900)
def test_send_list_delivery_path_stays_cheap():
    """`send --list` must not re-grow send's own dependency tree."""
    help_proc, help_modules = _run_importtime(["send", "--help"])
    list_proc, list_modules = _run_importtime(["send", "--list"])

    assert list_proc.returncode == 0, (
        f"`send --list` exited {list_proc.returncode}.\n"
        f"stdout:\n{list_proc.stdout}\nstderr tail:\n{list_proc.stderr[-3000:]}"
    )
    # Behaviour first: this has to be the real directory read, not a stub that
    # got cheap by doing nothing.
    assert list_proc.stdout.strip(), (
        "`send --list` printed nothing; it must render the channel directory "
        "(or the 'no platforms configured' guidance) via "
        "gateway.channel_directory."
    )
    assert help_modules and list_modules, (
        "no importtime report parsed -- the measurement itself failed.\n"
        f"stderr tail:\n{list_proc.stderr[-3000:]}"
    )

    delta = set(list_modules) - set(help_modules)

    offenders = _delta_offenders(delta)
    assert not offenders, (
        f"`hermes send --list` imported {len(offenders)} module(s) the "
        f"delivery path cannot need: {offenders}\n"
        "Read the block above this test: get_hermes_home comes from "
        "hermes_constants, ${VAR} expansion from hermes_cli._env_expand, and "
        "gateway/__init__.py must stay lazy so importing "
        "gateway.channel_directory does not drag in gateway.config/session/"
        "delivery."
    )

    assert len(delta) < MAX_LIST_DELTA, (
        f"`hermes send --list` imported {len(delta)} modules that "
        f"`send --help` did not (ceiling {MAX_LIST_DELTA}, pre-fix baseline "
        f"{LIST_BASELINE_DELTA}). That delta IS send's delivery-path "
        f"dependency tree.\nNew since --help: {sorted(delta)}"
    )


@pytest.mark.timeout(900)
def test_expand_env_vars_reexport_is_the_same_object():
    """`hermes_cli.config._expand_env_vars` must stay the extracted function.

    The delivery path stopped importing `hermes_cli.config` by taking `${VAR}`
    expansion from `hermes_cli._env_expand` instead. Six other call sites --
    cli.py, cron/jobs.py, cron/scheduler.py, gateway/run.py,
    hermes_cli/managed_scope.py, tests/hermes_cli/test_config_env_expansion.py
    -- still reach it through `hermes_cli.config`. If someone re-adds a local
    definition there, those two spellings silently diverge.

    Run in a subprocess for the same reason as everything else in this file:
    importing `hermes_cli.config` in-process would pollute `sys.modules` for
    sibling tests.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.config import _expand_env_vars\n"
            "from hermes_cli._env_expand import expand_env_vars\n"
            "assert _expand_env_vars is expand_env_vars, 'diverged'\n"
            "print('same')",
        ],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0 and "same" in proc.stdout, (
        "hermes_cli.config._expand_env_vars is no longer the function in "
        "hermes_cli/_env_expand.py.\n"
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-2000:]}"
    )
