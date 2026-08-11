"""Tests for dev-pipeline secret scanning."""

from __future__ import annotations

import re
from pathlib import Path

from hermes_cli.dev_pipeline import scan_diff_for_secrets


def _ghp_pat() -> str:
    return "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"


def _sk_live_key() -> str:
    return "sk-" + "live-" + "abcdefghijklmnopqrstuv"


def _aws_access_key() -> str:
    return "AKIA" + "IOSFODNN7EXAMPLE"


def _private_key_header() -> str:
    return "-----BEGIN " + "RSA PRIVATE KEY-----"


PRIVATE_KEY_DIFF = f"""\
diff --git a/key.pem b/key.pem
+++ b/key.pem
+{_private_key_header()}
+MIIEpAIBAAKCAQEAsecretmaterial
+-----END RSA PRIVATE KEY-----
"""

GHP_DIFF = f"""\
diff --git a/config.py b/config.py
+++ b/config.py
+TOKEN = "{_ghp_pat()}"
"""

SK_DIFF = f"""\
+api_key = "{_sk_live_key()}"
"""

XOXB_DIFF = (
    '+slack = "'
    + "xox"
    + "b-"
    + "1234567890-abcdefghijklmnop"
    + '"\n'
)

AWS_DIFF = f"""\
+AWS_KEY = "{_aws_access_key()}"
"""

ENV_DIFF = """\
+.env
+DATABASE_PASSWORD=supersecretvalue
+API_KEY=notactuallysecretname
+MY_API_KEY=alsoblocked
"""


def test_private_key_detected():
    findings = scan_diff_for_secrets(PRIVATE_KEY_DIFF)
    assert any(f["pattern"] == "private_key_pem" for f in findings)


def test_ghp_detected():
    findings = scan_diff_for_secrets(GHP_DIFF)
    assert any(f["pattern"] == "github_pat" for f in findings)


def test_sk_prefix_detected():
    findings = scan_diff_for_secrets(SK_DIFF)
    assert any(f["pattern"] == "generic_sk_prefix" for f in findings)


def test_xoxb_detected():
    findings = scan_diff_for_secrets(XOXB_DIFF)
    assert any(f["pattern"] == "slack_bot_token" for f in findings)


def test_aws_access_key_detected():
    findings = scan_diff_for_secrets(AWS_DIFF)
    assert any(f["pattern"] == "aws_access_key_id" for f in findings)


def test_env_sensitive_assignment_detected():
    findings = scan_diff_for_secrets(ENV_DIFF)
    patterns = {f["pattern"] for f in findings}
    assert "env_sensitive_assignment" in patterns


def test_findings_never_contain_secret_values():
    diff = GHP_DIFF + SK_DIFF + ENV_DIFF
    findings = scan_diff_for_secrets(diff)
    blob = str(findings)
    assert _ghp_pat() not in blob
    assert _sk_live_key() not in blob
    assert "supersecretvalue" not in blob


def test_ordinary_code_is_negative():
    diff = """\
diff --git a/main.py b/main.py
+++ b/main.py
+def hello():
+    return "world"
+TOKEN_NAME = "placeholder"
+example = "sk-...redacted..."
"""
    findings = scan_diff_for_secrets(diff)
    assert findings == []


def test_env_var_names_without_values_are_negative():
    diff = "+# Set PASSWORD and API_KEY in your environment\n"
    findings = scan_diff_for_secrets(diff)
    assert findings == []


def _secret_guard_patterns() -> list[re.Pattern[str]]:
    ghp_prefix = "ghp_"
    ghp_body = "[A-Za-z0-9]{20,}"
    pem_begin = "-----BEGIN "
    pem_kind = "[A-Z ]*PRIVATE KEY-----"
    sk_prefix = "sk-(live|prod)-"
    sk_body = "[A-Za-z0-9-]{8,}"
    aws_prefix = "AKIA"
    aws_body = "[0-9A-Z]{16}"
    return [
        re.compile(ghp_prefix + ghp_body),
        re.compile(pem_begin + pem_kind),
        re.compile(sk_prefix + sk_body),
        re.compile(aws_prefix + aws_body),
    ]


def test_changed_dev_pipeline_test_sources_have_no_full_secret_literals():
    repo_root = Path(__file__).resolve().parents[2]
    scan_paths = [
        repo_root / "tests" / "dev_pipeline",
        repo_root / "tests" / "tools" / "test_moa_tool.py",
    ]
    patterns = _secret_guard_patterns()
    offenders: list[str] = []
    for scan_path in scan_paths:
        files = (
            [scan_path]
            if scan_path.is_file()
            else sorted(scan_path.glob("*.py"))
        )
        for path in files:
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                if pattern.search(text):
                    offenders.append(f"{path.relative_to(repo_root)}:{pattern.pattern}")
    assert offenders == []
