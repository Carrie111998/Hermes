"""Detect Git operations that can rewrite the checkout backing this process."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tools.approval import (
    _bash_exec_payload,
    _deobfuscate_shell_word_for_detection,
    _iter_shell_command_starts,
    _read_shell_word,
)


_WORKTREE_MUTATIONS = frozenset({
    "checkout",
    "switch",
    "rebase",
    "merge",
    "pull",
    "restore",
    "clean",
    "cherry-pick",
    "revert",
    # bisect drives repeated checkouts of the running root — the exact
    # module-version-skew hazard this guard exists for.
    "bisect",
})
_WORKTREE_TARGET_ACTIONS = frozenset({"move", "remove"})
_STASH_SAFE_ACTIONS = frozenset({"list", "show", "create", "store", "drop", "clear"})
_RESET_WORKTREE_MODES = frozenset({"--hard", "--merge", "--keep"})
_KNOWN_GIT_BUILTINS = frozenset({
    "add",
    "am",
    "apply",
    "blame",
    "branch",
    "bundle",
    "cat-file",
    # `reset`/`stash`/`clean`/`restore` reach this set only in their SAFE
    # forms — _mutates_worktree classifies the dangerous forms first (see
    # _inspect_git) — so listing them here only prevents a pointless
    # `git config --get alias.<sub>` subprocess for `stash list`,
    # `reset --soft`, `clean -n`, `restore --staged`, which agent dev
    # sessions run constantly inside the source repo.
    "clean",
    "clone",
    "commit",
    "config",
    "describe",
    "diff",
    "fetch",
    "format-patch",
    "grep",
    "help",
    "init",
    "log",
    "ls-files",
    "ls-remote",
    "ls-tree",
    "maintenance",
    "merge-base",
    "mv",
    "notes",
    "push",
    "range-diff",
    "reflog",
    "remote",
    "repack",
    "replace",
    "reset",
    "restore",
    "rev-list",
    "rev-parse",
    "rm",
    "shortlog",
    "show",
    "show-ref",
    "stash",
    "status",
    "submodule",
    "tag",
    "worktree",
})
_SHELL_EXECUTABLES = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(.*)", re.DOTALL)
_SUDO_OPTIONS_WITH_ARG = frozenset({
    "-C",
    "--chdir",
    "-c",
    "--close-from",
    "-g",
    "--group",
    "-h",
    "--host",
    "-p",
    "--prompt",
    "-R",
    "--chroot",
    "-T",
    "--command-timeout",
    "-u",
    "--user",
})
_ENV_OPTIONS_WITH_ARG = frozenset({
    "-a",
    "--argv0",
    "-C",
    "--chdir",
    "-S",
    "--split-string",
    "-u",
    "--unset",
})
_WRAPPER_OPTIONS_WITH_ARG = {
    "exec": frozenset({"-a"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
}
_SIMPLE_WRAPPERS = frozenset({"builtin", "exec", "nohup", "setsid", "time"})
_MAX_RECURSION = 4
# Native Windows path: drive letter + separator. Used to avoid treating `\` as a
# shell escape when the operand is clearly a filesystem path for Git.
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
# After approval.py's escape stripping, `D:\work\repo` becomes `D:workrepo` —
# drive letter, colon, then a non-separator. Treat as untrusted (fail closed).
_MANGLED_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[^\\/]")
# Git-Bash / MSYS drive form: `/d/work/...` → `D:/work/...` (explicit Git
# path operands only; always recognized so tests can pin behavior on POSIX).
# Require a slash (or EOS) immediately after the single drive letter so
# ordinary Unix paths like `/tmp/foo` and `/dev/null` never match.
_GIT_BASH_DRIVE_RE = re.compile(r"^/([A-Za-z])(?:/(.*))?\Z")


@dataclass
class _Heredoc:
    delimiter: str
    strip_tabs: bool
    execute_as_shell: bool
    body: list[str] = field(default_factory=list)


@dataclass
class _ShellContext:
    kind: str
    opener: int
    quote: str | None = None


def get_running_source_root() -> Path | None:
    """Return the source checkout backing this process, if there is one."""
    try:
        root = Path(__file__).resolve().parent.parent
    except (OSError, RuntimeError):
        return None
    return root if (root / ".git").exists() else None


def _resolve(path_str: str, base: Path) -> Path:
    path = Path(os.path.expanduser(path_str))
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False


def _unquote_raw_preserve(raw: str) -> str:
    """Strip a single layer of matching quotes without interpreting escapes."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return raw[1:-1]
    return raw


