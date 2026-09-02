from __future__ import annotations

import re
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

"""Workspace-binding guards for side-effecting project tools.

The gateway can bind a chat/channel to an authoritative workspace repo.  Tool
side effects should not silently land in a different checkout just because the
model inferred project context from room names or stale memory.
"""




_MUTATING_GIT_SUBCOMMANDS = frozenset({
    "add",
    "am",
    "apply",
    "bisect",
    "checkout",
    "cherry-pick",
    "clean",
    "clone",
    "commit",
    "fetch",
    "gc",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "submodule",
    "switch",
    "tag",
    "worktree",
})

_READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "blame",
    "branch",  # treated as mutating when args imply config/ref changes
    "diff",
    "grep",
    "log",
    "ls-files",
    "remote",  # treated as mutating when args imply config/ref changes
    "rev-parse",
    "show",
    "status",
})

_MUTATING_REMOTE_SUBCOMMANDS = frozenset({
    "add",
    "remove",
    "rm",
    "rename",
    "set-branches",
    "set-head",
    "set-url",
    "prune",
    "update",
})

_MUTATING_BRANCH_FLAGS = frozenset({
    "-d",
    "-D",
    "-m",
    "-M",
    "-c",
    "-C",
    "-u",
    "--delete",
    "--move",
    "--copy",
    "--track",
    "--force",
    "-f",
    "--set-upstream-to",
    "--unset-upstream",
    "--edit-description",
})

_UNSAFE_GIT_CWD_SUBCOMMAND = "__unsafe_explicit_git_dir__"

_UNSAFE_GIT_ENV_VARS = frozenset({
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
})

_UNSAFE_GIT_CONFIG_KEYS = frozenset({
    "core.worktree",
    "include.path",
})

_UNSAFE_GIT_CONFIG_KEY_PREFIXES = (
    "includeif.",
    "alias.",
)

_UNSAFE_GIT_CONFIG_ENV_PREFIXES = (
    "GIT_CONFIG_",
)


@dataclass(frozen=True)
class GitInvocation:
    subcommand: str
    args: list[str]
    cwd: Path


def check_path_side_effect_allowed(
    path: str | Path,
    backend: Optional[str] = None,
) -> Optional[str]:
    """Return an error if a file write targets a repo outside the binding.

    Non-gateway callers and non-repo paths are allowed.  Gateway project writes
    are blocked when they target a git checkout that is not the bound repo, or
    when the session has no workspace binding at all.
    """

    if not _in_gateway_session():
        return None

    backend = (backend or "local").strip().lower() or "local"
    if backend != "local":
        return (
            f"Blocked repo side effect: workspace guard cannot verify file write "
            f"on nonlocal `{backend}` backend paths."
        )

    resolved = _safe_resolve(path)
    repo_root = _find_git_root(resolved if resolved.is_dir() else resolved.parent)
    if repo_root is None:
        return None

    bound_repo = _bound_repo_path()
    if bound_repo is None:
        return _blocked_message(repo_root, None)
    if not _same_repo(repo_root, bound_repo):
        return _blocked_message(repo_root, bound_repo)
    return None


def check_terminal_side_effect_allowed(
    command: str,
    cwd: str | Path,
    backend: Optional[str] = None,
) -> Optional[str]:
    """Return an error if a mutating git command is outside the binding."""

    if not _in_gateway_session():
        return None

    cwd_path = _safe_resolve(cwd)
    backend = (backend or "local").strip().lower() or "local"
    unsafe_ambient = any(
        value and _env_name_is_unsafe_git(name)
        for name, value in __import__("os").environ.items()
    )
    for invocation in _iter_git_invocations(command, cwd_path):
        if not _git_invocation_is_mutating(invocation):
            continue
        if backend != "local":
            return (
                f"Blocked repo side effect: workspace guard cannot verify git command "
                f"on nonlocal `{backend}` backend."
            )
        if unsafe_ambient:
            return _unverified_git_message()
        if invocation.subcommand == _UNSAFE_GIT_CWD_SUBCOMMAND:
            return _unverified_git_message()
        if _git_subcommand_target_is_unverifiable(invocation.subcommand):
            return _unverified_git_message()
        repo_root = _find_git_root(invocation.cwd)
        if repo_root is None:
            # Preserve current behavior for non-repo scratch contexts while
            # still checking later git invocations in the same shell command.
            continue

        bound_repo = _bound_repo_path()
        if bound_repo is None:
            return _blocked_message(repo_root, None)
        if not _same_repo(repo_root, bound_repo):
            return _blocked_message(repo_root, bound_repo)
    return None


