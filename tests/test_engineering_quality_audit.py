from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "engineering_quality_audit.py"
spec = importlib.util.spec_from_file_location("engineering_quality_audit", MODULE_PATH)
assert spec and spec.loader
engineering_quality_audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engineering_quality_audit
spec.loader.exec_module(engineering_quality_audit)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def test_audit_detects_mature_engineering_controls(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "pyproject.toml", """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
        addopts = "-m 'not integration'"

        [tool.ruff.lint]
        select = ["PLW1514"]
    """)
    write(repo / "scripts" / "run_tests.sh", """
        #!/usr/bin/env bash
        exec env -i python scripts/run_tests_parallel.py "$@"
    """)
    write(repo / "scripts" / "run_tests_parallel.py", """
        test_durations = "test_durations.json"
        print(test_durations)
    """)
    write(repo / "scripts" / "coverage_gate.py", """
        # coverage.py and diff-cover gate for changed-line coverage
        print("coverage xml --fail-under=85")
    """)
    write(repo / "scripts" / "safe_change_runner.py", """
        # preflight snapshot apply verify rollback report bounded backoff transaction
        # --apply-json --verify-json
        class TransactionReport: pass
        def create_snapshot(): pass
        def rollback(): pass
        def parse_retry_delays(): pass
    """)
    sha = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    write(repo / ".github" / "workflows" / "tests.yml", f"""
        jobs:
          test:
            steps:
              - uses: actions/checkout@{sha}
              - run: scripts/run_tests.sh --files '${{{{ matrix.slice.files }}}}'
              - run: python scripts/run_tests_parallel.py --generate-slices 8
    """)
    write(repo / ".github" / "workflows" / "lint.yml", f"""
        jobs:
          ruff-blocking:
            steps:
              - uses: actions/setup-python@{sha}
              - run: ruff check .
          type-diff:
            steps:
              - run: ty check --output-format gitlab --exit-zero
    """)
    write(repo / ".github" / "workflows" / "supply-chain-audit.yml", """
        jobs:
          scan:
            steps:
              - run: grep -E 'base64|exec|.pth'
    """)
    write(repo / ".github" / "workflows" / "osv-scanner.yml", f"""
        jobs:
          detect-lockfile-changes:
            steps:
              - uses: actions/checkout@{sha}
          scan:
            uses: google/osv-scanner-action/.github/workflows/osv-scanner-reusable.yml@{sha}
            with:
              fail-on-vuln: false
          new-lockfile-vuln-gate:
            needs: detect-lockfile-changes
            steps:
              - run: python3 scripts/ci/osv_new_vuln_gate.py --base /tmp/osv-base.sarif --head /tmp/osv-head.sarif
    """)
    write(repo / ".github" / "workflows" / "appsec.yml", """
        jobs:
          semgrep:
            steps:
              - run: semgrep ci
    """)
    write(repo / "tests" / "test_sample.py", """
        def test_sync_case():
            assert True

        async def test_async_case():
            assert True
    """)

    report = engineering_quality_audit.audit_repo(repo)
    by_area = {finding.area: finding for finding in report.findings}

    assert report.pytest_functions == 2
    assert by_area["Testing"].status == "CONFIRMADO"
    assert by_area["CI parity"].status == "CONFIRMADO"
    assert by_area["Test runtime"].status == "CONFIRMADO"
    assert by_area["Coverage"].status == "CONFIRMADO"
    assert by_area["Lint"].status == "CONFIRMADO"
    assert by_area["Type checking"].status == "CONFIRMADO"
    assert by_area["Supply chain"].status == "CONFIRMADO"
    assert by_area["Transactional changes"].status == "CONFIRMADO"
    assert by_area["Dependency security"].status == "CONFIRMADO"
    assert by_area["AppSec scanners"].status == "CONFIRMADO"
    assert by_area["CI supply chain"].status == "CONFIRMADO"


def test_audit_flags_missing_coverage_report_only_osv_and_floating_actions(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "pyproject.toml", """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
    """)
    write(repo / "scripts" / "run_tests.sh", """
        # no canonical isolation here
        pytest tests
    """)
    write(repo / ".github" / "workflows" / "tests.yml", """
        jobs:
          test:
            steps:
              - uses: actions/checkout@v4
              - run: pytest tests
    """)
    write(repo / ".github" / "workflows" / "lint.yml", """
        jobs:
          lint:
            steps:
              - run: ruff check --exit-zero .
    """)
    write(repo / ".github" / "workflows" / "osv-scanner.yml", """
        jobs:
          scan:
            with:
              fail-on-vuln: false
    """)
    write(repo / "tests" / "test_sample.py", """
        def test_case():
            assert True
    """)

    report = engineering_quality_audit.audit_repo(repo)
    areas = {(finding.area, finding.status) for finding in report.findings}

    assert ("Coverage", "GAP") in areas
    assert ("Dependency security", "RIESGO") in areas
    assert ("CI supply chain", "RIESGO") in areas
    assert ("CI parity", "RIESGO") in areas
    assert any(f.area == "AppSec scanners" and f.status == "GAP" for f in report.findings)
    assert any(f.area == "Transactional changes" and f.status == "GAP" for f in report.findings)


