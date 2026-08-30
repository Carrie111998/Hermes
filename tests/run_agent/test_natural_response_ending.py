"""_has_natural_response_ending must recognize common terminal emoji.

The truncation heuristic (_should_treat_stop_as_truncated) treats a reply with
no "natural ending" as a truncation signal and injects the output-limit
continuation nudge. The emoji check only covered U+1F300+, missing the
Miscellaneous Symbols and Dingbats blocks (U+2600–U+27BF) — so a completed reply
ending in ✅ ✔ ☑ ❤ was falsely flagged as truncated and re-fired the nudge
(#98255).
"""
import pytest

from run_agent import AIAgent

_ending = AIAgent._has_natural_response_ending


@pytest.mark.parametrize("text", ["✅", "✔", "☑", "❤", "Looks good ✅", "Deployed ✅️"])
def test_dingbat_emoji_endings_are_natural(text):
    # U+2600–U+27BF sits below the U+1F300 block; the trailing variation
    # selector on "✅️" must not defeat the check either.
    assert _ending(text) is True


@pytest.mark.parametrize("text", ["Done 🎉", "answer.", "really?", "yes：", "```"])
def test_existing_natural_endings_unchanged(text):
    assert _ending(text) is True


@pytest.mark.parametrize("text", ["bare word", "no ending here", ""])
def test_unpunctuated_endings_unchanged(text):
    # The bare-word behavior is intentionally left unchanged — whether an
    # unpunctuated reply should count as "finished" is a separate design
    # question raised in #98255. This pins that this fix does not alter it.
    assert _ending(text) is False
