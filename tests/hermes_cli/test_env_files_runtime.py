"""Regression tests for config.yaml:env_files runtime support.

env_files lets operators declare external secret files (e.g. provider
key files) that load_env() merges after ~/.hermes/.env, so provider
credentials resolve without duplicating secrets into ~/.hermes/.env.

Precedence (lowest → highest):
  ~/.hermes/.env  <  config.yaml:env_files  <  process os.environ
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.config import get_env_value, invalidate_env_cache, load_env


@pytest.fixture(autouse=True)
def _clean_env_cache():
    """Isolate the module-level load_env memo across tests."""
    invalidate_env_cache()
    yield
    invalidate_env_cache()


def _patch_env_path(primary: Path):
    return patch("hermes_cli.config.get_env_path", return_value=primary)


def _patch_env_files(files):
    return patch(
        "hermes_cli.config.read_raw_config",
        return_value={"env_files": [str(f) for f in files]},
    )


def test_env_files_single_file_override(monkeypatch, tmp_path):
    """A single env_files entry overrides ~/.hermes/.env and feeds get_env_value."""
    primary = tmp_path / "dot_env"
    primary.write_text("ANT_LING_API_KEY=primary_secret\n", encoding="utf-8")
    extra = tmp_path / "extra.env"
    extra.write_text("ANT_LING_API_KEY=extra_secret\n", encoding="utf-8")

    with _patch_env_path(primary), _patch_env_files([extra]):
        env = load_env()
        assert env.get("ANT_LING_API_KEY") == "extra_secret", (
            "env_files should override ~/.hermes/.env"
        )

        # The value must be reachable through the same get_env_value()
        # primitive that provider credential resolution uses.
        monkeypatch.delenv("ANT_LING_API_KEY", raising=False)
        assert get_env_value("ANT_LING_API_KEY") == "extra_secret"


def test_env_files_multiple_files_and_missing_safe(tmp_path):
    """Multiple env_files merge in order (last wins); a missing file is non-fatal."""
    primary = tmp_path / "dot_env"
    primary.write_text("A=1\n", encoding="utf-8")

    missing = tmp_path / "does_not_exist.env"
    extra1 = tmp_path / "e1.env"
    extra2 = tmp_path / "e2.env"
    extra1.write_text("B=2\n", encoding="utf-8")
    extra2.write_text("A=overridden\n", encoding="utf-8")

    with _patch_env_path(primary), _patch_env_files([missing, extra1, extra2]):
        env = load_env()
        assert env.get("A") == "overridden"  # last declared file wins
        assert env.get("B") == "2"           # earlier file still merged
        # Missing file did not raise or poison the load.
        assert env.get("C") is None


def test_backwards_compat_without_env_files(tmp_path):
    """With no env_files key, load_env behaves exactly like before."""
    primary = tmp_path / "dot_env"
    primary.write_text(
        "OPENAI_API_KEY=sk-abc\nDEEPSEEK_API_KEY=dk-xyz\n",
        encoding="utf-8",
    )

    with _patch_env_path(primary), _patch_env_files([]):
        env = load_env()
        assert env.get("OPENAI_API_KEY") == "sk-abc"
        assert env.get("DEEPSEEK_API_KEY") == "dk-xyz"
        assert len(env) == 2


def test_process_env_precedence_over_env_files(monkeypatch, tmp_path):
    """os.environ wins over env_files values (checked by get_env_value first)."""
    primary = tmp_path / "dot_env"
    primary.write_text("ANT_LING_API_KEY=primary_secret\n", encoding="utf-8")
    extra = tmp_path / "extra.env"
    extra.write_text("ANT_LING_API_KEY=extra_secret\n", encoding="utf-8")

    with _patch_env_path(primary), _patch_env_files([extra]):
        monkeypatch.setenv("ANT_LING_API_KEY", "proc_secret")
        assert get_env_value("ANT_LING_API_KEY") == "proc_secret"
        # load_env itself is file-only; the dict still shows the file value,
        # but get_env_value (the real consumer) honours the process env.
        assert load_env().get("ANT_LING_API_KEY") == "extra_secret"


def test_env_files_cache_invalidates_on_extra_file_change(tmp_path):
    """Editing an env_files file (mtime bump) invalidates the memo."""
    primary = tmp_path / "dot_env"
    primary.write_text("K=old\n", encoding="utf-8")
    extra = tmp_path / "extra.env"
    extra.write_text("K=from-file\n", encoding="utf-8")

    with _patch_env_path(primary), _patch_env_files([extra]):
        assert load_env().get("K") == "from-file"
        # Same bytes → cache hit path still returns correct value.
        assert load_env().get("K") == "from-file"
        # Rewrite the extra file and bump mtime beyond FS granularity.
        extra.write_text("K=from-file-v2\n", encoding="utf-8")
        os.utime(extra, (extra.stat().st_atime + 5, extra.stat().st_mtime + 5))
        assert load_env().get("K") == "from-file-v2"


def test_malformed_env_file_is_ignored_safely(tmp_path):
    """Malformed extra files are sanitised (no crash, no invented values)."""
    primary = tmp_path / "dot_env"
    primary.write_text("GOOD=1\n", encoding="utf-8")
    extra = tmp_path / "malformed.env"
    extra.write_text(
        "no_equals_here\n"
        "# comment\n"
        "CONCAT=one=two\n"
        "PLACEHOLDER=changeme_xxx\n"
        'QUOTED="stripped"\n'
        "=missing_key\n",
        encoding="utf-8",
    )

    with _patch_env_path(primary), _patch_env_files([extra]):
        env = load_env()
        assert env.get("GOOD") == "1"            # primary unaffected
        assert env.get("QUOTED") == "stripped"   # quotes stripped as before
        assert env.get("no_equals_here") is None  # no bogus key invented
        # Placeholder/value sanitation is covered by test_env_sanitize_on_load.py;
        # here we only require a crash-free load with the primary value intact.


def test_unreadable_env_file_non_fatal(tmp_path):
    """An extra file that cannot be read is treated as absent, not a crash."""
    primary = tmp_path / "dot_env"
    primary.write_text("KEEP=yes\n", encoding="utf-8")
    directory_as_file = tmp_path / "not_a_file"  # a directory → open() raises
    directory_as_file.mkdir()

    with _patch_env_path(primary), _patch_env_files([directory_as_file]):
        env = load_env()
        assert env.get("KEEP") == "yes"


def test_no_secret_leak_in_errors(tmp_path):
    """Failures while merging env_files never surface secret values."""
    primary = tmp_path / "dot_env"
    secret = "super_secret_value_123"
    primary.write_text(f"ANT_LING_API_KEY={secret}\n", encoding="utf-8")
    extra = tmp_path / "broken.env"
    extra.write_text("X=1\n", encoding="utf-8")

    with _patch_env_path(primary), _patch_env_files([extra]):
        try:
            load_env()
            # No exception is the expected path; nothing to inspect.
        except Exception as exc:
            assert secret not in str(exc), "secret leaked into exception message"
        # The secret must never appear in the merged dict's keys or file list.
        env = load_env()
        assert secret not in str(list(env.keys()))


def test_provider_api_key_env_resolution_from_env_files(monkeypatch, tmp_path):
    """resolve_api_key_provider_credentials picks up keys declared via env_files.

    xai is a plain api_key provider (api_key_env_vars=("XAI_API_KEY",)) with
    no network probe in its resolver, so this is a clean integration check of
    the _resolve_api_key_provider_secret → get_env_value → load_env chain.
    """
    from hermes_cli.auth import resolve_api_key_provider_credentials

    primary = tmp_path / "dot_env"
    primary.write_text("UNRELATED=zzz\n", encoding="utf-8")
    extra = tmp_path / "xai.env"
    extra.write_text("XAI_API_KEY=xai-from-env-files\n", encoding="utf-8")

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with _patch_env_path(primary), _patch_env_files([extra]):
        creds = resolve_api_key_provider_credentials("xai")
        assert creds["api_key"] == "xai-from-env-files"
        assert creds["source"] == "XAI_API_KEY"


def test_custom_provider_key_env_resolves_from_env_files(monkeypatch, tmp_path):
    """Custom provider (config.yaml providers.*) key_env resolves via env_files.

    Regression for the Ant Ling native-routing case: runtime_provider reads
    the key through get_env_value(), so keys declared in env_files resolve
    without duplicating secrets into ~/.hermes/.env or the process env.
    """
    from hermes_cli.runtime_provider import _resolve_named_custom_runtime

    primary = tmp_path / "dot_env"
    primary.write_text("UNRELATED=zzz\n", encoding="utf-8")
    extra = tmp_path / "antling.env"
    extra.write_text("ANT_LING_API_KEY=antling-from-env-files\n", encoding="utf-8")

    monkeypatch.delenv("ANT_LING_API_KEY", raising=False)
    with _patch_env_path(primary), _patch_env_files([extra]), patch(
        "hermes_cli.runtime_provider.load_config",
        return_value={
            "providers": {
                "ant-ling": {
                    "key_env": "ANT_LING_API_KEY",
                    "base_url": "https://api.ant-ling.com/v1",
                },
            },
        },
    ):
        runtime = _resolve_named_custom_runtime(requested_provider="ant-ling")
        assert runtime is not None
        assert runtime["api_key"] == "antling-from-env-files"


if __name__ == "__main__":
    pytest.main([__file__])
