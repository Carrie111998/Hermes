"""The NORMAL update route must not install registry sdists either.

`_install_repair._run_install_cmd` was hardened first, and it covers the repair
path.  It does not cover `update_cmd.py`, whose two Python install call sites --
the pip self-upgrade and the Hermes Tools dependency restore loop -- route
through `main._run_package_only_install` instead.  Hardening only the repair
path would have left the normal update route building registry sdists as root
while the finding read as closed, which is why this second chokepoint is
covered separately and tested separately.

The exemption is deliberate and worth stating: the injection is NOT applied to
`_run_install_with_heartbeat`, whose other caller is the Termux psutil path that
builds a patched LOCAL sdist on purpose.  Local paths are exempt from
`--only-binary` in both uv and pip anyway, so that path would still work -- but
narrowing the injection keeps the exemption obvious rather than incidental.
"""

from __future__ import annotations

import pytest

from hermes_cli import main as M

FLAG = "--only-binary=:all:"


@pytest.fixture
def seen(monkeypatch):
    """Capture the argv handed to the heartbeat runner, running nothing."""
    calls: list[list[str]] = []

    def fake_heartbeat(cmd, *, env=None):
        calls.append(list(cmd))

    monkeypatch.setattr(M, "_run_install_with_heartbeat", fake_heartbeat)
    return calls


def test_pip_self_upgrade_is_wheels_only(seen):
    """update_cmd.py's pip self-upgrade (~line 1802)."""
    M._run_package_only_install(["uv", "pip", "install", "--upgrade", "pip"])
    assert FLAG in seen[0]


def test_tools_dependency_restore_is_wheels_only(seen):
    """update_cmd.py's Hermes Tools restore loop (~line 1892).

    This one installs third-party registry packages, so it is the call site
    that actually carried the exposure.
    """
    M._run_package_only_install(
        ["uv", "pip", "install", "some-tool==1.2.3", "--quiet"]
    )
    assert FLAG in seen[0]


def test_flag_is_an_option_to_the_install_verb(seen):
    M._run_package_only_install(["uv", "pip", "install", "some-tool==1.2.3"])
    cmd = seen[0]
    assert cmd[cmd.index("install") + 1] == FLAG


def test_not_duplicated_when_caller_already_hardened(seen):
    M._run_package_only_install(["uv", "pip", "install", FLAG, "uv"])
    assert seen[0].count(FLAG) == 1


def test_non_install_command_untouched(seen):
    M._run_package_only_install(["uv", "pip", "list"])
    assert FLAG not in seen[0]


def test_caller_arguments_survive(seen):
    original = ["uv", "pip", "install", "--force-reinstall", "some-tool", "--quiet"]
    M._run_package_only_install(list(original))
    for token in original:
        assert token in seen[0]