def _unverified_git_message() -> str:
    return (
        "Blocked repo side effect: workspace guard cannot verify the target "
        "repository for this git command. Avoid complex cd/global git-dir/work-tree "
        "forms or run the command from the bound repository."
    )


def _blocked_message(actual_repo: Path, bound_repo: Optional[Path]) -> str:
    if bound_repo is None:
        return (
            "Blocked repo side effect: this gateway session has no authoritative "
            f"workspace binding, but the target is inside git repo `{actual_repo}`. "
            "Add a channel binding to workspaces.yaml or use read-only tools."
        )
    return (
        "Blocked repo side effect outside authoritative workspace binding: "
        f"target repo `{actual_repo}` does not match bound repo `{bound_repo}`."
    )


def _in_gateway_session() -> bool:
    return bool(_session_env("HERMES_SESSION_PLATFORM", "") and _session_env("HERMES_SESSION_CHAT_ID", ""))


def _bound_repo_path() -> Optional[Path]:
    repo_path = _session_env("HERMES_SESSION_WORKSPACE_REPO_PATH", "")
    if not repo_path:
        return None
    return _safe_resolve(repo_path)


def _session_env(name: str, default: str = "") -> str:
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, default)
    except Exception:
        return os.getenv(name, default)


def _find_git_root(start: Path) -> Optional[Path]:
    current = _safe_resolve(start)
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.abspath(left) == os.path.abspath(right)


def _gitdir_pointer(repo_root: Path) -> Optional[Path]:
    marker = repo_root / ".git"
    try:
        if marker.is_dir():
            return marker.resolve()
        if not marker.is_file():
            return None
        for line in marker.read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("gitdir:"):
                raw = Path(line.split(":", 1)[1].strip())
                if not raw.is_absolute():
                    raw = repo_root / raw
                return raw.resolve()
    except OSError:
        return None
    return None


def _git_common_dir(repo_root: Path) -> Optional[Path]:
    gitdir = _gitdir_pointer(repo_root)
    if gitdir is None or not gitdir.is_dir():
        return None
    commondir = gitdir / "commondir"
    try:
        if commondir.is_file():
            raw = Path(commondir.read_text(encoding="utf-8").strip())
            if not raw.is_absolute():
                raw = gitdir / raw
            resolved = raw.resolve()
            if resolved.is_dir():
                return resolved
    except OSError:
        pass
    if gitdir.parent.name == "worktrees":
        candidate = gitdir.parent.parent
        if candidate.is_dir():
            return candidate
    return gitdir


def _linked_worktree_backref_ok(repo_root: Path, gitdir: Path) -> bool:
    back = gitdir / "gitdir"
    try:
        if not back.is_file():
            return False
        pointed = Path(back.read_text(encoding="utf-8").strip())
        if not pointed.is_absolute():
            pointed = gitdir / pointed
        return pointed.resolve() == (repo_root / ".git").resolve()
    except OSError:
        return False


def _is_authorized_checkout(repo_root: Path, gitdir: Path) -> bool:
    """True when repo_root is the primary checkout or a validated linked worktree."""
    marker = repo_root / ".git"
    try:
        if marker.is_symlink():
            return False
        if marker.is_dir():
            return _same_path(gitdir, marker)
        if not marker.is_file():
            return False
        if gitdir.parent.name == "worktrees":
            return _linked_worktree_backref_ok(repo_root, gitdir)
        # Separate-git-dir / bare common. Do not treat a pointer at another
        # checkout's `.git` directory as this root's primary gitdir.
        if gitdir.name == ".git" and gitdir.is_dir() and not _same_path(gitdir.parent, repo_root):
            return False
        return True
    except OSError:
        return False


def _same_repo(left: Path, right: Path) -> bool:
    """True when both paths are the same checkout or linked worktrees."""
    if _same_path(left, right):
        return True
    left_git = _gitdir_pointer(left)
    right_git = _gitdir_pointer(right)
    left_common = _git_common_dir(left)
    right_common = _git_common_dir(right)
    if left_git is None or right_git is None or left_common is None or right_common is None:
        return False
    if not _same_path(left_common, right_common):
        return False
    return _is_authorized_checkout(left, left_git) and _is_authorized_checkout(right, right_git)


