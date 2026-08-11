"""Shared truncation / context-compression markers.

Used by:
- ``agent.context_compressor`` when shrinking past assistant tool-call args
  (must NOT look like model-authored prose — see #83714 / #83843).
- ``tools.file_tools`` write/patch guards so any imitation of those markers
  (old bare ``...[truncated]``, AI "unchanged" stubs, or the new Hermes
  compression disclaimer) fails closed before touching disk (#83752, #68512).

Keep this module dependency-light so both agent and tools layers can import it
without circular imports.
"""

from __future__ import annotations

import re

# Marker injected into compressed tool-call argument strings. Deliberately
# non-prose-shaped so models do not treat it as a reusable abbreviation.
COMPRESSION_MARKER_NEEDLE = "HERMES-CONTEXT-COMPRESSION"
COMPRESSION_MARKER_TEMPLATE = (
    "⟪HERMES-CONTEXT-COMPRESSION: {omitted:,} of {total:,} chars omitted here "
    "by Hermes's context compressor. This is NOT part of the original tool "
    "call and must never be reproduced in new output — always write full, "
    "untruncated content.⟫"
)

# Literal AI "I omitted the rest" stubs (from #68512 / issue #20805).
# Matched case-insensitively via count comparison against original content.
LITERAL_TRUNCATION_SIGNATURES: tuple[str, ...] = (
    "/* ... full function ... */",
    "/* ... unchanged ... */",
    "// ... unchanged ...",
    "// ... rest of file ...",
    "# ... rest of file ...",
    "# ... unchanged ...",
    "/* ... rest unchanged ... */",
    "... (rest of file unchanged)",
    "... (unchanged)",
    "<!-- ... unchanged ... -->",
    "# ... (rest of the function remains the same)",
    "// ... (rest of the function remains the same)",
)

# Regex family for Hermes-style truncation markers and the new compressor tag.
# Applied with occurrence-count comparison when an original baseline is given.
_REGEX_TRUNCATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\.+\s*\[truncated\]", re.IGNORECASE),
    re.compile(r"…\s*\[truncated\]", re.IGNORECASE),
    re.compile(r"\[truncated\]", re.IGNORECASE),
    re.compile(r"//\s*\.+\s*(?:rest\s+)?unchanged\s*\.+", re.IGNORECASE),
    re.compile(r"/\*\s*\.+\s*(?:rest\s+)?unchanged\s*\.+\s*\*/", re.IGNORECASE),
    # New compressor marker (#83843) — refuse if model copies it into writes.
    re.compile(
        r"⟪\s*HERMES-CONTEXT-COMPRESSION:.*?⟫",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"HERMES-CONTEXT-COMPRESSION\s*:", re.IGNORECASE),
)


def format_compression_marker(*, omitted: int, total: int) -> str:
    """Build the per-instance compressor marker text."""
    return COMPRESSION_MARKER_TEMPLATE.format(omitted=omitted, total=total)


def find_new_truncation_placeholder(
    content: str | None,
    original: str | None = None,
) -> str | None:
    """Return a matched placeholder if *content* introduces more than *original*.

    When ``original`` is None, any match is treated as new (unconditional
    write_file path). When provided, only signatures whose occurrence count
    in ``content`` exceeds the count in ``original`` are flagged — so
    legitimate pre-existing docs/tests that mention markers are not blocked,
    but adding a second truncated stub still is (#68512 count-based fix).
    """
    if not content:
        return None

    content_lower = content.lower()
    original_lower = original.lower() if original is not None else None

    for sig in LITERAL_TRUNCATION_SIGNATURES:
        sig_lower = sig.lower()
        content_count = content_lower.count(sig_lower)
        if content_count == 0:
            continue
        original_count = (
            original_lower.count(sig_lower) if original_lower is not None else 0
        )
        if content_count > original_count:
            return sig

    for pattern in _REGEX_TRUNCATION_PATTERNS:
        content_matches = pattern.findall(content)
        if not content_matches:
            continue
        original_count = (
            len(pattern.findall(original)) if original is not None else 0
        )
        if len(content_matches) > original_count:
            # findall may return tuples for groups; normalize to a string.
            hit = content_matches[0]
            if isinstance(hit, tuple):
                hit = next((p for p in hit if p), str(hit))
            return str(hit)

    return None
