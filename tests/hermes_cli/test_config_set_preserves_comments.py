"""Regression: `hermes config set` must not delete config.yaml comment blocks.

`save_config` appends commented-out documentation sections (Security,
Fallback Model) via ``atomic_yaml_write(extra_content=...)``. ``set_config_value``
took a *different* write path that called ``atomic_yaml_write`` with no
``extra_content``, so a single ``hermes config set`` rewrote the document and
silently dropped those blocks. They are comments, not mapping entries, so no
later read or write could restore them — the loss was permanent and invisible.

Observed live: ``hermes config set mcp_servers.gitlab.enabled false`` removed
38 lines of Security + Fallback Model documentation from a user's config.yaml.
"""

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point Hermes at a temp HOME and return the fresh config module."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HOME", str(tmp_path))
    import hermes_cli.config as config_mod

    importlib.reload(config_mod)
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    return config_mod


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_config_set_preserves_trailing_comment_sections(hermes_home, monkeypatch):
    """A single `config set` must leave the doc comment blocks intact."""
    config_mod = hermes_home
    config_path = Path(config_mod.get_config_path())
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed a config that looks like a real one: some settings plus the
    # trailing commented-out doc sections that save_config emits.
    seed = (
        "model:\n"
        "  default: anthropic/claude-sonnet-5\n"
        + config_mod._SECURITY_COMMENT
        + config_mod._FALLBACK_COMMENT
    )
    config_path.write_text(seed, encoding="utf-8")

    before = _read(config_path)
    assert "── Security ──" in before
    assert "── Fallback Model ──" in before

    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    config_mod.set_config_value("agent.max_turns", "42")

    after = _read(config_path)

    # The write landed...
    assert "max_turns" in after
    # ...and the comment blocks survived it.
    assert "── Security ──" in after, "Security comment block was deleted"
    assert "── Fallback Model ──" in after, "Fallback Model block was deleted"
    assert "# security:" in after
    assert "# fallback_model:" in after


def test_config_set_repeated_does_not_duplicate_comments(hermes_home, monkeypatch):
    """Comment blocks must appear exactly once no matter how many writes."""
    config_mod = hermes_home
    config_path = Path(config_mod.get_config_path())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "model:\n  default: anthropic/claude-sonnet-5\n"
        + config_mod._SECURITY_COMMENT
        + config_mod._FALLBACK_COMMENT,
        encoding="utf-8",
    )

    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    for i in range(3):
        config_mod.set_config_value("agent.max_turns", str(40 + i))

    after = _read(config_path)
    assert after.count("── Security ──") == 1, "Security block duplicated"
    assert after.count("── Fallback Model ──") == 1, "Fallback block duplicated"


def test_configured_fallback_model_suppresses_its_comment(hermes_home, monkeypatch):
    """Once fallback_model is really configured, its placeholder stops printing.

    This is the behavior contract of _optional_comment_sections: the block is
    documentation for an UNSET feature, so a configured feature must not keep
    re-emitting the placeholder.
    """
    config_mod = hermes_home
    config_path = Path(config_mod.get_config_path())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "fallback_model:\n"
        "  provider: openrouter\n"
        "  model: anthropic/claude-sonnet-5\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    config_mod.set_config_value("agent.max_turns", "7")

    after = _read(config_path)
    assert "── Fallback Model ──" not in after
    # Security is still unset, so its block is still offered.
    assert "── Security ──" in after


def test_optional_comment_sections_contract():
    """Unit-level invariants for the shared helper."""
    import hermes_cli.config as config_mod

    # Empty config: both blocks offered.
    assert len(config_mod._optional_comment_sections({})) == 2

    # Valid fallback as a mapping suppresses only the fallback block.
    parts = config_mod._optional_comment_sections(
        {"fallback_model": {"provider": "openrouter", "model": "x/y"}}
    )
    assert len(parts) == 1
    assert "Security" in parts[0]

    # Valid fallback as a LIST also suppresses it (multi-tier failover).
    parts = config_mod._optional_comment_sections(
        {"fallback_model": [{"provider": "openrouter", "model": "x/y"}]}
    )
    assert len(parts) == 1

    # An incomplete fallback entry is not valid — keep documenting it.
    parts = config_mod._optional_comment_sections(
        {"fallback_model": {"provider": "openrouter"}}
    )
    assert len(parts) == 2

    # security.redact_secrets explicitly set suppresses the security block.
    parts = config_mod._optional_comment_sections(
        {"security": {"redact_secrets": False}}
    )
    assert len(parts) == 1
    assert "Fallback" in parts[0]
