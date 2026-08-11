"""Hermetic tests for hermes_cli.install_doctor.

Every test here runs identically on a machine with no editable install.
The checker's LOGIC is what is under test; the environment assertion lives
in the doctor command, not in pytest. See
docs/superpowers/specs/2026-08-10-editable-finder-drift-guard-design.md.
"""

from pathlib import Path

from hermes_cli.install_doctor import (
    declared_names,
    parse_finder_mapping,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The five packages the installed finder was missing on 2026-08-10 while
#: pyproject declared all 23 correctly.
DRIFTED_ON_2026_08_10 = (
    "activity_policy",
    "activity_telemetry",
    "devflow_delegation",
    "jobflow_dispatch",
    "session_bridge",
)


def _finder_source(mapping: dict[str, str], *, annotated: bool = True) -> str:
    """Build a stand-in for a setuptools-generated editable finder."""
    decl = "MAPPING: dict[str, str] = " if annotated else "MAPPING = "
    return (
        "from __future__ import annotations\n"
        "import sys\n"
        f"{decl}{mapping!r}\n"
        "NAMESPACES: dict[str, list[str]] = {}\n"
        "def install():\n    pass\n"
    )


def test_declared_names_covers_packages_and_py_modules(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.setuptools]\n"
        'py-modules = ["hermes_constants", "utils"]\n'
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "events.*", "jobflow_dispatch", "jobflow_dispatch.*"]\n',
        encoding="utf-8",
    )

    assert declared_names(pyproject) == {
        "events",
        "jobflow_dispatch",
        "hermes_constants",
        "utils",
    }


def test_declared_names_on_real_pyproject_includes_the_drifted_five():
    """Anchor on the real declaration list — it was always correct."""
    names = declared_names(REPO_ROOT / "pyproject.toml")
    for name in DRIFTED_ON_2026_08_10:
        assert name in names, f"{name} vanished from pyproject packages.find include"
    # py-modules must be covered too: they share the finder's MAPPING and
    # fail identically. hermes_constants is imported nearly everywhere.
    assert "hermes_constants" in names


def test_parse_finder_mapping_reads_the_annotated_form():
    source = _finder_source({"events": r"C:\repo\events"})
    assert parse_finder_mapping(source) == {"events": r"C:\repo\events"}


def test_parse_finder_mapping_reads_the_unannotated_form():
    """The `: dict[str, str]` annotation must be optional, not required.

    Matching a bare `MAPPING = ` against today's generated file returns None,
    which is why the annotation has to be in the pattern — but hard-requiring
    it would break the day setuptools drops it.
    """
    source = _finder_source({"events": r"C:\repo\events"}, annotated=False)
    assert parse_finder_mapping(source) == {"events": r"C:\repo\events"}


def test_parse_finder_mapping_returns_none_on_garbage():
    assert parse_finder_mapping("this is not a finder at all\n") is None


def test_parse_finder_mapping_returns_none_when_value_is_not_a_dict():
    assert parse_finder_mapping("MAPPING: dict[str, str] = ['nope']\n") is None


def test_parse_finder_mapping_ignores_the_namespaces_block():
    """NAMESPACES follows MAPPING in the real file and must not be captured."""
    source = _finder_source({"a": "/x", "b": "/y"})
    assert parse_finder_mapping(source) == {"a": "/x", "b": "/y"}


def test_parse_finder_mapping_returns_none_on_an_unhashable_key():
    """The diagnosis layer must DEGRADE, never raise.

    ast.literal_eval raises TypeError (not ValueError/SyntaxError) for a
    dict literal with an unhashable key, and the regex cannot rule that out
    — it only matches the braces.
    """
    assert parse_finder_mapping("MAPPING: dict[str, str] = {[1, 2]: 'x'}\n") is None


def test_smoke_entrypoints_include_the_regression_chain():
    from hermes_cli.install_doctor import SMOKE_ENTRYPOINTS

    assert "events.gateway_integration" in SMOKE_ENTRYPOINTS


