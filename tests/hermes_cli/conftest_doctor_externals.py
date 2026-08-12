"""Shared stubs that keep ``hermes_cli.doctor.run_doctor`` off slow host probes.

Every test module that calls ``run_doctor`` pays for the same three real
subprocesses, none of which any of those tests assert on. Measured on Windows
they cost ~31s of a ~39s ``run_doctor`` call:

  * ``install_doctor.probe``            ~16.6s
  * ``gh auth status --json ...``       ~10.7s
  * ``agent-browser --version``          ~3.9s

That put every ``run_doctor`` test over the repo's 30s ``--timeout`` cap from
``pyproject.toml`` (``addopts = "-m 'not integration' --timeout=30
--timeout-method=thread"``), so those files could only pass under an explicit
command-line ``--timeout=600`` -- i.e. they were silently red in a default
run, and because ``--timeout-method=thread`` kills the interpreter, the first
one to blow took the rest of the file with it.

The probes are host state rather than behaviour under test, so removing them
makes the tests deterministic as well as fast.

Usage -- in a module that calls ``run_doctor``::

    from tests.hermes_cli.conftest_doctor_externals import stub_doctor_externals

    @pytest.fixture(autouse=True)
    def _stub_doctor_externals(monkeypatch):
        stub_doctor_externals(monkeypatch)

Importing this module matters as much as using the fixture, because of the
warm-up imports below.

Deliberate duplication: ``tests/hermes_cli/test_doctor.py`` carries its own
copy of these stubs (``b59c1d852`` + ``dca0d179a``) rather than importing this
module. Consolidating the two was tried and reverted -- an interleaved A/B of
the whole file, both trees under identical load, had the refactored version
blow the 30s cap while the original passed 83/83. That file has several tests
sitting at 13-17s against the cap, so it has no headroom to spend on churn.
Keep the two copies in sync by hand; if you change one, change the other.
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Warm-up imports. Do not delete: these are load-bearing, not conveniences.
#
# ``run_doctor`` imports about three dozen modules lazily (function-local
# ``import`` statements -- ``ast`` them out of ``hermes_cli/doctor.py`` if you
# want the list). Two of them dominate, and both are session-shared costs that
# a lazy import bills entirely to whichever test happens to reach them first:
#
#   * ``model_tools``       -- its module body runs ``discover_builtin_tools()``,
#                              the whole builtin tool registry. ~66s cold on
#                              Windows, ~13s warm. Also pulls in
#                              ``tools.browser_tool``, which ``run_doctor``
#                              imports separately for its Chromium check and
#                              which is ~13s on its own.
#   * ``hermes_cli.gateway`` -- reached via ``_check_gateway_service_linger``.
#                              Measured at ~50s on a loaded box.
#
# Either one alone is enough to push the first ``run_doctor`` test past the 30s
# cap. Importing them here moves the cost into collection, which pytest-timeout
# does not cover, so no single test is billed for it.
#
# ``tests/hermes_cli/test_doctor.py`` already imports both at module scope for
# exactly this reason; this block is what gives the sibling modules the same
# protection. Note that stubbing ``model_tools`` into ``sys.modules`` (as those
# siblings do per-test) does NOT substitute for the real import -- the stub
# suppresses the call, but only for tests that install it, and it does nothing
# for ``tools.browser_tool`` or ``hermes_cli.gateway``.
import hermes_cli.gateway  # noqa: F401
import model_tools  # noqa: F401


def fake_install_probe(names, entrypoints, python=None, env=None):
    """Stand in for ``install_doctor.probe`` without spawning an interpreter.

    The real probe launches ``sys.executable -c <script>`` which imports every
    declared package from a neutral cwd. On this host that single spawn costs
    ~16s per call, and ``run_doctor`` calls it once per invocation -- it was
    the largest slice of the ~60s each ``run_doctor`` test used to take. It
    also makes the result depend on whatever happens to be installed in the
    developer's venv, so the doctor section it feeds was never deterministic.

    Returning an all-clean result keeps the real ``_collect``/``analyze``/
    render path under test and only removes the subprocess.
    """
    return {
        "resolved": {n: {"ok": True, "origin": f"<stub>/{n}", "error": None} for n in names},
        "imports": {n: {"ok": True, "error": None} for n in entrypoints},
        "executable": sys.executable,
    }


def fast_agent_browser_runnable(path):
    """``hermes_constants.agent_browser_runnable`` minus the ``--version`` spawn.

    The real helper execs the resolved binary to reject dangling symlinks;
    on Windows that npm ``.CMD`` shim costs ~4s per call. The cheap checks it
    performs first (npx form, exists, executable) are kept verbatim so the
    dangling-symlink semantics the callers rely on still hold.
    """
    if not path:
        return False
    if " " in path and path.split()[0].endswith("npx"):
        return True
    return os.path.exists(path) and os.access(path, os.X_OK)


def stub_doctor_externals(monkeypatch):
    """Point ``run_doctor``'s three slowest host probes at in-process stubs.

    Call this from an ``autouse`` fixture.

    None of the modules using this helper test the gh probe itself, so it is
    always stubbed here. ``test_doctor.py`` does test it, which is why its own
    fixture carries an ``exercises_real_gh_probe`` opt-out.
    """
    from hermes_cli import doctor as doctor_mod
    from hermes_cli import install_doctor as _install_doctor

    _real_section_lines = _install_doctor.doctor_section_lines

    def _stubbed_section_lines(probe_fn=None, root=None):
        return _real_section_lines(probe_fn=fake_install_probe, root=root)

    monkeypatch.setattr(_install_doctor, "doctor_section_lines", _stubbed_section_lines)
    monkeypatch.setattr(doctor_mod, "agent_browser_runnable", fast_agent_browser_runnable)

    # ``dca0d179a`` hoisted ``_gh_authenticated`` out of ``run_doctor`` to
    # module level precisely so it has a patchable seam; use it rather than
    # wrapping ``subprocess.run`` and having to reason about wrapper ordering.
    monkeypatch.setattr(doctor_mod, "_gh_authenticated", lambda: False)
