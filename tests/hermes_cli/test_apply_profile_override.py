"""Regression tests for _apply_profile_override HERMES_HOME guard (issue #22502).

When HERMES_HOME is set to the hermes root (e.g. systemd hardcodes
HERMES_HOME=/root/.hermes), _apply_profile_override must still read
active_profile and update HERMES_HOME to the profile directory.

When HERMES_HOME is already a profile directory (.../profiles/<name>),
_apply_profile_override must trust it and return without re-reading
active_profile (child-process inheritance contract).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace


def _run_apply_profile_override(
    tmp_path, monkeypatch, *, hermes_home: str | None, active_profile: str | None,
    argv: list[str] | None = None,
):
    """Run _apply_profile_override in isolation.

    Returns the value of os.environ["HERMES_HOME"] after the call,
    or None if unset.
    """
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir(parents=True, exist_ok=True)

    if active_profile is not None:
        (hermes_root / "active_profile").write_text(active_profile)

    if active_profile and active_profile != "default":
        (hermes_root / "profiles" / active_profile).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if hermes_home is not None:
        monkeypatch.setenv("HERMES_HOME", hermes_home)
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)

    monkeypatch.setattr(sys, "argv", argv or ["hermes", "gateway", "start"])

    from hermes_cli.main import _apply_profile_override
    _apply_profile_override()

    return os.environ.get("HERMES_HOME")


class TestApplyProfileOverrideHermesHomeGuard:
    """Regression guard for issue #22502.

    Verifies that HERMES_HOME pointing to the hermes root does NOT suppress
    the active_profile check, while HERMES_HOME already pointing to a
    profile directory IS trusted as-is.
    """

    def test_hermes_home_at_root_with_active_profile_is_redirected(
        self, tmp_path, monkeypatch
    ):
        """HERMES_HOME=/root/.hermes + active_profile=coder must redirect
        HERMES_HOME to .../profiles/coder.

        Bug scenario from #22502: systemd sets HERMES_HOME to the hermes root
        and the user switches to a profile via `hermes profile use`.
        Before the fix, the guard returned early and active_profile was ignored.
        """
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)

        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=str(hermes_root),
            active_profile="coder",
        )

        assert result is not None, "HERMES_HOME must be set after profile redirect"
        assert "profiles" in result, (
            f"Expected HERMES_HOME to point into profiles/ dir, got: {result!r}"
        )
        assert result.endswith("coder"), (
            f"Expected HERMES_HOME to end with 'coder', got: {result!r}"
        )


    def test_sudo_explicit_profile_resolves_invoking_users_profile(self, tmp_path, monkeypatch):
        """sudo elias ... should resolve `-p elias` under SUDO_USER, not root."""
        root_home = tmp_path / "root"
        user_home = tmp_path / "home" / "hermes"
        profile_dir = user_home / ".hermes" / "profiles" / "elias"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (root_home / ".hermes").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: root_home)
        monkeypatch.setenv("SUDO_USER", "hermes")
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "elias", "gateway", "install", "--system"])

        import pwd

        monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(user_home)))

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") == str(profile_dir)
        assert sys.argv == ["hermes", "gateway", "install", "--system"]




class TestSupervisedChildIgnoresStickyProfile:
    """The reserved default gateway s6 slot must not follow active_profile.

    Inside the Docker s6 image the ``gateway-default`` service slot runs a
    bare ``hermes gateway run`` (no ``-p``) to mean "the root HERMES_HOME
    profile". The run-script exports ``HERMES_S6_SUPERVISED_CHILD=1``.
    Without a guard, ``_apply_profile_override`` would read the sticky
    ``active_profile`` file (set by e.g. the dashboard profile switcher) and
    redirect the reserved default gateway into that profile — producing a
    duplicate gateway for the active profile and no real default gateway.
    """


    def test_non_supervised_run_still_follows_active_profile(
        self, tmp_path, monkeypatch
    ):
        """Without the sentinel, a normal `hermes gateway run` still honors
        active_profile — the guard is scoped strictly to supervised children."""
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            hermes_home=None,
            active_profile="briefer",
            argv=["hermes", "gateway", "run"],
        )

        assert result is not None
        assert result.endswith("briefer")

    def test_supervised_named_profile_flag_still_wins(self, tmp_path, monkeypatch):
        """A supervised named-profile slot passes ``-p <name>`` explicitly;
        that must still resolve (the sentinel guard only skips the sticky
        active_profile fallback, never an explicit flag)."""
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        (hermes_root / "active_profile").write_text("briefer")
        (hermes_root / "profiles" / "briefer").mkdir(parents=True, exist_ok=True)
        (hermes_root / "profiles" / "coder").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HERMES_S6_SUPERVISED_CHILD", "1")
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "coder", "gateway", "run"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        result = os.environ.get("HERMES_HOME")
        assert result is not None
        assert result.endswith("coder")


class TestRunpyModuleExecutionRegistersCanonicalAlias:
    """``python -m hermes_cli.main`` must execute the module exactly once.

    runpy runs the module as ``__main__`` WITHOUT registering it under its
    canonical name in sys.modules. The first lazy
    ``from hermes_cli.main import ...`` in the process (gateway/code_skew.py's
    boot fingerprint is the first such import in a gateway process) then
    re-executed the whole module, running _apply_profile_override() a second
    time against the already-stripped argv. With a sticky active_profile
    present, that re-resolved HERMES_HOME to the named profile and silently
    re-scoped a ``-p default`` gateway (the systemd unit's ExecStart) to that
    profile — losing the multiplexer's port-binding platforms (api_server
    8642) after every in-process restart.
    """

    def test_lazy_import_does_not_rescope_launcher_profile(self, tmp_path):
        hermes_root = tmp_path / ".hermes"
        (hermes_root / "profiles" / "biz-assistant").mkdir(parents=True)
        (hermes_root / "active_profile").write_text("biz-assistant")
        script = textwrap.dedent(
            f"""
            import importlib.util
            import os
            import sys

            os.environ["HERMES_HOME"] = {str(hermes_root)!r}
            sys.argv = ["hermes_cli.main", "-p", "default", "gateway", "run"]

            spec = importlib.util.find_spec("hermes_cli.main")
            source = open(spec.origin, encoding="utf-8").read()
            # Neutralize only the trailing main() call — this test exercises
            # the module-level profile resolution, the way runpy does, while
            # keeping the __main__ guard block (which registers the canonical
            # sys.modules alias) intact.
            source = source.rsplit("    main()", 1)[0] + "    pass\\n"
            import __main__
            globs = __main__.__dict__
            globs.update(
                __name__="__main__",
                __spec__=spec,
                __file__=spec.origin,
                __loader__=spec.loader,
                __package__="hermes_cli",
            )
            exec(compile(source, spec.origin, "exec"), globs)

            # The lazy import that used to re-execute the module:
            from hermes_cli.main import _read_git_revision_fingerprint  # noqa: F401
            print("FINAL_HOME=" + os.environ["HERMES_HOME"])
            """
        )
        env = dict(os.environ)
        env.pop("INVOCATION_ID", None)
        env.pop("HERMES_S6_SUPERVISED_CHILD", None)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        assert result.returncode == 0, result.stderr
        final_lines = [
            line for line in result.stdout.splitlines() if line.startswith("FINAL_HOME=")
        ]
        assert final_lines, result.stdout
        assert final_lines[-1] == f"FINAL_HOME={hermes_root}", result.stdout