def _raw_looks_like_windows_path(raw: str) -> bool:
    """True when *raw* (possibly quoted) is a native Windows drive path."""
    inner = _unquote_raw_preserve(raw)
    if _WINDOWS_DRIVE_PATH_RE.match(inner):
        return True
    return bool(re.match(r"^[A-Za-z]:", inner) and "\\" in inner)


def _strip_quotes_preserve_windows_path(raw_word: str) -> str:
    """Strip shell quotes but keep `\\` as path separators in Windows paths."""
    return _unquote_raw_preserve(raw_word)


_GIT_PATH_OPTIONS_WITH_ARG = frozenset({
    "-C",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
})
_GIT_PATH_ENV_VARS = frozenset({"GIT_DIR", "GIT_WORK_TREE"})


def _path_aware_shell_word(raw_word: str) -> str:
    """Deobfuscate a shell word, preserving native Windows path separators.

    approval.py's escape strip treats `\\` as a shell escape, which mangles
    quoted paths like ``"D:\\work\\hermes-agent"`` into ``D:workhermes-agent``.
    For explicit filesystem operands we keep those separators instead.

    Callers must only invoke this for Git path-operand positions (see
    ``_shell_words_at``); it is not safe to apply to every shell token.
    """
    assign = _ASSIGNMENT_RE.fullmatch(raw_word)
    if assign:
        name, _, value_raw = raw_word.partition("=")
        if name in _GIT_PATH_ENV_VARS and _raw_looks_like_windows_path(value_raw):
            return f"{name}={_strip_quotes_preserve_windows_path(value_raw)}"
        return _deobfuscate_shell_word_for_detection(raw_word)

    for prefix in ("--work-tree=", "--git-dir="):
        if raw_word.startswith(prefix):
            value = raw_word[len(prefix) :]
            if _raw_looks_like_windows_path(value):
                return prefix + _strip_quotes_preserve_windows_path(value)
            return _deobfuscate_shell_word_for_detection(raw_word)

    if (
        raw_word.startswith("-C")
        and len(raw_word) > 2
        and not raw_word.startswith("--")
    ):
        value = raw_word[2:]
        if _raw_looks_like_windows_path(value):
            return "-C" + _strip_quotes_preserve_windows_path(value)
        return _deobfuscate_shell_word_for_detection(raw_word)

    if _raw_looks_like_windows_path(raw_word):
        return _strip_quotes_preserve_windows_path(raw_word)
    return _deobfuscate_shell_word_for_detection(raw_word)


def _is_glued_git_path_option(raw_word: str) -> bool:
    """True for ``-Cpath`` / ``--git-dir=`` / ``--work-tree=`` glued forms."""
    if raw_word.startswith("--work-tree=") or raw_word.startswith("--git-dir="):
        return True
    return (
        raw_word.startswith("-C")
        and len(raw_word) > 2
        and not raw_word.startswith("--")
    )


def _is_git_path_env_assignment(raw_word: str) -> bool:
    """True for ``GIT_DIR=...`` / ``GIT_WORK_TREE=...`` assignment words."""
    if not _ASSIGNMENT_RE.fullmatch(raw_word):
        return False
    name, _, _ = raw_word.partition("=")
    return name in _GIT_PATH_ENV_VARS


