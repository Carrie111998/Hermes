"""FIX-017: Evaluation-Harness.

Run-Framework fuer Eval-Suites (golden / trajectory / red_team).
Liest JSONL-Suiten, ruft einen Runner je Case auf, vergleicht
Got vs Expected mit konfigurierbarer Toleranz (lenient/strict/
block_or_quarantine).

Public API:
    EvalCase    -- ein Test-Case aus der Suite
    EvalResult  -- Resultat eines Case-Runs
    Summary     -- aggregierte Metriken einer Suite
    EvalHarness -- orchestriert load + run + compare + aggregate
"""

from __future__ import annotations

# Standardbibliothek
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ein Runner ist eine Funktion (input: str) -> {"output": Any, "tokens_used": int, ...}
RunnerType = Callable[[str], Dict[str, Any]]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class EvalCase:
    """Ein einzelner Test-Case aus einer JSONL-Suite."""

    id: str
    intent: str = ""
    category: str = ""
    input: str = ""
    expected: Any = None
    expected_outcome: Any = None
    tolerance: str = "lenient"
    owner: str = ""
    # Falls das JSONL-File die anderen Felder unter 'expected_outcome'
    # statt 'expected' speichert, mappen wir das in __post_init__.

    def __post_init__(self) -> None:
        # Wenn 'expected' None aber 'expected_outcome' gesetzt -> uebernehmen.
        if self.expected is None and self.expected_outcome is not None:
            self.expected = self.expected_outcome
        # 'intent' wird auch als category akzeptiert.
        if not self.category and self.intent:
            self.category = self.intent


