"""Regression tests for the browser_exec URL pre-scan (#95027).

``_blocked_url_in_code`` extracts http(s) literals from exec code and checks
each against ``evaluate_url_safety``. The old extraction pattern cut the
candidate at the first backslash, so a regex-escaped public literal like
``r"https://go\\.solupay\\.com/payment"`` was checked as the bare host
``go`` — whose DNS failure surfaced as a bogus "private or internal address"
block. The fix captures backslashes, unescapes regex-escaped punctuation
before evaluation, and skips candidates whose authority still carries regex
syntax (patterns a browser can never navigate). Concrete URLs — including
escaped private ones — are checked unchanged.
"""

from unittest.mock import patch


def _capture(monkeypatch):
    seen = []

    def _fake_evaluate(url):
        seen.append(url)
        return None

    monkeypatch.setattr(
        "tools.browser_tool.evaluate_url_safety", _fake_evaluate
    )
    return seen


def test_escaped_public_url_evaluated_unescaped(monkeypatch):
    seen = _capture(monkeypatch)
    from tools.browser_use_cli import _blocked_url_in_code

    code = r'pattern = re.compile(r"https://go\.solupay\.com/payment")'
    assert _blocked_url_in_code(code) is None
    assert seen == ["https://go.solupay.com/payment"]


def test_escaped_private_url_still_reaches_evaluation(monkeypatch):
    seen = _capture(monkeypatch)
    from tools.browser_use_cli import _blocked_url_in_code

    code = r'pattern = re.compile(r"https://127\.0\.0\.1/admin")'
    _blocked_url_in_code(code)
    # The escaped private host must normalize to the concrete literal and go
    # through the safety check — unescaping must not bypass the policy.
    assert seen == ["https://127.0.0.1/admin"]


def test_regex_authority_is_skipped_without_dns_lookup(monkeypatch):
    seen = _capture(monkeypatch)
    from tools.browser_use_cli import _blocked_url_in_code

    code = r'pattern = re.compile(r"https://(?:www\.)?example\.com/path")'
    assert _blocked_url_in_code(code) is None
    assert seen == [], "regex-shaped authorities must not reach DNS evaluation"


def test_plain_concrete_url_checked_unchanged(monkeypatch):
    seen = _capture(monkeypatch)
    from tools.browser_use_cli import _blocked_url_in_code

    _blocked_url_in_code('x = "https://example.com/page?a=1"')
    assert seen == ["https://example.com/page?a=1"]


def test_unescape_only_strips_backslash_pairs(monkeypatch):
    from tools.browser_use_cli import _normalize_url_candidate

    assert _normalize_url_candidate(r"https://a\.b\.c/x") == "https://a.b.c/x"
    # A plain Windows-style path with no scheme passes through normalization
    # untouched is out of scope here (only URL candidates reach it).
    assert _normalize_url_candidate("https://example.com/x") == "https://example.com/x"


def test_pattern_authority_returns_none():
    from tools.browser_use_cli import _normalize_url_candidate

    assert _normalize_url_candidate(r"https://(?:www\.)?example\.com/p") is None
    assert _normalize_url_candidate(r"https://[a-z]+\.example\.com/p") is None
    assert _normalize_url_candidate("https://example.com/p") == "https://example.com/p"