def _windows_git_bash_to_drive(path: str) -> str | None:
    """Translate Git-Bash ``/d/work/...`` to ``D:/work/...``, else None."""
    match = _GIT_BASH_DRIVE_RE.fullmatch(path)
    if not match:
        return None
    drive = match.group(1).upper()
    rest = match.group(2)
    if rest is None:
        return f"{drive}:/"
    return f"{drive}:/{rest}"


def _is_mangled_windows_drive(path: str) -> bool:
    """True when a drive path lost its separators (escape-strip artifact)."""
    return bool(_MANGLED_WINDOWS_DRIVE_RE.match(path))


def _normalize_git_path_operand(path: str) -> str:
    """Normalize an explicit Git path operand for resolve/compare.

    Preserves native Windows separators; maps Git-Bash drive form to
    ``D:/...``. Does not require running on Windows.
    """
    git_bash = _windows_git_bash_to_drive(path)
    if git_bash is not None:
        return git_bash
    return path


def _explicit_git_path(path: str, base: Path) -> Path:
    """Resolve an explicit Git ``-C`` / work-tree path operand."""
    return _resolve_git_target(_normalize_git_path_operand(path), base)


def _resolve_git_target(path_str: str, base: Path) -> Path:
    """Resolve a Git path operand with Windows-aware absolute detection.

    Drive-letter paths (``D:/...``, ``D:\\...``) are absolute for comparison
    even when pathlib on POSIX would treat them as relative.
    """
    normalized = _normalize_git_path_operand(path_str)
    expanded = os.path.expanduser(normalized)
    if _WINDOWS_DRIVE_PATH_RE.match(expanded):
        # Keep a stable absolute form without joining *base* (POSIX Path would
        # otherwise treat ``D:/x`` as relative and prepend cwd/base).
        return Path(expanded.replace("\\", "/"))
    return _resolve(expanded, base)


def _executable_name(value: str) -> str:
    return Path(value.replace("\\", "/")).name.removesuffix(".exe").lower()


def _shell_words_at(command: str, start: int) -> list[str]:
    """Tokenize a shell command start, path-preserving only Git path operands.

    Windows backslash preservation applies solely to:
    - the next word after bare ``-C`` / ``--git-dir`` / ``--work-tree`` /
      ``--namespace`` / ``--exec-path``
    - glued ``--work-tree=VALUE`` / ``--git-dir=VALUE`` / ``-CVALUE``
    - ``GIT_DIR=`` / ``GIT_WORK_TREE=`` assignment values (when Windows-looking)

    Every other token uses ``_deobfuscate_shell_word_for_detection`` unchanged
    so non-path words (aliases, subcommands, ordinary args) are not rewritten.
    """
    words: list[str] = []
    cursor = start
    expect_path_operand = False
    for _ in range(64):
        word_start, word_end, raw_word = _read_shell_word(command, cursor)
        if word_start == word_end:
            break
        if words and "\n" in command[cursor:word_start]:
            break

        if (
            expect_path_operand
            or _is_glued_git_path_option(raw_word)
            or _is_git_path_env_assignment(raw_word)
        ):
            word = _path_aware_shell_word(raw_word)
        else:
            word = _deobfuscate_shell_word_for_detection(raw_word)
        words.append(word)

        if expect_path_operand:
            expect_path_operand = False
        else:
            expect_path_operand = word in _GIT_PATH_OPTIONS_WITH_ARG

        cursor = word_end
    return words


def _consume_options(
    words: list[str],
    start: int,
    options_with_arg: frozenset[str],
) -> int:
    index = start
    while index < len(words):
        option = words[index]
        if option == "--":
            return index + 1
        if not option.startswith("-") or option == "-":
            break
        option_name = option.split("=", 1)[0]
        if "=" not in option and option_name in options_with_arg:
            index += 2
        else:
            index += 1
    return index


