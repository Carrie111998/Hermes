"""DAN-3143: a ``${VAR}`` template must never be replaced by a plaintext secret.

The regression: ``_preserve_env_ref_templates`` restored the template only when
the value still matched some expansion of ``raw``. All three of its checks
compare against ``_expand_env_vars(raw)``, which leaves an unresolved
``${VAR}`` verbatim when the variable is NOT in ``os.environ``. So if the real
secret reached the config from somewhere else (Keychain, 1Password, a sourced
.env), nothing matched, the guard fell through, and ``save_config`` persisted
the credential as plaintext.

That is how a live unified-gateway bearer token ended up on disk in a
world-readable ``~/.hermes/config.yaml`` on mfc1.
"""

import textwrap

from hermes_cli.config import load_config, save_config


def _write_config(tmp_path, body: str):
    (tmp_path / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


def _read_config(tmp_path) -> str:
    return (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_expanded_secret_does_not_overwrite_template_when_var_unset(monkeypatch, tmp_path):
    """The exact mfc1 leak: var unset, secret supplied out-of-band, then saved."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("UNIFIED_GATEWAY_BEARER_TOKEN", raising=False)
    _write_config(
        tmp_path,
        """\
        mcp_servers:
          unified-gateway:
            url: https://mcp.example.com/unified/mcp
            headers:
              Authorization: Bearer ${UNIFIED_GATEWAY_BEARER_TOKEN}
        """,
    )

    config = load_config()
    # Simulate the secret arriving from Keychain/1Password rather than env.
    config["mcp_servers"]["unified-gateway"]["headers"]["Authorization"] = (
        "Bearer sk-live-REDACTED-0123456789abcdef"
    )
    save_config(config)

    on_disk = _read_config(tmp_path)
    assert "sk-live-REDACTED-0123456789abcdef" not in on_disk, (
        "plaintext secret was persisted over a ${VAR} template"
    )
    assert "${UNIFIED_GATEWAY_BEARER_TOKEN}" in on_disk


def test_rotated_secret_still_preserves_template_when_var_set(monkeypatch, tmp_path):
    """Rotation between load and save must keep the template, not the value."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SOME_API_TOKEN", "old-value")
    _write_config(
        tmp_path,
        """\
        mcp_servers:
          svc:
            headers:
              Authorization: Bearer ${SOME_API_TOKEN}
        """,
    )
    config = load_config()
    monkeypatch.setenv("SOME_API_TOKEN", "rotated-value")
    config["mcp_servers"]["svc"]["headers"]["Authorization"] = "Bearer rotated-value"
    save_config(config)

    on_disk = _read_config(tmp_path)
    assert "rotated-value" not in on_disk
    assert "${SOME_API_TOKEN}" in on_disk


def test_non_secret_literal_edit_is_still_caller_owned(monkeypatch, tmp_path):
    """The backstop must not seize ordinary edits.

    A template whose name is not credential-shaped stays caller-owned: replacing
    it with a literal is a deliberate edit and must persist.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    _write_config(
        tmp_path,
        """\
        mcp_servers:
          svc:
            url: ${GATEWAY_BASE_URL}
        """,
    )
    config = load_config()
    config["mcp_servers"]["svc"]["url"] = "https://pinned.example.com/mcp"
    save_config(config)

    on_disk = _read_config(tmp_path)
    assert "https://pinned.example.com/mcp" in on_disk, (
        "a deliberate non-secret literal edit must still be written"
    )


def test_clearing_a_secret_to_empty_is_not_treated_as_a_secret(monkeypatch, tmp_path):
    """Blanking a credential is a real edit, not a leak to guard against."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("SOME_API_TOKEN", raising=False)
    _write_config(
        tmp_path,
        """\
        mcp_servers:
          svc:
            headers:
              Authorization: ${SOME_API_TOKEN}
        """,
    )
    config = load_config()
    config["mcp_servers"]["svc"]["headers"]["Authorization"] = ""
    save_config(config)

    on_disk = _read_config(tmp_path)
    assert "${SOME_API_TOKEN}" not in on_disk


def test_intentional_replacement_wins_when_the_var_is_set(monkeypatch, tmp_path):
    """The guard must not block a deliberate new key when the var IS resolvable.

    This is the boundary between the leak and a legitimate edit. A first
    attempt at this fix keyed only on "does the value look like a secret",
    which broke ``test_save_config_allows_intentional_secret_value_change``:
    pasting a replacement API key through the UI is caller-owned and must
    persist. The guard therefore fires only when the referenced credential var
    is UNSET -- the one case where no expansion of the template could have
    produced the new value, so it must have arrived out-of-band.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SOME_API_TOKEN", "sk-old")
    _write_config(
        tmp_path,
        """\
        custom_providers:
          - name: svc
            api_key: ${SOME_API_TOKEN}
            model: claude-opus-4-6
        """,
    )
    config = load_config()
    config["custom_providers"][0]["api_key"] = "sk-deliberately-pasted"
    save_config(config)

    on_disk = _read_config(tmp_path)
    assert "sk-deliberately-pasted" in on_disk
    assert "${SOME_API_TOKEN}" not in on_disk
