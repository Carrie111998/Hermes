"""Hermes plugin bridge for the oh-my-hermes git submodule.

The upstream project remains the source of truth. This thin adapter only adds
its bundled Hermes plugin to the host import path, exposes the upstream
registration contract, and provides lifecycle commands for the checked-out
workflow skills.
"""

from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_NAME = "oh-my-hermes"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _submodule_root() -> Path:
    return _repo_root() / "vendor" / "oh-my-hermes"


def _plugin_bundle_root() -> Path:
    return _submodule_root() / "src" / "plugin_bundle"


def _upstream_skills_root() -> Path:
    return _submodule_root() / "skills"


def _load_upstream_package():
    bundle = _plugin_bundle_root()
    if not bundle.is_dir():
        raise RuntimeError(f"oh-my-hermes submodule is not initialized: {bundle}")
    bundle_text = str(bundle)
    if bundle_text not in sys.path:
        sys.path.insert(0, bundle_text)
    import omh  # type: ignore[import-not-found]

    return omh


def _register_cli(ctx) -> None:
    try:
        from .cli import register_cli, oh_my_hermes_command
    except ImportError:
        return
    ctx.register_cli_command(
        name=PLUGIN_NAME,
        help="Manage the vendored oh-my-hermes workflow pack",
        setup_fn=register_cli,
        handler_fn=oh_my_hermes_command,
        description=(
            "Check the oh-my-hermes submodule, install its workflow skills into "
            "the active Hermes profile, and report readiness."
        ),
    )


def register(ctx) -> None:
    """Register upstream OMH tools/hooks plus the local lifecycle CLI."""
    _load_upstream_package().register(ctx)
    _register_cli(ctx)