def _safe_resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _iter_git_invocations(command: str, cwd: Path) -> Iterable[GitInvocation]:
    current_cwd = cwd
    unsafe_export = False
    if _has_unverifiable_shell_git(command):
        yield GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, [], current_cwd)
        return
    for segment in _split_shell_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            if _command_has_git_executable_token(segment):
                yield GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, [], current_cwd)
            continue
        if not tokens:
            continue
        if tokens[0] == "export":
            for token in tokens[1:]:
                name = _env_assignment_name(token) or token
                if _env_name_is_unsafe_git(name):
                    unsafe_export = True
            continue
        unsafe_env, tokens = _strip_env_prefix(tokens)
        if (unsafe_env or unsafe_export) and any(_is_git_executable_token(token) for token in tokens):
            yield GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, [], current_cwd)
            continue
        if not tokens:
            continue
        if tokens[0] == "cd":
            if len(tokens) != 2 or _path_has_shell_expansion(tokens[1]):
                if _command_has_git_executable_token(command):
                    yield GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, [], current_cwd)
                continue
            current_cwd = _safe_resolve(current_cwd / tokens[1])
            continue
        for index, token in enumerate(tokens):
            if not _is_git_executable_token(token):
                continue
            if unsafe_export:
                yield GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, [], current_cwd)
                continue
            invocation = _parse_git_invocation(tokens[index:], current_cwd)
            if invocation:
                yield invocation


def _is_git_executable_token(token: str) -> bool:
    return Path(token.strip("()")).name == "git"


def _path_has_shell_expansion(value: str) -> bool:
    return "$" in value or "`" in value or "$(" in value or value.startswith("~")


def _has_unquoted_shell_paren(value: str) -> bool:
    in_single = False
    in_double = False
    escaped = False

    for ch in value:
        if escaped:
            escaped = False
            continue

        if ch == "\\" and not in_single:
            escaped = True
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            continue

        if ch in "()" and not in_single and not in_double:
            return True

    return False


_GIT_EXECUTABLE_TOKEN_RE = re.compile(r"(?:^|[;|&\n\t ])git(?:[;|&\n\t ]|$)")


def _command_has_git_executable_token(command: str) -> bool:
    """True when git appears as a path basename token, not a hostname substring."""
    try:
        tokens = _shell_payload_tokens(command)
    except ValueError:
        return bool(_GIT_EXECUTABLE_TOKEN_RE.search(f" {command} "))
    return any(_is_git_executable_token(token) for token in tokens)


def _iter_unquoted_substitutions(command: str) -> Iterable[str]:
    """Yield $(...) and backtick payloads that the shell would expand."""
    in_single = False
    in_double = False
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if in_single:
            index += 1
            continue
        if command.startswith("$(", index):
            depth = 1
            cursor = index + 2
            while cursor < len(command) and depth:
                if command.startswith("$(", cursor):
                    depth += 1
                    cursor += 2
                    continue
                nested = command[cursor]
                if nested == "(":
                    depth += 1
                elif nested == ")":
                    depth -= 1
                cursor += 1
            payload = command[index + 2 :] if depth else command[index + 2 : cursor - 1]
            yield payload
            index = cursor if not depth else len(command)
            continue
        if char == "`":
            cursor = index + 1
            while cursor < len(command) and command[cursor] != "`":
                cursor += 1
            yield command[index + 1 : cursor]
            index = cursor + 1 if cursor < len(command) else len(command)
            continue
        index += 1


def _substitution_contains_git_command(command: str) -> bool:
    payloads = list(_iter_unquoted_substitutions(command))
    if not payloads:
        return False
    return any(
        _payload_has_git_command(payload) or _substitution_contains_git_command(payload)
        for payload in payloads
    )


def _has_unverifiable_shell_git(command: str) -> bool:
    # Command substitution can redirect the git target at runtime and is
    # intentionally fail-closed rather than partially shell-parsed.
    if _substitution_contains_git_command(command):
        return True

    if _shell_command_executes_git(command):
        return True

    # Only unquoted parentheses imply shell grouping/subshell syntax. Quoted
    # parentheses are common in conventional commit scopes and format strings.
    return _has_unquoted_shell_paren(command) and _command_has_git_executable_token(command)


