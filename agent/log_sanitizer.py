"""FIX-020: PII-Masking in Logs.

Sanitisiert Log-Texte BEVOR sie auf Disk landen. Drei Policies:
- mask:     PII -> [MASKED_<KIND>]
- redact:   PII -> [REDACTED]
- hash:     PII -> 8-stelliger SHA256-Hex

Unterstuetzte PII-Typen: email, phone_de, iban_de, credit_card, ipv4.

Public API:
    LogSanitizer(policy)  -- die Sanitizer-Klasse
    sanitize(text, policy)  -- Convenience-Funktion
"""

from __future__ import annotations

# Standardbibliothek
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, Literal, Optional


# Regex-Pattern fuer die 5 PII-Typen.
# Wichtig: Reihenfolge = Iterations-Reihenfolge. IBAN/CC MUSS vor
# phone_de kommen, weil sonst phone_de mitten in einer IBAN matcht
# und die IBAN zerstoert (z. B. "DE89 3704 0044 0532" -> phone).
PII_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "iban_de": re.compile(
        r"\bDE\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{2}\b"
    ),
    "credit_card": re.compile(r"\b(?:\d[\s\-]?){13,19}\b"),
    "email": re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "ipv4": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "phone_de": re.compile(
        r"(?:\+49|0)\s?\(?\d{2,4}\)?[\s\-]?\d{3,}[\s\-]?\d{3,}"
    ),
}

Policy = Literal["mask", "redact", "hash"]

# Gueltige Policies
VALID_POLICIES = ("mask", "redact", "hash")


def _make_hash_token(match: "re.Match[str]") -> str:
    """Berechnet einen 8-stelligen SHA256-Hex aus dem Match."""
    h = hashlib.sha256(match.group().encode("utf-8")).hexdigest()
    return f"[HASH_{h[:8]}]"


class LogSanitizer:
    """Sanitisiert Log-Texte gemaess gewaehlter Policy.

    Verwendung:
        s = LogSanitizer(policy="mask")
        s.sanitize("Email: foo@bar.com")  # -> "Email: [MASKED_EMAIL]"
        s.sanitize_dict({"k": "foo@bar.com"})  # -> {"k": "[MASKED_EMAIL]"}

        # In Logging-Handler einhaengen:
        handler = logging.FileHandler("/var/log/hermes.log")
        s.install_file_handler_filter(handler)
    """

    def __init__(self, policy: Policy = "mask") -> None:
        if policy not in VALID_POLICIES:
            raise ValueError(
                f"Unbekannte Policy '{policy}'. Erlaubt: {VALID_POLICIES}"
            )
        self.policy = policy

    def sanitize(self, text: str) -> str:
        """Sanitisiert einen Text-String."""
        if not isinstance(text, str) or not text:
            return text
        for kind, pattern in PII_PATTERNS.items():
            if self.policy == "mask":
                text = pattern.sub(f"[MASKED_{kind.upper()}]", text)
            elif self.policy == "redact":
                text = pattern.sub("[REDACTED]", text)
            elif self.policy == "hash":
                text = pattern.sub(_make_hash_token, text)
        return text

    def sanitize_dict(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitisiert rekursiv alle String-Values in einem Dict.

        Verschachtelte Dicts werden mit-sanitisiert. Listen mit Strings
        ebenfalls. Andere Typen bleiben unveraendert.
        """
        if d is None:
            return d
        out: Dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, str):
                out[k] = self.sanitize(v)
            elif isinstance(v, dict):
                out[k] = self.sanitize_dict(v)
            elif isinstance(v, list):
                out[k] = [self.sanitize(x) if isinstance(x, str) else x for x in v]
            else:
                out[k] = v
        return out

    def install_file_handler_filter(
        self, handler: logging.Handler
    ) -> logging.Handler:
        """Hängt einen Sanitize-Filter an einen Logging-Handler.

        Wrappt handler.emit so dass record.msg und record.args vor
        dem Schreiben sanitisiert werden.
        """
        original_emit = handler.emit
        sanitizer = self

        def wrapped_emit(record: logging.LogRecord) -> None:  # type: ignore[no-untyped-def]
            try:
                # record.msg kann ein String oder ein Format-String sein
                if isinstance(record.msg, str):
                    record.msg = sanitizer.sanitize(record.msg)
                if record.args:
                    new_args = tuple(
                        sanitizer.sanitize(a) if isinstance(a, str) else a
                        for a in record.args
                    )
                    record.args = new_args
                original_emit(record)
            except Exception:
                # Bei Sanitize-Fehlern: Original-Record durchlassen
                original_emit(record)

        handler.emit = wrapped_emit  # type: ignore[method-assign]
        return handler


def sanitize(text: str, policy: Policy = "mask") -> str:
    """Convenience-Funktion: LogSanitizer().sanitize(text) ohne State."""
    return LogSanitizer(policy=policy).sanitize(text)
