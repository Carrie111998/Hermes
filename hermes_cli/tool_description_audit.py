"""FIX-016: Tool-Description-Audit.

Prueft alle Tool-Descriptions gegen 8 Heuristiken (Laenge,
Side-Effect-Warnung, Approval-Erwaehnung, Timeout, vage Verben,
Imperativ, Side-Effect-Class, Beispiel).

Liest aus tool-manifest.yaml und schreibt einen Markdown-Report
mit Findings.

Public API:
    ToolDescriptionAuditor  -- der Auditor
    Finding                 -- ein Audit-Befund
    Report                  -- Aggregat
"""

from __future__ import annotations

# Standardbibliothek
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Drittpartei
import yaml


# Side-Effect-Warnwoerter. MUSS in Description enthalten sein bei
# high-risk Tools (destructive, external_write, financial, regulated).
SIDE_EFFECT_WARNINGS = (
    "destroys", "persists", "external", "irreversible",
    "cannot be undone", "permanent", "destructive",
    "deletes", "writes to", "modifies",
)

# Vage Verben, die in Description NICHT vorkommen sollten.
VAGUE_VERBS = ("maybe", "might", "try", "perhaps", "could", "possibly")

# Side-Effect-Klassen, die zwingend eine Warnung brauchen.
HIGH_RISK_CLASSES = {
    "destructive", "external_write", "financial",
    "regulated", "critical", "privileged",
}


@dataclass
class Finding:
    """Ein einzelner Audit-Befund."""

    tool: str
    check: str
    severity: str           # P0..P3
    rationale: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class Report:
    """Audit-Aggregat."""

    tools: List[Dict[str, Any]]
    findings: List[Finding]

    @property
    def passed(self) -> bool:
        """True wenn keine P0-Findings vorliegen."""
        return not any(f.severity == "P0" for f in self.findings)

    def by_check(self) -> Dict[str, int]:
        """Zaehlt Findings je Heuristik."""
        out: Dict[str, int] = {}
        for f in self.findings:
            out[f.check] = out.get(f.check, 0) + 1
        return out


