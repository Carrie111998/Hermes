import pytest

from devflow_delegation.agent_policy import (
    Budget,
    CeilingExceeded,
    redact_secrets,
    scan_for_secrets,
    scrubbed_env,
    secret_values,
)


def test_scrubbed_env_drops_secret_shaped_names_and_keeps_the_allowlist():
    base = {
        "PATH": "/usr/bin", "OPENAI_API_KEY": "sk-live-abc", "GBRAIN_MCP_TOKEN": "t-123",
        "MY_PASSWORD": "hunter2", "SOME_SECRET": "s", "AWS_CREDENTIALS": "c",
        "LANG": "en_US.UTF-8", "RANDOM_UNLISTED": "nope",
    }
    env = scrubbed_env(base)
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "en_US.UTF-8"
    for dropped in ("OPENAI_API_KEY", "GBRAIN_MCP_TOKEN", "MY_PASSWORD", "SOME_SECRET", "AWS_CREDENTIALS"):
        assert dropped not in env
    # Deny-by-default: anything not on the allow-list is dropped even if it looks harmless.
    assert "RANDOM_UNLISTED" not in env


def test_scrubbed_env_always_sets_git_terminal_prompt_off():
    assert scrubbed_env({"PATH": "/usr/bin"})["GIT_TERMINAL_PROMPT"] == "0"


def test_secret_values_returns_the_values_that_were_dropped():
    values = secret_values({"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-live-abc", "X_TOKEN": "t-123"})
    assert "sk-live-abc" in values
    assert "t-123" in values
    assert "/usr/bin" not in values


def test_scan_for_secrets_flags_a_known_credential_value():
    findings = scan_for_secrets("config = 'sk-live-abc'\n", known_values=("sk-live-abc",))
    assert findings and any("known-credential" in f for f in findings)


def test_scan_for_secrets_flags_a_private_key_block():
    # Assembled at runtime: a literal key header in this file would trip the
    # repository's own gitleaks pre-commit hook (it did, the first time).
    marker = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
    assert scan_for_secrets(f"{marker}\nabc\n")


def test_scan_for_secrets_flags_an_aws_access_key():
    # The canonical AWS documentation example key (AKIA + 16 chars), not a real
    # credential — used throughout AWS's own docs and gitleaks' default allowlist.
    marker = "AKIA" + "IOSFODNN7EXAMPLE"
    findings = scan_for_secrets(f"aws_access_key_id = {marker}\n")
    assert "aws-access-key" in findings


def test_scan_for_secrets_flags_a_bearer_token():
    marker = "Bearer " + "x" * 12 + "Y" * 12 + "0123456789"
    findings = scan_for_secrets(f"Authorization: {marker}\n")
    assert "bearer-token" in findings


def test_scan_for_secrets_flags_a_plain_openai_key():
    marker = "sk-" + "aB1" * 8  # 24 chars after "sk-", well past the 20-char floor
    findings = scan_for_secrets(f"OPENAI_API_KEY={marker}\n")
    assert "openai-key" in findings


def test_scan_for_secrets_flags_a_scoped_openai_key():
    # Current project/vendor-scoped formats insert a hyphen after "sk-" (e.g.
    # sk-proj-..., sk-ant-...); the old pattern required 20+ contiguous
    # alphanumerics right after "sk-" and missed these entirely (F1).
    marker = "sk-proj-" + "aB1" * 8
    findings = scan_for_secrets(f"OPENAI_API_KEY={marker}\n")
    assert "openai-key" in findings


def test_scan_for_secrets_does_not_flag_ordinary_code_containing_sk_dash():
    # "sk-" appears as a substring of ordinary words/identifiers; that alone
    # must never trip the openai-key pattern.
    body = "def task-runner(): return risk-free_score\n"
    assert scan_for_secrets(body) == []


def test_scan_for_secrets_ignores_short_and_clean_text():
    assert scan_for_secrets("def add(a, b):\n    return a + b\n", known_values=("sk-live-abc",)) == []


def test_scan_for_secrets_ignores_empty_known_values():
    # A blank/whitespace env value must never match every diff.
    assert scan_for_secrets("anything at all", known_values=("", "   ")) == []


# --- F5: main()'s failure path must never print raw secret material.
# redact_secrets is the piece that makes that possible.


def test_redact_secrets_replaces_a_known_credential_value():
    marker = "fk-" + "leak" + "-0123456789abcdef"
    body = f"upstream 401: token {marker} was rejected"
    redacted = redact_secrets(body, known_values=(marker,))
    assert marker not in redacted
    assert "[REDACTED]" in redacted
    # Only the secret is touched -- surrounding context survives.
    assert "upstream 401" in redacted and "was rejected" in redacted


def test_redact_secrets_redacts_a_regex_matched_pattern_even_without_known_values():
    marker = "sk-" + "aB1" * 8
    redacted = redact_secrets(f"OPENAI_API_KEY={marker}\n")
    assert marker not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_leaves_clean_text_untouched():
    body = "def add(a, b):\n    return a + b\n"
    assert redact_secrets(body, known_values=("fk-leak-0123456789abcdef",)) == body


def test_redact_secrets_ignores_short_known_values():
    # Mirrors scan_for_secrets' floor: a short "known" value would redact
    # almost anything by coincidence.
    assert redact_secrets("id: ab", known_values=("ab",)) == "id: ab"


def test_budget_trips_on_iterations():
    b = Budget(max_iterations=2, max_tokens=1000, timeout_seconds=100)
    b.start()
    b.tick()
    b.tick()
    with pytest.raises(CeilingExceeded, match="iterations"):
        b.tick()


def test_budget_trips_on_tokens():
    b = Budget(max_iterations=100, max_tokens=50, timeout_seconds=100)
    b.start()
    b.tick(tokens_used=30)
    with pytest.raises(CeilingExceeded, match="tokens"):
        b.tick(tokens_used=30)


def test_budget_trips_on_wall_clock():
    # Budget reads the clock exactly twice on this path: once in start(), once in tick().
    clock = iter([0.0, 999.0])
    b = Budget(max_iterations=100, max_tokens=1000, timeout_seconds=10, now=lambda: next(clock))
    b.start()
    with pytest.raises(CeilingExceeded, match="wall-clock"):
        b.tick()
