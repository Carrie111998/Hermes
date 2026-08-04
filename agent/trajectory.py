"""Trajectory saving utilities and static helpers.

_convert_to_trajectory_format stays as an AIAgent method (batch_runner.py
calls agent._convert_to_trajectory_format). Only the static helpers and
the file-write logic live here.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Redirects already reported, keyed by (source, target). A datagen run appends
# once per turn; the destination only needs to be announced once per process.
_REDIRECT_WARNED: set[tuple[str, str]] = set()


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk *start* and its parents looking for a ``.git`` entry.

    Returns the directory containing ``.git``, or ``None`` if we reach the
    filesystem root without finding one. Mirrors
    ``agent.prompt_builder._find_git_root``. ``.exists()`` (not ``.is_dir()``)
    because a linked worktree and a submodule both carry ``.git`` as a *file*
    holding a gitdir pointer, and a transcript is just as committable there.
    """
    try:
        current = start.resolve()
    except OSError:  # pragma: no cover - defensive: unresolvable cwd
        return None
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
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


def resolve_trajectory_path(filename: str) -> str:
    """Keep a relative trajectory filename out of a git work tree.

    ``save_trajectory`` is called from ``finalize_turn``, so its default
    relative filename resolves against whatever CWD the agent was launched in
    — routinely a source checkout. A full verbatim transcript (message text,
    tool results, tool-call arguments) then lands next to the source as an
    untracked file, one ``git add -A`` away from being published.

    So when the resolved target sits inside a git work tree, place the file
    under ``<HERMES_HOME>/trajectories/`` instead and say so. Nothing is
    dropped or truncated — trajectories are training data and stay
    full-fidelity; only the destination changes, to the same private directory
    the rest of Hermes' state already uses.

    Three escape hatches keep CWD placement supported:

    * an **absolute** ``filename`` is honoured as-is — an explicit path is a
      deliberate choice, not an ambient default;
    * ``agent.trajectory_allow_git_cwd: true`` in ``config.yaml`` restores the
      old behaviour globally;
    * a CWD outside any git work tree (a scratch dir, ``/tmp``, a datagen box)
      is untouched, which is the common datagen shape already.

    Returns the path to write, as a string. On any unexpected failure the
    original *filename* is returned so tracing never breaks a turn.
    """
    try:
        path = Path(filename)
        if path.is_absolute():
            return filename

        target = Path(os.getcwd()) / path
        git_root = _find_git_root(target.parent)
        if git_root is None or _allow_git_cwd():
            return filename

        base = get_hermes_home() / "trajectories"
        # Preserve any relative subdirectory the caller asked for so two
        # callers using the same basename don't collide, but never let ".."
        # walk the write back out of the trajectories dir.
        relative = path if ".." not in path.parts else Path(path.name)
        redirected = base / relative
        redirected.parent.mkdir(parents=True, exist_ok=True)

        key = (str(target), str(redirected))
        if key not in _REDIRECT_WARNED:
            _REDIRECT_WARNED.add(key)
            logger.warning(
                "Trajectory would have been written inside the git work tree at %s; "
                "writing to %s instead so a full transcript is not left in your "
                "checkout. Set agent.trajectory_allow_git_cwd: true in config.yaml "
                "to write to the working directory anyway, or pass an absolute path.",
                git_root, redirected,
            )
        return str(redirected)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Trajectory path resolution failed (%s); using %s", e, filename)
        return filename


def convert_scratchpad_to_think(content: str) -> str:
    """Convert <REASONING_SCRATCHPAD> tags to <think> tags."""
    if not content or "<REASONING_SCRATCHPAD>" not in content:
        return content
    return content.replace("<REASONING_SCRATCHPAD>", "<think>").replace("</REASONING_SCRATCHPAD>", "</think>")


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
    under ``<HERMES_HOME>/trajectories/`` — see :func:`resolve_trajectory_path`.
    Absolute paths are always honoured as given.
    """
    if filename is None:
        filename = "trajectory_samples.jsonl" if completed else "failed_trajectories.jsonl"

    filename = resolve_trajectory_path(filename)

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
        logger.warning("Failed to save trajectory: %s", e)
