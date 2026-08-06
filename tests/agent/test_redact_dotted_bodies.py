"""Dotted credential bodies must be redacted in full, for every vendor prefix.

Regression cover for a class of partial masks: most entries in
``_PREFIX_PATTERNS`` use a character class that excludes ``.``, and the
trailing boundary lets a dot terminate the match, so a credential carrying
dots inside its body was masked only up to its first dot.

A partial mask is more dangerous than no mask: ``sk-sp-…xxxx`` reads as
"redacted" while the tail is still a working key.

All tokens here are synthesised.
"""

import secrets

import pytest

from agent.redact import redact_sensitive_text, redact_terminal_output


def _seg(n: int) -> str:
    return secrets.token_hex(n // 2)


# Vendor prefixes that already exist in _PREFIX_PATTERNS, each given a body
# containing dots. Before the fix every one of these leaked its tail.
DOTTED_TOKENS = [
    pytest.param("sk-sp-" + _seg(30) + "." + _seg(30) + "." + _seg(30),
                 id="alibaba-token-plan"),
    pytest.param("sk-" + _seg(24) + "." + _seg(24), id="openai-style-dotted"),
    pytest.param("SG." + _seg(22) + "." + _seg(43), id="sendgrid-three-part"),
    pytest.param("xoxb-" + _seg(20) + "." + _seg(20), id="slack-bot-dotted"),
    pytest.param("ghp_" + _seg(20) + "." + _seg(20), id="github-pat-dotted"),
    pytest.param("sk_live_" + _seg(20) + "." + _seg(20), id="stripe-live-dotted"),
    pytest.param("hf_" + _seg(20) + "." + _seg(20), id="huggingface-dotted"),
    pytest.param("AIza" + _seg(34) + "." + _seg(20), id="google-api-dotted"),
]


@pytest.mark.parametrize("token", DOTTED_TOKENS)
def test_dotted_token_body_is_fully_masked(token):
    """No segment of a dotted credential may survive redaction."""
    out = redact_terminal_output(f"TOKEN={token}")
    assert token not in out
    for segment in token.split("."):
        if len(segment) >= 12:
            assert segment not in out, (
                f"segment {segment[:8]}... survived redaction — a partial mask "
                f"reads as redacted while the tail is still live"
            )


@pytest.mark.parametrize("token", DOTTED_TOKENS)
def test_dotted_token_masked_in_prose(token):
    """The same must hold on the sentence path, not just terminal output."""
    out = redact_sensitive_text(f"the key is {token} and it is live")
    assert token not in out


def test_trailing_sentence_punctuation_is_not_consumed():
    """A full stop ending a sentence must not be eaten as part of the token."""
    token = "sk-sp-" + _seg(20) + "." + _seg(20)
    out = redact_sensitive_text(f"leaked {token}.")
    assert token not in out
    assert out.endswith(".")


@pytest.mark.parametrize(
    "text",
    [
        "see file.txt for details",
        "version 1.2.3 released",
        "run npm install --save-dev",
        "visit example.com/path for docs",
        "the value 3.14159 is pi",
        "sk- is a common prefix",
        "config at ~/.hermes/config.yaml",
    ],
)
def test_ordinary_prose_is_untouched(text):
    """Widening the match must not start masking normal text."""
    assert redact_terminal_output(text) == text
    assert redact_sensitive_text(text) == text
