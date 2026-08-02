from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGED_GATE = load_module("changed_line_coverage_gate", REPO_ROOT / "scripts" / "ci" / "changed_line_coverage_gate.py")
OSV_GATE = load_module("osv_new_vuln_gate", REPO_ROOT / "scripts" / "ci" / "osv_new_vuln_gate.py")
RUNNER = load_module("run_tests_parallel", REPO_ROOT / "scripts" / "run_tests_parallel.py")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def test_changed_line_parser_extracts_only_added_source_lines():
    diff = """
        diff --git a/agent/foo.py b/agent/foo.py
        index 111..222 100644
        --- a/agent/foo.py
        +++ b/agent/foo.py
        @@ -10,2 +10,4 @@
         unchanged_before()
        -old_line()
        +new_line()
        +another_line()
         unchanged_after()
        diff --git a/agent/deleted.py b/agent/deleted.py
        --- a/agent/deleted.py
        +++ /dev/null
        @@ -1 +0,0 @@
        -gone()
        diff --git a/tests/test_foo.py b/tests/test_foo.py
        --- a/tests/test_foo.py
        +++ b/tests/test_foo.py
        @@ -1,0 +1,1 @@
        +def test_ignored(): pass
        diff --git a/scripts/tool.sh b/scripts/tool.sh
        --- a/scripts/tool.sh
        +++ b/scripts/tool.sh
        @@ -1,0 +1,1 @@
        +echo ignored
    """

    changed = CHANGED_GATE.parse_changed_lines(textwrap.dedent(diff))

    assert changed == {"agent/foo.py": {11, 12}}


def test_changed_line_gate_looks_up_absolute_coverage_suffixes():
    changed = {"agent/foo.py": {10, 11}}
    coverage = {"/tmp/repo/agent/foo.py": ({10}, {11}, set())}

    result = CHANGED_GATE.evaluate_changed_line_coverage(changed, coverage)

    assert result.total == 2
    assert result.covered == 1
    assert result.missing == {"agent/foo.py": [11]}


def test_changed_line_gate_treats_files_without_coverage_as_uncovered():
    result = CHANGED_GATE.evaluate_changed_line_coverage(
        {"agent/unmeasured.py": {7}},
        {"agent/other.py": ({1}, set(), set())},
    )

    assert result.total == 1
    assert result.covered == 0
    assert result.missing == {"agent/unmeasured.py": [7]}


