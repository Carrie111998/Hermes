"""Tests for security-guidance credential detection (user-space extension).

Tests the CREDENTIAL_PATTERNS added to the user-space copy of the
security-guidance plugin (~/.hermes/plugins/security-guidance/), NOT the
bundled upstream copy. Follows the structure of the upstream
tests/plugins/test_security_guidance_plugin.py so the credential cases can
be merged into the upstream suite when the plugin change is PR'd.

Covered:
  * CREDENTIAL_PATTERNS data integrity (fields, uniqueness, user: prefix)
  * _scan_content true positives — sk- keys, ghp_ PATs, generic vendor shapes
  * _scan_content true negatives — redacted doc examples, short strings,
    non-secret tokens, doc-extension skip
  * .env path_check rule
  * Hooks — warn mode appends, block mode blocks, non-target tools pass

Run:  cd ~/.hermes/hermes-agent && uv run pytest tests/plugins/test_security_guidance_credential.py -v
      (or: python3 -m pytest <this file>)
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# The user-space copy is the code under test — NOT the bundled one.
USER_PLUGIN_DIR = Path.home() / ".hermes/plugins/security-guidance"


def _load_patterns():
    """Import the user-space patterns.py in isolation."""
    spec = importlib.util.spec_from_file_location(
        "security_guidance_cred_patterns_under_test",
        USER_PLUGIN_DIR / "patterns.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_init():
    """Import the user-space plugin __init__.py with patterns.py as sibling."""
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.security_guidance_cred",
        USER_PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(USER_PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.security_guidance_cred"
    mod.__path__ = [str(USER_PLUGIN_DIR)]
    sys.modules["hermes_plugins.security_guidance_cred"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SECURITY_GUIDANCE_BLOCK", raising=False)
    monkeypatch.delenv("SECURITY_GUIDANCE_DISABLE", raising=False)


# ---------------------------------------------------------------------------
# CREDENTIAL_PATTERNS data integrity
# ---------------------------------------------------------------------------

class TestCredentialPatternsData:
    def test_has_credential_rules(self):
        p = _load_patterns()
        assert len(p.CREDENTIAL_PATTERNS) >= 1

    def test_every_rule_has_required_fields(self):
        p = _load_patterns()
        for rule in p.CREDENTIAL_PATTERNS:
            assert "ruleName" in rule
            assert "reminder" in rule and rule["reminder"]
            assert any(
                k in rule for k in ("substrings", "regex", "path_check")
            ), rule

    def test_rule_names_unique_and_user_prefixed(self):
        p = _load_patterns()
        names = [r["ruleName"] for r in p.CREDENTIAL_PATTERNS]
        assert len(names) == len(set(names))
        # user: prefix bypasses the static RuleId enum — required so the
        # upstream assert (set(_RULE_NAME_TO_ID) == SECURITY_PATTERNS names)
        # stays green.
        assert all(n.startswith("user:") for n in names)

    def test_no_vendor_tokens_hardcoded(self):
        """Generalization guarantee: the regex must not enumerate vendor
        prefixes. It matches any `vendor_prefix_secret` shape."""
        p = _load_patterns()
        regexes = " ".join(
            r.get("regex", "") for r in p.CREDENTIAL_PATTERNS
        )
        # These were this project's tokens — if they appear as literals in
        # the regex, the rule is repo-specific, not general.
        for vendor_literal in ("figd", "ctx7sk", "tvly", "ghp", "AKIA"):
            assert vendor_literal not in regexes


# ---------------------------------------------------------------------------
# _scan_content — true positives
# ---------------------------------------------------------------------------

class TestCredentialScanTruePositives:
    def test_sk_key_in_py_detected(self):
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/app.py", 'api_key = "sk-abcdef1234567890abcdef"\n'
        )
        names = [n for n, _ in findings]
        assert "user:hardcoded_api_key" in names

    def test_ghp_pat_in_conf_detected(self):
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/settings.conf", '{"token": "ghp_1234567890abcdefghij"}'
        )
        names = [n for n, _ in findings]
        assert "user:hardcoded_api_key" in names

    def test_underscore_separator_detected(self):
        """AKIA-style AWS key: uppercase prefix + no separator — separate rule."""
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/app.py", "aws_key = 'AKIAIOSFODNN7EXAMPLE123456'\n"
        )
        names = [n for n, _ in findings]
        assert "user:hardcoded_aws_key_id" in names

    def test_xoxb_slack_token_detected(self):
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/app.py", "token = 'xoxb-123456789012-1234567890123-abcdefgh'\n"
        )
        names = [n for n, _ in findings]
        assert "user:hardcoded_api_key" in names


# ---------------------------------------------------------------------------
# _scan_content — true negatives (no false positives)
# ---------------------------------------------------------------------------

class TestCredentialScanTrueNegatives:
    def test_redacted_doc_example_not_detected(self):
        """ghp_... (3 dots) in a .md must NOT match — redacted placeholder."""
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/README.md", "Use ghp_... or sk-xxx as placeholders\n"
        )
        assert findings == []

    def test_doc_extension_skipped_for_key_rule(self):
        """.json is in _DOC_EXTS; a real key in JSON is intentionally not
        flagged by the content rule (conservative default — JSON configs
        legitimately hold many strings)."""
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/config.json", '{"key": "sk-abcdef1234567890abcdef"}'
        )
        assert "user:hardcoded_api_key" not in [n for n, _ in findings]

    def test_short_non_secret_not_detected(self):
        mod = _load_plugin_init()
        findings = mod._scan_content("/tmp/app.py", 'color = "red-blue"\n')
        assert findings == []

    def test_semver_not_detected(self):
        """version numbers look like prefix-secret; must not trip."""
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/app.py", '__version__ = "1.2.3-rc1"\n'
        )
        assert findings == []

    def test_uuid_not_detected(self):
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/app.py", "uuid4 = '550e8400-e29b-41d4-a716-446655440000'\n"
        )
        assert findings == []

    def test_env_var_reference_not_detected(self):
        """${ENV_VAR} refs in templates are placeholders, not secrets."""
        mod = _load_plugin_init()
        findings = mod._scan_content(
            "/tmp/config.yaml.template", "api_key: ${OPENAI_API_KEY}\n"
        )
        assert findings == []


# ---------------------------------------------------------------------------
# .env path_check rule
# ---------------------------------------------------------------------------

class TestSecretEnvFileRule:
    def test_env_file_path_fires(self):
        mod = _load_plugin_init()
        findings = mod._scan_content("/tmp/.env", "FOO=bar\n")
        names = [n for n, _ in findings]
        assert "user:secret_env_file" in names

    def test_env_local_fires(self):
        mod = _load_plugin_init()
        findings = mod._scan_content("/tmp/.env.local", "FOO=bar\n")
        names = [n for n, _ in findings]
        assert "user:secret_env_file" in names

    def test_non_env_path_does_not_fire(self):
        mod = _load_plugin_init()
        findings = mod._scan_content("/tmp/app.py", "FOO=bar\n")
        assert "user:secret_env_file" not in [n for n, _ in findings]

    def test_env_example_does_not_fire(self):
        """.env.example is meant to be committed — must not fire."""
        mod = _load_plugin_init()
        findings = mod._scan_content("/tmp/.env.example", "FOO=bar\n")
        assert "user:secret_env_file" not in [n for n, _ in findings]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

class TestCredentialHooks:
    def test_warn_mode_appends_warning(self):
        mod = _load_plugin_init()
        args = {
            "path": "/tmp/app.py",
            "content": 'api_key = "sk-abcdef1234567890abcdef"\n',
        }
        result = mod._on_transform_tool_result(
            tool_name="write_file", args=args, result='{"success": true}'
        )
        assert isinstance(result, str)
        assert "Security guidance" in result
        assert "user:hardcoded_api_key" in result
        assert result.startswith('{"success": true}')

    def test_warn_mode_clean_content_no_warning(self):
        mod = _load_plugin_init()
        args = {"path": "/tmp/app.py", "content": "import json\n"}
        assert (
            mod._on_transform_tool_result(
                tool_name="write_file", args=args, result='{"success": true}'
            )
            is None
        )

    def test_block_mode_blocks_secret(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("SECURITY_GUIDANCE_BLOCK", "1")
        args = {
            "path": "/tmp/app.py",
            "content": 'token = "ghp_1234567890abcdefghij"\n',
        }
        out = mod._on_pre_tool_call(tool_name="write_file", args=args)
        assert isinstance(out, dict)
        assert out["action"] == "block"
        assert "user:hardcoded_api_key" in out["message"]

    def test_block_mode_clean_content_passes(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("SECURITY_GUIDANCE_BLOCK", "1")
        args = {"path": "/tmp/app.py", "content": "import json\n"}
        assert (
            mod._on_pre_tool_call(tool_name="write_file", args=args) is None
        )

    def test_non_target_tool_passes(self):
        mod = _load_plugin_init()
        args = {"command": "echo sk-abcdef1234567890abcdef"}
        assert (
            mod._on_transform_tool_result(
                tool_name="terminal", args=args, result='{"output": "ok"}'
            )
            is None
        )
