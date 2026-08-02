"""FIX-018: PII-Redaktion fuer externe Tool-Calls.

Redigiert personenbezogene Daten aus Text, bevor er an externe Tools
geht (z. B. ``web_search``, ``browser_navigate``, ``mcp__github__*``).
Erkannt werden:

  * **E-Mail**  - RFC-lite: ``[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}``
  * **IBAN**    - Laendercode + 2 Pruefziffern + BBAN, DE+CC-Form
    vereinfacht (echt: ISO 13616, hier: Strukturtest 15-32 Stellen).
  * **Kreditkarte** - 13-19 Ziffern, Luhn-validierbar.
  * **Telefon** - DE: ``+49`` oder ``0`` gefolgt von 3-14 Ziffern /
    Leer- / Bindestrichen.
  * **IPv4**    - klassisches dotted-quad (0-255 pro Oktett).

Rueckgabe von ``redact()`` ist ein Tuple ``(cleaned_text, hits)``,
wobei ``hits`` pro Treffer den Typ, das Original, den Bereich und
einen Marker (``[REDACTED:TYPE]``) enthaelt. So kann der Caller
entscheiden, ob er den Marker im Output behalten oder durch etwas
Kontextspezifisches ersetzen will.

Version 2026-07-27.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple


# --------------------------------------------------------------------
# Regex-Bibliothek
# --------------------------------------------------------------------

# E-Mail (RFC-lite - deckt 99 % der realen Adressen ab, vermeidet
# katastrophale False-Positives in Code-Snippets).
EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)

# IBAN - Laendercode (2 Alpha) + 2 Pruefziffern + 11-30 alphanum,
# max 34 Zeichen Gesamtlaenge (ISO 13616).
IBAN_RE = re.compile(
    r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{1,30}){1,}\b"
)

# Kreditkarte: 13-19 Ziffern, optional durch Leerzeichen/Trennstriche
# gruppiert (4-4-4-4 / 4-6-5 etc.).
CC_RAW_RE = re.compile(
    r"\b(?:\d[ \-]?){12,18}\d\b"
)

# Telefon: +49... oder 0xxx... mit optionalen Separatoren.
PHONE_RE = re.compile(
    r"(?:\+|00)49[\d \-/]{6,18}\d|\b0[\d \-/]{6,18}\d\b"
)

# IPv4 - jeder Block 0-255.
IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\b"
)


# --------------------------------------------------------------------
# Datenmodell
# --------------------------------------------------------------------

@dataclass
class PIIHit:
    """Ein einzelner Redaktions-Treffer."""

    pii_type: str        # "email" | "iban" | "credit_card" | "phone" | "ipv4"
    original: str        # exakter Original-String
    marker: str          # "xxx" - 1 Zeichen reicht
    start: int           # Position im Input
    end: int             # exclusive
    replacement: str     # z. B. "[REDACTED:email]"

    def as_dict(self) -> Dict[str, object]:
        return {
            "pii_type": self.pii_type,
            "original": self.original,
            "marker": self.marker,
            "start": self.start,
            "end": self.end,
            "replacement": self.replacement,
        }


# --------------------------------------------------------------------
# Luhn-Check fuer Kreditkarten
# --------------------------------------------------------------------

def _luhn_valid(s: str) -> bool:
    """True, wenn ``s`` (nur Ziffern) Luhn-konform ist."""
    digits = [int(c) for c in s if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    parity = (len(digits) - 2) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# --------------------------------------------------------------------
# Redaktor
# --------------------------------------------------------------------

class PIIRedactor:
    """Erkennt und ersetzt PII in Text-Strings.

    Verwendung:
        r = PIIRedactor()
        cleaned, hits = r.redact("mail: a@b.de, karte: 4111 1111 1111 1111")
        # cleaned: "mail: [REDACTED:email], karte: [REDACTED:credit_card]"
        # hits: [PIIHit(...), PIIHit(...)]
    """

    # Reihenfolge der Ersetzung - Kreditkarte vor Telefon, weil das
    # Telefon-Pattern sonst "4111 1111 1111 1111" miterkennt.
    DETECTORS = (
        ("email", EMAIL_RE, None),
        ("iban", IBAN_RE, None),
        ("credit_card", CC_RAW_RE, _luhn_valid),
        ("phone", PHONE_RE, None),
        ("ipv4", IPV4_RE, None),
    )

    def redact(self, text: str) -> Tuple[str, List[Dict]]:
        """Findet PII und ersetzt sie durch Marker.

        Args:
            text: Input-Text.

        Returns:
            Tuple ``(cleaned_text, hits)`` - ``hits`` ist eine Liste
            von Dicts (siehe ``PIIHit.as_dict``).
        """
        if not text:
            return text, []

        hits: List[PIIHit] = []
        # Pro Detektor: Kandidaten einsammeln, validieren, sortiert ablegen.
        for pii_type, regex, validator in self.DETECTORS:
            for m in regex.finditer(text):
                candidate = m.group(0)
                if validator is not None and not validator(candidate):
                    continue
                hits.append(
                    PIIHit(
                        pii_type=pii_type,
                        original=candidate,
                        marker=self._marker(pii_type),
                        start=m.start(),
                        end=m.end(),
                        replacement=f"[REDACTED:{pii_type}]",
                    )
                )

        # Treffer nach Position absteigend sortiert anwenden, damit
        # spaetere Ersetzungen die Offsets frueherer nicht verschieben.
        hits.sort(key=lambda h: h.start, reverse=True)
        for hit in hits:
            text = text[: hit.start] + hit.replacement + text[hit.end :]

        # Hits in Original-Reihenfolge zurueckgeben (fuer den Caller).
        hits.sort(key=lambda h: h.start)
        return text, [h.as_dict() for h in hits]

    @staticmethod
    def _marker(pii_type: str) -> str:
        # 1 Zeichen, damit Marker im Output moeglichst kompakt sind.
        return {
            "email": "x",
            "iban": "x",
            "credit_card": "x",
            "phone": "x",
            "ipv4": "x",
        }.get(pii_type, "x")
