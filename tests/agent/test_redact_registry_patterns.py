"""Tests for the registry-fed exact-value redaction pass.

The pass loads an optional user pattern file (``HERMES_REDACT_PATTERNS``,
default ``~/.hermes/state/credential-firewall/redact_patterns.json``) and
masks exact registered secrets plus registered ``KEY=value`` forms whose
keys the built-in keyword families do not recognize (e.g. ``PIN=1234``).
Fail-safe: a missing or unreadable file is a no-op. File-content redaction
uses a non-reusable sentinel (issue #35519 semantics) so masked values can
never be written back over the real file.
"""

import json

import pytest

from agent.redact import redact_sensitive_text


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    """Ensure HERMES_REDACT_SECRETS is not disabled by prior test imports."""
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


@pytest.fixture
def patterns_file(tmp_path, monkeypatch):
    """Point HERMES_REDACT_PATTERNS at a throwaway file and reset the cache."""
    p = tmp_path / "patterns.json"
    monkeypatch.setenv("HERMES_REDACT_PATTERNS", str(p))
    monkeypatch.setattr(
        "agent.redact._PATTERNS_CACHE",
        {"mtime": None, "lit_re": None, "key_re": None},
    )
    return p


def _write(p, literals, keys):
    p.write_text(json.dumps({
        "literals": literals,
        "key_patterns": {k: True for k in keys},
    }))


def test_exact_literal_masked(patterns_file):
    _write(patterns_file, ["SUPERSECRET" + "TESTVALUE12345"], [])
    out = redact_sensitive_text("the token SUPERSECRETTESTVALUE12345 appears")
    assert "SUPERSECRETTESTVALUE12345" not in out
    assert "***" in out


def test_key_pattern_masked_keeps_separator(patterns_file):
    _write(patterns_file, [], ["PIN", "GITHUB_PAT"])
    out = redact_sensitive_text("PIN=1234 and GITHUB_PAT=xYzQwEr" + "12345AbCd")
    assert "PIN=1234" not in out
    assert "PIN=***" in out
    assert "xYzQwEr12345AbCd" not in out
    assert "GITHUB_PAT=***" in out


def test_file_read_uses_nonreusable_sentinel(patterns_file):
    _write(patterns_file, ["SUPERSECRET" + "TESTVALUE12345"], [])
    out = redact_sensitive_text("value SUPERSECRETTESTVALUE12345", file_read=True)
    assert "SUPERSECRETTESTVALUE12345" not in out
    assert out != "value ***"


def test_missing_file_is_fail_safe_noop(monkeypatch):
    monkeypatch.setenv("HERMES_REDACT_PATTERNS", "/nonexistent/patterns.json")
    monkeypatch.setattr(
        "agent.redact._PATTERNS_CACHE",
        {"mtime": None, "lit_re": None, "key_re": None},
    )
    t = "plain text and a built-in sk-" + "abcdef1234567890"
    out = redact_sensitive_text(t)
    assert "sk-abcdef1234567890" not in out  # built-in families still run
    assert "plain text" in out


def test_rotation_picked_up_via_mtime(patterns_file):
    _write(patterns_file, ["OLD" + "SECRETVALUE1"], [])
    assert "OLDSECRETVALUE1" not in redact_sensitive_text("OLDSECRETVALUE1")
    _write(patterns_file, ["ROTATED" + "SECRETVALUE2"], [])
    out = redact_sensitive_text("fresh ROTATEDSECRETVALUE2")
    assert "ROTATEDSECRETVALUE2" not in out