def _command_parts(words: list[str]) -> tuple[dict[str, str], str | None, list[str]]:
    env: dict[str, str] = {}
    index = 0

    while index < len(words):
        if _ASSIGNMENT_RE.fullmatch(words[index]):
            name, value = words[index].split("=", 1)
            env[name] = value
            index += 1
            continue

        executable = _executable_name(words[index])
        if executable == "sudo":
            index = _consume_options(words, index + 1, _SUDO_OPTIONS_WITH_ARG)
            continue
        if executable == "env":
            index = _consume_options(words, index + 1, _ENV_OPTIONS_WITH_ARG)
            continue
        if executable == "command":
            if index + 1 < len(words) and words[index + 1] in {"-v", "-V"}:
                return env, None, []
            index = _consume_options(words, index + 1, frozenset())
            continue
        if executable in _SIMPLE_WRAPPERS:
            index = _consume_options(
                words,
                index + 1,
                _WRAPPER_OPTIONS_WITH_ARG.get(executable, frozenset()),
            )
            continue
        return env, words[index], words[index + 1 :]

    return env, None, []


def _scope_keys(command: str, starts: list[int]) -> dict[int, tuple[int, ...]]:
    contexts = [_ShellContext("root", -1)]
    scopes: dict[int, tuple[int, ...]] = {}
    cursor = 0

    for start in sorted(set(starts)):
        while cursor < start:
            context = contexts[-1]
            quote = context.quote
            char = command[cursor]

            if quote == "'":
                if char == "'":
                    context.quote = None
                cursor += 1
                continue
            if quote == '"':
                if char == "\\" and cursor + 1 < start:
                    cursor += 2
                    continue
                if char == '"':
                    context.quote = None
                    cursor += 1
                    continue
                if command.startswith("$(", cursor):
                    contexts.append(_ShellContext("$(", cursor))
                    cursor += 2
                    continue
                if char == "`":
                    contexts.append(_ShellContext("`", cursor))
                cursor += 1
                continue

            if char in {"'", '"'}:
                context.quote = char
                cursor += 1
                continue
            if char == "\\" and cursor + 1 < start:
                cursor += 2
                continue
            if command.startswith("$(", cursor):
                contexts.append(_ShellContext("$(", cursor))
                cursor += 2
                continue
            if char == "(":
                contexts.append(_ShellContext("(", cursor))
                cursor += 1
                continue
            if char == ")" and len(contexts) > 1 and contexts[-1].kind in {"(", "$("}:
                contexts.pop()
                cursor += 1
                continue
            if char == "`":
                if len(contexts) > 1 and contexts[-1].kind == "`":
                    contexts.pop()
                else:
                    contexts.append(_ShellContext("`", cursor))
            cursor += 1

        scopes[start] = tuple(item.opener for item in contexts[1:])

    return scopes


def _operator_before(command: str, start: int) -> str | None:
    index = start - 1
    saw_newline = False
    while index >= 0 and command[index].isspace():
        saw_newline = saw_newline or command[index] == "\n"
        index -= 1
    if index < 0:
        return "\n" if saw_newline else None
    if index > 0 and command[index - 1 : index + 1] in {"&&", "||"}:
        return command[index - 1 : index + 1]
    if command[index] in {";", "|", "&", "(", "{"}:
        return command[index]
    return "\n" if saw_newline else None


def _cd_target(executable: str, args: list[str], cwd: Path) -> Path | None:
    if _executable_name(executable) not in {"cd", "pushd"}:
        return None
    index = _consume_options(args, 0, frozenset())
    if index >= len(args) or args[index] == "-":
        return None
    target = _resolve(args[index], cwd)
    return target if target.is_dir() else None


