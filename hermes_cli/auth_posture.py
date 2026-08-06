"""FIX-015: Auth-Posture-Review.

Prueft die Auth-Posture aller externen Integrationen gegen eine
12-Punkt-Checkliste. Findet Posture-Luecken (fehlende MFA, zu breite
Scopes, fehlender Audit-Trail, ...) und schreibt einen Report.

Public API:
    AuthIntegration       -- ein Auth-Endpoint
    Finding               -- ein Posture-Befund
    Report                -- Aggregat aus Integrationen + Findings
    AuthPostureReviewer   -- orchestriert Discover + Review + Emit
"""

from __future__ import annotations

# Standardbibliothek
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Drittpartei
import yaml


# Die 12-Punkt-Checkliste. Wird in review() als Reihenfolge der Pruefungen benutzt.
AUTH_CHECKS: List[str] = [
    "scope_least_privilege",          # 1
    "scope_bleed_prevented",          # 2
    "token_rotation_configured",      # 3
    "mfa_enforced",                   # 4
    "credential_storage_separated",   # 5
    "audit_trail_present",            # 6
    "expiry_configured",              # 7
    "least_privilege_scope_set",      # 8
    "no_wildcard_scopes",             # 9
    "no_admin_scopes_in_production",  # 10
    "env_var_for_secret",             # 11
    "provider_known",                 # 12
]


@dataclass
class AuthIntegration:
    """Eine einzelne Auth-Stelle (z. B. github, google, telegram)."""

    name: str
    provider: str = "unknown"
    scopes: List[str] = field(default_factory=list)
    credential_ref: Optional[str] = None
    mfa: bool = False
    token_rotation_days: int = 0
    audit_log: bool = False
    expiry_days: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    """Ein einzelner Posture-Befund."""

    integration: str
    check: str
    severity: str           # P0..P3
    rationale: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class Report:
    """Aggregat-Report einer Posture-Pruefung."""

    integrations: List[AuthIntegration]
    findings: List[Finding]

    @property
    def passed(self) -> bool:
        """True wenn keine P0/P1-Findings vorliegen."""
        return not any(f.severity in ("P0", "P1") for f in self.findings)

    def by_severity(self) -> Dict[str, int]:
        """Zaehlt Findings je Schweregrad."""
        out: Dict[str, int] = {}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out


