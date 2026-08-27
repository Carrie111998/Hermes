"""Regression test: the installer's npm step must expose a python3 to node-gyp.

node-pty has no prebuild on fresh Node ABIs, so `npm install` always compiles
it via node-gyp — which needs a python3 on PATH. Minimal Docker images ship
none, and the failure was invisible: ``--silent`` swallowed node-gyp's output
entirely, the captured mktemp log stayed empty, and the BuildKit failure had
no diagnostics at all (#96072). The installer's own venv provides a python3;
the npm step now puts that bin dir on PATH for its duration only.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_npm_install_injects_installer_python_on_path() -> None:
    text = INSTALL_SH.read_text()

    # The npm step resolves the installer's python bin dir and prepends it to
    # PATH inside the subshell, scoped to the npm call only.
    assert '_py_bin="$(dirname "${PYTHON_PATH:-}")"' in text
    assert 'PATH="$_py_bin:$PATH"' in text
    # The injection guards on an actual python3 executable there, so a missing
    # or unset PYTHON_PATH degrades to today's behavior instead of breaking.
    assert '[ -x "$_py_bin/python3" ]' in text
    # The failure branch (empty-log cat, #87340) is preserved.
    assert 'log_error "npm install failed or timed out; Node.js dependencies were not installed"' in text


def test_npm_injection_is_scoped_to_the_subshell() -> None:
    text = INSTALL_SH.read_text()

    # The PATH assignment lives inside the `if ! ( ... ) >"$npm_log"` subshell
    # that wraps the npm call — it must not leak into the rest of the install
    # (e.g. shadowing a system python3 for later steps).
    assert 'if ! (\n                _py_bin=' in text
    assert 'run_with_timeout "$NODE_DEPS_TIMEOUT" npm install --silent' in text