def _shell_script_arg(args: list[str]) -> str | None:
    """Return the script string owned by a shell's ``-c``, if present.

    Tries approval.py's ``_bash_exec_payload`` first: it parses bash's real
    option grammar (``-O/-o`` consume the next argument, short-option
    bundles, ``--init-file``/``--rcfile``), catching payloads a naive scan
    misses — ``bash -o pipefail -c '<script>'`` hides the ``-c`` behind an
    operand. When it finds no ``-c``, fall back to the permissive positional
    scan: ``_SHELL_EXECUTABLES`` also covers zsh/dash/ksh, whose option
    letters (``zsh -yc``, ``dash -Vc``) fall outside bash's alphabet and
    would otherwise make this block-guard fail open.
    """
    has_c, payload = _bash_exec_payload(args)
    if has_c:
        return payload
    for index, arg in enumerate(args):
        if arg == "--":
            break
        if arg.startswith("-") and "c" in arg[1:]:
            return args[index + 1] if index + 1 < len(args) else None
        if not arg.startswith("-"):
            break
    return None


def _heredoc_specs(line: str) -> list[_Heredoc]:
    specs: list[_Heredoc] = []
    quote: str | None = None
    index = 0

    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(line):
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if not line.startswith("<<", index) or line.startswith("<<<", index):
            index += 1
            continue

        operator_at = index
        index += 2
        strip_tabs = index < len(line) and line[index] == "-"
        if strip_tabs:
            index += 1
        while index < len(line) and line[index] in {" ", "\t"}:
            index += 1
        if index >= len(line):
            break

        delimiter_quote = line[index] if line[index] in {"'", '"'} else None
        if delimiter_quote:
            index += 1
            end = line.find(delimiter_quote, index)
            if end == -1:
                break
            delimiter = line[index:end]
            index = end + 1
        else:
            end = index
            while (
                end < len(line) and not line[end].isspace() and line[end] not in ";|&<>"
            ):
                end += 1
            delimiter = line[index:end]
            index = end
        if not delimiter:
            continue

        header = line[:operator_at]
        starts = list(_iter_shell_command_starts(header))
        words = _shell_words_at(header, starts[-1]) if starts else []
        _, executable, args = _command_parts(words)
        execute_as_shell = bool(
            executable
            and _executable_name(executable) in _SHELL_EXECUTABLES
            and _shell_script_arg(args) is None
            and not any(arg and not arg.startswith("-") for arg in args)
        )
        specs.append(_Heredoc(delimiter, strip_tabs, execute_as_shell))

    return specs


def _masked_line(line: str) -> str:
    return "".join(char if char in {"\r", "\n"} else " " for char in line)


def _mask_heredocs(command: str) -> tuple[str, list[str]]:
    output: list[str] = []
    pending: list[_Heredoc] = []
    shell_scripts: list[str] = []

    for line in command.splitlines(keepends=True):
        if pending:
            current = pending[0]
            candidate = line.rstrip("\r\n")
            if current.strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == current.delimiter:
                if current.execute_as_shell:
                    shell_scripts.append("".join(current.body))
                pending.pop(0)
            else:
                current.body.append(line)
            output.append(_masked_line(line))
            continue

        output.append(line)
        pending.extend(_heredoc_specs(line))

    for current in pending:
        if current.execute_as_shell:
            shell_scripts.append("".join(current.body))
    return "".join(output), shell_scripts


