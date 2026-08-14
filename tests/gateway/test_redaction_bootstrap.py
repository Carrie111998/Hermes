import os
import subprocess
import sys
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_gateway_redaction_probe(tmp_path, config_yaml: str, env_text: str, body: str):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(textwrap.dedent(config_yaml))
    (hermes_home / ".env").write_text(env_text)

    probe = textwrap.dedent(
        """\
        import os
        import sys

        os.environ.pop("HERMES_REDACT_SECRETS", None)
        os.environ.pop("HERMES_REDACT_PHONE_NUMBERS", None)
        sys.path.insert(0, {repo_root!r})
        """
    ).format(repo_root=str(REPO_ROOT))
    probe += textwrap.dedent(body)

    env = dict(os.environ)
    env["HERMES_HOME"] = str(hermes_home)
    env.pop("HERMES_REDACT_SECRETS", None)
    env.pop("HERMES_REDACT_PHONE_NUMBERS", None)

    return subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )


def test_gateway_package_init_bridges_phone_redaction_before_session_import(tmp_path):
    result = _run_gateway_redaction_probe(
        tmp_path,
        """
        privacy:
          redact_phone_numbers: false
        """,
        "",
        """
        import gateway
        from agent.redact import redact_sensitive_text

        print(redact_sensitive_text(
            "Call +15551234567 with OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"
        ))
        """,
    )

    assert result.returncode == 0, result.stderr
    assert "+15551234567" in result.stdout
    assert "abc123def456" not in result.stdout


def test_gateway_run_import_honors_secret_redaction_config_before_agent_snapshot(tmp_path):
    result = _run_gateway_redaction_probe(
        tmp_path,
        """
        security:
          redact_secrets: false
        """,
        "",
        """
        import gateway.run
        from agent.redact import redact_sensitive_text

        print(redact_sensitive_text(
            "OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"
        ))
        """,
    )

    assert result.returncode == 0, result.stderr
    assert "sk-proj-abc123def456ghi789jkl012" in result.stdout


def test_gateway_run_import_config_secret_redaction_overrides_dotenv(tmp_path):
    result = _run_gateway_redaction_probe(
        tmp_path,
        """
        security:
          redact_secrets: false
        """,
        "HERMES_REDACT_SECRETS=true\n",
        """
        import gateway.run
        from agent.redact import redact_sensitive_text

        print(os.environ.get("HERMES_REDACT_SECRETS"))
        print(redact_sensitive_text(
            "OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"
        ))
        """,
    )

    assert result.returncode == 0, result.stderr
    assert "false" in result.stdout
    assert "sk-proj-abc123def456ghi789jkl012" in result.stdout


def test_gateway_import_ignores_non_boolean_redaction_config_values(tmp_path):
    result = _run_gateway_redaction_probe(
        tmp_path,
        """
        security:
          redact_secrets: "no"
        privacy:
          redact_phone_numbers: "false"
        """,
        "",
        """
        import gateway
        from agent.redact import redact_sensitive_text

        print(os.environ.get("HERMES_REDACT_SECRETS", "<unset>"))
        print(os.environ.get("HERMES_REDACT_PHONE_NUMBERS", "<unset>"))
        print(redact_sensitive_text(
            "Call +15551234567 with OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"
        ))
        """,
    )

    assert result.returncode == 0, result.stderr
    assert "<unset>" in result.stdout
    assert "true" in result.stdout
    assert "+15551234567" not in result.stdout
    assert "sk-proj-abc123def456ghi789jkl012" not in result.stdout


def test_gateway_import_fails_closed_when_managed_overlay_fails(tmp_path):
    result = _run_gateway_redaction_probe(
        tmp_path,
        """
        security:
          redact_secrets: false
        privacy:
          redact_phone_numbers: false
        """,
        "",
        """
        from hermes_cli import managed_scope

        def fail_overlay(cfg):
            raise RuntimeError("managed overlay unavailable")

        managed_scope.apply_managed_overlay = fail_overlay

        import gateway
        from agent.redact import redact_sensitive_text

        print(os.environ.get("HERMES_REDACT_SECRETS", "<unset>"))
        print(os.environ.get("HERMES_REDACT_PHONE_NUMBERS", "<unset>"))
        print(redact_sensitive_text(
            "Call +15551234567 with OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"
        ))
        """,
    )

    assert result.returncode == 0, result.stderr
    assert "<unset>" in result.stdout
    assert "true" in result.stdout
    assert "+15551234567" not in result.stdout
    assert "sk-proj-abc123def456ghi789jkl012" not in result.stdout