def test_find_editable_finder_picks_the_hermes_agent_finder(tmp_path):
    from hermes_cli.install_doctor import find_editable_finder

    (tmp_path / "__editable___hermes_agent_0_19_0_finder.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    (tmp_path / "__editable___hermes_hudui_0_3_1_finder.py").write_text(
        "x = 1\n", encoding="utf-8"
    )

    found = find_editable_finder([tmp_path])
    assert found is not None
    assert found.name == "__editable___hermes_agent_0_19_0_finder.py"


def test_find_editable_finder_returns_none_when_absent(tmp_path):
    from hermes_cli.install_doctor import find_editable_finder

    assert find_editable_finder([tmp_path]) is None


def test_resolve_install_root_uses_the_common_parent_of_mapping_targets(tmp_path):
    from hermes_cli.install_doctor import resolve_install_root

    root = tmp_path / "agent-src"
    finder = tmp_path / "__editable___hermes_agent_0_19_0_finder.py"
    finder.write_text(
        _finder_source(
            {
                "events": str(root / "events"),
                "jobflow_dispatch": str(root / "jobflow_dispatch"),
                "hermes_constants": str(root / "hermes_constants"),
            }
        ),
        encoding="utf-8",
    )

    resolved = resolve_install_root(finder)
    assert resolved.path == root
    assert resolved.mapping is not None
    assert "jobflow_dispatch" in resolved.mapping
    assert finder.name in resolved.provenance


def test_resolve_install_root_handles_a_single_mapping_entry(tmp_path):
    """commonpath over targets alone would return the package dir, not the root."""
    from hermes_cli.install_doctor import resolve_install_root

    root = tmp_path / "agent-src"
    finder = tmp_path / "__editable___hermes_agent_0_19_0_finder.py"
    finder.write_text(
        _finder_source({"events": str(root / "events")}), encoding="utf-8"
    )

    assert resolve_install_root(finder).path == root


def test_resolve_install_root_reports_an_unparseable_finder(tmp_path):
    from hermes_cli.install_doctor import resolve_install_root

    finder = tmp_path / "__editable___hermes_agent_0_19_0_finder.py"
    finder.write_text("garbage, no mapping here\n", encoding="utf-8")

    resolved = resolve_install_root(finder)
    assert resolved.path is None
    assert resolved.mapping is None
    assert "did not parse" in resolved.provenance


def test_resolve_install_root_falls_back_when_no_finder_exists(monkeypatch):
    """A wheel install has no finder; fall back to the running module's repo."""
    import hermes_cli.install_doctor as mod

    monkeypatch.setattr(mod, "find_editable_finder", lambda *a, **k: None)
    resolved = mod.resolve_install_root()

    assert resolved.mapping is None
    assert "no editable finder" in resolved.provenance


def test_probe_resolves_present_and_missing_names():
    """Real subprocess, stdlib-only targets — hermetic on any machine."""
    from hermes_cli.install_doctor import probe

    result = probe(["json", "hermes_definitely_missing_xyz"], [])

    assert result["resolved"]["json"]["ok"] is True
    assert result["resolved"]["json"]["origin"]
    assert result["resolved"]["hermes_definitely_missing_xyz"]["ok"] is False
    assert result["executable"]


def test_probe_reports_a_failing_entrypoint_import():
    from hermes_cli.install_doctor import probe

    result = probe([], ["hermes_definitely_missing_xyz"])

    entry = result["imports"]["hermes_definitely_missing_xyz"]
    assert entry["ok"] is False
    assert "ModuleNotFoundError" in entry["error"]


def test_probe_runs_from_a_directory_that_is_not_the_repo(tmp_path):
    """The probe must not resolve names via the caller's cwd.

    A module dropped in the CALLER's cwd must be invisible to the probe --
    that is the whole falsifier. Run with cwd=tmp_path containing a decoy.
    """
    import os

    from hermes_cli.install_doctor import probe

    (tmp_path / "hermes_decoy_module.py").write_text("x = 1\n", encoding="utf-8")
    previous = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = probe(["hermes_decoy_module"], [])
    finally:
        os.chdir(previous)

    assert result["resolved"]["hermes_decoy_module"]["ok"] is False, (
        "probe resolved a module from the caller's cwd — the neutral-cwd "
        "guarantee is broken and the guard would report false clean"
    )