def test_changed_line_gate_runs_git_diff_and_fails_closed(monkeypatch, tmp_path):
    calls = []

    def fake_run_success(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return type("Proc", (), {"returncode": 0, "stdout": "diff text", "stderr": ""})()

    monkeypatch.setattr(CHANGED_GATE.subprocess, "run", fake_run_success)

    assert CHANGED_GATE.run_git_diff(tmp_path, "origin/main") == "diff text"
    assert calls[0][0][:3] == ["git", "diff", "--unified=0"]
    assert calls[0][1]["cwd"] == tmp_path

    def fake_run_failure(cmd, **kwargs):
        return type("Proc", (), {"returncode": 2, "stdout": "", "stderr": "fatal diff"})()

    monkeypatch.setattr(CHANGED_GATE.subprocess, "run", fake_run_failure)

    try:
        CHANGED_GATE.run_git_diff(tmp_path, "origin/main")
    except RuntimeError as exc:
        assert "fatal diff" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_changed_line_gate_treats_excluded_and_non_executable_lines_as_neutral_and_reports_missing():
    changed = {"agent/foo.py": {10, 11, 12, 13, 14}}
    coverage = {"agent/foo.py": ({10, 13}, {11}, {12})}

    result = CHANGED_GATE.evaluate_changed_line_coverage(changed, coverage)

    assert result.total == 3
    assert result.covered == 2
    assert round(result.percent, 2) == 66.67
    assert result.missing == {"agent/foo.py": [11]}


def test_changed_line_gate_fails_closed_when_measured_file_has_no_executable_lines():
    changed = {"agent/generated.py": {3}}
    coverage = {"agent/generated.py": (set(), set(), set())}

    result = CHANGED_GATE.evaluate_changed_line_coverage(changed, coverage)

    assert result.total == 1
    assert result.covered == 0
    assert result.missing == {"agent/generated.py": [3]}


def test_changed_line_gate_cli_fails_below_threshold(tmp_path, capsys):
    diff_file = tmp_path / "diff.patch"
    coverage_json = tmp_path / "coverage.json"
    write(diff_file, """
        diff --git a/tools/foo.py b/tools/foo.py
        --- a/tools/foo.py
        +++ b/tools/foo.py
        @@ -1,0 +1,2 @@
        +covered()
        +missing()
    """)
    coverage_json.write_text(json.dumps({
        "files": {
            "tools/foo.py": {
                "executed_lines": [1],
                "missing_lines": [2],
                "excluded_lines": [],
            }
        }
    }), encoding="utf-8")

    exit_code = CHANGED_GATE.main([
        "--coverage-json", str(coverage_json),
        "--diff-file", str(diff_file),
        "--fail-under", "100",
    ])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Changed-line coverage: 50.00%" in captured.out
    assert "tools/foo.py" in captured.out
    assert "below required" in captured.err


def test_changed_line_gate_passes_when_no_source_lines_changed(tmp_path, capsys):
    diff_file = tmp_path / "diff.patch"
    coverage_json = tmp_path / "coverage.json"
    write(diff_file, """
        diff --git a/tests/test_only.py b/tests/test_only.py
        --- a/tests/test_only.py
        +++ b/tests/test_only.py
        @@ -1,0 +1,1 @@
        +def test_only(): pass
    """)
    coverage_json.write_text(json.dumps({"files": {}}), encoding="utf-8")

    exit_code = CHANGED_GATE.main([
        "--coverage-json", str(coverage_json),
        "--diff-file", str(diff_file),
        "--fail-under", "100",
    ])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Changed executable lines: 0" in captured.out
    assert "Changed-line coverage: 100.00%" in captured.out


def test_osv_gate_allows_existing_baseline_vulnerabilities(capsys):
    base = {
        "runs": [{"results": [{"ruleId": "GHSA-old", "locations": [{"physicalLocation": {"artifactLocation": {"uri": "uv.lock"}}}]}]}]
    }
    head = {
        "runs": [{"results": [{"ruleId": "GHSA-old", "locations": [{"physicalLocation": {"artifactLocation": {"uri": "uv.lock"}}}]}]}]
    }

    new_ids = OSV_GATE.newly_introduced(base, head)

    assert new_ids == set()
    assert OSV_GATE.render(new_ids) == "No newly introduced OSV vulnerabilities found.\n"


def test_osv_gate_fails_only_new_head_vulnerability(tmp_path, capsys):
    base_file = tmp_path / "base.sarif.json"
    head_file = tmp_path / "head.sarif.json"
    base_file.write_text(json.dumps({
        "runs": [{"results": [{"ruleId": "GHSA-old", "locations": [{"physicalLocation": {"artifactLocation": {"uri": "uv.lock"}}}]}]}]
    }), encoding="utf-8")
    head_file.write_text(json.dumps({
        "runs": [{"results": [
            {"ruleId": "GHSA-old", "locations": [{"physicalLocation": {"artifactLocation": {"uri": "uv.lock"}}}]},
            {"ruleId": "GHSA-new", "locations": [{"physicalLocation": {"artifactLocation": {"uri": "package-lock.json"}}}]},
        ]}]
    }), encoding="utf-8")

    exit_code = OSV_GATE.main(["--base", str(base_file), "--head", str(head_file)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GHSA-new@package-lock.json" in captured.out
    assert "GHSA-old@uv.lock" not in captured.out


def test_parallel_runner_builds_coverage_subprocess_command():
    cmd = RUNNER._pytest_subprocess_cmd(Path("tests/test_sample.py"), ["-q"], coverage=True)

    assert cmd[:7] == [sys.executable, "-m", "coverage", "run", "--parallel-mode", "--branch", "-m"]
    assert cmd[7:] == ["pytest", "tests/test_sample.py", "-q"]


def test_parallel_runner_run_one_file_supports_coverage_mode(tmp_path):
    test_file = tmp_path / "test_generated.py"
    write(test_file, """
        def test_generated():
            assert True
    """)

    _path, return_code, output, summary, _wall = RUNNER._run_one_file(
        test_file,
        ["-q"],
        tmp_path,
        30,
        coverage=True,
    )

    assert return_code == 0
    assert summary.get("passed") == 1
    assert "1 passed" in output
    assert any(tmp_path.glob(".coverage.*"))


def test_parallel_runner_main_parses_coverage_flag_without_running_tests(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_tests_parallel.py", "--coverage", "--generate-slices", "2", "tests/test_coverage_gates.py"],
    )

    assert RUNNER.main() == 0
    out = capsys.readouterr().out

    assert '"slice"' in out
    assert "tests/test_coverage_gates.py" in out


def test_parallel_runner_default_command_stays_pytest_direct():
    cmd = RUNNER._pytest_subprocess_cmd(Path("tests/test_sample.py"), ["-q"], coverage=False)

    assert cmd == [sys.executable, "-m", "pytest", "tests/test_sample.py", "-q"]
