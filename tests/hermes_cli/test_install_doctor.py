"""Hermetic tests for hermes_cli.install_doctor.

Every test here runs identically on a machine with no editable install.
The checker's LOGIC is what is under test; the environment assertion lives
in the doctor command, not in pytest. See
docs/superpowers/specs/2026-08-10-editable-finder-drift-guard-design.md.
"""

from pathlib import Path

import pytest

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


def test_find_editable_finder_picks_the_highest_version_not_lexicographically_last(tmp_path):
    """Finding E: string-sorting filenames ranks 0_9_0 after 0_10_0.

    A leftover older finder would win once a double-digit minor version
    exists (the next minor is 0.20.0, so this is close). Selection must be
    by parsed numeric version, not lexicographic string order.
    """
    from hermes_cli.install_doctor import find_editable_finder

    (tmp_path / "__editable___hermes_agent_0_9_0_finder.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    (tmp_path / "__editable___hermes_agent_0_10_0_finder.py").write_text(
        "x = 1\n", encoding="utf-8"
    )

    found = find_editable_finder([tmp_path])
    assert found is not None
    assert found.name == "__editable___hermes_agent_0_10_0_finder.py"


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
    """A parse failure degrades the diagnosis only, never the verdict.

    `path` must still resolve (falling back to the running module's repo,
    same as the no-finder branch) so `declared` is not silently dropped to
    None -- that used to make breadth SKIP and `ok` come back True. Only
    `mapping` and the provenance wording are allowed to reflect the failure.
    """
    from hermes_cli.install_doctor import resolve_install_root

    finder = tmp_path / "__editable___hermes_agent_0_19_0_finder.py"
    finder.write_text("garbage, no mapping here\n", encoding="utf-8")

    resolved = resolve_install_root(finder)
    assert resolved.path is not None
    assert resolved.mapping is None
    assert "did not parse" in resolved.provenance


def test_unparseable_mapping_still_produces_a_breadth_verdict(tmp_path):
    """A diagnosis-layer failure must not disable the verdict.

    resolve_install_root uses the MAPPING parse for the install root as well
    as the diagnosis, so a parse failure used to leave `declared` None ->
    breadth SKIPPED -> ok=True -> exit 0: a silent no-op reporting success,
    which is the exact failure mode this guard exists to catch.
    """
    import hermes_cli.install_doctor as mod

    finder = tmp_path / "__editable___hermes_agent_0_19_0_finder.py"
    finder.write_text("garbage, no mapping here\n", encoding="utf-8")

    root = mod.resolve_install_root(finder)
    assert root.mapping is None            # diagnosis degraded
    assert root.path is not None           # but declarations still resolvable

    declared = mod.declared_names(root.path / "pyproject.toml")

    def fake_probe(names, entrypoints, python=None, env=None):
        # Nothing resolves -> real drift that must be reported.
        return _probe_result([], list(names))

    probe_result = fake_probe(sorted(declared), ())
    findings = mod.analyze(declared, probe_result, root)

    assert findings.checked_breadth is True
    assert findings.missing
    assert findings.ok is False
    assert findings.finder_is_stale is None   # undetermined, not "not stale"


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


def test_probe_translates_a_timeout_into_probe_error(monkeypatch):
    """Every probe failure mode must surface as ProbeError.

    A caller that catches ProbeError would otherwise be blindsided by a raw
    TimeoutExpired from a hung entrypoint import.
    """
    import subprocess

    import hermes_cli.install_doctor as mod

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=120)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    with pytest.raises(mod.ProbeError):
        mod.probe(["json"], [])


def _probe_result(resolved_ok, resolved_missing=(), imports=None):
    return {
        "resolved": {
            **{n: {"ok": True, "origin": f"/root/{n}", "error": None} for n in resolved_ok},
            **{n: {"ok": False, "origin": None, "error": "not found"} for n in resolved_missing},
        },
        "imports": imports or {},
        "executable": "/usr/bin/python3.11",
    }


def _install_root_with(mapping, root=Path("/root")):
    from hermes_cli.install_doctor import InstallRoot

    return InstallRoot(path=root, provenance="test", mapping=mapping)