class AuthPostureReviewer:
    """Orchestriert Auth-Posture-Review.

    Verwendung:
        reviewer = AuthPostureReviewer(Path("config.yaml"))
        integrations = reviewer.discover()
        report = reviewer.review(integrations)
        reviewer.emit_report(report, Path("auth-posture.md"))
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)

    def discover(self) -> List[AuthIntegration]:
        """Sammelt alle Auth-Integrationen aus der YAML-Config.

        Erwartetes Schema:
            auth:
              github:
                provider: oauth
                scopes: [repo, read:user]
                credential_env_var: GITHUB_TOKEN
                mfa: true
                token_rotation_days: 90
                audit_log: true
                expiry_days: 365
        """
        if not self.config_path.exists():
            return []
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        auth_section = cfg.get("auth", {}) or {}
        out: List[AuthIntegration] = []
        for name, section in auth_section.items():
            section = section or {}
            out.append(
                AuthIntegration(
                    name=name,
                    provider=section.get("provider", "unknown"),
                    scopes=list(section.get("scopes", []) or []),
                    credential_ref=section.get("credential_env_var"),
                    mfa=bool(section.get("mfa", False)),
                    token_rotation_days=int(section.get("token_rotation_days", 0) or 0),
                    audit_log=bool(section.get("audit_log", False)),
                    expiry_days=int(section.get("expiry_days", 0) or 0),
                    metadata={k: v for k, v in section.items()
                              if k not in {"provider", "scopes", "credential_env_var",
                                           "mfa", "token_rotation_days",
                                           "audit_log", "expiry_days"}},
                )
            )
        return out

    def review(self, integrations: List[AuthIntegration]) -> Report:
        """Prueft jede Integration gegen die 12-Punkt-Checkliste."""
        findings: List[Finding] = []
        for integ in integrations:
            findings.extend(self._check(integ))
        return Report(integrations=integrations, findings=findings)

    def _check(self, integ: AuthIntegration) -> List[Finding]:
        """Fuehrt alle 12 Checks fuer eine einzelne Integration aus."""
        f: List[Finding] = []

        # 1+2: Scope-Least-Privilege / Bleed-Prevention
        if "*" in integ.scopes or "admin" in integ.scopes:
            f.append(Finding(
                integration=integ.name,
                check="scope_least_privilege",
                severity="P1",
                rationale=f"Scopes enthalten Wildcard/Admin: {integ.scopes}",
            ))
            f.append(Finding(
                integration=integ.name,
                check="scope_bleed_prevented",
                severity="P1",
                rationale="Admin/Wildcard-Scope kann auf andere Workspaces ueberlaufen",
            ))

        # 3: Token-Rotation
        if integ.token_rotation_days <= 0:
            f.append(Finding(
                integration=integ.name,
                check="token_rotation_configured",
                severity="P2",
                rationale="Keine Token-Rotation konfiguriert",
            ))

        # 4: MFA
        if not integ.mfa:
            f.append(Finding(
                integration=integ.name,
                check="mfa_enforced",
                severity="P1",
                rationale="Keine MFA-Erzwingung im Manifest konfiguriert",
            ))

        # 5: Credential-Storage
        if not integ.credential_ref:
            f.append(Finding(
                integration=integ.name,
                check="credential_storage_separated",
                severity="P1",
                rationale="credential_env_var fehlt -> Secret im Klartext moeglich",
            ))

        # 6: Audit-Trail
        if not integ.audit_log:
            f.append(Finding(
                integration=integ.name,
                check="audit_trail_present",
                severity="P2",
                rationale="Kein audit_log-Flag -> kein Nachweis der Nutzung",
            ))

        # 7: Expiry
        if integ.expiry_days <= 0:
            f.append(Finding(
                integration=integ.name,
                check="expiry_configured",
                severity="P3",
                rationale="Kein Token-Expiry konfiguriert",
            ))

        # 8: Least-Privilege-Scope gesetzt
        if "least_privilege_scope" not in integ.metadata:
            f.append(Finding(
                integration=integ.name,
                check="least_privilege_scope_set",
                severity="P2",
                rationale="least_privilege_scope nicht im Manifest gesetzt",
            ))

        # 9+10: Wildcard/Admin-Scopes in Production
        if "*" in integ.scopes:
            f.append(Finding(
                integration=integ.name,
                check="no_wildcard_scopes",
                severity="P1",
                rationale="Wildcard-Scope '*' in Production unsicher",
            ))
        if "admin" in integ.scopes:
            f.append(Finding(
                integration=integ.name,
                check="no_admin_scopes_in_production",
                severity="P1",
                rationale="Admin-Scope in Production vermeiden",
            ))

        # 11: Env-Var fuer Secret
        if integ.credential_ref and not integ.credential_ref.startswith("$") and \
                len(integ.credential_ref) > 0 and not integ.credential_ref.isupper():
            f.append(Finding(
                integration=integ.name,
                check="env_var_for_secret",
                severity="P3",
                rationale=f"credential_env_var '{integ.credential_ref}' ist kein Uppercase-Env-Namen",
            ))

        # 12: Provider bekannt
        if integ.provider == "unknown":
            f.append(Finding(
                integration=integ.name,
                check="provider_known",
                severity="P2",
                rationale="Provider nicht spezifiziert",
            ))

        return f

    def emit_report(self, report: Report, path: Path) -> Path:
        """Schreibt den Report als Markdown-Datei."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        lines.append("# Auth-Posture Report")
        lines.append("")
        lines.append(f"**Status:** {'PASS' if report.passed else 'FAIL'}")
        lines.append(f"**Integrations:** {len(report.integrations)}")
        sev = report.by_severity()
        lines.append(f"**Findings by severity:** {sev}")
        lines.append("")

        for integ in report.integrations:
            lines.append(f"## {integ.name}")
            lines.append(f"- Provider: `{integ.provider}`")
            lines.append(f"- Scopes: `{integ.scopes}`")
            lines.append(f"- Credential-Env: `{integ.credential_ref}`")
            lines.append(f"- MFA: `{integ.mfa}`")
            lines.append(f"- Token-Rotation: `{integ.token_rotation_days}d`")
            lines.append(f"- Audit-Log: `{integ.audit_log}`")
            lines.append(f"- Expiry: `{integ.expiry_days}d`")
            lines.append("")

        if report.findings:
            lines.append("## Findings")
            for f in report.findings:
                lines.append(
                    f"- **{f.severity}** `{f.integration}.{f.check}` — {f.rationale}"
                )
        else:
            lines.append("## Findings")
            lines.append("_Keine Findings._")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def emit_json(self, report: Report, path: Path) -> Path:
        """Schreibt den Report als JSON-Datei (maschinenlesbar)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "passed": report.passed,
            "by_severity": report.by_severity(),
            "integrations": [asdict(i) for i in report.integrations],
            "findings": [f.to_dict() for f in report.findings],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path