class ToolDescriptionAuditor:
    """Orchestriert Tool-Description-Audit.

    Verwendung:
        auditor = ToolDescriptionAuditor(Path("tool-manifest.yaml"))
        report = auditor.audit_all()
        auditor.emit_report(report, Path("out.md"))
    """

    # Mindest- und Maximal-Laenge einer Description (Zeichen).
    MIN_DESC_LEN = 20
    MAX_DESC_LEN = 500

    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = Path(manifest_path)

    # ------------------------------------------------------------------
    # Manifest laden
    # ------------------------------------------------------------------
    def _load_manifest(self) -> List[Dict[str, Any]]:
        """Laedt die Tool-Liste aus YAML-Manifest."""
        if not self.manifest_path.exists():
            return []
        cfg = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8")) or {}
        tools = cfg.get("tools", []) or []
        # Stelle sicher, dass jede Tool-Def ein 'description' hat.
        for t in tools:
            t.setdefault("description", "")
        return tools

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    def audit_all(self) -> Report:
        """Audit jedes Tool im Manifest."""
        tools = self._load_manifest()
        findings: List[Finding] = []
        for t in tools:
            findings.extend(self.audit_one(t))
        return Report(tools=tools, findings=findings)

    def audit_one(self, tool_def: Dict[str, Any]) -> List[Finding]:
        """Audit einer einzelnen Tool-Definition."""
        name = tool_def.get("name", "<unknown>")
        desc = tool_def.get("description", "") or ""
        side_class = tool_def.get("side_effect_class", "")
        approval = tool_def.get("required_approval", "never")
        timeout = tool_def.get("timeout_ms", 0)
        findings: List[Finding] = []
        desc_lc = desc.lower()

        # 1. Laenge
        if not (self.MIN_DESC_LEN <= len(desc) <= self.MAX_DESC_LEN):
            findings.append(Finding(
                tool=name,
                check="description_length",
                severity="P2",
                rationale=f"Laenge {len(desc)} nicht in [{self.MIN_DESC_LEN}, {self.MAX_DESC_LEN}]",
            ))

        # 2. Side-Effect-Warnung bei high-risk
        if side_class in HIGH_RISK_CLASSES:
            if not any(w in desc_lc for w in SIDE_EFFECT_WARNINGS):
                findings.append(Finding(
                    tool=name,
                    check="side_effect_warning",
                    severity="P0",
                    rationale=(f"High-risk tool ({side_class}) ohne "
                               f"Side-Effect-Warnung in Description"),
                ))

        # 3. Approval-Erwaehnung
        if approval in {"when_workspace", "when_external", "always"}:
            if "approval" not in desc_lc and "confirm" not in desc_lc \
                    and "permission" not in desc_lc and "ask" not in desc_lc:
                findings.append(Finding(
                    tool=name,
                    check="approval_mentioned",
                    severity="P2",
                    rationale=(f"Tool braucht Approval ({approval}) aber "
                               f"Description erwaehnt keine User-Bestaetigung"),
                ))

        # 4. Timeout-Wert genannt
        if timeout and timeout > 0:
            if "timeout" not in desc_lc and str(timeout) not in desc:
                findings.append(Finding(
                    tool=name,
                    check="timeout_mentioned",
                    severity="P3",
                    rationale=f"Timeout {timeout}ms nicht in Description erwaehnt",
                ))

        # 5. Vage Verben
        vague = [v for v in VAGUE_VERBS if re.search(rf"\b{v}\b", desc_lc)]
        if vague:
            findings.append(Finding(
                tool=name,
                check="vague_verbs",
                severity="P2",
                rationale=f"Vage Verben gefunden: {vague}",
            ))

        # 6. Imperativ-Satz (mind. einer)
        sentences = re.split(r"[.!?]+", desc)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            findings.append(Finding(
                tool=name,
                check="imperative_sentence",
                severity="P3",
                rationale="Description enthaelt keinen vollstaendigen Satz",
            ))
        else:
            # Sehr einfach: kein Imperativ-Satz startet mit "the", "a", "an"
            non_imperative = [s for s in sentences
                              if s.split()[0].lower() in {"the", "a", "an"}]
            if len(non_imperative) == len(sentences):
                findings.append(Finding(
                    tool=name,
                    check="imperative_sentence",
                    severity="P3",
                    rationale="Kein Satz startet mit Imperativ-Verb",
                ))

        # 7. Side-Effect-Class explizit erwaehnt
        if side_class and side_class not in desc_lc:
            findings.append(Finding(
                tool=name,
                check="side_effect_class_mentioned",
                severity="P3",
                rationale=f"Side-Effect-Class '{side_class}' nicht in Description",
            ))

        # 8. Beispiel-Aufruf
        if "example" not in desc_lc and "e.g." not in desc_lc \
                and "for example" not in desc_lc and "such as" not in desc_lc:
            findings.append(Finding(
                tool=name,
                check="example_present",
                severity="P3",
                rationale="Description enthaelt kein Beispiel ('e.g.', 'example:')",
            ))

        return findings

    # ------------------------------------------------------------------
    # Emit
    # ------------------------------------------------------------------
    def emit_report(self, report: Report, path: Path) -> Path:
        """Schreibt Markdown-Report."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        lines.append("# Tool-Description Audit")
        lines.append("")
        lines.append(f"**Status:** {'PASS' if report.passed else 'FAIL'}")
        lines.append(f"**Tools:** {len(report.tools)}")
        lines.append(f"**Findings:** {len(report.findings)}")
        lines.append("")
        lines.append("## Findings by check")
        for k, v in sorted(report.by_check().items(), key=lambda x: -x[1]):
            lines.append(f"- `{k}`: {v}")
        lines.append("")
        if report.findings:
            lines.append("## Findings (Detail)")
            # gruppiert nach Tool
            by_tool: Dict[str, List[Finding]] = {}
            for f in report.findings:
                by_tool.setdefault(f.tool, []).append(f)
            for tool, fs in by_tool.items():
                lines.append(f"### {tool}")
                for f in fs:
                    lines.append(f"- **{f.severity}** `{f.check}` — {f.rationale}")
                lines.append("")
        else:
            lines.append("_Keine Findings._")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def emit_json(self, report: Report, path: Path) -> Path:
        """Schreibt JSON-Report."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "passed": report.passed,
            "by_check": report.by_check(),
            "tools": report.tools,
            "findings": [f.to_dict() for f in report.findings],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path