def test_analyze_reports_exactly_the_drifted_five():
    """The 18-of-23 case, reproduced against the checker."""
    from hermes_cli.install_doctor import analyze

    present = [f"pkg{i}" for i in range(18)]
    declared = set(present) | set(DRIFTED_ON_2026_08_10)
    mapping = {name: f"/root/{name}" for name in present}

    findings = analyze(
        declared,
        _probe_result(present, DRIFTED_ON_2026_08_10),
        _install_root_with(mapping),
    )

    assert findings.missing == tuple(sorted(DRIFTED_ON_2026_08_10))
    assert findings.ok is False
    assert findings.checked_breadth is True
    assert "holds 18 of the 23 declared" in findings.diagnosis


def test_analyze_is_clean_when_everything_resolves():
    from hermes_cli.install_doctor import analyze

    names = ["events", "jobflow_dispatch"]
    findings = analyze(
        set(names),
        _probe_result(names, imports={"events.gateway_integration": {"ok": True, "error": None}}),
        _install_root_with({n: f"/root/{n}" for n in names}),
    )

    assert findings.ok is True
    assert findings.missing == ()
    assert findings.broken_imports == ()


def test_analyze_catches_a_broken_chain_when_breadth_is_clean():
    """The depth layer must fire even when every top-level resolves."""
    from hermes_cli.install_doctor import analyze

    findings = analyze(
        {"events"},
        _probe_result(
            ["events"],
            imports={
                "events.gateway_integration": {
                    "ok": False,
                    "error": "ModuleNotFoundError: No module named 'jobflow_dispatch'",
                }
            },
        ),
        _install_root_with({"events": "/root/events"}),
    )

    assert findings.missing == ()
    assert findings.ok is False
    assert findings.broken_imports[0][0] == "events.gateway_integration"
    assert "jobflow_dispatch" in findings.broken_imports[0][1]


def test_analyze_still_reports_drift_when_the_diagnosis_is_unavailable():
    """An unparseable finder degrades the explanation, never the verdict."""
    from hermes_cli.install_doctor import analyze

    findings = analyze(
        {"events", "jobflow_dispatch"},
        _probe_result(["events"], ["jobflow_dispatch"]),
        _install_root_with(None),
    )

    assert findings.missing == ("jobflow_dispatch",)
    assert findings.ok is False
    assert findings.diagnosis is None


def test_analyze_flags_finder_stale_when_it_omits_a_missing_name():
    """The finder is genuinely missing an entry -> stale diagnosis + reinstall remedy."""
    from hermes_cli.install_doctor import analyze, remedy_lines

    declared = {"events", "jobflow_dispatch"}
    mapping = {"events": "/root/events"}  # jobflow_dispatch not in the finder
    findings = analyze(
        declared,
        _probe_result(["events"], ["jobflow_dispatch"]),
        _install_root_with(mapping),
    )

    assert findings.finder_is_stale is True
    assert "holds" in findings.diagnosis
    assert "generated ONCE at install time" in findings.diagnosis
    remedy = "\n".join(remedy_lines(_install_root_with(mapping), findings.finder_is_stale))
    assert "pip install -e . --no-deps" in remedy


def test_analyze_flags_finder_not_stale_when_it_lists_every_missing_name():
    """This reproduces the 2026-08-10 verification defect: finder is complete,

    but every declared name still fails to resolve from a neutral cwd. The
    finder is not stale -- it is not being loaded at all -- and the remedy
    must not tell anyone to reinstall.
    """
    from hermes_cli.install_doctor import analyze, render

    declared = {"events", "jobflow_dispatch"}
    mapping = {"events": "/root/events", "jobflow_dispatch": "/root/jobflow_dispatch"}
    install_root = _install_root_with(mapping)
    probe_result = _probe_result([], ["events", "jobflow_dispatch"])
    findings = analyze(declared, probe_result, install_root)

    assert findings.finder_is_stale is False
    assert "not being loaded" in findings.diagnosis
    assert "holds" not in findings.diagnosis

    text = "\n".join(render(findings, install_root, probe_result))
    assert "pip install -e . --no-deps" not in text