_SHELL_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ash", "ksh"})
_SHELL_WRAPPERS = frozenset({
    "env",
    "command",
    "nice",
    "nohup",
    "timeout",
    "stdbuf",
    "ionice",
    "time",
})
_WRAPPER_VALUE_OPTS = frozenset({
    "-n",
    "-u",
    "-k",
    "-s",
    "-o",
    "-E",
    "--nice",
    "--adjustment",
    "--signal",
    "--kill-after",
})


def _skip_wrapper(tokens: list[str], index: int, name: str) -> int:
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if token.startswith("-"):
            index += 1
            takes_value = token in _WRAPPER_VALUE_OPTS or (
                token.startswith("--") and "=" not in token
            )
            if takes_value and index < len(tokens) and not tokens[index].startswith("-"):
                index += 1
            continue
        break
    if name == "timeout" and index < len(tokens):
        next_name = Path(tokens[index]).name
        if (
            next_name not in _SHELL_NAMES
            and next_name not in _SHELL_WRAPPERS
            and next_name != "eval"
            and not _is_git_executable_token(tokens[index])
        ):
            index += 1
    return index


def _shell_command_executes_git(command: str) -> bool:
    """True when a shell -c/-lc payload executes git (not quoted/echoed text)."""
    try:
        tokens = _shell_payload_tokens(command)
    except ValueError:
        return _command_has_git_executable_token(command) and (
            " -c " in f" {command} " or " -lc " in f" {command} "
        )

    command_position = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_CONNECTORS:
            command_position = True
            index += 1
            continue
        if not command_position:
            index += 1
            continue
        if _env_assignment_name(token):
            index += 1
            continue

        name = Path(token).name
        if name in _SHELL_WRAPPERS:
            index = _skip_wrapper(tokens, index, name)
            continue
        if name == "eval":
            rest = " ".join(tokens[index + 1 :])
            if rest and _payload_has_git_command(rest):
                return True
            command_position = False
            index += 1
            continue
        if name in _SHELL_NAMES:
            payload = _shell_c_payload(tokens, index)
            if payload is not None and _payload_has_git_command(payload):
                return True
        command_position = False
        index += 1
    return False


_SHELL_CONNECTORS = frozenset({";", "|", "||", "&&", "&"})


def _shell_payload_tokens(payload: str) -> list[str]:
    lexer = shlex.shlex(payload, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _shell_c_payload(tokens: list[str], shell_index: int) -> Optional[str]:
    index = shell_index + 1
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index]
        if token in {"-c", "-lc"}:
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if (
            not token.startswith("--")
            and "c" in token[1:]
        ):
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if token in {"-o", "-O"} and index + 1 < len(tokens):
            index += 2
            continue
        index += 1
    return None


def _payload_has_git_command(payload: str) -> bool:
    """Return True if git appears as an executable token in a shell payload."""
    try:
        parts = _shell_payload_tokens(payload)
    except ValueError:
        return bool(re.search(r"(?:^|[;|&\n\t ])git(?:[;|&\n\t ]|$)", payload))

    command_position = True
    index = 0
    while index < len(parts):
        part = parts[index]
        if part in _SHELL_CONNECTORS:
            command_position = True
            index += 1
            continue
        if not command_position:
            index += 1
            continue
        if _env_assignment_name(part):
            index += 1
            continue

        name = Path(part).name
        if name in _SHELL_WRAPPERS:
            index = _skip_wrapper(parts, index, name)
            continue
        if name == "eval":
            rest = " ".join(parts[index + 1 :])
            if rest and _payload_has_git_command(rest):
                return True
            command_position = False
            index += 1
            continue
        if _is_git_executable_token(part):
            return True
        if name in _SHELL_NAMES:
            nested_payload = _shell_c_payload(parts, index)
            if nested_payload is not None and _payload_has_git_command(nested_payload):
                return True
        command_position = False
        index += 1
    return False


def _env_assignment_name(token: str) -> Optional[str]:
    if "=" not in token or token.startswith("="):
        return None
    name, _, _ = token.partition("=")
    if not name or any(ch in name for ch in "-./"):
        return None
    return name


def _env_name_is_unsafe_git(name: str) -> bool:
    return name in _UNSAFE_GIT_ENV_VARS or any(
        name.startswith(prefix) for prefix in _UNSAFE_GIT_CONFIG_ENV_PREFIXES
    )


def _git_config_key(token: str) -> str:
    key, _, _value = token.partition("=")
    return key.strip().lower()


