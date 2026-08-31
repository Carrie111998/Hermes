#!/usr/bin/env python3
"""Run pinned, read-only external audits and bind results to exact git state."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, cast

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_constants import get_hermes_home


_MAX_CAPTURE_CHARS = 200_000


@dataclass(frozen=True)
class AuditCommand:
    name: str
    argv: tuple[str, ...]
    expected_version: str

    @property
    def version_argv(self) -> tuple[str, ...]:
        """Return the command used to observe the installed tool realization."""
        return (self.argv[0], "--version")


class AuditProcessResult(Protocol):
    """The subprocess fields consumed by the read-only audit pipeline."""

    @property
    def returncode(self) -> int: ...

    @property
    def stdout(self) -> str: ...

    @property
    def stderr(self) -> str: ...


@dataclass(frozen=True)
class SkippedAuditResult:
    """Typed result for an audit that cannot run after preparation fails."""

    returncode: int
    stdout: str
    stderr: str


def build_commands(
    *, repo_root: Path, tool_root: Path, requirements_path: Path | None = None
) -> tuple[AuditCommand, ...]:
    """Return fixed audit argv; registry or user data can never inject commands."""
    requirements_path = requirements_path or repo_root / ".audit-requirements.txt"
    return (
        AuditCommand(
            name="zizmor",
            expected_version="1.30.0",
            argv=(
                str(tool_root / "zizmor"),
                "--offline",
                "--format",
                "json",
                "--no-progress",
                "--no-exit-codes",
                ".github",
            ),
        ),
        AuditCommand(
            name="import-linter",
            expected_version="2.14",
            argv=(
                str(tool_root / "lint-imports"),
                "--config",
                ".importlinter",
                "--no-cache",
                "--no-logo",
            ),
        ),
        AuditCommand(
            name="pip-audit",
            expected_version="2.10.1",
            argv=(
                str(tool_root / "pip-audit"),
                "-r",
                str(requirements_path),
                "--require-hashes",
                "--disable-pip",
                "--format",
                "json",
                "--progress-spinner",
                "off",
                "--cache-dir",
                str(Path(tempfile.gettempdir()) / "hermes-pip-audit-cache"),
            ),
        ),
    )


def build_uv_export_command(
    *, requirements_path: Path, cache_path: Path | None = None
) -> tuple[str, ...]:
    """Export the existing uv lock without resolving or modifying dependencies."""
    cache_path = cache_path or Path(tempfile.gettempdir()) / "hermes-uv-audit-cache"
    return (
        shutil.which("uv") or "uv",
        "export",
        "--locked",
        "--cache-dir",
        str(cache_path),
        "--no-dev",
        "--no-emit-project",
        "--no-annotate",
        "--no-header",
        "--output-file",
        str(requirements_path),
    )


def _run(
    runner: Callable[..., Any],
    argv: Sequence[str],
    *,
    repo_root: Path,
    timeout: int,
) -> AuditProcessResult:
    return cast(
        AuditProcessResult,
        runner(
            list(argv),
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        ),
    )


def _git_value(runner: Callable[..., Any], repo_root: Path, *arguments: str) -> str:
    result = _run(runner, ("git", *arguments), repo_root=repo_root, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def _sha256(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _bounded_output(text: str) -> str:
    if len(text) <= _MAX_CAPTURE_CHARS:
        return text
    return text[:_MAX_CAPTURE_CHARS] + "\n...[capture truncated]"


def _extract_version(stdout: str, stderr: str) -> str | None:
    """Extract a concrete semantic version from a tool's observed output."""
    match = re.search(r"(?<!\d)(\d+(?:\.\d+){1,3})(?!\d)", f"{stdout}\n{stderr}")
    return match.group(1) if match else None