class TestReinstallCommandIsDetectedNotHardcoded:
    """The remedy must name a command that RUNS on the box that printed it.

    Regression for 2026-08-12: doctor prescribed a bare
    ``pip install -e . --no-deps`` on a uv-created ``.venv``. uv does not
    install pip into the environments it builds, so the prescribed command
    died on "No module named pip" — a remediation that cannot be executed.
    Hardcoding the uv form instead would be the same defect mirrored onto
    every pip-based venv, so the form is detected.
    """

    def test_pip_form_when_the_interpreter_has_pip(self, monkeypatch):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: True)
        monkeypatch.setattr(install_doctor.sys, "executable", "/venv/bin/python")

        assert install_doctor.reinstall_command() == (
            "/venv/bin/python -m pip install -e . --no-deps"
        )

    def test_uv_form_with_explicit_python_when_pip_is_missing(self, monkeypatch):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: False)
        monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.setattr(install_doctor.sys, "executable", "/agent-src/.venv/bin/python")

        cmd = install_doctor.reinstall_command()

        assert cmd == (
            "/usr/bin/uv pip install -e . --no-deps "
            "--python /agent-src/.venv/bin/python"
        )
        # --python is not optional: without it uv resolves an environment
        # from the cwd, which need not be the one that owns the install.
        assert "--python /agent-src/.venv/bin/python" in cmd

    def test_default_spec_stays_the_finder_regeneration_case(self, monkeypatch):
        """`spec` exists for doctor's entry-point check, which wants
        `-e '.[all]'`. The default must keep this module's own case: `--no-deps`,
        because re-resolving dependencies risks version drift for every agent
        on the machine when all we need is a regenerated finder.
        """
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: True)
        monkeypatch.setattr(install_doctor.sys, "executable", "/venv/bin/python")

        assert install_doctor.reinstall_command().endswith("-e . --no-deps")
        assert install_doctor.reinstall_command(spec="-e '.[all]'").endswith(
            "-e '.[all]'"
        )

    def test_never_falls_back_to_a_bare_pip_on_path(self, monkeypatch):
        """A bare `pip` is the scoop/MSIX interpreter's pip on Windows.

        Running it would install into an entirely different environment,
        which is worse than failing loudly.
        """
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: False)
        monkeypatch.setattr(install_doctor.shutil, "which", lambda name: None)

        cmd = install_doctor.reinstall_command()

        assert not cmd.startswith("pip ")
        assert cmd.startswith("uv pip install -e .")

    def test_windows_paths_with_spaces_stay_copy_pasteable(self, monkeypatch):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: True)
        monkeypatch.setattr(
            install_doctor.sys, "executable", r"C:\Program Files\Python\python.exe"
        )

        assert install_doctor.reinstall_command() == (
            r'"C:\Program Files\Python\python.exe" -m pip install -e . --no-deps'
        )

    def test_remedy_block_carries_the_uv_command_and_its_two_traps(self, monkeypatch):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: False)
        monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.setattr(install_doctor.sys, "executable", "/agent-src/.venv/bin/python")

        text = "\n".join(
            install_doctor.remedy_lines(_install_root_with({}, root=Path("/agent-src")))
        )

        assert "uv pip install -e . --no-deps --python /agent-src/.venv/bin/python" in text
        assert "`--python` is NOT optional" in text
        assert "bare `pip` from PATH" in text

    def test_remedy_block_omits_the_uv_traps_on_a_pip_environment(self, monkeypatch):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: True)

        text = "\n".join(install_doctor.remedy_lines(_install_root_with({})))

        assert "-m pip install -e . --no-deps" in text
        assert "--python" not in text
        assert "no pip" not in text

    def test_remedy_offers_ensurepip_when_neither_pip_nor_uv_exists(self, monkeypatch):
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: False)
        monkeypatch.setattr(install_doctor.shutil, "which", lambda name: None)

        text = "\n".join(install_doctor.remedy_lines(_install_root_with({})))

        assert "uv is not on PATH here" in text
        assert "-m ensurepip" in text

    def test_doctor_one_liner_follows_the_same_detection(self, monkeypatch, tmp_path):
        """The summary line and the full remedy must not prescribe different tools."""
        from hermes_cli import install_doctor

        monkeypatch.setattr(install_doctor, "_pip_is_importable", lambda: False)
        monkeypatch.setattr(install_doctor.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.setattr(install_doctor.sys, "executable", "/agent-src/.venv/bin/python")

        root_dir = tmp_path / "agent-src"
        root_dir.mkdir()
        (root_dir / "pyproject.toml").write_text(
            '[tool.setuptools.packages.find]\ninclude = ["events", "jobflow_dispatch"]\n',
            encoding="utf-8",
        )
        root = install_doctor.InstallRoot(
            path=root_dir, provenance="test", mapping={"events": "x"}
        )

        def fake_probe(names, entrypoints, python=None):
            return _probe_result(["events"], ["jobflow_dispatch"])

        _rows, remediation = install_doctor.doctor_section_lines(
            probe_fn=fake_probe, root=root
        )

        assert remediation is not None
        assert (
            "uv pip install -e . --no-deps --python /agent-src/.venv/bin/python"
            in remediation
        )
        assert "run `pip install" not in remediation

    def test_pip_probe_degrades_to_false_rather_than_raising(self, monkeypatch):
        """A mangled .pth can make find_spec raise; that is 'no pip', not a crash."""
        from hermes_cli import install_doctor

        def boom(name):
            raise ValueError("__spec__ is None")

        monkeypatch.setattr(install_doctor.importlib.util, "find_spec", boom)

        assert install_doctor._pip_is_importable() is False


def test_remedy_lines_defaults_to_reinstall_when_stale_is_undetermined():
    """No mapping was parseable -> finder_is_stale is None -> reinstall is still the default."""
    from hermes_cli.install_doctor import analyze, remedy_lines

    declared = {"events", "jobflow_dispatch"}
    install_root = _install_root_with(None)
    findings = analyze(
        declared,
        _probe_result(["events"], ["jobflow_dispatch"]),
        install_root,
    )

    assert findings.finder_is_stale is None
    remedy = "\n".join(remedy_lines(install_root, findings.finder_is_stale))
    assert "pip install -e . --no-deps" in remedy


def test_doctor_section_lines_remediation_excludes_reinstall_when_finder_not_stale(tmp_path):
    """The doctor surface must not contradict the standalone surface's remedy choice."""
    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "jobflow_dispatch"]\n',
        encoding="utf-8",
    )
    mapping = {"events": "x", "jobflow_dispatch": "y"}
    root = InstallRoot(path=root_dir, provenance="test", mapping=mapping)

    def fake_probe(names, entrypoints, python=None):
        return _probe_result([], ["events", "jobflow_dispatch"])

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert remediation is not None
    assert "pip install -e . --no-deps" not in remediation


