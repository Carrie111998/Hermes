"""Trajectory saving utilities and static helpers.

_convert_to_trajectory_format stays as an AIAgent method (batch_runner.py
calls agent._convert_to_trajectory_format). Only the static helpers and
the file-write logic live here.
"""

import json
import hashlib
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Notices already delivered, keyed by a caller-supplied string. A datagen run
# appends once per turn; each distinct notice only needs to be reported once
# per process.
_REDIRECT_WARNED: set[str] = set()

# Dropped at the top of ``<HERMES_HOME>/trajectories/``. ``*`` ignores the
# ``.gitignore`` itself, so this never adds a *tracked* file — it only makes
# the directory's contents unstageable if it ever sits inside a work tree.
_GITIGNORE_BODY = (
    "# Hermes trajectory transcripts are private training data.\n"
    "# '*' also ignores this file, so nothing here becomes committable\n"
    "# even when HERMES_HOME sits inside a git work tree.\n"
    "*\n"
)


class _GitRootUndetermined(Exception):
    """The upward walk could not complete, so containment is unknown.

    Distinct from "definitely not in a work tree": an unreadable ancestor or a
    symlink loop means we cannot *prove* the target is safe, and this guard
    exists precisely to avoid writing a full transcript into a checkout on a
    guess.
    """


def _notify_once(key: str, message: str, *args: Any) -> None:
    """Log a warning and put exactly one line on the terminal, once per process.

    ``logger.warning`` alone is invisible: the real CLI logging setup
    (``hermes_logging.setup_logging``) installs no stderr ``StreamHandler``
    unless ``--verbose``, so a redirect the user has to know about only reached
    ``errors.log``. One stderr line per distinct notice keeps the destination
    discoverable without spamming a datagen run that saves every turn. stderr,
    not stdout, so piped trajectory data is never polluted.
    """
    if key in _REDIRECT_WARNED:
        return
    _REDIRECT_WARNED.add(key)
    logger.warning(message, *args)
    _print_notice(message, *args)