def _git_target_and_subcommand(
    args: list[str],
    current_dir: Path,
    env: dict[str, str],
    root: Path | None = None,
) -> tuple[Path, str | None, list[str], dict[str, str]]:
    target = current_dir
    work_tree: str | None = None
    aliases: dict[str, str] = {}
    index = 0
    dash_c_count = 0
    # Conservative multi -C rule: if any intermediate -C lands inside the
    # source root, treat the effective target as the source root so mutating
    # subcommands block — even when a later -C leaves the tree. Ambiguous /
    # mangled Windows operands also fail closed (target := root) rather than
    # falling back to ambient cwd.
    any_dash_c_within_root = False
    untrusted_path_operand = False

    def _apply_explicit_path(path_str: str, base: Path) -> Path:
        nonlocal any_dash_c_within_root, untrusted_path_operand
        if _is_mangled_windows_drive(path_str):
            untrusted_path_operand = True
            return root if root is not None else base
        resolved = _explicit_git_path(path_str, base)
        if root is not None and _is_within(resolved, root):
            any_dash_c_within_root = True
        return resolved

    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg == "-C" and index + 1 < len(args):
            dash_c_count += 1
            target = _apply_explicit_path(args[index + 1], target)
            index += 2
            continue
        if arg.startswith("-C") and len(arg) > 2:
            dash_c_count += 1
            target = _apply_explicit_path(arg[2:], target)
            index += 1
            continue
        if arg in {"--work-tree", "--git-dir", "--namespace", "--exec-path"}:
            if arg == "--work-tree" and index + 1 < len(args):
                work_tree = args[index + 1]
            index += 2
            continue
        if arg.startswith("--work-tree="):
            work_tree = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "-c" and index + 1 < len(args):
            config = args[index + 1]
            if config.lower().startswith("alias.") and "=" in config:
                key, value = config.split("=", 1)
                aliases[key[6:].lower()] = value
            index += 2
            continue
        if arg.startswith("-calias.") and "=" in arg:
            key, value = arg[2:].split("=", 1)
            aliases[key[6:].lower()] = value
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break

    explicit_work_tree = work_tree or env.get("GIT_WORK_TREE")
    if explicit_work_tree:
        target = _apply_explicit_path(explicit_work_tree, target)

    if root is not None and (
        untrusted_path_operand
        or (dash_c_count > 1 and any_dash_c_within_root)
    ):
        target = root

    subcommand = args[index].lower() if index < len(args) else None
    return target, subcommand, args[index + 1 :], aliases


def _mutates_worktree(subcommand: str, args: list[str]) -> bool:
    if subcommand == "reset":
        hard = re.compile(r"--h(?:a(?:r(?:d)?)?)?\Z")
        return any(arg in _RESET_WORKTREE_MODES or hard.fullmatch(arg) for arg in args)
    if subcommand == "stash":
        action = next((arg for arg in args if not arg.startswith("-")), "push")
        return action not in _STASH_SAFE_ACTIONS
    if subcommand == "clean":
        dry_run = any(
            arg == "--dry-run"
            or (arg.startswith("-") and not arg.startswith("--") and "n" in arg[1:])
            for arg in args
        )
        return not dry_run
    if subcommand == "restore":
        staged = any(
            arg == "--staged" or (arg.startswith("-") and "S" in arg[1:])
            for arg in args
        )
        worktree = any(
            arg == "--worktree" or (arg.startswith("-") and "W" in arg[1:])
            for arg in args
        )
        return worktree or not staged
    return subcommand in _WORKTREE_MUTATIONS


def _next_positional(args: list[str], index: int) -> int:
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return index + 1
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        return index
    return index


def _inspect_git_worktree(args: list[str], cwd: Path, root: Path) -> str | None:
    """Block `worktree remove|move` aimed at the running root, from any directory."""
    action_index = _next_positional(args, 0)
    if action_index >= len(args):
        return None
    action = args[action_index].lower()
    if action not in _WORKTREE_TARGET_ACTIONS:
        return None
    target_index = _next_positional(args, action_index + 1)
    if target_index >= len(args):
        return None
    if _explicit_git_path(args[target_index], cwd) == root:
        return f"git worktree {action}"
    return None