def test_doctor_section_lines_remediation_does_not_contradict_not_stale(tmp_path):
    """Finding B: 'has drifted' must not lead a remedy that says nothing drifted.

    When finder_is_stale is False, the editable finder already lists every
    missing name -- the remedy that follows says reinstalling will NOT fix
    this. A hardcoded "Editable install has drifted." prefix contradicts
    that, which is the same defect commit 50edfc58e removed at a different
    surface (remedy_lines) -- it survived here because the fix threaded the
    flag into remedy_lines but left doctor_section_lines' prefix hardcoded.
    """
    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "jobflow_dispatch"]\n',
        encoding="utf-8",
    )
    mapping = {"events": "x", "jobflow_dispatch": "y"}
    root = InstallRoot(path=root_dir, provenance="test", mapping=mapping)

    def fake_probe(names, entrypoints, python=None):
        return _probe_result([], ["events", "jobflow_dispatch"])

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert remediation is not None
    assert "has drifted" not in remediation.lower()
    assert "not" in remediation.lower()  # "not loaded" / "will not fix"


def test_doctor_section_lines_remediation_is_a_short_one_liner_when_stale(tmp_path):
    """Finding C: the doctor summary item must not be a ~1140-char paragraph.

    Joining all of remedy_lines with spaces collapses a ~20-line formatted
    block into one line that doctor renders as a summary item wrapping to
    ~14 terminal lines among one-line neighbours. The remediation must stay
    a short, actionable one-liner that names the concrete next action and
    points at the standalone tool for the full text.

    The bound is on the PROSE, not the raw length: the line embeds the
    install root and — since the remedy became environment-detected — a
    command carrying absolute interpreter/uv paths. Those are load-bearing
    but arbitrarily long, so a raw cap would fail on a deep tmp_path while
    saying nothing about the defect this guards. The backticked command and
    the root path are excluded; everything else is the prose that collapsed.
    """
    import re

    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "jobflow_dispatch"]\n',
        encoding="utf-8",
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(["events"], ["jobflow_dispatch"])

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert remediation is not None
    prose = re.sub(r"`[^`]*`", "``", remediation).replace(str(root_dir), "")
    assert len(prose) < 250, remediation
    assert "pip install -e . --no-deps" in remediation
    assert "install_doctor" in remediation


