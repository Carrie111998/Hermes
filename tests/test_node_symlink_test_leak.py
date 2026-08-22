"""Regression tests: a test run must never write node/npm/npx symlinks into
the developer's real command dir.

Background. ``tests/conftest.py`` sandboxes ``HERMES_HOME`` but deliberately
NOT ``HOME``. Any test that reaches a real dependency install (e.g.
``hermes_cli.dep_ensure.ensure_dependency`` shelling out to
``scripts/install.sh``) therefore ran the installer's final "link node/npm/npx
onto PATH" step against the developer's actual ``~/.local/bin``, pointing the
links at the per-test tmpdir. Pytest deletes that tmpdir on teardown, leaving
``node``/``npx`` unresolvable on the machine.

The failure self-perpetuated: with the links broken, the next run's installer
found no node, installed one, and relinked to a fresh tmpdir.

Two independent defences are asserted here — the env flag conftest sets, and
the shell scripts' own refusal, which holds even if the flag is not passed.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
NODE_BOOTSTRAP = REPO_ROOT / "scripts" / "lib" / "node-bootstrap.sh"


def test_conftest_sets_node_skip_links():
    """The sandbox fixture must disarm installer symlinking for every test."""
    assert os.environ.get("HERMES_NODE_SKIP_LINKS") == "1"


def test_test_isolation_marker_is_set():
    """The shell guard keys off this marker; it must actually be present."""
    assert os.environ.get("HERMES_TEST_ISOLATION")


def _run_guard(env_overrides):
    """Source node-bootstrap.sh and ask its guard for a verdict.

    Returns (exit_code, reason). Exit 0 means "linking is blocked".
    """
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    env.update(env_overrides)
    result = subprocess.run(
        ["bash", "-c", f'source "{NODE_BOOTSTRAP}" >/dev/null 2>&1; '
                       f'_nb_node_links_blocked_reason'],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout.strip()


@pytest.mark.parametrize(
    "overrides,expect_blocked",
    [
        ({"HERMES_NODE_SKIP_LINKS": "1", "HERMES_HOME": "/opt/hermes"}, True),
        ({"HERMES_TEST_ISOLATION": "/tmp/x/hermes_test", "HERMES_HOME": "/opt/hermes"}, True),
        ({"HERMES_HOME": "/var/tmp/p/pytest-of-jiddy/pytest-1/t0/hermes_test"}, True),
        ({"HERMES_HOME": "/tmp/pytest-of-someone/pytest-9/hermes_test"}, True),
        # A real install must still link — the guard must not be a blanket off.
        ({"HERMES_HOME": "/home/someone/.hermes"}, False),
        ({"HERMES_HOME": "/opt/hermes"}, False),
    ],
)
def test_node_bootstrap_guard_verdicts(overrides, expect_blocked):
    code, reason = _run_guard(overrides)
    if expect_blocked:
        assert code == 0, f"expected linking to be BLOCKED for {overrides}, got exit {code}"
        assert reason, "a block must explain itself"
    else:
        assert code == 1, (
            f"expected linking to be ALLOWED for {overrides}, but it was blocked: {reason}"
        )


def test_node_bootstrap_link_block_is_guarded():
    """The ln -sf block must sit behind the guard, not run unconditionally."""
    text = NODE_BOOTSTRAP.read_text(encoding="utf-8")
    assert "_nb_node_links_blocked_reason" in text
    _, _, after = text.partition("_nb_node_links_blocked_reason)\"; then")
    assert 'ln -sf "$HERMES_HOME/node/bin/node"' in after, (
        "the node symlink must be created only on the guard's else branch"
    )


def test_install_sh_link_block_is_guarded():
    """install.sh carries its own copy of the link logic — guard it too.

    It is not sourced here: install.sh runs its entrypoint on source, so this
    asserts against the script text, matching test_install_sh_node_global_prefix.py.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "node_links_blocked_reason()" in text, "guard helper must be defined"
    assert 'if node_link_skip="$(node_links_blocked_reason)"; then' in text, (
        "the link block must consult the guard"
    )
    # Every node link line must live after the guard call.
    guard_at = text.index('node_link_skip="$(node_links_blocked_reason)"')
    for line in (
        'ln -sf "$HERMES_HOME/node/bin/node" "$node_link_dir/node"',
        'ln -sf "$HERMES_HOME/node/bin/npm"  "$node_link_dir/npm"',
        'ln -sf "$HERMES_HOME/node/bin/npx"  "$node_link_dir/npx"',
    ):
        assert text.index(line) > guard_at, f"unguarded link: {line}"


def test_both_scripts_agree_on_the_markers():
    """The two copies must block on the same conditions or one becomes the gap."""
    for path in (INSTALL_SH, NODE_BOOTSTRAP):
        text = path.read_text(encoding="utf-8")
        assert "HERMES_NODE_SKIP_LINKS" in text
        assert "HERMES_TEST_ISOLATION" in text
        assert "*/pytest-of-*" in text
        assert "*/hermes_test" in text