def _summarize_json_output(name: str, stdout: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None

    if name == "zizmor" and isinstance(payload, list):
        determinations = [row.get("determinations", {}) for row in payload]
        return {
            "finding_groups": len(payload),
            "locations": sum(len(row.get("locations", [])) for row in payload),
            "by_severity": dict(
                Counter(
                    determination.get("severity", "Unknown")
                    for determination in determinations
                )
            ),
            "by_confidence": dict(
                Counter(
                    determination.get("confidence", "Unknown")
                    for determination in determinations
                )
            ),
            "by_ident": dict(Counter(row.get("ident", "unknown") for row in payload)),
            "high_confidence_high_severity": sum(
                determination.get("severity") == "High"
                and determination.get("confidence") == "High"
                for determination in determinations
            ),
        }
    if name == "pip-audit" and isinstance(payload, dict):
        dependencies = payload.get("dependencies", [])
        return {
            "dependencies": len(dependencies),
            "vulnerabilities": sum(len(row.get("vulns", [])) for row in dependencies),
        }
    return None


def _policy_ok(
    name: str, returncode: int, summary: dict[str, Any] | None
) -> bool:
    """Evaluate audit findings instead of treating process success as policy success."""
    if returncode != 0:
        return False
    if name == "zizmor":
        return summary is not None and summary["high_confidence_high_severity"] == 0
    if name == "pip-audit":
        return summary is not None and summary["vulnerabilities"] == 0
    return True


def run_audits(
    *,
    repo_root: Path,
    tool_root: Path,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    head = _git_value(runner, repo_root, "rev-parse", "HEAD")
    branch = _git_value(runner, repo_root, "branch", "--show-current")
    status = _git_value(runner, repo_root, "status", "--porcelain")

    audits: list[dict[str, Any]] = []
    preparations: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="hermes-external-audit-") as temp_dir:
        requirements_path = Path(temp_dir) / "requirements.txt"
        export_command = build_uv_export_command(requirements_path=requirements_path)
        export_result = _run(runner, export_command, repo_root=repo_root, timeout=120)
        export_stdout = str(export_result.stdout or "")
        export_stderr = str(export_result.stderr or "")
        preparations.append({
            "name": "uv-lock-export",
            "command": list(export_command),
            "returncode": int(export_result.returncode),
            "stdout_sha256": _sha256(export_stdout),
            "stderr_sha256": _sha256(export_stderr),
            "stdout": _bounded_output(export_stdout),
            "stderr": _bounded_output(export_stderr),
        })

        for command in build_commands(
            repo_root=repo_root,
            tool_root=tool_root,
            requirements_path=requirements_path,
        ):
            version_result = _run(
                runner, command.version_argv, repo_root=repo_root, timeout=30
            )
            version_stdout = str(version_result.stdout or "")
            version_stderr = str(version_result.stderr or "")
            observed_version = _extract_version(version_stdout, version_stderr)
            version_matches = (
                version_result.returncode == 0
                and observed_version == command.expected_version
            )
            if command.name == "pip-audit" and export_result.returncode != 0:
                result: AuditProcessResult = SkippedAuditResult(
                    returncode=1,
                    stdout="",
                    stderr="uv lock export failed; pip-audit was not run",
                )
            else:
                result = _run(runner, command.argv, repo_root=repo_root, timeout=600)
            stdout = str(result.stdout or "")
            stderr = str(result.stderr or "")
            summary = _summarize_json_output(command.name, stdout)
            audits.append({
                "name": command.name,
                "expected_version": command.expected_version,
                "observed_version": observed_version,
                "version_matches": version_matches,
                "version_command": list(command.version_argv),
                "version_returncode": int(version_result.returncode),
                "version_stdout_sha256": _sha256(version_stdout),
                "version_stderr_sha256": _sha256(version_stderr),
                "version_stdout": _bounded_output(version_stdout),
                "version_stderr": _bounded_output(version_stderr),
                "command": list(command.argv),
                "returncode": int(result.returncode),
                "stdout_sha256": _sha256(stdout),
                "stderr_sha256": _sha256(stderr),
                "summary": summary,
                "policy_ok": _policy_ok(command.name, result.returncode, summary),
                "stdout": _bounded_output(stdout),
                "stderr": _bounded_output(stderr),
            })

    return {
        "schema_version": "hermes_external_tooling_audit_v1",
        "audited_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository": {
            "path": str(repo_root),
            "head": head,
            "branch": branch,
            "dirty": bool(status),
            "status_porcelain": status,
        },
        "read_only": True,
        "auto_fix": False,
        "ok": all(row["returncode"] == 0 for row in preparations)
        and all(row["version_matches"] and row["policy_ok"] for row in audits),
        "preparations": preparations,
        "audits": audits,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--tool-root",
        type=Path,
        default=get_hermes_home() / "tooling" / "capability-audit" / "venv" / "bin",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--plan", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo.resolve()
    tool_root = args.tool_root.expanduser().resolve()
    if args.plan:
        requirements_path = repo_root / ".audit-requirements.txt"
        payload = {
            "read_only": True,
            "auto_fix": False,
            "preparations": [
                {
                    "name": "uv-lock-export",
                    "argv": list(
                        build_uv_export_command(requirements_path=requirements_path)
                    ),
                }
            ],
            "commands": [
                {"name": command.name, "argv": list(command.argv)}
                for command in build_commands(
                    repo_root=repo_root,
                    tool_root=tool_root,
                    requirements_path=requirements_path,
                )
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    receipt = run_audits(repo_root=repo_root, tool_root=tool_root)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
