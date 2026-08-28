"""Refuse un-ledgered terminal mutations of the skills tree by background forks.

Every skill mutation the ``skill_manage`` tool performs is captured by
``tools.skill_ledger`` as a whole-directory before/after snapshot, which is
what ``hermes curator rollback`` replays. A ``mv``/``cp``/``rm`` issued
through the ``terminal`` tool bypasses that capture entirely: the skill
package changes on disk and the ledger never learns about it.

That gap is not theoretical. When the curator re-homed a skill's
``references/`` into an umbrella with terminal ``mv`` and then archived the
now-stripped original, the archive entry faithfully recorded what was left
(``files: 1``) and rollback restored a hollow skill — the support files were
only recoverable by hand out of the pre-run ``.curator_backups`` tar
(issue #96962).

The background review / curator fork is the one actor that must never take
that path: it runs headless, with no user to notice the loss, and it already
has a fully ledgered write surface for the same operations
(``skill_manage`` ``write_file`` / ``remove_file`` / ``delete``). So for that
origin only, terminal writes into any skills root are refused.

Like the self-repo git guard this fires before the approval layer: ``--yolo``,
``approvals.mode=off`` and ``force=True`` do not lift it, because none of
those represent a user consenting to an unrecoverable curator write.
Foreground agents are untouched — a user-directed ``mv`` under
``~/.hermes/skills/`` still works exactly as before.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from tools.approval import (
    _deobfuscate_shell_word_for_detection,
    _read_shell_word,
)
from tools.self_repo_guard import (
    _MAX_RECURSION,
    _SHELL_EXECUTABLES,
    _cd_target,
    _command_parts,
    _executable_name,
    _is_within,
    _iter_shell_command_starts,
    _mask_heredocs,
    _operator_before,
    _resolve,
    _scope_keys,
    _shell_script_arg,
)

# Executables where EVERY positional path is written to or destroyed.
# ``mv`` belongs here rather than with the destination-only commands: it
# unlinks its sources, so moving a support file OUT of a skill is exactly the
# un-ledgered strip that produced #96962.
_ALL_ARGS_MUTATE = frozenset({
    "chmod",
    "chown",
    "mkdir",
    "mv",
    "rm",
    "rmdir",
    "shred",
    "tee",
    "touch",
    "truncate",
    "unlink",
})

# Executables that only write their LAST positional. Reading a skill out of
# the tree (``cp <skill>/x.md /tmp/``) stays allowed; writing into it does not.
_DEST_ONLY_MUTATE = frozenset({"cp", "install", "ln", "rsync"})

# In-place editors: harmless without the in-place flag, destructive with it.
_INPLACE_EDITORS = frozenset({"awk", "gawk", "perl", "ruby", "sed"})
_INPLACE_FLAGS = ("-i", "--in-place")

# Interpreters whose payload arrives as one opaque ``-c``/``-e`` string. There
# is no cheap way to tell a read from a write inside it, so any inline program
# that names a protected root is refused; the curator has no reason to reach
# the skills tree through an interpreter at all.
_INLINE_INTERPRETERS = frozenset({
    "node",
    "perl",
    "python",
    "python3",
    "ruby",
})
_INLINE_FLAGS = frozenset({"-c", "-e", "--eval", "--command"})


def _protected_roots() -> list[Path]:
    """Every skills root a background fork must not mutate through a shell."""
    roots: list[Path] = []

    def _add(value) -> None:
        try:
            resolved = Path(value).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return
        if resolved not in roots:
            roots.append(resolved)

    try:
        from agent.skill_utils import get_scan_ordered_skills_dirs
        for entry in get_scan_ordered_skills_dirs():
            _add(entry)
    except Exception:
        try:
            from hermes_constants import get_skills_dir
            _add(get_skills_dir())
        except Exception:
            pass

    try:
        from tools.skills_sync import _get_bundled_dir, _get_optional_dir
        _add(_get_bundled_dir())
        _add(_get_optional_dir())
    except Exception:
        pass

    return roots


def _unquote(word: str) -> str:
    """Strip one matched pair of surrounding shell quotes."""
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        return word[1:-1]
    return word


def _word_variants(command: str, start: int) -> list[tuple[str, str]]:
    """Shell words at *start* as ``(deobfuscated, raw)`` pairs.

    The deobfuscated form is what command classification needs; the raw form
    is what path matching needs. POSIX word reading treats ``\\`` as an escape
    and eats it, which silently destroys every Windows path
    (``C:\\Users\\...\\skills`` becomes ``C:UsersskillsC``), so both spellings
    are kept and either one may match a protected root.
    """
    pairs: list[tuple[str, str]] = []
    cursor = start
    for _ in range(64):
        word_start, word_end, raw_word = _read_shell_word(command, cursor)
        if word_start == word_end:
            break
        if pairs and "\n" in command[cursor:word_start]:
            break
        pairs.append(
            (_deobfuscate_shell_word_for_detection(raw_word), _unquote(raw_word))
        )
        cursor = word_end
    return pairs


def _positional_indices(args: list[str]) -> list[int]:
    """Indices of positional arguments (flags and the ``--`` marker dropped)."""
    indices: list[int] = []
    seen_terminator = False
    for index, arg in enumerate(args):
        if not seen_terminator and arg == "--":
            seen_terminator = True
            continue
        if not seen_terminator and arg.startswith("-") and arg != "-":
            continue
        indices.append(index)
    return indices


def _hits_root(path_str: str, cwd: Path, roots: list[Path]) -> Optional[Path]:
    """Return the protected root containing *path_str*, if any."""
    if not path_str:
        return None
    resolved = _resolve(path_str, cwd)
    for root in roots:
        if _is_within(resolved, root):
            return root
    return None


def _first_hit(paths, cwd: Path, roots: list[Path]) -> Optional[tuple[str, Path]]:
    """First path spelling that lands inside a protected root.

    Each entry may be a single string or a tuple of alternative spellings of
    the same argument (deobfuscated and raw); a hit on either counts.
    """
    for candidate in paths:
        spellings = candidate if isinstance(candidate, tuple) else (candidate,)
        for spelling in spellings:
            root = _hits_root(spelling, cwd, roots)
            if root is not None:
                return spelling, root
    return None


def _dd_outputs(args: list[str], raw_args: list[str]) -> list[tuple[str, ...]]:
    for index, arg in enumerate(args):
        if arg.startswith("of="):
            return [(arg[3:], raw_args[index][3:])]
    return []


def _inline_payloads(args: list[str], raw_args: list[str]) -> list[str]:
    """Program text passed to an interpreter via ``-c`` / ``-e``."""
    payloads: list[str] = []
    for index, arg in enumerate(args):
        if arg in _INLINE_FLAGS and index + 1 < len(args):
            payloads.extend((args[index + 1], raw_args[index + 1]))
        elif arg.startswith("--eval="):
            payloads.append(arg.split("=", 1)[1])
            payloads.append(raw_args[index].split("=", 1)[-1])
    return payloads


def _redirect_targets(command: str) -> list[str]:
    """Paths written by ``>`` / ``>>`` redirects, outside quotes."""
    targets: list[str] = []
    quote: Optional[str] = None
    index = 0
    length = len(command)

    while index < length:
        char = command[index]

        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            index += 2
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        # `2>&1` and friends duplicate a descriptor; they write no file.
        if char == ">":
            cursor = index + 1
            if cursor < length and command[cursor] == ">":
                cursor += 1
            if cursor < length and command[cursor] == "&":
                index = cursor + 1
                continue
            word_start, word_end, raw_word = _read_shell_word(command, cursor)
            if word_start != word_end:
                targets.append(raw_word.strip("'\""))
                index = word_end
                continue
        index += 1

    return targets


def _find_mutation(
    command: str,
    cwd: Path,
    roots: list[Path],
    depth: int = 0,
) -> Optional[str]:
    """Return a short description of the first skills-tree write found."""
    if depth > _MAX_RECURSION:
        return None

    masked_command, heredoc_scripts = _mask_heredocs(command)
    for script in heredoc_scripts:
        found = _find_mutation(script, cwd, roots, depth + 1)
        if found:
            return found

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

        pairs = _word_variants(masked_command, start)
        words = [deobfuscated for deobfuscated, _ in pairs]
        raw_words = [raw for _, raw in pairs]
        env, executable, args = _command_parts(words)
        if executable is None:
            continue

        # ``_command_parts`` peels leading assignments and wrappers (sudo,
        # env, nohup, ...) off the front, so ``args`` is a suffix of ``words``.
        # Recover the offset to keep the raw spellings index-aligned.
        raw_args = raw_words[len(words) - len(args):]

        current_dir = cwd_by_scope[scope]
        cd_target = _cd_target(executable, raw_args, current_dir)
        if cd_target is None:
            cd_target = _cd_target(executable, args, current_dir)
        if cd_target is not None:
            pending_cd[scope] = cd_target
            continue

        name = _executable_name(executable)
        positionals = [(args[i], raw_args[i]) for i in _positional_indices(args)]

        if name in _SHELL_EXECUTABLES:
            for script in {_shell_script_arg(args), _shell_script_arg(raw_args)}:
                if not script:
                    continue
                found = _find_mutation(script, current_dir, roots, depth + 1)
                if found:
                    return found

        targets: list[tuple[str, ...]] = []
        if name in _ALL_ARGS_MUTATE:
            targets = positionals
        elif name in _DEST_ONLY_MUTATE and positionals:
            targets = positionals[-1:]
        elif name == "dd":
            targets = _dd_outputs(args, raw_args)
        elif name in _INPLACE_EDITORS and any(
            arg == flag or arg.startswith(flag)
            for arg in args
            for flag in _INPLACE_FLAGS
        ):
            targets = positionals

        hit = _first_hit(targets, current_dir, roots)
        if hit is not None:
            return f"{name} {hit[0]}"

        if name in _INLINE_INTERPRETERS:
            for payload in _inline_payloads(args, raw_args):
                for root in roots:
                    if str(root) in payload or root.name in payload:
                        return f"{name} -c <inline program touching {root}>"

    hit = _first_hit(_redirect_targets(masked_command), cwd, roots)
    if hit is not None:
        return f"redirect to {hit[0]}"

    return None


def guard_active() -> bool:
    """Whether this call is an autonomous background-review/curator write.

    Foreground and subagent origins are exempt: a user-directed shell edit of
    their own skills tree is legitimate, and a human is present to notice it.
    """
    try:
        from tools.skill_provenance import is_background_review
        return is_background_review()
    except Exception:
        # Fail open rather than break every terminal call if provenance
        # cannot be imported; skill_manage's own guards still apply.
        return False


def detect_skills_tree_mutation(
    command: str,
    cwd: str | None,
    roots: Optional[list[Path]] = None,
) -> tuple[bool, Optional[str]]:
    """Return whether *command* writes into a protected skills root."""
    if not command:
        return False, None

    protected = roots if roots is not None else _protected_roots()
    if not protected:
        return False, None

    base = _resolve(cwd, Path(os.sep)) if cwd else Path.cwd()
    operation = _find_mutation(command, base, protected)
    if operation is None:
        return False, None
    return True, _block_message(operation)


def _block_message(operation: str) -> str:
    return (
        f"Blocked: `{operation}` mutates the skills tree from the background "
        "curator through the terminal, which bypasses the skill ledger. "
        "Nothing done this way appears in `hermes curator rollback`, so a "
        "later archive or delete snapshots an already-stripped package and "
        "the rollback restores a hollow skill (issue #96962). Use the "
        'ledgered tool surface instead: skill_manage(action="write_file", '
        'name=<umbrella>, file_path="references/<topic>.md", '
        "file_content=...) to re-home content, then "
        'skill_manage(action="remove_file", name=<source>, '
        'file_path="references/<topic>.md") to drop the original, and '
        'skill_manage(action="delete", name=<source>, '
        "absorbed_into=<umbrella>) to archive. Reading the tree with "
        "`cat`/`ls`/`find` is still fine."
    )
