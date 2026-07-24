"""
setup.py — wheel/sdist build guard.

pip/PyPI and Homebrew are no longer supported distribution methods for
Hermes Agent (see website/docs/getting-started/platform-support.md). The
supported Nix wheel still needs complete runtime data, so this file also
enumerates the non-package skills, catalogs, locales, and installer scripts
without flattening their source layout.

This file overrides the ``bdist_wheel`` and ``sdist`` setuptools commands
to raise an error when run outside a Nix build. The PEP 517
``build_wheel`` / ``build_sdist`` hooks in
``setuptools.build_meta`` call these commands internally, so the guard
fires for ``uv build``, ``pip wheel``, ``python -m build``, and direct
``setup.py`` invocations alike.

The one legitimate consumer of ``build_wheel`` is uv2nix, which calls
``setuptools.build_meta.build_wheel`` (→ ``bdist_wheel``) inside a Nix
build sandbox. ``nix/python.nix`` sets ``HERMES_NIX_BUILD=1`` on the
Hermes package derivation, so only that build may create an artifact.

Editable installs (``uv sync``, ``pip install -e .``, ``nix develop``)
use ``build_editable``, which does NOT call ``bdist_wheel`` — it calls
``build_ext`` in editable mode. So the guard does not affect development.
"""

import os
from pathlib import Path

from setuptools import setup
from setuptools.command.sdist import sdist

_IN_NIX_BUILD = os.environ.get("HERMES_NIX_BUILD") == "1"
_PROJECT_ROOT = Path(__file__).resolve().parent
_RUNTIME_DATA_ROOTS = ("locales", "optional-mcps", "optional-skills", "skills")
_IGNORED_DATA_PARTS = {"__pycache__", "node_modules"}

_BLOCK_MESSAGE = (
    "Building wheels or sdists for hermes-agent is not supported.\n"
    "Hermes is distributed via the shell installer, Docker image, or Nix.\n"
    "See: https://hermes-agent.nousresearch.com/docs/getting-started/installation\n"
    "\n"
    "If you are developing, use an editable install instead:\n"
    "  uv sync          # or: uv pip install -e .\n"
    "\n"
    "If you are building with Nix (uv2nix), this error should not fire —\n"
    "the Hermes Nix derivation sets HERMES_NIX_BUILD=1. If it does, file a bug."
)


class _GuardedSdist(sdist):
    def run(self, *args, **kwargs):
        if not _IN_NIX_BUILD:
            raise RuntimeError(_BLOCK_MESSAGE)
        return super().run(*args, **kwargs)


cmdclass = {"sdist": _GuardedSdist}


def _runtime_data_files():
    """Enumerate non-package runtime trees without flattening their layout."""
    grouped = {}

    for root_name in _RUNTIME_DATA_ROOTS:
        root = _PROJECT_ROOT / root_name
        for source_path in sorted(root.rglob("*")):
            relative_path = source_path.relative_to(_PROJECT_ROOT)
            if (
                not source_path.is_file()
                or any(part in _IGNORED_DATA_PARTS for part in relative_path.parts)
                or source_path.suffix in {".pyc", ".pyo"}
            ):
                continue
            target = relative_path.parent.as_posix()
            grouped.setdefault(target, []).append(relative_path.as_posix())

    install_scripts = [
        path
        for name in ("install.sh", "install.ps1")
        if (path := _PROJECT_ROOT / "scripts" / name).is_file()
    ]
    if install_scripts:
        grouped["hermes-agent/scripts"] = [
            path.relative_to(_PROJECT_ROOT).as_posix() for path in install_scripts
        ]

    return sorted(grouped.items())

# bdist_wheel is only available when the `wheel` package is installed.
# setuptools.build_meta.build_wheel() calls it internally, so the guard
# fires for all PEP 517 wheel build paths. Define the subclass only when
# the import succeeds — otherwise a None base class raises TypeError at
# class-definition time, before the cmdclass guard can run.
try:
    from setuptools.command.bdist_wheel import bdist_wheel

    class _GuardedBdistWheel(bdist_wheel):
        def run(self, *args, **kwargs):
            if not _IN_NIX_BUILD:
                raise RuntimeError(_BLOCK_MESSAGE)
            return super().run(*args, **kwargs)

    cmdclass["bdist_wheel"] = _GuardedBdistWheel
except ImportError:
    pass

setup(cmdclass=cmdclass, data_files=_runtime_data_files())
