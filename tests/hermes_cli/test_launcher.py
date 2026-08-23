"""Tests for the top-level `./hermes` launcher script."""

import builtins
import runpy
import subprocess
import sys
import types
from pathlib import Path


def test_launcher_delegates_to_argparse_entrypoint(monkeypatch):
    """`./hermes` should use `hermes_cli.main`, not the legacy Fire wrapper."""
    launcher_path = Path(__file__).resolve().parents[2] / "hermes"
    called = []

    fake_main_module = types.ModuleType("hermes_cli.main")

    def fake_main():
        called.append("hermes_cli.main")

    fake_main_module.main = fake_main
    monkeypatch.setitem(sys.modules, "hermes_cli.main", fake_main_module)

    fake_cli_module = types.ModuleType("cli")

    def legacy_cli_main(*args, **kwargs):
        raise AssertionError("launcher should not import cli.main")

    fake_cli_module.main = legacy_cli_main
    monkeypatch.setitem(sys.modules, "cli", fake_cli_module)

    fake_fire_module = types.ModuleType("fire")

    def legacy_fire(*args, **kwargs):
        raise AssertionError("launcher should not invoke fire.Fire")

    fake_fire_module.Fire = legacy_fire
    monkeypatch.setitem(sys.modules, "fire", fake_fire_module)

    monkeypatch.setattr(sys, "argv", [str(launcher_path), "gateway", "status"])

    runpy.run_path(str(launcher_path), run_name="__main__")

    assert called == ["hermes_cli.main"]


def _launcher() -> Path:
    return Path(__file__).resolve().parents[2] / "hermes"


def test_launcher_fails_actionably_when_deps_are_missing():
    """Running under an interpreter with none of Hermes' dependencies — a
    bare uv-managed CPython, or a generated .desktop Exec= whose interpreter
    escaped the install venv — must name the running interpreter and the fix,
    never a bare `ModuleNotFoundError` traceback (#92882, #90292, #91504)."""
    result = subprocess.run(
        [sys.executable, "-S", str(_launcher()), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "hermes:" in result.stderr
    assert sys.executable in result.stderr
    assert "venv" in result.stderr.lower()


def test_launcher_still_runs_cli_when_deps_are_present():
    """The import guard must not change the healthy path: with dependencies
    installed, `./hermes --help` still exits 0 and prints usage."""
    result = subprocess.run(
        [sys.executable, str(_launcher()), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0
    assert "usage" in (result.stdout + result.stderr).lower()


def _launcher_namespace() -> dict:
    """The launcher's module namespace without running its `__main__` block."""
    return runpy.run_path(str(_launcher()))


def _import_failure_from(tmp_path: Path, rel_path: str, code: str):
    """`exec` `code` from a temp file so the raised `ModuleNotFoundError`'s
    innermost traceback frame is that file — mirroring a real import
    statement living there."""
    file = tmp_path / rel_path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(code)
    try:
        exec(compile(code, str(file), "exec"), {"__builtins__": builtins.__dict__})
    except ModuleNotFoundError as exc:
        return exc
    raise AssertionError(f"expected {code!r} to raise ModuleNotFoundError")


def test_classify_flags_hermes_cli_internal_import_as_bug(tmp_path):
    """A `ModuleNotFoundError` for a `hermes_cli.*` submodule raised from a
    file *inside* the package is an internal bug — the package failing to
    import its own code — not a missing dependency."""
    classify = _launcher_namespace()["_classify_import_failure"]
    pkg_dir = tmp_path / "hermes_cli"

    exc = _import_failure_from(
        tmp_path,
        "hermes_cli/broken.py",
        "import hermes_cli.definitely_missing_submodule",
    )

    missing, internal = classify(exc, pkg_dir=pkg_dir)
    assert missing == "hermes_cli.definitely_missing_submodule"
    assert internal is True


def test_classify_reports_missing_third_party_dep(tmp_path):
    """A missing external module (yaml, dotenv, ...) is a missing dependency
    even when the failing import statement sits inside hermes_cli."""
    classify = _launcher_namespace()["_classify_import_failure"]
    pkg_dir = tmp_path / "hermes_cli"

    exc = _import_failure_from(
        tmp_path,
        "hermes_cli/config.py",
        "import definitely_missing_dependency",
    )

    missing, internal = classify(exc, pkg_dir=pkg_dir)
    assert missing == "definitely_missing_dependency"
    assert internal is False


def test_classify_hermes_cli_name_from_outside_is_not_internal(tmp_path):
    """The same `hermes_cli.*`-named failure raised from *outside* the
    package (e.g. the launcher itself) is not an internal bug."""
    classify = _launcher_namespace()["_classify_import_failure"]
    pkg_dir = tmp_path / "hermes_cli"

    exc = _import_failure_from(
        tmp_path,
        "outside.py",
        "import hermes_cli.definitely_missing_submodule",
    )

    missing, internal = classify(exc, pkg_dir=pkg_dir)
    assert missing == "hermes_cli.definitely_missing_submodule"
    assert internal is False


def test_report_launcher_failure_distinguishes_internal_bug(capsys):
    """The internal-bug report must say 'internal error', never claim a
    missing dependency or suggest the venv fix."""
    report = _launcher_namespace()["_report_launcher_failure"]
    exc = ModuleNotFoundError("No module named 'hermes_cli.typo'")
    exc.name = "hermes_cli.typo"

    report("hermes_cli.typo", internal=True, exc=exc)

    err = capsys.readouterr().err
    assert "hermes_cli.typo" in err
    assert "internal error" in err.lower()
    assert "missing dependency" not in err.lower()


def test_launcher_flags_internal_import_bug_not_missing_deps(tmp_path):
    """End-to-end: a checkout whose hermes_cli imports its own missing
    submodule must exit with an 'internal error' diagnostic and the real
    traceback — it must never be misreported as a missing dependency."""
    checkout = tmp_path / "checkout"
    pkg = checkout / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(
        "import hermes_cli.definitely_missing_submodule\ndef main():\n    pass\n"
    )
    script = checkout / "hermes"
    script.write_text(_launcher().read_text())

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert "internal error" in result.stderr.lower()
    assert "missing dependency" not in result.stderr.lower()
    assert "hermes_cli.definitely_missing_submodule" in result.stderr