def test_file_inventory_ignores_virtualenv_and_counts_async_tests(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "tests" / "test_real.py", """
        def test_one():
            assert True

        async def test_two():
            assert True
    """)
    write(repo / "venv" / "lib" / "test_fake.py", """
        def test_should_not_count():
            assert False
    """)

    test_files = engineering_quality_audit.iter_files(repo / "tests", "test_*.py")
    all_files = engineering_quality_audit.iter_files(repo, "test_*.py")

    assert [path.name for path in test_files] == ["test_real.py"]
    assert all(path.name != "test_fake.py" for path in all_files)
    assert engineering_quality_audit.count_pytest_functions(test_files) == 2


def test_canonical_runner_skips_runtime_venv_without_pytest():
    runner = MODULE_PATH.parents[2] / "scripts" / "run_tests.sh"
    text = runner.read_text(encoding="utf-8")

    assert "import pytest" in text
    assert "skipping venv without pytest" in text
    assert "SKIPPED_VENVS" in text
    assert "Scripts/python.exe" in text


def test_lint_workflow_runs_engineering_quality_audit_report_only():
    lint_workflow = MODULE_PATH.parents[2] / ".github" / "workflows" / "lint.yml"
    text = lint_workflow.read_text(encoding="utf-8")

    assert "engineering-quality-audit:" in text
    assert "python scripts/ci/engineering_quality_audit.py" in text
    assert "--fail-on-risk" not in text
    assert "GITHUB_STEP_SUMMARY" in text


def test_auditor_does_not_count_its_own_control_vocabulary(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    fake_auditor = repo / "scripts" / "ci" / "engineering_quality_audit.py"
    write(fake_auditor, """
        # This file says coverage.py, diff-cover, semgrep and codeql while
        # documenting the audit, but that must not prove those gates exist.
    """)
    write(repo / "pyproject.toml", """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
    """)
    write(repo / "tests" / "test_sample.py", """
        def test_case():
            assert True
    """)
    monkeypatch.setattr(engineering_quality_audit, "__file__", str(fake_auditor))

    report = engineering_quality_audit.audit_repo(repo)

    assert any(f.area == "Coverage" and f.status == "GAP" for f in report.findings)
    assert any(f.area == "AppSec scanners" and f.status == "GAP" for f in report.findings)


def test_audit_utility_edges_and_fail_on_risk_paths(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert engineering_quality_audit.rel(repo, tmp_path / "elsewhere.py").endswith("elsewhere.py")
    assert engineering_quality_audit.iter_files(tmp_path / "missing", "*.py") == []

    write(repo / "tests" / "test_bad.py", """
        def test_broken(:
            pass
    """)
    assert engineering_quality_audit.count_pytest_functions([repo / "tests" / "test_bad.py"]) == 0

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(engineering_quality_audit.subprocess, "run", raise_timeout)
    assert engineering_quality_audit.run_git(repo, ["status"]) == ""

    workflow = repo / ".github" / "workflows" / "actions.yml"
    write(workflow, """
        jobs:
          demo:
            steps:
              - uses: ./local-action
              - uses: actions/checkout@v4
              - uses: owner/action-without-ref
    """)
    assert engineering_quality_audit.count_action_refs([workflow]) == (0, 2, 1)

    exit_code = engineering_quality_audit.main(["--repo", str(repo), "--fail-on-risk"])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "# Hermes engineering-quality audit" in out


def test_audit_recognizes_osv_two_lane_policy(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "pyproject.toml", """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
    """)
    write(repo / ".github" / "workflows" / "osv-scanner.yml", """
        jobs:
          detect-lockfile-changes:
            steps: []
          scan:
            with:
              fail-on-vuln: false
          new-lockfile-vuln-gate:
            needs: detect-lockfile-changes
            steps:
              - run: python3 scripts/ci/osv_new_vuln_gate.py --base /tmp/osv-base.sarif --head /tmp/osv-head.sarif
    """)
    write(repo / "tests" / "test_sample.py", """
        def test_case():
            assert True
    """)

    report = engineering_quality_audit.audit_repo(repo)

    assert any(
        finding.area == "Dependency security"
        and finding.status == "CONFIRMADO"
        and "bloquea solo vulnerabilidades nuevas" in finding.detail
        for finding in report.findings
    )


def test_audit_recognizes_non_report_only_osv_scanner(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "pyproject.toml", """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
    """)
    write(repo / ".github" / "workflows" / "osv-scanner.yml", """
        jobs:
          scan:
            steps:
              - run: osv-scanner --lockfile=uv.lock
    """)
    write(repo / "tests" / "test_sample.py", """
        def test_case():
            assert True
    """)

    report = engineering_quality_audit.audit_repo(repo)

    assert any(
        finding.area == "Dependency security"
        and finding.status == "CONFIRMADO"
        and "no está explícitamente en report-only" in finding.detail
        for finding in report.findings
    )


def test_markdown_and_json_interfaces_are_deterministic(tmp_path, capsys):
    repo = tmp_path / "repo"
    write(repo / "pyproject.toml", """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
    """)
    write(repo / "tests" / "test_sample.py", """
        def test_case():
            assert True
    """)

    report = engineering_quality_audit.audit_repo(repo)
    markdown = engineering_quality_audit.render_markdown(report)

    assert markdown.startswith("# Hermes engineering-quality audit")
    assert "## Findings" in markdown
    assert "## Resumen ejecutivo" in markdown
    assert "Gaps/riesgos" in markdown

    exit_code = engineering_quality_audit.main(["--repo", str(repo), "--format", "json"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert '"findings"' in out
    assert '"pytest_functions": 1' in out