def _read_git_alias(executable: str, target: Path, alias: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "-C", str(target), "config", "--get", f"alias.{alias}"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _inspect_git(
    executable: str,
    args: list[str],
    current_dir: Path,
    env: dict[str, str],
    root: Path,
    depth: int,
) -> str | None:
    target, subcommand, sub_args, inline_aliases = _git_target_and_subcommand(
        args, current_dir, env, root=root
    )
    if subcommand is None:
        return None
    # `worktree` names its victim as an argument, so the cwd check does not apply.
    if subcommand == "worktree":
        return _inspect_git_worktree(sub_args, target, root)
    if not _is_within(target, root):
        return None
    if _mutates_worktree(subcommand, sub_args):
        return f"git {subcommand}"
    if subcommand in _KNOWN_GIT_BUILTINS:
        return None
    if depth >= _MAX_RECURSION:
        return None

    alias = inline_aliases.get(subcommand)
    if alias is None:
        alias = _read_git_alias(executable, target, subcommand)
    if not alias:
        return None
    if alias.startswith("!"):
        return _find_mutation(alias[1:], target, root, depth + 1)
    try:
        alias_args = shlex.split(alias, posix=True)
    except ValueError:
        return None
    return _inspect_git(
        executable,
        [*alias_args, *sub_args],
        target,
        {},
        root,
        depth + 1,
    )


def _inspect_github_cli(
    executable: str,
    args: list[str],
    current_dir: Path,
    root: Path,
) -> str | None:
    if not _is_within(current_dir, root):
        return None
    name = _executable_name(executable)
    index = _consume_options(args, 0, frozenset({"-R", "--repo", "--hostname"}))
    if args[index : index + 2] == ["pr", "checkout"]:
        return f"{name} pr checkout"
    return None


def _find_mutation(command: str, cwd: Path, root: Path, depth: int = 0) -> str | None:
    if depth > _MAX_RECURSION:
        return None

    masked_command, heredoc_scripts = _mask_heredocs(command)
    for script in heredoc_scripts:
        operation = _find_mutation(script, cwd, root, depth + 1)
        if operation:
            return operation

    starts = sorted(set(_iter_shell_command_starts(masked_command)))
    scopes = _scope_keys(masked_command, starts)
    cwd_by_scope: dict[tuple[int, ...], Path] = {(): cwd}
    pending_cd: dict[tuple[int, ...], Path] = {}

    for start in starts:
        scope = scopes[start]
        if scope not in cwd_by_scope:
            cwd_by_scope[scope] = cwd_by_scope.get(scope[:-1], cwd)

        operator = _operator_before(masked_command, start)
        pending = pending_cd.pop(scope, None)
        if pending is not None and operator in {"&&", ";", "\n"}:
            cwd_by_scope[scope] = pending

        words = _shell_words_at(masked_command, start)
        env, executable, args = _command_parts(words)
        if executable is None:
            continue

        current_dir = cwd_by_scope[scope]
        cd_target = _cd_target(executable, args, current_dir)
        if cd_target is not None:
            pending_cd[scope] = cd_target
            continue

        executable_name = _executable_name(executable)
        if executable_name == "git":
            operation = _inspect_git(executable, args, current_dir, env, root, depth)
            if operation:
                return operation
        elif executable_name in {"gh", "hub"}:
            operation = _inspect_github_cli(executable, args, current_dir, root)
            if operation:
                return operation
        elif executable_name in _SHELL_EXECUTABLES:
            script = _shell_script_arg(args)
            if script:
                operation = _find_mutation(script, current_dir, root, depth + 1)
                if operation:
                    return operation

    return None


def detect_self_repo_git_mutation(
    command: str,
    cwd: str | None,
    source_root: Path | None = None,
) -> tuple[bool, str | None]:
    """Return whether a command would rewrite the live source checkout."""
    root = source_root if source_root is not None else get_running_source_root()
    if root is None or not command:
        return False, None

    root = _resolve(str(root), Path("/"))
    base = _resolve(cwd, Path("/")) if cwd else Path("/")
    operation = _find_mutation(command, base, root)
    if operation is None:
        return False, None
    return True, _block_message(operation, root)


def _block_message(operation: str, root: Path) -> str:
    return (
        f"Blocked: `{operation}` would rewrite Hermes's live source checkout "
        f"({root}) and can mix module versions in this running process. "
        "Use a separate worktree or temporary clone. To change this checkout, "
        "stop Hermes, run the command externally, then restart Hermes."
    )
