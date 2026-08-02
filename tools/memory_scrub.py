"""FIX-019: Memory-Scrubbing vor ``mnemosyne_remember``.

Bevor Inhalte ins Langzeitgedaechtnis geschrieben werden, muessen
Secrets entfernt werden, die versehentlich im Kontext gelandet sind
(Logs, Stacktraces, User-Inputs). Erkannt werden:

  * **AWS Access Key**  - ``AKIA[0-9A-Z]{16}``
  * **GitHub PAT**      - ``ghp_[a-zA-Z0-9]{36}``
  * **JWT**             - ``eyJ<base64>.eyJ<base64>(.<base64>)?``
  * **Passwort in URL** - ``password=...`` (bis ``&`` oder ``"``)

Ersetzt wird jeder Treffer durch ``[REDACTED:<typ>]``, damit spaetere
Audit-Trails sehen, dass hier etwas stand. Rueckgabe ist
``(cleaned, count)``.

Anders als ``PIIRedactor`` (FIX-018) ist dieser Schritt destruktiv:
Es gibt *keine* Liste der Originale, weil Geheimnisse nicht in Logs
landen duerfen - auch nicht als redacted-diff.

Version 2026-07-27.
"""
from __future__ import annotations

import re
from typing import Tuple


# --------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------

# AWS Access Key ID (20 Zeichen, AKIA + 16 alphanum).
AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# GitHub Personal Access Token (classic): ghp_ + 36 alphanum.
GH_TOKEN_RE = re.compile(r"\bghp_[a-zA-Z0-9]{36}\b")

# JWT: header.payload.signature - 3 Base64URL-Segmente, getrennt durch '.'.
# Base64URL = [A-Za-z0-9_-]. Wir verlangen min. 10 Zeichen pro Segment,
# damit "ab.cd.ef" nicht matcht.
JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}"
    r"(?:\.[A-Za-z0-9_\-]{10,})?\b"
)

# Passwort in URL/Query: password=... - Wert endet bei &, Leerzeichen
# oder Stringende. Wir matchen "password=" gefolgt von Wert, NICHT
# greedy.
PASSWORD_URL_RE = re.compile(
    r"(?i)(password\s*=\s*)([^\s&\"'<>]+)"
)

# Generisches Bearer-Token (Bonus, nicht in der Spec, aber haeufig).
BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}")


# --------------------------------------------------------------------
# Redaktor
# --------------------------------------------------------------------

class MemoryScrubber:
    """Erkennt und ersetzt Secrets in Memory-Texten.

    Verwendung:
        s = MemoryScrubber()
        cleaned, n = s.scrub("used AKIAIOSFODNN7EXAMPLE for sign-in")
        # cleaned: "used [REDACTED:aws_key] for sign-in"
        # n: 1
    """

    # Reihenfolge: spezifisch (AWS, ghp_, JWT) zuerst, generisch zuletzt,
    # damit das Passwort-Pattern nicht z. B. "Bearer=..." mitsauegt.
    PATTERNS = (
        ("aws_key", AWS_KEY_RE),
        ("github_token", GH_TOKEN_RE),
        ("jwt", JWT_RE),
        ("password_url", PASSWORD_URL_RE),
        ("bearer", BEARER_RE),
    )

    def scrub(self, content: str) -> Tuple[str, int]:
        """Ersetzt alle Secrets durch Marker.

        Args:
            content: Input-Text (typischerweise Mnemosyne-Inhalt).

        Returns:
            Tuple ``(cleaned, count)``. ``count`` ist die Gesamtanzahl
            der Ersetzungen ueber alle Patterntypen hinweg.
        """
        if not content:
            return content, 0

        total = 0
        for ptype, regex in self.PATTERNS:
            if ptype == "password_url":
                # Sonderfall: wir wollen nur den Wert ersetzen, nicht
                # den Praefix "password=".
                content, n = regex.subn(
                    lambda m: m.group(1) + "[REDACTED:password_url]",
                    content,
                )
            elif ptype == "bearer":
                content, n = regex.subn(
                    lambda m: m.group(1) + "[REDACTED:bearer]",
                    content,
                )
            else:
                content, n = regex.subn(f"[REDACTED:{ptype}]", content)
            total += n
        return content, total
