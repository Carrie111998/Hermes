"""Tests for .env sanitization during load to prevent token duplication (#8908)."""

import tempfile
from pathlib import Path
from unittest.mock import patch


def test_load_env_preserves_concatenated_text_as_value_data():
    """Verify load_env() does not infer assignments within a physical line.

    A missing newline is ambiguous: text resembling a second assignment may
    instead be part of the first value, so it must remain opaque value data.
    """
    from hermes_cli.config import load_env

    token = "0123456789:test"
    # Simulate concatenated line: TOKEN=xxx followed immediately by another key
    corrupted = f"TELEGRAM_BOT_TOKEN={token}ANTHROPIC_API_KEY=sk-ant-test123\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(corrupted)
        env_path = Path(f.name)

    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            result = load_env()
        assert result.get("TELEGRAM_BOT_TOKEN") == (
            f"{token}ANTHROPIC_API_KEY=sk-ant-test123"
        )
        assert "ANTHROPIC_API_KEY" not in result
    finally:
        env_path.unlink(missing_ok=True)


def test_load_env_normal_file_unchanged():
    """A well-formed .env file should be parsed identically."""
    from hermes_cli.config import load_env

    content = (
        "TELEGRAM_BOT_TOKEN=mytoken123\n"
        "ANTHROPIC_API_KEY=sk-ant-key\n"
        "# comment\n"
        "\n"
        "OPENAI_API_KEY=sk-openai\n"
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        env_path = Path(f.name)

    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            result = load_env()
        assert result["TELEGRAM_BOT_TOKEN"] == "mytoken123"
        assert result["ANTHROPIC_API_KEY"] == "sk-ant-key"
        assert result["OPENAI_API_KEY"] == "sk-openai"
    finally:
        env_path.unlink(missing_ok=True)


def test_env_loader_does_not_split_concatenated_text():
    """Verify sanitization preserves one assignment per physical line."""
    from hermes_cli.env_loader import _sanitize_env_file_if_needed

    token = "0123456789:test"
    corrupted = f"TELEGRAM_BOT_TOKEN={token}ANTHROPIC_API_KEY=sk-ant-test\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(corrupted)
        env_path = Path(f.name)

    try:
        _sanitize_env_file_if_needed(env_path)
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()
        assert lines == [corrupted]
        parsed_token = lines[0].strip().split("=", 1)[1]
        assert parsed_token == f"{token}ANTHROPIC_API_KEY=sk-ant-test"
    finally:
        env_path.unlink(missing_ok=True)


def test_load_env_drops_lone_mask_placeholder_line():
    """A KEY=*** line is a pasted masked display, not a credential.

    Masked secret output echoes ``***``; copying that block into .env must
    not install the literal mask as a live value.
    """
    import os

    from hermes_cli.config import load_env

    content = "ANTHROPIC_API_KEY=***\nOPENAI_API_KEY=sk-real\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        env_path = Path(f.name)

    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            result = load_env()
        assert "ANTHROPIC_API_KEY" not in result
        assert result["OPENAI_API_KEY"] == "sk-real"
    finally:
        env_path.unlink(missing_ok=True)
        os.environ.pop("OPENAI_API_KEY", None)


def test_load_env_drops_quoted_mask_placeholder():
    """Quoted variants of the mask (KEY=\"***\") are placeholders too."""
    from hermes_cli.config import load_env

    content = 'TELEGRAM_BOT_TOKEN="***"\n'

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        env_path = Path(f.name)

    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            result = load_env()
        assert "TELEGRAM_BOT_TOKEN" not in result
    finally:
        env_path.unlink(missing_ok=True)


def test_sanitize_keeps_values_merely_containing_mask():
    """Only a *complete* *** value is a placeholder — substrings stay."""
    from hermes_cli.config import _sanitize_env_lines

    lines = [
        "SECRET_KEY=abc***def\n",
        "MASKED_IN_MIDDLE=x***y=z\n",
        "# note: *** in comment stays\n",
        "\n",
        "REAL_KEY=value\n",
    ]
    out = _sanitize_env_lines(lines)
    assert out == lines


def test_sanitize_drops_only_placeholder_entries():
    """partition('=') semantics: empty values and suffixed keys survive."""
    from hermes_cli.config import _sanitize_env_lines

    lines = [
        "EMPTY_KEY=\n",
        "KNOWN=value=***\n",
        "GONE=***\n",
    ]
    out = _sanitize_env_lines(lines)
    assert out == ["EMPTY_KEY=\n", "KNOWN=value=***\n"]
