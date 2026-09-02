"""Package-manager acquisition grammar for the approval boundary.

This module owns package-manager vocabulary and argv-level classification.
Shell tokenization and execution-context parsing remain in ``tools.approval``.
``execute_code`` policy can reuse :func:`is_package_argv_acquisition` after
extracting a static process-launch argv, avoiding a second manager vocabulary.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

PACKAGE_ACQUISITION_PATTERN_KEY = "package acquisition"
PACKAGE_ACQUISITION_DESCRIPTION = (
    "package acquisition executes code obtained from a package registry; "
    "verify the exact package, version, registry, and publisher before approving"
)


def _commands(value: str) -> frozenset[str]:
    return frozenset(value.split())


PACKAGE_EXEC_WRAPPERS = _commands(
    "command builtin exec nohup setsid time nice timeout stdbuf sudo env xargs "
    "docker podman nerdctl cmd wsl"
)

# (acquisition commands, known non-acquisition commands). Selecting the first
# known command token supports global options with operands while preventing
# token-anywhere false positives such as ``npm run install``.
_MANAGER_COMMANDS = {
    "npm": (
        _commands("add ci create exec i init install update upgrade"),
        _commands(
            "access audit bugs cache config dedupe deprecate diff dist-tag docs "
            "doctor explain explore find-dupes fund help hook install-test link "
            "list login logout ls outdated owner pack ping prefix profile prune "
            "publish query rebuild repo restart root run run-script search start "
            "stop test token uninstall version view whoami"
        ),
    ),
    "pnpm": (
        _commands("add create dlx i install up update"),
        _commands(
            "audit bin config deploy env exec fetch import link list outdated pack "
            "patch prune publish rebuild remove run setup store test unlink view why"
        ),
    ),
    "yarn": (
        _commands("add create dlx install up upgrade"),
        _commands(
            "bin cache config constraints dedupe exec explain info init link node "
            "pack patch plugin rebuild remove run set unlink version why workspace "
            "workspaces"
        ),
    ),
    "bun": (
        _commands("add create i install update x"),
        _commands(
            "build completions init link outdated pm publish remove repl run test "
            "unlink upgrade"
        ),
    ),
    "pipx": (
        _commands(
            "inject install reinstall reinstall-all run runpip upgrade upgrade-all"
        ),
        _commands(
            "completions ensurepath environment interpreter list pin uninject "
            "uninstall uninstall-all unpin"
        ),
    ),
    "cargo": (
        _commands("install"),
        _commands(
            "add bench build check clean doc fetch fix info init metadata new owner "
            "package publish remove run search test tree uninstall update vendor"
        ),
    ),
    "gem": (
        _commands("install update"),
        _commands(
            "build cert check cleanup contents dependency environment fetch help "
            "info list lock open outdated owner push query search server uninstall "
            "unpack which"
        ),
    ),
    "winget": (
        _commands("import install upgrade"),
        _commands(
            "configure download export features hash list pin repair search settings "
            "show source uninstall validate"
        ),
    ),
    "choco": (
        _commands("install upgrade"),
        _commands(
            "cache config download export feature find info list new outdated pack "
            "pin push search source sync uninstall"
        ),
    ),
    "scoop": (
        _commands("install update"),
        _commands(
            "alias bucket cache cat checkup cleanup config depends download export "
            "hold home info list prefix reset search status uninstall which"
        ),
    ),
    "brew": (
        _commands("bundle install reinstall upgrade"),
        _commands(
            "autoremove casks cleanup commands config deps desc doctor fetch formulae "
            "home info leaves link list livecheck log missing outdated pin search "
            "services tap uninstall unlink unpin untap uses"
        ),
    ),
    "apk": (
        _commands("add fix upgrade"),
        _commands(
            "audit cache del fetch index info list manifest policy search stats "
            "update verify version"
        ),
    ),
    "apt": (
        _commands("full-upgrade install reinstall satisfy upgrade"),
        _commands(
            "autoremove autopurge changelog depends download list policy purge "
            "rdepends remove search show source update"
        ),
    ),
    "apt-get": (
        _commands(
            "build-dep dist-upgrade dselect-upgrade full-upgrade install reinstall "
            "satisfy upgrade"
        ),
        _commands(
            "autoremove autopurge changelog check clean download purge remove source "
            "update"
        ),
    ),
    "dnf": (
        _commands(
            "distro-sync groupinstall groupupdate install reinstall update upgrade "
            "upgrade-minimal"
        ),
        _commands(
            "autoremove check check-upgrade clean deplist downgrade download group "
            "history info list makecache module provides repoquery search swap"
        ),
    ),
    "yum": (
        _commands(
            "distro-sync groupinstall groupupdate install reinstall update upgrade"
        ),
        _commands(
            "autoremove check clean deplist downgrade group history info list "
            "makecache provides repolist search shell swap"
        ),
    ),
    "zypper": (
        _commands("dist-upgrade dup in install patch up update"),
        _commands(
            "addlock addrepo clean download info licenses locks packages patterns "
            "products repos search verify"
        ),
    ),
    "conda": (
        _commands("create install update upgrade"),
        _commands(
            "activate clean compare config deactivate doctor env export info init "
            "list package remove rename repoquery run search"
        ),
    ),
    "mamba": (
        _commands("create install update upgrade"),
        _commands(
            "activate clean config env info list package remove repoquery run search"
        ),
    ),
    "micromamba": (
        _commands("create install update upgrade"),
        _commands(
            "activate auth clean completion config constructor deactivate env info "
            "list package remove repoquery run search self-update shell"
        ),
    ),
    "poetry": (
        _commands("add install sync update"),
        _commands(
            "build cache check config debug env export init list lock new publish "
            "remove run search self shell show source version"
        ),
    ),
    "composer": (
        _commands("create-project install reinstall require update"),
        _commands(
            "archive audit browse check-platform-reqs clear-cache config depends "
            "diagnose dump-autoload exec fund global licenses list outdated remove "
            "run-script search self-update show status validate"
        ),
    ),
    "bundle": (
        _commands("add inject install update"),
        _commands(
            "cache check clean config console doctor exec gem info init list lock "
            "open outdated platform plugin pristine remove show version"
        ),
    ),
}


_GLOBAL_OPTION_VALUES = {
    "npm": _commands(
        "--cache --loglevel --prefix --registry --userconfig --workspace -C"
    ),
    "uv": _commands("--cache-dir --color --config-file --directory --project -C"),
}


def package_executable_basename(value: str) -> str:
    """Return a normalized executable basename from an argv word."""
    return re.split(r"[\\/]", value)[-1].removesuffix(".exe")


def _first_known_command_index(
    args: Sequence[str],
    known: frozenset[str],
    option_value_flags: frozenset[str] = frozenset(),
) -> int | None:
    """Find the first command index while skipping known global-option operands."""
    skip_next = False
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg in option_value_flags:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if arg in known:
            return index
    return None


def _first_known_command(
    args: Sequence[str],
    known: frozenset[str],
    option_value_flags: frozenset[str] = frozenset(),
) -> str | None:
    index = _first_known_command_index(args, known, option_value_flags)
    return args[index] if index is not None else None


def _help_requested(args: Sequence[str]) -> bool:
    """Return whether manager help is requested before an argument separator."""
    before_separator = args[: args.index("--")] if "--" in args else args
    return any(arg in {"--help", "-h"} for arg in before_separator)


def _first_positional_index(
    args: Sequence[str], option_value_flags: frozenset[str] = frozenset()
) -> int | None:
    """Return the first positional argv index after known option operands."""
    skip_next = False
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg in option_value_flags:
            skip_next = True
            continue
        if not arg.startswith("-"):
            return index
    return None


def _help_precedes(args: Sequence[str], index: int) -> bool:
    """Return whether global help appears before an execution-bearing operand."""
    separator = args.index("--") if "--" in args else len(args)
    return any(arg in {"--help", "-h"} for arg in args[: min(index, separator)])


def _command_is(
    args: Sequence[str],
    acquisitions: frozenset[str],
    non_acquisitions: frozenset[str],
    option_value_flags: frozenset[str] = frozenset(),
) -> bool:
    command_index = _first_known_command_index(
        args, acquisitions | non_acquisitions, option_value_flags
    )
    if command_index is None:
        return False
    command = args[command_index]
    if command in acquisitions:
        return not _help_requested(args)

    # Unknown manager options may consume the next word. If that operand happens
    # to equal a benign subcommand (for example ``pip --cache-dir list install``),
    # treating it as authoritative would let the real acquisition command pass.
    # Fail closed only when an acquisition verb follows such an ambiguous prefix;
    # known value-taking options above remain parsed precisely.
    ambiguous_option_prefix = any(
        arg.startswith("-")
        and "=" not in arg
        and arg not in option_value_flags
        and arg not in {"--help", "-h"}
        for arg in args[:command_index]
    )
    return ambiguous_option_prefix and any(
        arg in acquisitions for arg in args[command_index + 1 :]
    )


def _manager_command_is_acquisition(exe: str, args: Sequence[str]) -> bool:
    grammar = _MANAGER_COMMANDS.get(exe)
    if grammar is None:
        return False
    return _command_is(args, *grammar, _GLOBAL_OPTION_VALUES.get(exe, frozenset()))


def _has_uv_run_dependency(args: Sequence[str]) -> bool:
    dependency_flags = {"--with", "--with-editable", "--with-requirements"}
    return any(
        arg in dependency_flags
        or any(arg.startswith(f"{flag}=") for flag in dependency_flags)
        for arg in args
    )


def is_package_argv_acquisition(words: Sequence[str]) -> bool:
    """Classify a static command argv as package acquisition.

    Callers that parse shell text must deobfuscate shell words first. Literal
    argv callers, including ``execute_code`` AST policy, may pass raw casing
    and executable paths directly.
    """
    if not words:
        return False

    canonical_words = [str(word).lower() for word in words]
    exe = package_executable_basename(canonical_words[0])
    args = canonical_words[1:]

    pip_acquire = _commands("install wheel")
    pip_other = _commands(
        "cache check config debug download freeze hash index inspect list search show "
        "uninstall"
    )
    if re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)", exe):
        for index in range(len(args) - 1):
            if args[index] == "-m" and args[index + 1] == "pip":
                return _command_is(args[index + 2 :], pip_acquire, pip_other)
        return False
    if re.fullmatch(r"pip(?:\d+(?:\.\d+)*)?", exe):
        return _command_is(args, pip_acquire, pip_other)

    if exe == "uv":
        top_commands = _commands(
            "add auth build cache export init lock pip publish remove run self sync "
            "tool tree venv version"
        )
        top_index = _first_known_command_index(
            args, top_commands, _GLOBAL_OPTION_VALUES["uv"]
        )
        if top_index is None:
            return False
        top = args[top_index]
        tail = args[top_index + 1 :]
        if top in {"add", "sync"}:
            return not _help_requested(args)
        if top == "run":
            return _has_uv_run_dependency(tail)
        if top == "pip":
            return _command_is(
                tail,
                _commands("install sync"),
                _commands("check compile freeze list show tree uninstall"),
            )
        if top == "tool":
            return _command_is(
                tail,
                _commands("install run upgrade"),
                _commands("dir list uninstall update-shell"),
            )
        return False
    if exe == "uvx":
        command_index = _first_positional_index(
            args,
            _commands(
                "--from --index --python --with --with-editable --with-requirements"
            ),
        )
        return command_index is not None and not _help_precedes(args, command_index)
    if exe == "npx":
        command_index = _first_positional_index(
            args, _commands("--cache --call --node-options --package --shell -c -p")
        )
        return command_index is not None and not _help_precedes(args, command_index)

    if exe == "deno":
        deno_commands = _commands(
            "add bench cache check compile doc eval fmt info init install lint "
            "publish remove repl run serve task test uninstall upgrade"
        )
        command_index = _first_known_command_index(args, deno_commands)
        if command_index is None:
            return False
        command = args[command_index]
        if command in {"add", "install"}:
            return not _help_requested(args)
        return command == "run" and any(flag in args for flag in {"--allow-all", "-a"})

    if exe == "go":
        go_commands = _commands(
            "build clean doc env fix fmt generate get install list mod run test tool "
            "version vet work"
        )
        command_index = _first_known_command_index(args, go_commands, _commands("-C"))
        if command_index is None:
            return False
        command = args[command_index]
        if command in {"get", "install"}:
            return not _help_requested(args)
        if command == "run":
            return any(
                "@" in arg and not arg.startswith("-")
                for arg in args[command_index + 1 :]
            )
        return False

    if exe == "pacman":
        return not _help_requested(args) and any(
            arg.startswith("-s") and arg != "-ss" for arg in args
        )

    if exe == "dotnet":
        top = _first_known_command(
            args,
            _commands(
                "add build clean format help list new nuget pack publish remove "
                "restore run sdk sln store test tool vstest workload"
            ),
        )
        if top == "restore":
            return True
        if top == "add":
            child = _first_known_command(
                args[args.index("add") + 1 :], _commands("package reference")
            )
            return child == "package"
        if top == "tool":
            return _command_is(
                args[args.index("tool") + 1 :],
                _commands("install restore update"),
                _commands("list run search uninstall"),
            )
        if top == "workload":
            return _command_is(
                args[args.index("workload") + 1 :],
                _commands("install repair restore update"),
                _commands("config history list search uninstall"),
            )
        return False

    return _manager_command_is_acquisition(exe, args)