def test_doctor_section_lines_remediation_is_a_short_one_liner_when_not_stale(tmp_path):
    """Finding C, mirrored for the not-stale remedy."""
    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "jobflow_dispatch"]\n',
        encoding="utf-8",
    )
    mapping = {"events": "x", "jobflow_dispatch": "y"}
    root = InstallRoot(path=root_dir, provenance="test", mapping=mapping)

    def fake_probe(names, entrypoints, python=None):
        return _probe_result([], ["events", "jobflow_dispatch"])

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert remediation is not None
    # Bound is generous because this wording names sys.executable, which on
    # a WindowsApps install can itself run ~150+ chars; the wording around
    # it stays a concise one-liner, not a reproduction of the full block.
    assert len(remediation) < 350
    assert "install_doctor" in remediation


def test_analyze_skips_breadth_without_declarations_and_says_so():
    """A sealed wheel install has no source tree to diff against."""
    from hermes_cli.install_doctor import analyze

    findings = analyze(
        None,
        _probe_result([], imports={"events.gateway_integration": {"ok": True, "error": None}}),
        _install_root_with(None, root=None),
    )

    assert findings.checked_breadth is False
    assert findings.ok is True
    assert any("no pyproject" in note.lower() for note in findings.notes)


def test_analyze_treats_an_empty_declaration_list_as_skipped_not_clean():
    """Zero declared names must not render as "everything resolves".

    declared_names returns set() (not None) for a pyproject with no
    [tool.setuptools.packages.find].include — the list form, or a move to
    another build backend. Reporting that as a pass would be a confident
    clean bill of health over zero checks.
    """
    from hermes_cli.install_doctor import analyze

    findings = analyze(set(), _probe_result([]), _install_root_with({}))

    assert findings.checked_breadth is False
    assert any("declared no top-level names" in note for note in findings.notes)


def test_remedy_names_the_command_the_root_and_the_console_script_trap():
    from hermes_cli.install_doctor import remedy_lines

    root_path = Path("/agent-src")
    text = "\n".join(remedy_lines(_install_root_with({}, root=root_path)))

    assert "pip install -e . --no-deps" in text
    # Compare against str(Path(...)), NOT the literal "/agent-src":
    # remedy_lines renders str(path), which is "\agent-src" on Windows.
    assert str(root_path) in text
    assert "WinError 32" in text
    assert "python -m hermes_cli.main" in text


def test_render_failure_output_contains_the_remedy():
    from hermes_cli.install_doctor import analyze, render

    root = _install_root_with({"events": "/root/events"})
    result = _probe_result(["events"], ["jobflow_dispatch"])
    findings = analyze({"events", "jobflow_dispatch"}, result, root)

    text = "\n".join(render(findings, root, result))

    assert "jobflow_dispatch" in text
    assert "pip install -e . --no-deps" in text
    assert "/usr/bin/python3.11" in text


def test_render_never_claims_a_pass_when_breadth_was_skipped():
    from hermes_cli.install_doctor import analyze, render

    root = _install_root_with(None, root=None)
    result = _probe_result([], imports={"events.gateway_integration": {"ok": True, "error": None}})
    findings = analyze(None, result, root)

    text = "\n".join(render(findings, root, result))

    assert "breadth not checked" in text
    assert "no pyproject.toml was readable" in text
    assert "every declared package resolves" not in text


