"""A config version bump must not destroy comments, quoting, or set values.

Regression for the v33 -> v34 bump that silently deleted the two lines
recording that the dashboard signing secret lives in ~/.hermes/.env rather than
in the tracked config -- the only in-file pointer to its real home -- and
blanked an explicitly-set display.personality via default-stripping.

The bump is a pure metadata write, so it goes through
atomic_roundtrip_yaml_update (ruamel round-trip) instead of re-serialising the
whole document with PyYAML, which cannot represent comments at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from utils import atomic_roundtrip_yaml_update

SECRET_POINTER = (
    "    # signing secret is NOT stored here — set "
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET\n"
    "    # in ~/.hermes/.env (env overrides this block per config_defaults.py)\n"
)

SAMPLE = f"""\
# Top-of-file banner that must survive a version bump.
display:
  # inline comment above a set value
  personality: kawaii
  compact: false
dashboard:
  basic_auth:
    username: admin
    password_hash: "scrypt$16384$8$1$abc==$def="
{SECRET_POINTER}    session_ttl_seconds: 43200
_config_version: 33
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(SAMPLE)
    return path


def test_bump_changes_only_the_version(config_file: Path):
    atomic_roundtrip_yaml_update(config_file, "_config_version", 34)
    after = config_file.read_text()

    assert "_config_version: 34" in after
    assert "_config_version: 33" not in after


def test_bump_preserves_comments(config_file: Path):
    atomic_roundtrip_yaml_update(config_file, "_config_version", 34)
    after = config_file.read_text()

    # The load-bearing one: without it, someone re-adds the secret to the
    # tracked file because nothing on disk says where it actually lives.
    assert "signing secret is NOT stored here" in after
    assert "env overrides this block per config_defaults.py" in after
    assert "Top-of-file banner that must survive a version bump." in after
    assert "inline comment above a set value" in after


def test_bump_preserves_set_values_and_quoting(config_file: Path):
    atomic_roundtrip_yaml_update(config_file, "_config_version", 34)
    after = config_file.read_text()

    # Default-stripping blanked this on the real config.
    assert "personality: kawaii" in after
    # preserve_quotes keeps the hash quoted; bare scrypt$... invites a reparse
    # surprise on the `$` and `=` characters.
    assert 'password_hash: "scrypt$16384$8$1$abc==$def="' in after


def test_bump_is_idempotent(config_file: Path):
    atomic_roundtrip_yaml_update(config_file, "_config_version", 34)
    once = config_file.read_text()
    atomic_roundtrip_yaml_update(config_file, "_config_version", 34)
    assert config_file.read_text() == once


def test_pyyaml_roundtrip_would_lose_the_comments(config_file: Path):
    """Guard the rationale: the naive path this replaced is genuinely lossy.

    If PyYAML ever learns to round-trip comments this fails loudly and the
    workaround can be revisited -- rather than surviving as cargo cult.
    """
    import yaml

    data = yaml.safe_load(config_file.read_text())
    data["_config_version"] = 34
    naive = yaml.dump(data, sort_keys=False)

    assert "signing secret is NOT stored here" not in naive
    assert "Top-of-file banner" not in naive
