"""#80025: vision_tools must not hardcode max_tokens — contradicts
auxiliary_client.py's own no-cap policy.  Only pass max_tokens when the
user explicitly configures auxiliary.vision.max_tokens.
"""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path

import pytest


# ── Source-level tests (no API calls needed) ──────────────────────────


def test_no_hardcoded_max_tokens_in_image_call_site():
    """The image-analysis call_kwargs block must not contain a literal
    ``"max_tokens": <int>`` — the cap is silently truncating analyses."""
    source = Path("tools/vision_tools.py").read_text(encoding="utf-8")
    # The old hardcoded lines were "max_tokens": 2000 and "max_tokens": 4000.
    # After the fix, max_tokens only appears inside the conditional guard.
    assert '"max_tokens": 2000' not in source, (
        "Hardcoded max_tokens: 2000 still present in vision_tools.py"
    )
    assert '"max_tokens": 4000' not in source, (
        "Hardcoded max_tokens: 4000 still present in vision_tools.py"
    )


def test_max_tokens_is_conditional_on_config():
    """The call_kwargs dict must NOT include max_tokens by default; it
    should only be added when _vision_cfg.get('max_tokens') returns a value."""
    source = Path("tools/vision_tools.py").read_text(encoding="utf-8")
    # The fix reads _vmax from _vision_cfg and conditionally sets max_tokens.
    assert "_vmax = _vision_cfg.get" in source, (
        "vision_tools must read max_tokens from auxiliary.vision config, "
        "not hardcode it"
    )
    assert '"auto"' in source.lower(), (
        "vision_tools must treat 'auto' as omit (do not pass max_tokens)"
    )


def test_call_kwargs_does_not_include_max_tokens_by_default():
    """Verify the call_kwargs dict literal in the image path omits
    max_tokens — it should be added after, conditionally."""
    source = Path("tools/vision_tools.py").read_text(encoding="utf-8")
    # Find the image call_kwargs block
    # The block should have "task", "messages", "temperature", "timeout"
    # but NOT "max_tokens" in the dict literal.
    # After the fix, max_tokens is added via call_kwargs["max_tokens"] = ...
    # only when _vmax is set.
    lines = source.splitlines()
    in_call_kwargs = False
    call_kwargs_lines: list[str] = []
    for line in lines:
        if "call_kwargs = {" in line:
            in_call_kwargs = True
            call_kwargs_lines = [line]
            continue
        if in_call_kwargs:
            call_kwargs_lines.append(line)
            if line.strip() == "}":
                # Check this block — should not contain max_tokens as a
                # direct key in the dict literal.
                block = "\n".join(call_kwargs_lines)
                if '"task": "vision"' in block:
                    assert '"max_tokens"' not in block, (
                        "call_kwargs dict literal should not include "
                        "max_tokens — it must be added conditionally"
                    )
                in_call_kwargs = False
                call_kwargs_lines = []


def test_both_image_and_video_paths_are_fixed():
    """Both the image (line ~1270) and video (line ~1777) call sites must
    be fixed — the bug report confirms both hardcode caps."""
    source = Path("tools/vision_tools.py").read_text(encoding="utf-8")
    # Count occurrences of _vmax — should appear at least twice (image + video).
    assert source.count("_vmax = _vision_cfg.get") >= 2, (
        "Both image and video call sites must read max_tokens from config"
    )