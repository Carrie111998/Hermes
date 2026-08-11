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