@dataclass
class EvalResult:
    """Resultat eines einzelnen Case-Runs."""

    case_id: str
    passed: bool
    score: float                  # 0.0 .. 1.0
    actual: Any = None
    expected: Any = None
    latency_ms: int = 0
    tokens_used: int = 0
    error: str = ""
    tolerance: str = "lenient"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Summary:
    """Aggregat-Metriken einer Suite."""

    suite: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    median_latency_ms: int
    total_tokens: int
    top_failures: List[str] = field(default_factory=list)
    results: List[EvalResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite": self.suite,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "median_latency_ms": self.median_latency_ms,
            "total_tokens": self.total_tokens,
            "top_failures": self.top_failures,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rouge_l(got: str, expected: str) -> float:
    """Sehr einfache Rouge-L-Approximation.

    Score = |longest common subsequence| / max(len(got), len(expected)).
    """
    if not expected:
        return 0.0
    if not got:
        return 0.0
    got_tokens = got.split()
    exp_tokens = expected.split()
    if not exp_tokens:
        return 0.0
    # LCS-Standardimplementierung
    m, n = len(got_tokens), len(exp_tokens)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if got_tokens[i - 1] == exp_tokens[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    lcs = dp[m][n]
    return lcs / max(m, n)


# ---------------------------------------------------------------------------
# EvalHarness
# ---------------------------------------------------------------------------
class EvalHarness:
    """Orchestriert Eval-Suite-Runs gegen einen beliebigen Runner."""

    # Toleranz-Schwellwert fuer 'lenient' (Rouge-L >= 0.3)
    LENIENT_THRESHOLD = 0.3

    # Gueltige Toleranz-Modi
    VALID_TOLERANCES = ("lenient", "strict", "block_or_quarantine")

    def __init__(self, runner: RunnerType) -> None:
        self.runner = runner

    # ------------------------------------------------------------------
    # Suite laden
    # ------------------------------------------------------------------
    # Erlaubte Felder einer EvalCase-Zeile. Alle anderen werden ignoriert,
    # damit trajectory/red_team-Suiten mit zusaetzlichen Feldern geladen
    # werden koennen ohne dass der Dataclass-Konstruktor bricht.
    _CASE_FIELDS = frozenset({
        "id", "intent", "category", "input", "expected",
        "expected_outcome", "tolerance", "owner",
    })

    def load_suite(self, suite_path: Path) -> List[EvalCase]:
        """Laedt eine JSONL-Suite in eine Liste von EvalCase."""
        suite_path = Path(suite_path)
        if not suite_path.exists():
            return []
        cases: List[EvalCase] = []
        for line in suite_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            filtered = {k: v for k, v in obj.items() if k in self._CASE_FIELDS}
            cases.append(EvalCase(**filtered))
        return cases

    # ------------------------------------------------------------------
    # Suite laufen
    # ------------------------------------------------------------------
    def run_suite(self, suite_path: Path) -> List[EvalResult]:
        """Laedt + laeuft eine Suite. Returnt Liste von EvalResult."""
        cases = self.load_suite(suite_path)
        results: List[EvalResult] = []
        for case in cases:
            results.append(self.run_case(case))
        return results

    def run_case(self, case: EvalCase) -> EvalResult:
        """Laeuft einen einzelnen Case via Runner und vergleicht."""
        start = time.monotonic()
        actual: Any = None
        tokens = 0
        error = ""
        try:
            response = self.runner(case.input)
            actual = response.get("output") if isinstance(response, dict) else response
            tokens = int(response.get("tokens_used", 0)) if isinstance(response, dict) else 0
        except Exception as exc:  # pragma: no cover - defensive
            error = str(exc)
            actual = None
        latency_ms = int((time.monotonic() - start) * 1000)
        passed, score = self._compare(actual, case.expected, case.tolerance)
        return EvalResult(
            case_id=case.id,
            passed=passed,
            score=score,
            actual=actual,
            expected=case.expected,
            latency_ms=latency_ms,
            tokens_used=tokens,
            error=error,
            tolerance=case.tolerance,
        )

    # ------------------------------------------------------------------
    # Compare
    # ------------------------------------------------------------------
    def _compare(self, got: Any, expected: Any, tolerance: str) -> Tuple[bool, float]:
        """Vergleicht Got vs Expected. Returnt (passed, score)."""
        tolerance = tolerance or "lenient"
        if tolerance == "strict":
            ok = got == expected
            return ok, 1.0 if ok else 0.0
        if tolerance == "lenient":
            if isinstance(got, str) and isinstance(expected, str):
                score = _rouge_l(got, expected)
            elif isinstance(got, str) and isinstance(expected, (list, tuple)):
                # Multiline-Expected: ueberpruefe ob alle Elemente in got vorkommen
                hits = sum(1 for e in expected if isinstance(e, str) and e in got)
                score = hits / max(len(expected), 1)
            else:
                # Default: exakter Match
                score = 1.0 if got == expected else 0.0
            return score >= self.LENIENT_THRESHOLD, score
        if tolerance == "block_or_quarantine":
            if isinstance(got, str):
                ok = got.lower() in {"block", "quarantine", "deny"}
            else:
                ok = got in {"block", "quarantine", "deny"}
            return ok, 1.0 if ok else 0.0
        # Unbekannte Toleranz = fail
        return False, 0.0

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    def aggregate(self, results: List[EvalResult], suite: str = "") -> Summary:
        """Aggregiert Resultate zu einer Summary."""
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed
        pass_rate = (passed / total) if total else 0.0
        latencies = sorted(r.latency_ms for r in results)
        median = latencies[len(latencies) // 2] if latencies else 0
        total_tokens = sum(r.tokens_used for r in results)
        # Top-3 Failures (case_id)
        failures = [r.case_id for r in results if not r.passed][:3]
        return Summary(
            suite=suite,
            total=total,
            passed=passed,
            failed=failed,
            pass_rate=pass_rate,
            median_latency_ms=median,
            total_tokens=total_tokens,
            top_failures=failures,
            results=list(results),
        )

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    def emit_report(self, summary: Summary, path: Path) -> Path:
        """Schreibt Markdown-Report."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        lines.append(f"# Eval-Suite Report: {summary.suite}")
        lines.append("")
        status = "PASS" if summary.failed == 0 else "FAIL"
        lines.append(f"**Status:** {status}")
        lines.append(f"**Pass-Rate:** {summary.passed}/{summary.total} "
                     f"({summary.pass_rate * 100:.1f}%)")
        lines.append(f"**Median Latency:** {summary.median_latency_ms}ms")
        lines.append(f"**Total Tokens:** {summary.total_tokens}")
        if summary.top_failures:
            lines.append("")
            lines.append("## Top Failures")
            for cid in summary.top_failures:
                lines.append(f"- {cid}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def emit_json(self, summary: Summary, path: Path) -> Path:
        """Schreibt JSON-Summary."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path
