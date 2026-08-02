"""FIX-021: Tool-Risk-Gate - zentrale Risk-Klassifikation.

Liest das Tool-Manifest (``release/tool-manifest.yaml``, FIX-013) und
entscheidet pro Tool-Aufruf, ob und wie eine Approval-Pflicht greift.
Ergebnis ist ein ``ApprovalDecision`` mit:

  * ``tool_name``       - der evaluierte Tool-Name.
  * ``risk_class``      - ``read_only | workspace_write | privileged |
    external_write | financial | destructive | regulated | unknown``.
  * ``decision``        - ``allow | ask | deny``.
  * ``rationale``       - kurze Begruendung (max. 500 Zeichen).
  * ``required_approval`` - ``never | when_workspace | when_external |
    always``.
  * ``timeout_ms``      - hartes Timeout aus dem Manifest.
  * ``retry``           - konfigurierte Retry-Anzahl.

Entscheidungsregeln (siehe auch manifest.yaml):
  * ``read_only``            -> ``allow`` (nie Approve noetig)
  * ``workspace_write``      -> ``ask`` (User fragen, ausser Manifest sagt always)
  * ``external_write``       -> ``ask`` (immer, weil Daten nach aussen gehen)
  * ``privileged``           -> ``ask`` (immer, weil hochprivilegierte Aktion)
  * ``financial``            -> ``deny`` ausser Manifest sagt always
  * ``destructive``          -> ``deny`` ausser Manifest sagt always
  * ``regulated``            -> ``ask`` (Compliance-Review noetig)
  * ``unknown`` (nicht im Manifest) -> ``ask`` mit risk_class ``privileged``
    (Default-Privileged-Strategie, siehe P1-Definition).

Version 2026-07-27.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# Default-Manifest-Pfad - kann im Test ueberschrieben werden.
DEFAULT_MANIFEST = "/home/bratan/20-Workspace/release/tool-manifest.yaml"


# --------------------------------------------------------------------
# Datenmodell
# --------------------------------------------------------------------

@dataclass
class ApprovalDecision:
    """Ergebnis einer Risk-Evaluation."""

    tool_name: str
    risk_class: str
    decision: str                       # "allow" | "ask" | "deny"
    rationale: str
    required_approval: str = "never"    # "never" | "when_workspace" |
                                       # "when_external" | "always"
    timeout_ms: int = 10000
    retry: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "risk_class": self.risk_class,
            "decision": self.decision,
            "rationale": self.rationale,
            "required_approval": self.required_approval,
            "timeout_ms": self.timeout_ms,
            "retry": self.retry,
            "metadata": self.metadata,
        }


# --------------------------------------------------------------------
# Risk-Gate
# --------------------------------------------------------------------

class ToolRiskGate:
    """Liest ein Tool-Manifest und entscheidet pro Tool-Aufruf.

    Verwendung:
        gate = ToolRiskGate()  # liest DEFAULT_MANIFEST
        d = gate.evaluate("terminal", {"command": "rm -rf /tmp/x"})
        # d.risk_class == "destructive"
        # d.decision in ("ask", "deny")
    """

    # Mapping Risk-Class -> (default_decision, default_required_approval)
    # Wird durch das Manifest pro Tool ueberschrieben, falls vorhanden.
    DEFAULT_POLICY = {
        "read_only": ("allow", "never"),
        "workspace_write": ("ask", "when_workspace"),
        "privileged": ("ask", "when_workspace"),
        "external_write": ("ask", "when_external"),
        "financial": ("deny", "always"),
        "destructive": ("deny", "always"),
        "regulated": ("ask", "always"),
        "unknown": ("ask", "always"),     # Default-Privileged-Strategie
    }

    def __init__(self, manifest_path: Optional[str] = None) -> None:
        # Wenn None -> Default. Sonst Pfad verwenden.
        path = manifest_path or DEFAULT_MANIFEST
        self.manifest_path = path
        self._tools_index: Dict[str, Dict[str, Any]] = {}
        self._manifest_version: str = "0"
        self._load_manifest(path)

    def _load_manifest(self, path: str) -> None:
        """Laedt YAML und baut einen name -> entry Index."""
        p = Path(path)
        if not p.is_file():
            # Fehlendes Manifest ist KEIN harter Fehler - alle Tools
            # landen dann im unknown-Pfad.
            self._tools_index = {}
            self._manifest_version = "missing"
            return
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception:
            self._tools_index = {}
            self._manifest_version = "invalid"
            return
        if not isinstance(data, dict):
            self._tools_index = {}
            self._manifest_version = "invalid"
            return
        self._manifest_version = str(data.get("version", "0"))
        for entry in data.get("tools", []) or []:
            if isinstance(entry, dict) and "name" in entry:
                self._tools_index[str(entry["name"])] = entry

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def lookup(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Direkter Manifest-Lookup. None, wenn Tool unbekannt."""
        return self._tools_index.get(tool_name)

    def evaluate(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> ApprovalDecision:
        """Evaluiert einen Tool-Aufruf und liefert eine ApprovalDecision.

        Args:
            tool_name: Name des Tools (z. B. ``"terminal"``).
            arguments: Argumente (aktuell nur fuer Metadaten verwendet,
                z. B. ``{"path": "..."}``; die eigentliche Logik basiert
                auf dem Manifest).

        Returns:
            ``ApprovalDecision`` mit risk_class, decision, rationale.
        """
        arguments = arguments or {}
        entry = self._tools_index.get(tool_name)

        if entry is None:
            # Default-Privileged-Strategie: unbekanntes Tool = ask +
            # risk_class=privileged. Rationale nennt Toolnamen + Manifest-
            # Version.
            return ApprovalDecision(
                tool_name=tool_name,
                risk_class="privileged",
                decision="ask",
                rationale=(
                    f"Tool {tool_name!r} nicht im Manifest "
                    f"(version={self._manifest_version}); "
                    "Default-Privileged-Strategie greift."
                ),
                required_approval="always",
                metadata={"manifest_path": self.manifest_path},
            )

        risk_class = str(entry.get("side_effect_class", "unknown"))
        required_approval = str(entry.get("required_approval", "never"))
        timeout_ms = int(entry.get("timeout_ms", 10000))
        retry = int(entry.get("retry", 0))

        # Decision ableiten.
        decision = self._derive_decision(risk_class, required_approval)

        rationale = (
            f"Manifest-Eintrag version={self._manifest_version} "
            f"risk_class={risk_class} required_approval={required_approval}"
        )

        return ApprovalDecision(
            tool_name=tool_name,
            risk_class=risk_class,
            decision=decision,
            rationale=rationale[:500],
            required_approval=required_approval,
            timeout_ms=timeout_ms,
            retry=retry,
            metadata={"arguments_keys": sorted(arguments.keys())},
        )

    # ------------------------------------------------------------
    # Internes
    # ------------------------------------------------------------

    def _derive_decision(self, risk_class: str, required_approval: str) -> str:
        """Entscheidungslogik: risk_class + approval -> decision."""
        # Wenn Manifest "always" sagt, immer ask.
        if required_approval == "always":
            # destructive/financial -> deny by default
            if risk_class in ("destructive", "financial"):
                return "deny"
            return "ask"
        # read_only + never -> allow
        if required_approval == "never" and risk_class == "read_only":
            return "allow"
        # Alles andere: ask (User entscheidet ueber Tool-Gateway).
        return "ask"