def _print_notice(message: str, *args: Any) -> None:
    """Put one line on stderr. Never raises — a notice must not break a turn."""
    try:
        print(f"⚠️  {message % args}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - a notice must never break a turn
        pass


def _notify_every_log_once_on_terminal(key: str, message: str, *args: Any) -> None:
    """Log *every* occurrence, but show the terminal one line per *key*.

    For a repeated failure of the same write: each occurrence belongs in
    ``errors.log`` (that is the forensic record of how many turns were lost),
    while the terminal only needs to say it once per destination so a datagen
    run saving every turn is not flooded.
    """
    logger.warning(message, *args)
    if key in _REDIRECT_WARNED:
        return
    _REDIRECT_WARNED.add(key)
    _print_notice(message, *args)


def _explicit_work_tree() -> Optional[Path]:
    """Honour git's own environment overrides.

    ``GIT_WORK_TREE`` names the work tree outright. ``GIT_DIR`` *alone* makes
    the current directory the work tree — verified with git itself: with only
    ``GIT_DIR`` set, ``git rev-parse --show-toplevel`` reports the CWD and
    ``git add`` stages a file there. CI runners and git wrappers drive a repo
    that way from a directory with no ``.git`` of its own, and the upward walk
    alone would find nothing and leave the transcript exposed.
    """
    work_tree = os.environ.get("GIT_WORK_TREE", "").strip()
    if work_tree:
        return Path(work_tree)
    if os.environ.get("GIT_DIR", "").strip():
        return Path(os.getcwd())
    return None


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk *start* and its parents looking for a ``.git`` entry.

    Returns the directory containing ``.git``, or ``None`` if we reach the
    filesystem root without finding one. Mirrors
    ``agent.prompt_builder._find_git_root``. ``.exists()`` (not ``.is_dir()``)
    because a linked worktree and a submodule both carry ``.git`` as a *file*
    holding a gitdir pointer, and a transcript is just as committable there.

    Raises :class:`_GitRootUndetermined` when the walk cannot complete.
    ``Path.exists()`` swallows ``ENOENT``/``ENOTDIR`` but **not** ``EACCES``,
    and ``Path.resolve()`` raises ``RuntimeError`` (not ``OSError``) on a
    symlink loop, so an unreadable ancestor or a loop used to escape this
    helper entirely and land the transcript in the repo. Neither is "no git
    root here" — the caller has to fail closed instead of guessing.
    """
    try:
        current = start.resolve()
    except (OSError, RuntimeError) as exc:
        raise _GitRootUndetermined(f"cannot resolve {start}: {exc}") from exc

    explicit = _explicit_work_tree()
    if explicit is not None:
        try:
            root = explicit.resolve()
        except (OSError, RuntimeError) as exc:
            raise _GitRootUndetermined(f"cannot resolve {explicit}: {exc}") from exc
        if current == root or root in current.parents:
            return root

    for parent in [current, *current.parents]:
        try:
            if (parent / ".git").exists():
                return parent
        except OSError as exc:
            raise _GitRootUndetermined(f"cannot stat {parent / '.git'}: {exc}") from exc
    return None


def _allow_git_cwd() -> bool:
    """Read ``agent.trajectory_allow_git_cwd`` (default False).

    Config is loaded lazily and read-only, per the ``moa_trace`` precedent:
    this runs once per saved trajectory, not per tool iteration. Any failure
    means "not allowed", which keeps the safe placement on a broken config
    rather than falling back into the checkout.
    """
    try:
        from hermes_cli.config import load_config_readonly

        agent_cfg = (load_config_readonly() or {}).get("agent") or {}
        return bool(agent_cfg.get("trajectory_allow_git_cwd", False))
    except Exception:  # pragma: no cover - defensive
        return False


def _work_tree_key(git_root: Path) -> str:
    """A stable per-work-tree directory name for the relocated dataset.

    The pre-fix path was CWD-relative, so ``projA/`` and ``projB/`` each had
    their own dataset. A single flat file under ``trajectories/`` would merge
    them irreversibly — the entry schema carries no repo/session field, so
    afterwards the two are indistinguishable. Keying the *path* per work tree
    keeps one dataset per repo, the way it was, and follows
    ``agent.moa_trace``'s per-session file naming.

    ``<basename>-<sha256[:8]>``: the basename so the directory is recognisable,
    the digest so ``~/a/proj`` and ``~/b/proj`` cannot land in the same place.
    """
    digest = hashlib.sha256(str(git_root).encode("utf-8", "surrogateescape")).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", git_root.name).strip("-.")
    return f"{name[:32] or 'repo'}-{digest}"


def _contained_relative(path: Path) -> Path:
    """Normalize *path* so joining it can't climb out of ``trajectories/``.

    ``os.path.normpath`` collapses interior ``..`` — ``a/../b/keep.jsonl``
    becomes ``b/keep.jsonl`` instead of silently losing ``b/``, which a plain
    ``".." in path.parts`` test did. Only a path that still escapes after
    normalization falls back to the bare basename.
    """
    normalized = Path(os.path.normpath(str(path)))
    if normalized.is_absolute() or ".." in normalized.parts:
        return Path(path.name)
    return normalized


def _shield_trajectories_dir(base: Path) -> bool:
    """Make ``trajectories/`` contents unstageable, wherever it lives.

    ``HERMES_HOME`` is a documented knob and ``git init ~`` (yadm, dotfiles)
    is a real pattern, so the redirect destination can itself sit inside a work
    tree — moving the transcript from one checkout into another while promising
    the opposite. A self-ignoring ``.gitignore`` closes that: measured with git,
    ``git add -A`` stops staging the transcript and the ``.gitignore`` ignores
    itself, so no tracked file is created. Written unconditionally so a later
    ``git init`` over ``HERMES_HOME`` is covered too. Returns whether the
    shield is in place, so the caller can tell the user the truth.
    """
    ignore = base / ".gitignore"
    try:
        if not ignore.exists():
            ignore.write_text(_GITIGNORE_BODY, encoding="utf-8")
        return True
    except OSError:
        return False


def _warn_pre_existing_dataset(target: Path, redirected: Path) -> None:
    """A dataset already in the checkout stops growing at upgrade — say so.

    The worst outcome of this guard is not an empty dataset (someone notices
    that) but a plausible-looking one that quietly stopped being appended to
    while a pipeline kept reading it. The user's file is deliberately left
    untouched — it is their data — and nothing is written into their checkout,
    since not leaving files in checkouts is this fix's entire point. The signal
    goes out of band instead: terminal, ``errors.log``, and the docs.
    """
    try:
        if not (target.is_file() and target.stat().st_size > 0):
            return
    except OSError:  # pragma: no cover - unreadable target; nothing to claim
        return
    _notify_once(
        f"stale-dataset:{target}",
        "An existing trajectory dataset at %s will NOT receive new entries — "
        "they now go to %s. Your file was neither moved nor modified, so a "
        "pipeline still reading the old path will silently see a dataset that "
        "stopped growing. Point the pipeline at the new path, concatenate the "
        "two, or set agent.trajectory_allow_git_cwd: true in config.yaml to "
        "keep appending to the old one.",
        target, redirected,
    )


def resolve_trajectory_path(filename: str) -> Optional[str]:
    """Keep a relative trajectory filename out of a git work tree.

    ``save_trajectory`` is called from ``finalize_turn``, so its default
    relative filename resolves against whatever CWD the agent was launched in
    — routinely a source checkout. A full verbatim transcript (message text,
    tool results, tool-call arguments) then lands next to the source as an
    untracked file, one ``git add -A`` away from being published.

    So when the resolved target sits inside a git work tree, place the file
    under ``<HERMES_HOME>/trajectories/<work-tree>/`` instead and say so.
    Nothing is dropped or truncated — trajectories are training data and stay
    full-fidelity; only the destination changes, to the same private directory
    the rest of Hermes' state already uses. The ``<work-tree>`` component keeps
    one dataset per repo, as the CWD-relative path did.

    Three escape hatches keep CWD placement supported:

    * an **absolute** ``filename`` is honoured as-is — an explicit path is a
      deliberate choice, not an ambient default;
    * ``agent.trajectory_allow_git_cwd: true`` in ``config.yaml`` restores the
      old behaviour globally;
    * a CWD outside any git work tree (a scratch dir, ``/tmp``, a datagen box)
      is untouched, which is the common datagen shape already.

    Returns the path to write, or ``None`` meaning **do not write**. It fails
    closed: returning the original filename on an unexpected failure would put
    a full transcript in the checkout, which is the one outcome this guard
    exists to prevent. ``None`` is a skipped side effect, never an exception —
    ``save_trajectory`` runs during turn finalization and must not break a turn.
    """
    try:
        path = Path(filename)
        if path.is_absolute():
            return filename

        try:
            cwd = Path(os.getcwd())
        except OSError as exc:
            # Fail closed, and note *why* this one is not merely theoretical:
            # with an unreadable ancestor, ``os.getcwd()`` raises EACCES while a
            # *relative* ``open()`` still succeeds — it resolves against the
            # process's CWD file descriptor, which needs no path traversal.
            # Measured: the write lands in the repo. So a failure here means
            # "containment unknown, write still possible", not "nothing to
            # protect". (A deleted CWD raises ENOENT here and could not be
            # written to anyway, so skipping costs nothing.)
            if _allow_git_cwd():
                return filename
            _notify_once(
                f"nocwd:{filename}",
                "Could not read the current directory (%s), so it is unknown "
                "whether it is inside a git checkout and this trajectory was "
                "NOT saved. A relative write can still land there, so the guard "
                "fails closed. Pass an absolute filename, or set "
                "agent.trajectory_allow_git_cwd: true in config.yaml.",
                exc,
            )
            return None

        target = cwd / path
        try:
            git_root = _find_git_root(target.parent)
        except _GitRootUndetermined as exc:
            if _allow_git_cwd():
                return filename
            _notify_once(
                f"undetermined:{target}",
                "Could not determine whether %s is inside a git work tree (%s), "
                "so this trajectory was NOT saved. Leaving a full transcript in "
                "a checkout is the risk this guard exists to prevent, so it "
                "fails closed. Fix the unreadable path, pass an absolute "
                "filename, or set agent.trajectory_allow_git_cwd: true in "
                "config.yaml to write to the working directory anyway.",
                target, exc,
            )
            return None
        if git_root is None or _allow_git_cwd():
            return filename

        base = get_hermes_home() / "trajectories"
        # One directory per work tree so two repos keep two datasets, plus any
        # relative subdirectory the caller asked for, normalized so ".." can
        # never walk the write back out of the trajectories dir.
        redirected = base / _work_tree_key(git_root) / _contained_relative(path)
        redirected.parent.mkdir(parents=True, exist_ok=True)
        shielded = _shield_trajectories_dir(base)

        # Post-condition on our own destination: is *it* committable?
        try:
            dest_root = _find_git_root(base)
        except _GitRootUndetermined:
            dest_root = None
        if dest_root is None:
            caveat = ""
        elif shielded:
            caveat = (
                f" Note: that destination is itself inside the checkout at {dest_root}, "
                "so a self-ignoring .gitignore was written into the trajectories "
                "directory to keep `git add -A` from staging it; if that directory is "
                "already tracked, or to keep the data out of a checkout entirely, "
                "point HERMES_HOME outside any git work tree."
            )
        else:
            caveat = (
                f" WARNING: that destination is itself inside the checkout at {dest_root} "
                "and the protective .gitignore could not be written, so the transcript "
                "IS committable from there — point HERMES_HOME outside any git work tree."
            )

        _warn_pre_existing_dataset(target, redirected)
        _notify_once(
            f"redirect:{target}->{redirected}",
            # "a git checkout (.git found at ...)" rather than "the git work
            # tree at ...": a stray or broken .git entry redirects too (the safe
            # direction), and claiming a work tree that may not exist would be
            # a lie in exactly the case the user needs to understand.
            "Trajectory would have been written inside a git checkout (.git found "
            "at %s); writing to %s instead so a full transcript is not left in your "
            "checkout. Set agent.trajectory_allow_git_cwd: true in config.yaml "
            "to write to the working directory anyway, or pass an absolute path.%s",
            git_root, redirected, caveat,
        )
        return str(redirected)
    except Exception as exc:
        # Fail closed. The pre-fix behaviour here was to return *filename*,
        # which on a read-only or full HERMES_HOME wrote the transcript into
        # the repo and said so only at debug level — measured, and the exact
        # outcome this guard is supposed to make impossible.
        _notify_once(
            f"resolve-failed:{filename}",
            "Trajectory path resolution failed (%s), so %r was NOT saved rather "
            "than being written to the working directory, which may be a git "
            "checkout. Set agent.trajectory_allow_git_cwd: true in config.yaml "
            "or pass an absolute filename to write there anyway.",
            exc, filename,
        )
        return None


def convert_scratchpad_to_think(content: str) -> str:
    """Convert <REASONING_SCRATCHPAD> tags to <think> tags."""
    if not content or "<REASONING_SCRATCHPAD>" not in content:
        return content
    return content.replace("<REASONING_SCRATCHPAD>", "<think>").replace("</REASONING_SCRATCHPAD>", "</think>")


def describe_trajectory_destination() -> str:
    """Where trajectory saving will actually write, for a startup status line.

    ``AIAgent(save_trajectories=True)`` — the path datagen uses — printed only
    "📝 Trajectory saving enabled", with no destination, so a redirected file
    was findable only by hunting through ``errors.log``. Pure and side-effect
    free apart from the resolver's own directory preparation, so a status line
    can call it safely; a skipped destination is described rather than hidden.
    """
    resolved = resolve_trajectory_path("trajectory_samples.jsonl")
    if resolved is None:
        return "destination unavailable — see warnings"
    if resolved == "trajectory_samples.jsonl":
        # Not redirected: the CWD-relative default, as documented.
        return os.path.join(os.getcwd(), resolved)
    # Redirected. Name the directory, not this one filename — a failed turn
    # writes failed_trajectories.jsonl into the same place.
    return f"{os.path.dirname(resolved)}{os.sep}"


def has_incomplete_scratchpad(content: str) -> bool:
    """Check if content has an opening <REASONING_SCRATCHPAD> without a closing tag."""
    if not content:
        return False
    return "<REASONING_SCRATCHPAD>" in content and "</REASONING_SCRATCHPAD>" not in content


def save_trajectory(trajectory: List[Dict[str, Any]], model: str,
                    completed: bool, filename: str = None):
    """Append a trajectory entry to a JSONL file.

    Args:
        trajectory: The ShareGPT-format conversation list.
        model: Model name for metadata.
        completed: Whether the conversation completed successfully.
        filename: Override output filename. Defaults to trajectory_samples.jsonl
                  or failed_trajectories.jsonl based on ``completed``.

    A *relative* filename that would land inside a git work tree is redirected
    under ``<HERMES_HOME>/trajectories/<work-tree>/`` — see
    :func:`resolve_trajectory_path`. Absolute paths are always honoured as
    given. If the destination cannot be determined safely the save is
    **skipped** rather than written into a checkout; a skipped save is logged
    and reported on the terminal, never raised, because this runs during turn
    finalization.

    A save that is *attempted* and fails (permissions, full disk, a destination
    replaced under us) is reported the same way. Only ``logger.warning`` fired
    before, and the CLI installs no stderr handler without ``--verbose``, so a
    dropped turn was visible only in ``errors.log`` — the same silent-drop the
    resolver deliberately refuses to allow.
    """
    if filename is None:
        filename = "trajectory_samples.jsonl" if completed else "failed_trajectories.jsonl"

    resolved = resolve_trajectory_path(filename)
    if resolved is None:
        # Already reported by the resolver with the reason and the remedy.
        return
    filename = resolved

    entry = {
        "conversations": trajectory,
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "completed": completed,
    }

    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("Trajectory saved to %s", filename)
    except Exception as e:
        # Every occurrence is logged (errors.log is the record of how many
        # turns were lost); the terminal gets one line per destination.
        _notify_every_log_once_on_terminal(
            f"write-failed:{filename}",
            "This trajectory was NOT saved: writing to %s failed (%s). Later "
            "turns will keep trying, and each failure is recorded in "
            "errors.log. Free space or fix permissions on that path, or pass "
            "an absolute filename to write elsewhere.",
            filename, e,
        )
