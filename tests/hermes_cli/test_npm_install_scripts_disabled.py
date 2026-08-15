"""`npm` lifecycle scripts must stay disabled unless a caller opts out loudly.

Why this file exists
--------------------
`hermes update` runs unattended, and on server installs it runs as root.  npm
`preinstall`/`install`/`postinstall` scripts execute arbitrary code from the
entire transitive closure of the registry, so leaving them enabled means the
trust boundary is every maintainer account behind every dependency — a set
that changes without our involvement.  `--ignore-scripts` removes install-time
code execution outright.  Version and hash pinning do not: they prevent
substitution of an artifact, not execution of the artifact's build hooks.

These tests pin the *security property*, not the cosmetic argv.  An exact-argv
assertion elsewhere would be "just updated to match" by whoever removed the
flag; the membership assertions below fail loudly instead.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import main as M


def _capture(monkeypatch, *, ci_fails: bool = False) -> list[list[str]]:
    """Record every npm argv the helper spawns, running none of them."""
    seen: list[list[str]] = []

    def fake_run(cmd, *, cwd, env, capture_output):
        seen.append(list(cmd))
        # Fail only the `npm ci` attempt when the caller wants to exercise the
        # `npm install` fallback; succeed otherwise so exactly one call is made.
        failed = ci_fails and "ci" in cmd
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, stdout="", stderr="")

    monkeypatch.setattr(M, "_run_npm_watching_for_engine_failure", fake_run)
    return seen


def test_ci_branch_disables_install_scripts_by_default(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    M._run_npm_install_deterministic("npm", tmp_path)

    assert len(seen) == 1
    assert "ci" in seen[0]
    assert "--ignore-scripts" in seen[0]


def test_install_fallback_also_disables_install_scripts(tmp_path, monkeypatch):
    """The fallback is the branch that matters most.

    It is reached precisely when `npm ci` refused because the lockfile is out
    of sync — i.e. when npm is free to resolve versions the lockfile never
    vouched for.  Hardening only the `ci` branch would leave scripts enabled on
    the one path where the dependency set is least verified.
    """
    seen = _capture(monkeypatch, ci_fails=True)
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    M._run_npm_install_deterministic("npm", tmp_path)

    fallback = [c for c in seen if "install" in c]
    assert fallback, f"expected an `npm install` fallback, saw: {seen}"
    for cmd in fallback:
        assert "--ignore-scripts" in cmd


def test_no_lockfile_path_disables_install_scripts(tmp_path, monkeypatch):
    """With no lockfile the helper goes straight to `npm install`."""
    seen = _capture(monkeypatch)

    M._run_npm_install_deterministic("npm", tmp_path)

    assert len(seen) == 1
    assert "install" in seen[0]
    assert "--ignore-scripts" in seen[0]


def test_desktop_opt_out_is_the_only_way_to_re_enable(tmp_path, monkeypatch):
    """`allow_install_scripts=True` is the narrow, deliberate exception.

    The desktop/Electron build installs the whole root rather than the update
    path's scoped workspace subset, and Electron's runtime binary plus
    node-pty's native module are install-script-only.  That opt-out is
    explicit at the call site; nothing else may re-enable scripts implicitly.
    """
    seen = _capture(monkeypatch)
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    M._run_npm_install_deterministic("npm", tmp_path, allow_install_scripts=True)

    assert len(seen) == 1
    assert "--ignore-scripts" not in seen[0]


@pytest.mark.parametrize("extra", [(), ("--workspace", "web"), ("--silent",)])
def test_flag_survives_arbitrary_caller_extra_args(tmp_path, monkeypatch, extra):
    """Callers pass their own argv; the control must not be positional-fragile."""
    seen = _capture(monkeypatch)
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    M._run_npm_install_deterministic("npm", tmp_path, extra_args=extra)

    assert "--ignore-scripts" in seen[0]
    for token in extra:
        assert token in seen[0]
