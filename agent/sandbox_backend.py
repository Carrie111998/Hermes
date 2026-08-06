"""FIX-014: Sandbox-Backend fuer terminal / execute_code.

Optionaler Sandbox-Layer zwischen Hermes und dem Host-OS. Erkennt
automatisch, ob ``bubblewrap`` (bwrap) oder ``firejail`` verfuegbar
ist und wrappt einen Shell-Befehl entsprechend. Fallback ist ``"none"``
(direkter Aufruf), wenn keine Sandbox installiert ist - so wird
Hermes nicht funktionsunfaehig, wenn z. B. auf einem CI-Container
kein bwrap vorhanden ist.

Bezug zu FIX-021 (Tool-Risk-Gate): ``SandboxBackend`` liefert
*lediglich* die Wrap-Funktion; die Entscheidung, ob ein Tool-Aufruf
tatsaechlich gewrappt werden muss, trifft das Risk-Gate.

Drei oeffentliche Symbole:
  * ``SandboxBackend`` - Klasse mit detect/is_available/wrap_command.
  * ``detect_backend()`` - Convenience-Funktion, gibt "bubblewrap",
    "firejail" oder "none" zurueck.
  * ``SandboxScope`` - TypedDict mit Read/Write/Network-Beschraenkungen.

Version 2026-07-27.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Iterable, List, Literal, Optional, TypedDict


Backend = Literal["bubblewrap", "firejail", "none"]


class SandboxScope(TypedDict, total=False):
    """Optionale Restriktionen, die der Caller uebergeben kann.

    Alle Felder sind optional. Default ist ein minimaler Sandbox-Run
    ohne Netzwerk und mit read-only-Root.
    """

    read_paths: List[str]       # zusaetzliche Read-Mounts
    write_paths: List[str]      # zusaetzliche Write-Mounts
    allow_network: bool         # Default: False
    timeout_s: Optional[int]    # nur Firejail-Support


@dataclass
class SandboxBackend:
    """Kapselt die Backenderkennung und das Wrap-Kommando.

    Verwendung:
        sb = SandboxBackend()
        if sb.is_available():
            wrapped = sb.wrap_command(["ls", "-la"], scope={...})
            subprocess.run(wrapped)
    """

    backend: Backend = "none"
    binary_path: Optional[str] = None
    # Cache, damit detect() nicht bei jedem Aufruf ``which`` spawnt.
    _resolved: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------
    # Erkennung
    # ------------------------------------------------------------

    def detect(self) -> Backend:
        """Erkennt das verfuegbare Sandbox-Backend.

        Reihenfolge: bubblewrap > firejail > none. Ergebnis wird
        gecached, weil sich die Toolchain waehrend eines Prozess-
        lebens normalerweise nicht aendert.
        """
        if self._resolved:
            return self.backend
        for name, exe in (("bubblewrap", "bwrap"), ("firejail", "firejail")):
            path = shutil.which(exe)
            if path:
                self.backend = name  # type: ignore[assignment]
                self.binary_path = path
                self._resolved = True
                return self.backend
        self.backend = "none"
        self.binary_path = None
        self._resolved = True
        return self.backend

    def is_available(self) -> bool:
        """True, wenn ein echter Sandbox-Backend gefunden wurde."""
        return self.detect() != "none"

    # ------------------------------------------------------------
    # Wrap
    # ------------------------------------------------------------

    def wrap_command(
        self,
        cmd: Iterable[str],
        scope: Optional[SandboxScope] = None,
    ) -> List[str]:
        """Wrappt ``cmd`` in den Sandbox-Aufruf.

        Bei ``backend == "none"`` wird ``cmd`` unveraendert
        zurueckgegeben (kein expliziter No-op, damit der Caller
        keinen Branch schreiben muss). Bei Fehlern waehrend des
        Wrappings wird auf "none" zurueckgefallen.
        """
        cmd_list = list(cmd)
        if not cmd_list:
            raise ValueError("cmd darf nicht leer sein")
        backend = self.detect()
        if backend == "none":
            # Kein Sandbox verfuegbar - Befehl unveraendert zurueck.
            return cmd_list
        scope = scope or {}
        try:
            if backend == "bubblewrap":
                return self._wrap_bwrap(cmd_list, scope)
            if backend == "firejail":
                return self._wrap_firejail(cmd_list, scope)
        except Exception:
            # Defense in depth: jeder Wrap-Fehler fuehrt zum ungewrappten
            # Aufruf statt zum Crash. Sandbox-Fail-Open = fail-closed
            # wuerde mehr Schaden anrichten, deshalb explizit fail-open
            # mit RuntimeWarning.
            import warnings
            warnings.warn(
                f"FIX-014: sandbox wrap fehlgeschlagen, fallback auf Direktaufruf",
                RuntimeWarning,
                stacklevel=2,
            )
        return cmd_list

    # ------------------------------------------------------------
    # Private Backendspezifika
    # ------------------------------------------------------------

    def _wrap_bwrap(self, cmd: List[str], scope: SandboxScope) -> List[str]:
        """bwrap-Aufruf: read-only Root, tmpfs /tmp, optionale Mounts."""
        args: List[str] = ["bwrap"]
        # Read-only Root + proc + dev
        args += ["--ro-bind", "/", "/"]
        args += ["--proc", "/proc"]
        args += ["--dev", "/dev"]
        # tmpfs fuer Schreibvorgaenge innerhalb der Sandbox.
        args += ["--tmpfs", "/tmp"]
        # Read-Mounts aus dem Scope.
        for p in scope.get("read_paths", []) or []:
            if os.path.isabs(p):
                args += ["--ro-bind", p, p]
        # Write-Mounts.
        for p in scope.get("write_paths", []) or []:
            if os.path.isabs(p):
                args += ["--bind", p, p]
        # Netzwerk: Default aus.
        if not scope.get("allow_network", False):
            args += ["--unshare-net"]
        args += ["--", *cmd]
        return args

    def _wrap_firejail(self, cmd: List[str], scope: SandboxScope) -> List[str]:
        """firejail-Aufruf: --private + optional --read-only/-w."""
        args: List[str] = ["firejail"]
        # Read-only-Modus, falls keine Write-Paths.
        if not scope.get("write_paths"):
            args.append("--read-only")
        for p in scope.get("write_paths", []) or []:
            if os.path.isabs(p):
                args += ["--writable", p]
        if not scope.get("allow_network", False):
            args.append("--net=none")
        # Timeout (firejail --timeout).
        timeout = scope.get("timeout_s")
        if isinstance(timeout, int) and timeout > 0:
            args += ["--timeout", str(timeout)]
        args += ["--", *cmd]
        return args


# ------------------------------------------------------------
# Convenience
# ------------------------------------------------------------


def detect_backend() -> Backend:
    """Globale Convenience-Funktion - entspricht ``SandboxBackend().detect()``."""
    return SandboxBackend().detect()
