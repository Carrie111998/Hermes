"""
setup.py — wheel/sdist build guard.

pip/PyPI and Homebrew are no longer supported distribution methods for
Hermes Agent (see website/docs/getting-started/platform-support.md). The
wheel would ship without bundled assets (locales, skills, optional-mcps,
web_dist, tui_dist, plugin manifests) since those are resolved at runtime
via env-var overrides set by the nix wrapper or the source-checkout layout.

This file overrides the ``bdist_wheel`` and ``sdist`` setuptools commands.
Sdists remain Nix-only; wheels additionally allow exact in-tree sealed-release
roles. The PEP 517 ``build_wheel`` / ``build_sdist`` hooks in
``setuptools.build_meta`` call these commands internally, so the guard
fires for ``uv build``, ``pip wheel``, ``python -m build``, and direct
``setup.py`` invocations alike.

The approved in-tree consumers of ``build_wheel`` are uv2nix and the sealed,
revision-bound Canonical Writer, production-owner runtime, and Muncho legacy
production release builders. The Nix path sets ``HERMES_NIX_BUILD=1``. Each
sealed release path uses a hash-constrained source snapshot, then seals and
attests its runtime tree; each sets its exact internal wheel-only role around
only that fixed build step.

Editable installs (``uv sync``, ``pip install -e .``, ``nix develop``)
use ``build_editable``, which does NOT call ``bdist_wheel`` — it calls
``build_ext`` in editable mode. So the guard does not affect development.
"""

import importlib.util
import os
from pathlib import Path

from setuptools import setup
from setuptools.command.sdist import sdist


def _load_selective_build_py():
    """Load the source-tree build command without relying on package imports.

    PEP 517 evaluates ``setup.py`` in an isolated build environment where the
    repository root is not guaranteed to be importable.  Loading the command
    by its source path keeps editable installs working while preserving the
    single canonical implementation used by sealed builds.
    """

    module_path = (
        Path(__file__).resolve().parent
        / "scripts"
        / "canary"
        / "selective_build_py.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_hermes_selective_build_py",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load selective build command: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SelectiveBuildPy


SelectiveBuildPy = _load_selective_build_py()

_IN_NIX_BUILD = os.environ.get("HERMES_NIX_BUILD") == "1"
_SEALED_WHEEL_BUILD_ROLES = frozenset({
    "canonical-writer-release-v1",
    "muncho-legacy-production-release-v1",
    "owner-runtime-v1",
})
_IN_SEALED_WHEEL_BUILD = (
    os.environ.get("HERMES_SEALED_RELEASE_BUILD") in _SEALED_WHEEL_BUILD_ROLES
)

_BLOCK_MESSAGE = (
    "Building wheels or sdists for hermes-agent is not supported.\n"
    "Hermes is distributed via the shell installer, Docker image, or Nix.\n"
    "See: https://hermes-agent.nousresearch.com/docs/getting-started/installation\n"
    "\n"
    "If you are developing, use an editable install instead:\n"
    "  uv sync          # or: uv pip install -e .\n"
    "\n"
    "If an approved Nix or sealed release build reached this error, its exact\n"
    "internal build authorization is missing. File a bug."
)


class _GuardedSdist(sdist):
    def run(self, *args, **kwargs):
        if not _IN_NIX_BUILD:
            raise RuntimeError(_BLOCK_MESSAGE)
        return super().run(*args, **kwargs)


cmdclass = {
    "build_py": SelectiveBuildPy,
    "sdist": _GuardedSdist,
}

# bdist_wheel is only available when the `wheel` package is installed.
# setuptools.build_meta.build_wheel() calls it internally, so the guard
# fires for all PEP 517 wheel build paths. Define the subclass only when
# the import succeeds — otherwise a None base class raises TypeError at
# class-definition time, before the cmdclass guard can run.
try:
    from setuptools.command.bdist_wheel import bdist_wheel

    class _GuardedBdistWheel(bdist_wheel):
        def run(self, *args, **kwargs):
            if not (_IN_NIX_BUILD or _IN_SEALED_WHEEL_BUILD):
                raise RuntimeError(_BLOCK_MESSAGE)
            return super().run(*args, **kwargs)

    cmdclass["bdist_wheel"] = _GuardedBdistWheel
except ImportError:
    pass

setup(cmdclass=cmdclass)
