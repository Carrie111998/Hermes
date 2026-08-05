#!/usr/bin/env python3
"""Deterministic engineering-quality audit for the Hermes Agent checkout.

The audit is deliberately read-only and dependency-free. It inspects the repo's
local files and reports whether the engineering system has enforceable signals
for tests, CI parity, coverage, linting, security, and supply-chain hygiene.

It does NOT claim code quality from vibes. Every finding carries local evidence
and a status:

- CONFIRMADO: concrete local evidence exists.
- GAP: a desirable engineering control was not found.
- RIESGO: evidence suggests a weak/unsafe control.
- INFO: inventory/context.

Usage:
    python scripts/ci/engineering_quality_audit.py
    python scripts/ci/engineering_quality_audit.py --repo /path/to/repo --format json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}
COVERAGE_PATTERNS = (
    r"pytest-cov",
    r"coverage\.py",
    r"coverage\s+run",
    r"coverage\s+xml",
    r"--cov(?:\b|=)",
    r"diff-cover",
    r"fail-under",
    r"coverageThreshold",
    r"coverprofile",
    r"changed[-_ ]line",
)
APPSEC_PATTERNS = (
    r"\bsemgrep\b",
    r"\bcodeql\b",
    r"\bbandit\b",
    r"\bpip-audit\b",
    r"\bsafety\b",
    r"\bgitleaks\b",
    r"\btrufflehog\b",
    r"\btrivy\b",
    r"\bgrype\b",
    r"\bzap\b",
    r"\bsnyk\b",
)


@dataclass(frozen=True)
class Finding:
    status: str
    area: str
    detail: str
    evidence: str
    recommendation: str = ""


@dataclass(frozen=True)
class AuditReport:
    repo: str
    git_branch: str
    git_head: str
    dirty_paths: int
    python_files: int
    pytest_files: int
    pytest_functions: int
    findings: list[Finding]

    @property
    def gaps(self) -> list[Finding]:
        return [f for f in self.findings if f.status in {"GAP", "RIESGO"}]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return ""


def rel(repo: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def iter_files(repo: Path, pattern: str) -> list[Path]:
    if not repo.exists():
        return []
    out: list[Path] = []
    for path in repo.rglob(pattern):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.is_file():
            out.append(path)
    return sorted(out)


def grep_files(paths: Iterable[Path], patterns: Sequence[str]) -> list[Path]:
    combined = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)
    hits: list[Path] = []
    for path in paths:
        if combined.search(read_text(path)):
            hits.append(path)
    return hits


def count_pytest_functions(paths: Iterable[Path]) -> int:
    count = 0
    for path in paths:
        try:
            tree = ast.parse(read_text(path), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                count += 1
    return count


def run_git(repo: Path, args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()


def count_action_refs(workflow_files: Iterable[Path]) -> tuple[int, int, int]:
    """Return (sha_pinned, version_or_branch_pinned, local_actions)."""
    sha_pinned = 0
    floating = 0
    local = 0
    uses_line = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")
    for path in workflow_files:
        for line in read_text(path).splitlines():
            match = uses_line.match(line)
            if not match:
                continue
            target = match.group(1).strip().strip('"\'')
            if target.startswith("./"):
                local += 1
                continue
            if "@" not in target:
                floating += 1
                continue
            ref = target.rsplit("@", 1)[1]
            if re.fullmatch(r"[0-9a-f]{40}", ref):
                sha_pinned += 1
            else:
                floating += 1
    return sha_pinned, floating, local


def workflow_text(workflow_files: Iterable[Path]) -> str:
    return "\n".join(read_text(path) for path in workflow_files)


def audit_repo(repo: Path) -> AuditReport:
    repo = repo.resolve()
    py_files = iter_files(repo, "*.py")
    test_files = [path for path in iter_files(repo / "tests", "test_*.py")]
    workflow_files = iter_files(repo / ".github" / "workflows", "*.yml") + iter_files(repo / ".github" / "workflows", "*.yaml")
    script_files = iter_files(repo / "scripts", "*.py") + iter_files(repo / "scripts", "*.sh")
    audit_script = Path(__file__).resolve()
    # Do not let this auditor's own explanatory strings satisfy the controls it
    # is checking for. Otherwise merely adding the audit script can create a
    # false-positive "coverage/AppSec exists" result.
    evidence_scripts = [path for path in script_files if path.resolve() != audit_script]
    pyproject = repo / "pyproject.toml"
    pyproject_text = read_text(pyproject)
    workflows_blob = workflow_text(workflow_files)
    findings: list[Finding] = []

    branch = run_git(repo, ["branch", "--show-current"]) or "unknown"
    head = run_git(repo, ["rev-parse", "--short", "HEAD"]) or "unknown"
    dirty = run_git(repo, ["status", "--short"])
    dirty_paths = len([line for line in dirty.splitlines() if line.strip()])

    findings.append(Finding(
        "INFO",
        "Inventario",
        f"Repo con {len(py_files)} archivos Python, {len(test_files)} archivos pytest y {count_pytest_functions(test_files)} funciones test aproximadas.",
        "scan local de archivos *.py y tests/test_*.py",
    ))

    runner = repo / "scripts" / "run_tests.sh"
    runner_text = read_text(runner)
    if runner.exists() and "run_tests_parallel.py" in runner_text and "env -i" in runner_text:
        findings.append(Finding(
            "CONFIRMADO",
            "Testing",
            "Existe runner canónico que ejecuta pytest por archivo en subprocess aislado y entorno limpio.",
            rel(repo, runner),
        ))
    else:
        findings.append(Finding(
            "GAP",
            "Testing",
            "No se encontró runner canónico con aislamiento por archivo y entorno limpio.",
            rel(repo, runner),
            "Definir un único comando local equivalente al CI para evitar falsos verdes.",
        ))

    if "scripts/run_tests.sh" in workflows_blob:
        findings.append(Finding(
            "CONFIRMADO",
            "CI parity",
            "El CI invoca el mismo runner canónico que se documenta para local.",
            ".github/workflows/* contiene scripts/run_tests.sh",
        ))
    else:
        findings.append(Finding(
            "RIESGO",
            "CI parity",
            "El CI no parece invocar scripts/run_tests.sh; puede haber divergencia local/CI.",
            ".github/workflows/*",
            "Hacer que CI y local compartan el mismo runner o documentar divergencia explícita.",
        ))

    tests_yml = read_text(repo / ".github" / "workflows" / "tests.yml")
    parallel_runner = read_text(repo / "scripts" / "run_tests_parallel.py")
    if "--generate-slices" in tests_yml and "test_durations.json" in parallel_runner:
        findings.append(Finding(
            "CONFIRMADO",
            "Test runtime",
            "Los tests se dividen por slices y usan duraciones históricas para balancear CI.",
            ".github/workflows/tests.yml + scripts/run_tests_parallel.py",
        ))
    else:
        findings.append(Finding(
            "GAP",
            "Test runtime",
            "No se detectó sharding balanceado por duración histórica.",
            ".github/workflows/tests.yml + scripts/run_tests_parallel.py",
            "Añadir slicing LPT/duration-cache si el tiempo de CI escala demasiado.",
        ))

    coverage_haystack = [pyproject, *workflow_files, *evidence_scripts]
    coverage_hits = grep_files(coverage_haystack, COVERAGE_PATTERNS)
    if coverage_hits:
        findings.append(Finding(
            "CONFIRMADO",
            "Coverage",
            "Se detectó tooling o gate de cobertura en configuración/scripts.",
            ", ".join(rel(repo, p) for p in coverage_hits[:8]),
        ))
    else:
        findings.append(Finding(
            "GAP",
            "Coverage",
            "No se encontró gate de cobertura ni changed-line coverage en pyproject/workflows/scripts.",
            "sin matches para pytest-cov/coverage/diff-cover/--cov/changed-line",
            "Añadir medición primero y después bloquear regresión/changed-lines en módulos críticos.",
        ))

    lint_yml = read_text(repo / ".github" / "workflows" / "lint.yml")
    if "ruff check ." in lint_yml and "--exit-zero" not in lint_yml.split("ruff check .", 1)[1].splitlines()[0]:
        findings.append(Finding("CONFIRMADO", "Lint", "ruff check . existe como enforcement bloqueante.", ".github/workflows/lint.yml"))
    else:
        findings.append(Finding("GAP", "Lint", "No se detectó ruff bloqueante claro.", ".github/workflows/lint.yml", "Añadir un job bloqueante para reglas de bajo falso positivo."))

    if "ty check" in lint_yml:
        mode = "advisory" if "--exit-zero" in lint_yml else "blocking"
        findings.append(Finding("CONFIRMADO", "Type checking", f"ty check está configurado en modo {mode}.", ".github/workflows/lint.yml"))
    else:
        findings.append(Finding("GAP", "Type checking", "No se detectó typecheck en workflow de lint.", ".github/workflows/lint.yml"))

    if "PLW1514" in pyproject_text:
        findings.append(Finding("CONFIRMADO", "Windows quality", "Ruff bloquea footguns de encoding de texto para Windows.", "pyproject.toml [tool.ruff.lint]"))

    if (repo / ".github" / "workflows" / "supply-chain-audit.yml").exists():
        findings.append(Finding("CONFIRMADO", "Supply chain", "Existe scanner de diff para patrones críticos de supply chain.", ".github/workflows/supply-chain-audit.yml"))
    else:
        findings.append(Finding("GAP", "Supply chain", "No se detectó workflow de auditoría supply-chain.", ".github/workflows/supply-chain-audit.yml"))

    safe_change = read_text(repo / "scripts" / "safe_change_runner.py")
    transactional_markers = (
        "TransactionReport",
        "create_snapshot",
        "rollback",
        "parse_retry_delays",
        "--apply-json",
        "--verify-json",
    )
    if safe_change and all(marker in safe_change for marker in transactional_markers):
        findings.append(Finding(
            "CONFIRMADO",
            "Transactional changes",
            "Existe runner transaccional con snapshot, comandos argv, verificación, backoff y rollback.",
            "scripts/safe_change_runner.py",
        ))
    else:
        findings.append(Finding(
            "GAP",
            "Transactional changes",
            "No se detectó runner transaccional para aplicar cambios locales con rollback verificable.",
            "scripts/safe_change_runner.py",
            "Añadir preflight + snapshot + apply + verify + rollback + reporte para cambios de agente/infraestructura.",
        ))

    osv = read_text(repo / ".github" / "workflows" / "osv-scanner.yml")
    if osv:
        has_report_only_scan = re.search(r"fail-on-vuln:\s*false", osv) is not None
        has_base_head_comparison = (
            "new-lockfile-vuln-gate" in osv
            and "detect-lockfile-changes" in osv
            and "osv_new_vuln_gate.py" in osv
            and "osv-base.sarif" in osv
            and "osv-head.sarif" in osv
        )
        if has_report_only_scan and not has_base_head_comparison:
            findings.append(Finding(
                "RIESGO",
                "Dependency security",
                "OSV scanner existe pero no bloquea vulnerabilidades nuevas contra una línea base.",
                ".github/workflows/osv-scanner.yml fail-on-vuln: false",
                "Mantener report-only para deuda heredada, pero comparar base/head y bloquear solo vulnerabilidades nuevas.",
            ))
        elif has_report_only_scan and has_base_head_comparison:
            findings.append(Finding(
                "CONFIRMADO",
                "Dependency security",
                "OSV mantiene escaneo report-only para deuda heredada y bloquea solo vulnerabilidades nuevas cuando cambian lockfiles.",
                ".github/workflows/osv-scanner.yml report-only + base/head OSV gate",
            ))
        else:
            findings.append(Finding("CONFIRMADO", "Dependency security", "OSV scanner existe y no está explícitamente en report-only.", ".github/workflows/osv-scanner.yml"))
    else:
        findings.append(Finding("GAP", "Dependency security", "No se detectó OSV scanner.", ".github/workflows/osv-scanner.yml"))

    appsec_hits = grep_files(workflow_files + [pyproject], APPSEC_PATTERNS)
    if appsec_hits:
        findings.append(Finding("CONFIRMADO", "AppSec scanners", "Se detectaron scanners AppSec adicionales.", ", ".join(rel(repo, p) for p in appsec_hits[:8])))
    else:
        findings.append(Finding(
            "GAP",
            "AppSec scanners",
            "No se detectó SAST/secret/container scanner amplio en workflows/pyproject.",
            "sin matches para semgrep/codeql/bandit/pip-audit/gitleaks/trivy/etc.",
            "Añadir scanners por señal: Semgrep/CodeQL, secret scanning y container scan donde aplique.",
        ))

    sha_pinned, floating, local = count_action_refs(workflow_files)
    if floating:
        findings.append(Finding(
            "RIESGO",
            "CI supply chain",
            f"Actions externas: {sha_pinned} pineadas por SHA, {floating} no pineadas por SHA, {local} locales.",
            ".github/workflows/* uses:",
            "Pinear actions externas por SHA salvo excepción documentada.",
        ))
    else:
        findings.append(Finding(
            "CONFIRMADO",
            "CI supply chain",
            f"Actions externas pineadas por SHA: {sha_pinned}; acciones locales: {local}; no-SHA: 0.",
            ".github/workflows/* uses:",
        ))

    return AuditReport(
        repo=repo.as_posix(),
        git_branch=branch,
        git_head=head,
        dirty_paths=dirty_paths,
        python_files=len(py_files),
        pytest_files=len(test_files),
        pytest_functions=count_pytest_functions(test_files),
        findings=findings,
    )


def render_markdown(report: AuditReport) -> str:
    lines = [
        "# Hermes engineering-quality audit",
        "",
        f"Repo: `{report.repo}`",
        f"Git: `{report.git_branch}` @ `{report.git_head}`; dirty paths: `{report.dirty_paths}`",
        f"Inventory: `{report.python_files}` Python files, `{report.pytest_files}` pytest files, approx `{report.pytest_functions}` test functions",
        "",
        "## Findings",
    ]
    for finding in report.findings:
        lines.extend([
            "",
            f"### {finding.status} — {finding.area}",
            f"- Hallazgo: {finding.detail}",
            f"- Evidencia: `{finding.evidence}`",
        ])
        if finding.recommendation:
            lines.append(f"- Recomendación: {finding.recommendation}")
    lines.extend([
        "",
        "## Resumen ejecutivo",
        f"- Findings totales: {len(report.findings)}",
        f"- Gaps/riesgos: {len(report.gaps)}",
    ])
    if report.gaps:
        lines.append("- Prioridad sugerida: " + "; ".join(f"{f.area}" for f in report.gaps[:6]))
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Hermes engineering-quality controls.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2], help="Repository root to audit")
    parser.add_argument("--format", choices={"markdown", "json"}, default="markdown")
    parser.add_argument("--fail-on-risk", action="store_true", help="Exit non-zero if any RIESGO finding is present")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report = audit_repo(args.repo)
    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report), end="")
    if args.fail_on_risk and any(f.status == "RIESGO" for f in report.findings):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
