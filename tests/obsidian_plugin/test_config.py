from plugins.memory.obsidian.config import build_obsidian_config, ObsidianConfig


def test_defaults():
    c = build_obsidian_config({})
    assert isinstance(c, ObsidianConfig)
    assert c.vault_path == "/srv/dj/obsidian"
    assert c.top_k == 5
    assert ".git" in c.exclude_dirs
    assert c.pinned == ()
    assert c.sync_interval_minutes == 5


def test_overrides():
    c = build_obsidian_config(
        {"vault_path": "/x/vault", "top_k": 8,
         "exclude_dirs": [".git", "archive"],
         "pinned": ["memory/core.md", "memory/daniel.md"],
         "sync_interval_minutes": 12}
    )
    assert c.vault_path == "/x/vault"
    assert c.top_k == 8
    assert c.exclude_dirs == (".git", "archive")
    assert c.pinned == ("memory/core.md", "memory/daniel.md")
    assert c.sync_interval_minutes == 12


def test_none_config_uses_defaults():
    assert build_obsidian_config(None).vault_path == "/srv/dj/obsidian"


def test_explicit_empty_exclude_dirs_respected():
    c = build_obsidian_config({"exclude_dirs": []})
    assert c.exclude_dirs == ()  # explicit empty must NOT fall back to defaults


def test_negative_sync_interval_is_disabled():
    assert build_obsidian_config({"sync_interval_minutes": -1}).sync_interval_minutes == 0


def test_cli_encoded_lists_are_decoded():
    c = build_obsidian_config({
        "exclude_dirs": '[".git", "archive"]',
        "pinned": '["memory/daniel.md"]',
    })
    assert c.exclude_dirs == (".git", "archive")
    assert c.pinned == ("memory/daniel.md",)
