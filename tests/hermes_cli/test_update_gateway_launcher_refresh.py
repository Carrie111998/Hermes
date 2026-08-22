"""Legacy pythonw launcher normalization + post-update launcher refresh.

Covers the two halves of the "legacy pythonw gateways survive updates
forever" gap:

1. ``gateway_windows._resolve_detached_python`` — normalizes a legacy
   ``pythonw.exe`` interpreter (pre-aa2ae36c3f launchers / argv snapshots)
   to the sibling console ``python.exe`` so respawns and regenerated
   launchers use the hidden-console design (#54220/#56747) and don't die
   with ``RuntimeError: sys.stderr is None`` (#71671).
2. ``hermes_cli.main._refresh_windows_gateway_launchers`` — ``hermes
   update`` regenerates the installed Scheduled Task / Startup launcher
   scripts instead of leaving install-time artifacts stale forever.

``_resolve_detached_python`` is a pure path helper and runs on any host.
``windowless_gateway_restart_spec`` returns its argv unchanged off Windows,
so the test that exercises the rewrite is ``windows_only`` rather than run
against a faked ``sys.platform``.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import hermes_cli.gateway_windows as gateway_windows
import hermes_cli.main as cli_main


# ---------------------------------------------------------------------------
# _resolve_detached_python: legacy pythonw normalization
# ---------------------------------------------------------------------------


def _make_venv(tmp_path: Path, *, with_console_python: bool) -> tuple[Path, Path]:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    pythonw = scripts / "pythonw.exe"
    pythonw.write_text("", encoding="utf-8")
    python = scripts / "python.exe"
    if with_console_python:
        python.write_text("", encoding="utf-8")
    return pythonw, python


def test_resolve_detached_python_swaps_legacy_pythonw_for_console_sibling(tmp_path):
    pythonw, python = _make_venv(tmp_path, with_console_python=True)

    exe, venv_dir, extra = gateway_windows._resolve_detached_python(str(pythonw))

    assert exe == str(python)
    assert venv_dir == tmp_path / "venv"
    assert extra == []




@pytest.mark.windows_only
def test_restart_spec_normalizes_legacy_pythonw_argv(tmp_path):
    """A pre-rework Scheduled Task argv snapshot (leading pythonw.exe) must be
    respawned through the console python + hidden-console launch, with every
    argument after the interpreter preserved verbatim.

    ``windows_only``: ``windowless_gateway_restart_spec`` returns the argv
    untouched off Windows, so the fake was the only thing making the rewrite
    (and its ``Scripts/``-layout venv derivation) run at all.
    """
    pythonw, python = _make_venv(tmp_path, with_console_python=True)

    argv = [str(pythonw), "-m", "hermes_cli.main", "gateway", "run"]
    with mock.patch.object(
        gateway_windows, "_stable_gateway_working_dir", return_value=str(tmp_path)
    ), mock.patch("hermes_cli.config.get_hermes_home", return_value=str(tmp_path)):
        new_argv, cwd, env = gateway_windows.windowless_gateway_restart_spec(list(argv))

    assert new_argv[0] == str(python)
    assert new_argv[1:] == argv[1:]
    assert cwd == str(tmp_path)
    assert env["VIRTUAL_ENV"] == str(tmp_path / "venv")


# ---------------------------------------------------------------------------
# _refresh_windows_gateway_launchers: hermes update regenerates launchers
# ---------------------------------------------------------------------------


@pytest.fixture
def profile_root(tmp_path, monkeypatch):
    """A fake Hermes root whose ``profiles/`` dir the real scan can walk.

    ``list_profile_names`` is a plain directory scan, so pointing
    ``get_default_hermes_root`` at ``tmp_path`` exercises the real enumeration
    rather than a stubbed profile list.
    """
    import hermes_constants

    (tmp_path / "profiles" / "arthur_tutor").mkdir(parents=True)
    (tmp_path / "profiles" / "joao_pessoal").mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    return tmp_path


def _install_only(monkeypatch, installed_homes):
    """Make ``is_installed()`` answer for the home the CALLER scoped it to.

    It reads ``get_hermes_home()`` itself, so a test using this fails if the
    context-local override is not actually installed around the call — not
    merely if the loop is missing.
    """
    from hermes_constants import get_hermes_home

    wanted = {str(Path(h)) for h in installed_homes}
    monkeypatch.setattr(
        gateway_windows, "is_installed", lambda: str(get_hermes_home()) in wanted
    )


def test_enumeration_keeps_only_profiles_with_an_installed_entry(
    profile_root, monkeypatch
):
    _install_only(monkeypatch, [profile_root, profile_root / "profiles" / "arthur_tutor"])

    homes = cli_main._installed_gateway_profile_homes()

    assert [str(h) for h in homes] == [
        str(profile_root),
        str(profile_root / "profiles" / "arthur_tutor"),
    ]


def test_enumeration_restores_the_previous_home_afterwards(profile_root, monkeypatch):
    """The override is context-local; leaking one would retarget the rest of
    the update at a profile it was never asked to touch."""
    from hermes_constants import get_hermes_home

    _install_only(monkeypatch, [profile_root])
    before = str(get_hermes_home())

    cli_main._installed_gateway_profile_homes()

    assert str(get_hermes_home()) == before


def test_enumeration_survives_a_profile_that_raises(profile_root, monkeypatch):
    from hermes_constants import get_hermes_home

    bad = str(profile_root / "profiles" / "arthur_tutor")

    def flaky():
        if str(get_hermes_home()) == bad:
            raise OSError("schtasks exploded")
        return True

    monkeypatch.setattr(gateway_windows, "is_installed", flaky)

    homes = [str(h) for h in cli_main._installed_gateway_profile_homes()]

    assert bad not in homes
    assert str(profile_root / "profiles" / "joao_pessoal") in homes


def test_refresh_writes_a_launcher_for_every_installed_profile(
    profile_root, monkeypatch, capsys
):
    """The regression #91675 is about: only the active profile was refreshed,
    so a non-active profile's launcher stayed stale forever.

    Asserts the home each ``_write_task_script`` call OBSERVES, because that
    is what decides which profile's ``.cmd`` gets rewritten — iterating
    without scoping would write the same file N times.
    """
    from hermes_constants import get_hermes_home

    installed = [profile_root, profile_root / "profiles" / "joao_pessoal"]
    _install_only(monkeypatch, installed)
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)

    seen = []
    monkeypatch.setattr(
        gateway_windows, "_write_task_script", lambda: seen.append(str(get_hermes_home()))
    )

    cli_main._refresh_windows_gateway_launchers()

    assert seen == [str(h) for h in installed]
    assert "2 profiles" in capsys.readouterr().out


def test_refresh_reports_a_single_profile_the_way_it_always_did(
    profile_root, monkeypatch, capsys
):
    _install_only(monkeypatch, [profile_root])
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: None)

    cli_main._refresh_windows_gateway_launchers()

    out = capsys.readouterr().out
    assert "Refreshed Windows gateway launcher scripts" in out
    assert "profiles)" not in out


def test_one_failing_profile_does_not_cost_the_others_their_refresh(
    profile_root, monkeypatch
):
    from hermes_constants import get_hermes_home

    bad = str(profile_root / "profiles" / "arthur_tutor")
    good = str(profile_root / "profiles" / "joao_pessoal")
    _install_only(monkeypatch, [profile_root, Path(bad), Path(good)])
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)

    seen = []

    def flaky():
        home = str(get_hermes_home())
        if home == bad:
            raise PermissionError("locked by AV")
        seen.append(home)

    monkeypatch.setattr(gateway_windows, "_write_task_script", flaky)

    cli_main._refresh_windows_gateway_launchers()

    assert seen == [str(profile_root), good]


def test_refresh_prints_nothing_when_no_profile_has_a_launcher(
    profile_root, monkeypatch, capsys
):
    _install_only(monkeypatch, [])
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(
        gateway_windows,
        "_write_task_script",
        lambda: pytest.fail("nothing is installed; there is no launcher to write"),
    )

    cli_main._refresh_windows_gateway_launchers()

    assert capsys.readouterr().out == ""


def test_refresh_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(
        cli_main,
        "_installed_gateway_profile_homes",
        lambda: pytest.fail("no profile enumeration may run off Windows"),
    )

    cli_main._refresh_windows_gateway_launchers()
