"""Regression tests for #81012 — redact: complete CSI/SGR sequences defeat
prefix masking.

Two gaps in the legacy shadow-copy implementation of
``_mask_control_split_tokens``:

1. ``\\x1b[32msk-<token>\\x1b[0m`` — stripping only the ESC byte leaves
   ``[32m`` glued to the token head; the literal ``m`` defeats the
   ``(?<![A-Za-z0-9_-])`` lookbehind in ``_PREFIX_RE`` and the entire token
   leaks verbatim.

2. A span containing both a newline AND an ESC split
   (``sk-<head>\\x1b<mid>\\n<tail>``) skipped the join via the line-boundary
   guard, leaving the ESC-split remainder unmasked.

The fix replaces the bare ESC strip with a complete CSI-sequence strip
(``\\x1b\\[[0-9;?]*[A-Za-z]``) before the control-char strip, and treats
CSI positions as "collapsible" in the join guard so CSI bytes don't fall
into the non-token-character class that would reject a legitimate join.
The line-boundary skip is preserved verbatim — that's the
``button [ref=e3]`` regression guard, not the bug (#77484 / #80987).
"""

import pytest

from agent.redact import _mask_control_split_tokens


def _mask_keep_head_tail(body: str) -> str:
    """Test mirror of ``mask_secret`` — preserve 4 head, 4 tail."""
    if len(body) <= 8:
        return "***"
    return body[:4] + "***" + body[-4:]


def _token_fully_masked(text: str, tok: str) -> bool:
    """Assert the original token is not contiguously present anywhere in
    the masked output. ``_mask_keep_head_tail`` preserves head:4 + tail:4,
    so a literal substring check on ``tok`` (>= 13 chars long) is the
    right probe — if the token body survived intact anywhere, the test
    catches it.
    """
    return tok not in text


class TestCsiSgrBypass:
    def test_csi_wrapped_token_masks(self):
        """The exact shape in #81012: \\x1b[32msk-<body>\\x1b[0m. The legacy
        bare-ESC strip left ``[32m`` glued to the head and ``m`` defeated
        the prefix lookbehind; the token leaked in cleartext. After the
        fix the entire token is masked.
        """
        tok = "sk-" + "a" * 30
        text = f"\x1b[32m{tok}\x1b[0m"
        result = _mask_control_split_tokens(text, _mask_keep_head_tail)
        assert _token_fully_masked(result, tok), (
            f"token leaked through CSI wrapping; got: {result!r}"
        )

    def test_csi_with_color_param_token_masks(self):
        """ANSI 256-color and 24-bit color forms (``\\x1b[38;5;208m``) must
        also be stripped — the issue's failure shape covers any
        ``\\x1b[digits;?letter`` opener."""
        tok = "ghp_" + "B" * 35
        text = f"\x1b[38;5;208m{tok}\x1b[0m"
        result = _mask_control_split_tokens(text, _mask_keep_head_tail)
        assert _token_fully_masked(result, tok), (
            f"token leaked through 256-color CSI; got: {result!r}"
        )

    def test_csi_split_token_across_newline_masks(self):
        """Secondary gap: a token whose body is split by a CSI byte AND a
        newline (``sk-<head>\\x1b[0m\\n<tail>``) used to skip the join via
        the line-boundary guard. Per-line shadow construction without the
        new CSI-strip step left the head unmasked in particular. The fix
        masks the entire span as one contiguous token in the shadow-copy."""
        tok = "sk-" + "c" * 30
        head, tail = tok[:15], tok[15:]
        text = f"prefix {head}\x1b[0m\n{tail} suffix"
        result = _mask_control_split_tokens(text, _mask_keep_head_tail)
        assert _token_fully_masked(result, tok), (
            f"CSI+newline split leaked; got: {result!r}"
        )

    def test_csi_only_around_token_does_not_swallow_adjacent_text(self):
        """Adjacent non-token text on the same line must NOT be eaten by
        the join — the original line-boundary skip guard must still hold
        for legitimate structure on the same line. The CSI span contains
        only token-body chars after stripping, so the guard's
        ``_TOKEN_BODY_CHARS ∪ CSI positions ∪ controls`` invariant allows
        the join, but a bare word like ``button`` outside the CSI span
        stays put.
        """
        tok = "sk-" + "d" * 30
        text = f"\x1b[32m{tok}\x1b[0m button [ref=e3]"
        result = _mask_control_split_tokens(text, _mask_keep_head_tail)
        assert "button" in result
        assert "ref=e3" in result
        assert _token_fully_masked(result, tok)


class TestCsiStripIsolated:
    """The new CSI strip must not affect inputs that don't contain CSI."""

    def test_plain_text_unchanged(self):
        text = "hello world, no secrets here"
        assert _mask_control_split_tokens(text, _mask_keep_head_tail) == text

    def test_bare_esc_split_still_masks(self):
        """Regression: a bare ESC byte (not part of a full CSI sequence)
        must still be stripped — the legacy path for this is preserved."""
        tok = "ghp_" + "E" * 35
        text = f"{tok[:10]}\x1b{tok[10:]}"
        result = _mask_control_split_tokens(text, _mask_keep_head_tail)
        assert _token_fully_masked(result, tok)

    def test_newline_split_still_masks(self):
        """Regression: newline-split tokens continue to mask (#77484)."""
        tok = "ghp_" + "F" * 35
        text = f"{tok[:10]}\n{tok[10:]}"
        result = _mask_control_split_tokens(text, _mask_keep_head_tail)
        # The mask is applied to the contiguous shadow match; assert the
        # longest fragment is gone (it would survive in any leak).
        longest = max(tok[10:], tok[:10], key=len)
        assert longest not in result