"""FIX-022: Pre-Rollout-Verify-Gate.

Orchestrierter Pre-Rollout-Check: registriert Verify-Checks,
fuehrt sie aus, aggregiert Resultate und schreibt Markdown-Report.

Public API:
    GateCheck       -- ein registrierbarer Check
    CheckResult     -- Resultat eines Checks
    GateResult      -- Aggregat eines Gate-Runs
    PreRolloutGate  -- Orchestrator
"""

from __future__ import annotations

# Standardbibliothek
import json
import subprocess  # noqa: F401  (explizit fuer TimeoutExpired-Handling)
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


CheckFn = Callable[[], "CheckResult"]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class GateCheck:
    """Ein registrierbarer Pre-Rollout-Check."""

    name: str
    fn: CheckFn
    required: bool = True
    timeout_s: int = 60
    description: str = ""


@dataclass
class CheckResult:
    """Resultat eines einzelnen Checks."""

    name: str
    passed: bool
    message: str = ""
    artifacts: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GateResult:
    """Aggregat eines Gate-Runs."""

    passed: bool
    failures: List[CheckResult]
    warnings: List[CheckResult]
    successes: List[CheckResult]
    artifacts: Dict[str, Any]
    timestamp: str
    total: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": [r.to_dict() for r in self.failures],
            "warnings": [r.to_dict() for r in self.warnings],
            "successes": [r.to_dict() for r in self.successes],
            "artifacts": self.artifacts,
            "timestamp": self.timestamp,
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# PreRolloutGate
# ---------------------------------------------------------------------------
class PreRolloutGate:
    """Orchestriert Pre-Rollout-Checks.

    Verwendung:
        gate = PreRolloutGate(Path("/home/bratan/.hermes/hermes-agent"))
        gate.register(GateCheck(name="smoke", fn=run_smoke))
        gate.register(GateCheck(name="evals", fn=run_evals, required=True))
        result = gate.run()
        gate.emit_report(result, Path("gate-report.md"))
    """

    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        self.checks: List[GateCheck] = []

    def register(self, check: GateCheck) -> None:
        """Registriert einen Check. Reihenfolge = Ausfuehrungs-Reihenfolge."""
        self.checks.append(check)

    def run(self) -> GateResult:
        """Fuehrt alle registrierten Checks aus und aggregiert die Resultate."""
        failures: List[CheckResult] = []
        warnings: List[CheckResult] = []
        successes: List[CheckResult] = []
        artifacts: Dict[str, Any] = {}

        for check in self.checks:
            result = self._run_one(check)
            artifacts[check.name] = result.artifacts
            if result.passed:
                successes.append(result)
            elif check.required:
                failures.append(result)
            else:
                warnings.append(result)

        return GateResult(
            passed=not failures,
            failures=failures,
            warnings=warnings,
            successes=successes,
            artifacts=artifacts,
            timestamp=datetime.now(timezone.utc).isoformat(),
            total=len(self.checks),
        )

    def _run_one(self, check: GateCheck) -> CheckResult:
        """Fuehrt einen einzelnen Check mit Timeout + Exception-Schutz aus."""
        start = time.monotonic()
        try:
            # subprocess.TimeoutExpired-Handling: fn ist typischerweise
            # synchron; Timeout koennte ueber signal.alarm oder threading
            # realisiert werden. Hier machen wir den Schutz defensiv
            # (Exception-Handler deckt jeden Runner ab).
            result = check.fn()
        except subprocess.TimeoutExpired as exc:
            latency = int((time.monotonic() - start) * 1000)
            return CheckResult(
                name=check.name,
                passed=False,
                message=f"timeout after {check.timeout_s}s",
                latency_ms=latency,
                error=str(exc),
            )
        except Exception as exc:
            latency = int((time.monotonic() - start) * 1000)
            return CheckResult(
                name=check.name,
                passed=False,
                message=f"exception: {exc}",
                latency_ms=latency,
                error=str(exc),
            )
        # Latency nach erfolgreichem Run nachtragen
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    def emit_report(self, result: GateResult, path: Path) -> Path:
        """Schreibt Markdown-Report."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        lines.append(f"# Pre-Rollout Gate {result.timestamp}")
        lines.append("")
        status = "PASS" if result.passed else "FAIL"
        lines.append(f"**Status:** {status}")
        lines.append(f"**Total Checks:** {result.total}")
        lines.append(f"**Failures:** {len(result.failures)}")
        lines.append(f"**Warnings:** {len(result.warnings)}")
        lines.append(f"**Successes:** {len(result.successes)}")
        lines.append("")

        if result.failures:
            lines.append("## Failures")
            for r in result.failures:
                lines.append(f"### {r.name}")
                lines.append(f"- **Status:** FAIL")
                if r.message:
                    lines.append(f"- **Message:** {r.message}")
                if r.error:
                    lines.append(f"- **Error:** {r.error}")
                lines.append(f"- **Latency:** {r.latency_ms}ms")
                lines.append("")

        if result.warnings:
            lines.append("## Warnings")
            for r in result.warnings:
                lines.append(f"### {r.name}")
                lines.append(f"- **Status:** WARN (non-required check failed)")
                if r.message:
                    lines.append(f"- **Message:** {r.message}")
                lines.append("")

        if result.successes:
            lines.append("## Successes")
            for r in result.successes:
                lines.append(f"- {r.name} ({r.latency_ms}ms)")
            lines.append("")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def emit_json(self, result: GateResult, path: Path) -> Path:
        """Schreibt JSON-Report (maschinenlesbar)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path