def _is_unsafe_git_config_assignment(token: str) -> bool:
    key = _git_config_key(token)
    return key in _UNSAFE_GIT_CONFIG_KEYS or any(
        key.startswith(prefix) for prefix in _UNSAFE_GIT_CONFIG_KEY_PREFIXES
    )


def _strip_env_prefix(tokens: list[str]) -> tuple[bool, list[str]]:
    unsafe_env = False
    index = 0
    if tokens and tokens[0] == "env":
        index = 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
    while index < len(tokens):
        name = _env_assignment_name(tokens[index])
        if name is None:
            break
        if _env_name_is_unsafe_git(name):
            unsafe_env = True
        index += 1
    return unsafe_env, tokens[index:]


def _parse_git_invocation(tokens: list[str], base_cwd: Path) -> Optional[GitInvocation]:
    if not tokens or not _is_git_executable_token(tokens[0]):
        return None

    git_cwd = base_cwd
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if token == "-C":
            if index + 1 >= len(tokens) or _path_has_shell_expansion(tokens[index + 1]):
                return GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, tokens[index + 1 :], git_cwd)
            git_cwd = _safe_resolve(git_cwd / tokens[index + 1])
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            if _path_has_shell_expansion(token[2:]):
                return GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, tokens[index + 1 :], git_cwd)
            git_cwd = _safe_resolve(git_cwd / token[2:])
            index += 1
            continue
        if token in {"-c", "--config-env"}:
            if index + 1 >= len(tokens):
                return GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, tokens[index + 1 :], git_cwd)
            if _is_unsafe_git_config_assignment(tokens[index + 1]):
                return GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, tokens[index + 1 :], git_cwd)
            index += 2
            continue
        if token.startswith("-c") and token != "-c":
            inline_config = token[2:]
            if _is_unsafe_git_config_assignment(inline_config):
                return GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, tokens[index + 1 :], git_cwd)
            index += 1
            continue
        if token.startswith("--config-env="):
            if _is_unsafe_git_config_assignment(token.removeprefix("--config-env=")):
                return GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, tokens[index + 1 :], git_cwd)
            index += 1
            continue
        if token.startswith("--git-dir") or token.startswith("--work-tree"):
            return GitInvocation(_UNSAFE_GIT_CWD_SUBCOMMAND, tokens[index + 1 :], git_cwd)
        if token.startswith("-"):
            index += 1
            continue
        return GitInvocation(token, tokens[index + 1 :], git_cwd)
    return None


def _git_invocation_is_mutating(invocation: GitInvocation) -> bool:
    subcommand = invocation.subcommand
    if subcommand == _UNSAFE_GIT_CWD_SUBCOMMAND:
        return True
    if subcommand == "branch":
        return _branch_is_mutating(invocation.args)
    if subcommand == "remote":
        return _remote_is_mutating(invocation.args)
    if subcommand in _READ_ONLY_GIT_SUBCOMMANDS:
        return False
    if subcommand in _MUTATING_GIT_SUBCOMMANDS:
        return True
    # Unknown git subcommands are potentially side-effecting; guard them.
    return True


def _git_subcommand_target_is_unverifiable(subcommand: str) -> bool:
    """Return true when a git subcommand might be an alias or extension.

    Unknown git subcommands are treated as mutating above. They can also be
    repo-local or global aliases that shell out to a different repository, so
    the workspace guard cannot verify their target from the current cwd alone.
    """

    return (
        subcommand not in _READ_ONLY_GIT_SUBCOMMANDS
        and subcommand not in _MUTATING_GIT_SUBCOMMANDS
        and subcommand not in {"branch", "remote"}
    )


def _branch_is_mutating(args: list[str]) -> bool:
    if any(arg in _MUTATING_BRANCH_FLAGS for arg in args):
        return True
    # `git branch name` creates a branch; `git branch` and flags like `-vv` list.
    return bool(args and not any(arg.startswith("-") for arg in args))


def _remote_is_mutating(args: list[str]) -> bool:
    non_flags = [arg for arg in args if not arg.startswith("-")]
    return bool(non_flags and non_flags[0] in _MUTATING_REMOTE_SUBCOMMANDS)


def _split_shell_segments(command: str) -> Iterable[str]:
    for segment in command.replace("&&", ";").replace("||", ";").split(";"):
        segment = segment.strip()
        if segment:
            yield segment