def test_render_never_claims_a_pass_when_breadth_was_skipped_for_empty_declarations():
    """Finding F: the declared=set() skip path is only covered up through
    analyze()'s notes today; drive it through render() too, mirroring the
    existing declared=None render coverage above.
    """
    from hermes_cli.install_doctor import analyze, render

    root = _install_root_with({}, root=None)
    result = _probe_result([])
    findings = analyze(set(), result, root)

    text = "\n".join(render(findings, root, result))

    assert "declared no top-level names" in text
    assert "every declared package resolves" not in text


def test_render_reports_a_broken_import_even_when_breadth_was_skipped():
    from hermes_cli.install_doctor import analyze, render

    root = _install_root_with(None, root=None)
    result = _probe_result(
        [],
        imports={"events.gateway_integration": {"ok": False, "error": "ModuleNotFoundError: jobflow_dispatch"}},
    )
    findings = analyze(None, result, root)

    text = "\n".join(render(findings, root, result))

    assert findings.checked_breadth is False
    assert findings.ok is False
    assert "events.gateway_integration" in text
    assert "pip install -e . --no-deps" in text
    assert "[OK]" not in text


def test_run_returns_zero_when_clean(tmp_path):
    """Finding D: pin that breadth was actually CHECKED, not merely exit 0.

    Exit 0 alone is also true in the skipped-breadth world (Finding A's
    bug): `ok` is True whenever nothing was found wrong, including when
    breadth never ran. Assert on the rendered output so this test cannot
    pass against that regression.
    """
    from hermes_cli.install_doctor import InstallRoot, run

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\ninclude = [\"events\", \"events.*\"]\n",
        encoding="utf-8",
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(list(names), imports={e: {"ok": True, "error": None} for e in entrypoints})

    out = []
    assert run(probe_fn=fake_probe, root=root, stream=out) == 0
    text = "\n".join(out)
    assert "every declared package resolves" in text


def test_run_returns_one_on_drift_and_prints_the_remedy(tmp_path):
    from hermes_cli.install_doctor import InstallRoot, run

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "jobflow_dispatch"]\n',
        encoding="utf-8",
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(["events"], ["jobflow_dispatch"])

    out = []
    assert run(probe_fn=fake_probe, root=root, stream=out) == 1
    text = "\n".join(out)
    assert "jobflow_dispatch" in text
    assert "pip install -e . --no-deps" in text


def test_doctor_section_lines_yields_a_remediation_on_drift(tmp_path):
    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "jobflow_dispatch"]\n',
        encoding="utf-8",
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(["events"], ["jobflow_dispatch"])

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert any(status == "fail" for status, _, _ in rows)
    assert remediation and "pip install -e . --no-deps" in remediation


def test_doctor_section_lines_is_ok_when_clean(tmp_path):
    """Finding D: `all(status != "fail")` is also satisfied by an all-warn
    row set (the skipped-breadth world). Pin that the row set is exactly
    the single "ok" row, so this cannot pass against that regression.
    """
    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\ninclude = [\"events\"]\n", encoding="utf-8"
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(list(names), imports={e: {"ok": True, "error": None} for e in entrypoints})

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert remediation is None
    assert [s for s, _, _ in rows] == ["ok"]


def test_doctor_section_lines_warns_instead_of_ok_when_breadth_was_skipped(tmp_path):
    """ok=True with checked_breadth=False must never render as an OK row.

    Findings.ok means "nothing was found wrong", not "everything was
    checked". This is the boundary where a caller could otherwise report a
    clean bill of health over zero checks, and the analyze/render tests do
    not pin this row.
    """
    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    # Root exists but holds no pyproject.toml (a sealed wheel install), so
    # _collect leaves `declared` as None and analyze skips breadth.
    root_dir = tmp_path / "sealed-wheel"
    root_dir.mkdir()
    root = InstallRoot(path=root_dir, provenance="test", mapping=None)

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(
            [], imports={e: {"ok": True, "error": None} for e in entrypoints}
        )

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert remediation is None
    assert [status for status, _, _ in rows] == ["warn"]
    assert all(status != "ok" for status, _, _ in rows)
