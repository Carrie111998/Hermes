"""Hermes plugin bridge for the oh-my-hermes git submodule.

The upstream project remains the source of truth. This thin adapter only adds
its bundled Hermes plugin to the host import path, exposes the upstream
registration contract, and provides lifecycle commands for the checked-out
workflow skills.

CI and shallow checkouts do not recurse ``vendor/oh-my-hermes``. Registration
must therefore soft-fail when the submodule is absent so the host stays usable
and the lifecycle CLI can still report status / install guidance.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


PLUGIN_NAME = "oh-my-hermes"
logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _submodule_root() -> Path:
    return _repo_root() / "vendor" / "oh-my-hermes"


def _plugin_bundle_root() -> Path:
    return _submodule_root() / "src" / "plugin_bundle"


def _upstream_skills_root() -> Path:
    return _submodule_root() / "skills"


def submodule_ready() -> bool:
    """True when the vendored OMH plugin bundle directory is present."""
    return _plugin_bundle_root().is_dir()


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
    """Register upstream OMH tools/hooks plus the local lifecycle CLI.

    When ``vendor/oh-my-hermes`` is not checked out (typical for CI shallow
    clones), skip upstream registration with a warning and still expose the
    lifecycle CLI so operators can diagnose / install skills after init.
    """
    if submodule_ready():
        _load_upstream_package().register(ctx)
    else:
        logger.warning(
            "oh-my-hermes submodule not initialized at %s; "
            "skipping upstream tool/hook registration (CLI still available)",
            _plugin_bundle_root(),
        )
    _register_cli(ctx)
