"""Tests for credential exclusion and secret scrubbing during profile export.

Profile exports must exclude exact credentials, OAuth stores, credential trees,
private keys, and recognized backup variants. Surviving text files are
force-redacted in the staged archive, while the live profile remains untouched.
Both default and named-profile export paths are covered.
"""

import sqlite3
import tarfile
from pathlib import Path

import pytest

from hermes_cli.profile_export import (
    _DEFAULT_EXPORT_EXCLUDE_ROOT,
    _is_sensitive_export_name,
    _sqlite_sidecars_in_directory,
    _verify_compacted_sqlite_semantics,
)
from hermes_cli.profiles import export_profile


# Long enough to match agent.redact prefix patterns (sk- + 10+ chars).
_LEAKED_KEY = "sk-or-v1-reallyLongSecretKeyValue12345678"


def _patch_named_profile(monkeypatch, profiles_root, profile_dir):
    monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: profiles_root)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda n: profile_dir)
    monkeypatch.setattr("hermes_cli.profiles.validate_profile_name", lambda n: None)


def _archive_member_bytes(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.extractfile(name)
    assert member is not None
    return member.read()


class TestIsSensitiveExportName:
    """Unit coverage for the shared sensitive-name classifier."""

    @pytest.mark.parametrize(
        "name",
        [
            # Exact credential basenames
            ".env",
            ".envrc",
            ".claude.json",
            ".netrc",
            ".npmrc",
            ".pgpass",
            ".pypirc",
            "auth.json",
            "auth.lock",
            ".anthropic_oauth.json",
            "google_token.json",
            "google_oauth_pending.json",
            "google_oauth.json",
            "webhook_subscriptions.json",
            "feishu_comment_pairing.json",
            "bws_cache.json",
            "bws_cache.enc.json",
            "oauth_creds.json",
            ".git-credentials",
            # dotenv variants (non-template)
            ".env.local",
            ".env.production",
            ".env.bak-kiro-20260529115545",
            ".env.bak-gemini-embedding-20260506_004415",
            ".env.bak.example",
            # config backups (real-world shapes seen on disk)
            "config.yaml.bak.20260526_130938",
            "config.yaml.bak-pre-migrate-xai-20260410-040915",
            "config.yaml.bak-provider-key-cleanup-20260506_013334",
            "config.yml.bak.20260101_000000",
            "config.yaml.bak-kiro-context-20260529140131",
            "config.yaml.corrupt.20260812-123456.bak",
            "config.yaml.corrupt.20260812-123456.bak.copy",
            "config.yaml.corrupt.20260812-123456.bak.old",
            "config.yaml.corrupt.20260812-123456.bak.backup-before-reset",
            "config.yaml.corrupt.20260812-123456.bak.tmp.4242",
            "config.yaml.corrupt.20260812-123456.bak.20260813",
            "config.yaml.corrupt.20260812-123456.bak~",
            # auth/config/tilde backups
            "auth.json.bak",
            "auth.json.20260101",
            "auth.json~",
            "config.yaml~",
            ".env~",
            # canonical credential-store backups
            ".anthropic_oauth.json.bak",
            "google_token.json.bak",
            "google_oauth_pending.json.backup-20260101",
            "google_oauth.json.old",
            "webhook_subscriptions.json.copy",
            "feishu_comment_pairing.json.bak",
            "bws_cache.json.20260101",
            "bws_cache.enc.json.bak",
            "oauth_creds.json.tmp.4242.deadbeef",
            # private keys / keystores
            "id_rsa.key",
            "deploy.ppk",
            "deploy.ppk.bak",
            "deploy.ppk~",
            "store.p12",
            "cert.pfx",
            "release.keystore",
            "release.jks",
            # credential-/token-looking containers
            "credentials.json",
            "credentials.json.bak",
            "credentials",
            "client_secret.json",
            "access_token.txt",
            "refresh-tokens.yaml",
            "api_key.txt",
            "api-keys.ini",
            "secrets.yaml",
            # no-extension credential names
            "credentials",
            "id_rsa",
        ],
    )
    def test_sensitive_names_flagged(self, name):
        assert _is_sensitive_export_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            # Ordinary profile files
            "config.yaml",
            "config.yml",
            "SOUL.md",
            "MEMORY.md",
            "USER.md",
            "profile.yaml",
            "distribution.yaml",
            "feishu_comment_rules.json",
            "README.md",
            "ca-bundle.pem",
            "server-cert.pem",
            # dotenv templates are safe to ship
            ".env.example",
            ".env.sample",
            ".env.template",
            ".env.dist",
            # token/secret substrings that are NOT credentials
            "tokenizer.json",
            "token_count.md",
            "secret-santa.md",
            "my-secrets-notes.md",  # .md is not a credential container
            "apikeys-guide.md",
            "backup-notes.md",
            # Credential-tree basenames are path-sensitive, not globally banned.
            "mcp-tokens",
            "pairing",
            "mcp-tokens.bak",
            "pairing.backup-20260101",
            # Backups of non-sensitive files remain portable.
            "notes.txt.bak",
            "notes.corrupt.20260812-123456.bak",
            "draft.md.bak",
            "notes.txt~",
        ],
    )
    def test_safe_names_not_flagged(self, name):
        assert _is_sensitive_export_name(name) is False

    def test_case_insensitive(self):
        assert _is_sensitive_export_name("Config.YAML.BAK.20260101_000000") is True
        assert _is_sensitive_export_name("AUTH.JSON") is True
        assert _is_sensitive_export_name(".ENV.LOCAL") is True
        assert _is_sensitive_export_name("BWS_CACHE.ENC.JSON") is True
        assert _is_sensitive_export_name("MCP-TOKENS") is False


class TestCredentialExclusion:
    def test_auth_json_in_default_exclude_set(self):
        """auth.json must be in the default export exclusion set."""
        assert "auth.json" in _DEFAULT_EXPORT_EXCLUDE_ROOT


    def test_named_profile_export_excludes_auth(self, tmp_path, monkeypatch):
        """Named profile export must not contain auth.json or .env."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "testprofile"
        profile_dir.mkdir(parents=True)

        # Create a profile with credentials
        (profile_dir / "config.yaml").write_text("model: gpt-4\n")
        (profile_dir / "auth.json").write_text('{"tokens": {"access": "sk-secret"}}')
        (profile_dir / ".env").write_text("OPENROUTER_API_KEY=x\n")
        (profile_dir / "SOUL.md").write_text("I am helpful.\n")
        (profile_dir / "memories").mkdir()
        (profile_dir / "memories" / "MEMORY.md").write_text("# Memories\n")

        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda n: profile_dir
        )
        monkeypatch.setattr("hermes_cli.profiles.validate_profile_name", lambda n: None)

        output = tmp_path / "export.tar.gz"
        result = export_profile("testprofile", str(output))

        # Check archive contents
        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()

        assert any("config.yaml" in n for n in names), "config.yaml should be in export"
        assert any("SOUL.md" in n for n in names), "SOUL.md should be in export"
        assert not any("auth.json" in n for n in names), (
            "auth.json must NOT be in export"
        )
        assert not any(n.endswith("/.env") or n == ".env" for n in names), (
            ".env must NOT be in export"
        )

    def test_named_profile_export_excludes_backups_and_secrets(
        self, tmp_path, monkeypatch
    ):
        """Named profile export must drop config/env/auth backups and secrets,
        while keeping ordinary profile files (including .env.example)."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "testprofile"
        profile_dir.mkdir(parents=True)

        # Files that MUST be excluded
        sensitive = [
            ".env",
            ".envrc",
            ".env.bak-kiro-20260529115545",
            ".env.bak.example",
            ".env.local",
            ".env~",
            "auth.json",
            "auth.lock",
            "auth.json.bak",
            "auth.json~",
            ".anthropic_oauth.json",
            ".anthropic_oauth.json.bak",
            "google_token.json",
            "google_token.json.bak",
            "google_oauth_pending.json",
            "google_oauth_pending.json.backup-20260101",
            "webhook_subscriptions.json",
            "webhook_subscriptions.json.copy",
            "feishu_comment_pairing.json",
            "feishu_comment_pairing.json.bak",
            "bws_cache.enc.json",
            "bws_cache.enc.json.bak",
            "oauth_creds.json",
            "oauth_creds.json.tmp.4242.deadbeef",
            ".claude.json",
            ".git-credentials",
            ".netrc",
            ".netrc.bak",
            ".npmrc",
            ".pgpass",
            ".pypirc",
            "config.yaml.bak.20260526_130938",
            "config.yaml.bak-pre-migrate-xai-20260410-040915",
            "config.yaml.corrupt.20260812-123456.bak",
            "config.yaml.corrupt.20260812-123456.bak.copy",
            "config.yaml.corrupt.20260812-123456.bak.old",
            "config.yaml.corrupt.20260812-123456.bak.backup-before-reset",
            "config.yaml.corrupt.20260812-123456.bak.tmp.4242",
            "config.yaml.corrupt.20260812-123456.bak.20260813",
            "config.yaml.corrupt.20260812-123456.bak~",
            "config.yaml~",
            "credentials.json",
            "credentials.json.bak",
            "client_secret.json",
            "deploy.ppk",
            "deploy.ppk.bak",
            "deploy.ppk~",
        ]
        # Files that MUST survive
        kept = [
            "config.yaml",
            "SOUL.md",
            "profile.yaml",
            "feishu_comment_rules.json",
            ".env.example",
            "README.md",
            "public-ca.pem",
            "public-ca.pem.bak",
            "public-ca.pem~",
        ]
        for name in sensitive:
            (profile_dir / name).write_text("SENSITIVE\n")
        for name in kept:
            (profile_dir / name).write_text("ok\n")
        private_key = (
            "-----BEGIN PRIVATE KEY-----\nSENSITIVE\n-----END PRIVATE KEY-----\n"
        )
        (profile_dir / "private.pem").write_text(private_key)
        (profile_dir / "private.pem.bak").write_text(private_key)
        (profile_dir / "private.pem.20260101").write_text(private_key)
        (profile_dir / "private.pem~").write_text(private_key)
        (profile_dir / "late-private.pem").write_text("x" * 9000 + private_key)
        public_cert = "-----BEGIN CERTIFICATE-----\nPUBLIC\n-----END CERTIFICATE-----\n"
        (profile_dir / "public-ca.pem").write_text(public_cert)
        (profile_dir / "public-ca.pem.bak").write_text(public_cert)
        (profile_dir / "public-ca.pem~").write_text(public_cert)

        # A nested backup deep in a subdir must also be dropped.
        nested = profile_dir / "skins" / "old"
        nested.mkdir(parents=True)
        (nested / "config.yaml.bak.20260101_000000").write_text("SENSITIVE\n")
        (nested / "theme.json").write_text("{}\n")

        # Canonical credential files can live below nested auth/cache paths.
        canonical_nested = [
            profile_dir / "auth" / "google_oauth.json",
            profile_dir / "auth" / "google_oauth.json.bak",
            profile_dir / "cache" / "bws_cache.json",
            profile_dir / "cache" / "bws_cache.json.20260101",
            profile_dir / "cache" / "bws_cache.enc.json",
            profile_dir / "cache" / "bws_cache.enc.json.bak",
        ]
        for path in canonical_nested:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SENSITIVE\n")

        # Canonical credential trees must be excluded at their profile-root
        # locations, including legacy/new pairing paths and renamed backups.
        credential_tree_files = [
            profile_dir / "mcp-tokens" / "server.json",
            profile_dir / "mcp-tokens.bak" / "server.json",
            profile_dir / "mcp-tokens.bak-pre-migrate" / "server.json",
            profile_dir / "pairing" / "device.json",
            profile_dir / "pairing.backup-20260101" / "device.json",
            profile_dir / "pairing.backup-before-reset" / "device.json",
            profile_dir / "platforms" / "pairing" / "device.json",
            profile_dir
            / "platforms"
            / "pairing.backup-20260101"
            / "device.json",
            profile_dir
            / "platforms"
            / "pairing.bak-before-reset"
            / "device.json",
        ]
        # Same-named directories outside canonical paths are ordinary user data
        # and must remain portable.
        ordinary_tree_files = [
            profile_dir / "pairing-old-notes" / "README.md",
            profile_dir / "mcp-tokens_copy_of_docs" / "README.md",
            profile_dir / "pairing.tmp-notes" / "README.md",
            profile_dir / "mcp-tokens.old-docs" / "README.md",
            profile_dir / "plugins" / "demo" / "pairing" / "README.md",
            profile_dir
            / "plugins"
            / "demo"
            / "pairing.backup-20260101"
            / "README.md",
            profile_dir / "workspace" / "project" / "mcp-tokens" / "README.md",
            profile_dir / "workspace" / "project" / "credentials" / "README.md",
            profile_dir
            / "workspace"
            / "project"
            / "mcp-tokens.bak"
            / "README.md",
        ]
        for path in credential_tree_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SENSITIVE\n")
        for path in ordinary_tree_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("safe\n")
        (profile_dir / "plugins" / "demo" / "README.md").write_text("safe\n")

        # ``home/`` is the persistent subprocess HOME used by profile-backed
        # containers. Credential directories there must be removed without
        # dropping unrelated HOME or .config content.
        home_sensitive = [
            profile_dir / "home" / ".ssh" / "custom-production-key",
            profile_dir / "home" / ".ssh.bak" / "custom-production-key",
            profile_dir
            / "home"
            / ".ssh.bak-before-rotation"
            / "custom-production-key",
            profile_dir / "home" / ".aws" / "credentials",
            profile_dir / "home" / ".gnupg" / "private-keys-v1.d" / "key",
            profile_dir / "home" / ".kube" / "config",
            profile_dir / "home" / ".docker" / "config.json",
            profile_dir / "home" / ".azure" / "accessTokens.json",
            profile_dir / "home" / ".gcloud" / "credentials.db",
            profile_dir / "home" / ".config" / "gh" / "hosts.yml",
            profile_dir / "home" / ".config" / "gh.backup-20260101" / "hosts.yml",
            profile_dir
            / "home"
            / ".config"
            / "gh.backup-before-reset"
            / "hosts.yml",
            profile_dir / "home" / ".config" / "gcloud" / "credentials.db",
            profile_dir / "home" / ".config" / "github-copilot" / "hosts.json",
            profile_dir
            / "home"
            / ".config"
            / "github-copilot.backup-20260101"
            / "hosts.json",
            profile_dir / "home" / ".codex" / "auth.json",
            profile_dir / "home" / ".claude" / ".credentials.json",
            profile_dir / "home" / ".claude.json",
            profile_dir / "home" / ".minimax" / "credentials.json",
            profile_dir / "home" / ".qwen" / "oauth_creds.json",
            profile_dir
            / "home"
            / ".qwen"
            / "oauth_creds.json.tmp.4242.deadbeef",
            profile_dir / "home" / ".gemini" / "oauth_creds.json",
            profile_dir / "home" / "Library" / "Keychains" / "login.keychain-db",
        ]
        home_kept = [
            profile_dir / "home" / "README.md",
            profile_dir / "home" / "projects" / "demo.txt",
            profile_dir / "home" / ".config" / "editor" / "settings.json",
            profile_dir / "home" / ".config" / "editor" / "hosts.json",
            profile_dir / "home" / ".config" / "github-copilot-theme" / "settings.json",
            profile_dir / "home" / ".qwen" / "settings.json",
        ]
        for path in home_sensitive:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SENSITIVE\n")
        for path in home_kept:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("safe\n")

        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda n: profile_dir
        )
        monkeypatch.setattr("hermes_cli.profiles.validate_profile_name", lambda n: None)

        output = tmp_path / "export.tar.gz"
        result = export_profile("testprofile", str(output))

        with tarfile.open(result, "r:gz") as tf:
            archive_names = tf.getnames()
            basenames = {n.rsplit("/", 1)[-1] for n in archive_names}
            archive_name_set = set(archive_names)

        for name in sensitive:
            assert name not in basenames, f"{name} must NOT be in export"
        for name in kept:
            assert name in basenames, f"{name} should be in export"
        # Nested backup excluded, nested ordinary file kept.
        assert "theme.json" in basenames
        assert "README.md" in basenames
        assert "public-ca.pem" in basenames
        assert "public-ca.pem.bak" in basenames
        assert "public-ca.pem~" in basenames
        assert "private.pem" not in basenames
        assert "private.pem.bak" not in basenames
        assert "private.pem.20260101" not in basenames
        assert "private.pem~" not in basenames
        assert "late-private.pem" not in basenames
        assert "google_oauth.json" not in basenames
        assert "google_oauth.json.bak" not in basenames
        assert "bws_cache.json" not in basenames
        assert "bws_cache.json.20260101" not in basenames
        assert "bws_cache.enc.json" not in basenames
        assert "bws_cache.enc.json.bak" not in basenames
        for path in credential_tree_files:
            member = f"testprofile/{path.relative_to(profile_dir).as_posix()}"
            assert member not in archive_name_set, f"{member} must NOT be in export"
        for path in ordinary_tree_files:
            member = f"testprofile/{path.relative_to(profile_dir).as_posix()}"
            assert member in archive_name_set, f"{member} should be in export"
        # The only config.yaml.bak* we added are sensitive — none should survive.
        assert not any(b.startswith("config.yaml.bak") for b in basenames)
        for path in home_sensitive:
            member = f"testprofile/{path.relative_to(profile_dir).as_posix()}"
            assert member not in archive_name_set, f"{member} must NOT be in export"
        for path in home_kept:
            member = f"testprofile/{path.relative_to(profile_dir).as_posix()}"
            assert member in archive_name_set, f"{member} should be in export"

    def test_default_profile_export_excludes_backups_and_secrets(
        self, tmp_path, monkeypatch
    ):
        """Default-profile (~/.hermes) export must drop credentials and the
        config/env backups Hermes writes, while keeping ordinary files."""
        # The default profile IS the hermes home directory itself.
        default_home = tmp_path / ".hermes"
        default_home.mkdir(parents=True)

        sensitive = [
            ".env",
            ".envrc",
            ".env.bak-kiro-20260529115545",
            ".env.bak.example",
            ".env~",
            "auth.json",
            "auth.lock",
            "auth.json~",
            ".anthropic_oauth.json",
            ".anthropic_oauth.json.bak",
            "google_token.json",
            "google_token.json.bak",
            "google_oauth_pending.json",
            "google_oauth_pending.json.backup-20260101",
            "webhook_subscriptions.json",
            "webhook_subscriptions.json.copy",
            "feishu_comment_pairing.json",
            "feishu_comment_pairing.json.bak",
            "bws_cache.enc.json",
            "bws_cache.enc.json.bak",
            "oauth_creds.json",
            "oauth_creds.json.tmp.4242.deadbeef",
            ".claude.json",
            ".git-credentials",
            ".netrc",
            ".netrc.bak",
            ".npmrc",
            ".pgpass",
            ".pypirc",
            "config.yaml.bak.20260526_130938",
            "config.yaml.bak-pre-migrate-xai-20260410-040915",
            "config.yaml.corrupt.20260812-123456.bak",
            "config.yaml.corrupt.20260812-123456.bak.copy",
            "config.yaml.corrupt.20260812-123456.bak.old",
            "config.yaml.corrupt.20260812-123456.bak.backup-before-reset",
            "config.yaml.corrupt.20260812-123456.bak.tmp.4242",
            "config.yaml.corrupt.20260812-123456.bak.20260813",
            "config.yaml.corrupt.20260812-123456.bak~",
            "config.yaml~",
            "private.pem",
        ]
        kept = [
            "config.yaml",
            "SOUL.md",
            ".env.example",
            ".env.sample",
            ".env.template",
            ".env.dist",
        ]
        for name in sensitive:
            (default_home / name).write_text("SENSITIVE\n")
        for name in kept:
            (default_home / name).write_text("ok\n")
        (default_home / "memories").mkdir()
        (default_home / "memories" / "MEMORY.md").write_text("# Memories\n")

        # Root entries are constrained by the current allow-list. Put these
        # fixtures beneath allow-listed trees to prove sensitive filtering also
        # runs at deeper copytree levels.
        nested_root = default_home / "plugins" / "demo"
        nested_sensitive = [
            nested_root / ".anthropic_oauth.json",
            nested_root / ".anthropic_oauth.json.bak",
            nested_root / "google_token.json",
            nested_root / "google_token.json.bak",
            nested_root / "google_oauth_pending.json",
            nested_root / "google_oauth_pending.json.backup-20260101",
            nested_root / "feishu_comment_pairing.json",
            nested_root / "feishu_comment_pairing.json.bak",
            nested_root / "auth" / "google_oauth.json",
            nested_root / "auth" / "google_oauth.json.old",
            nested_root / "cache" / "bws_cache.json",
            nested_root / "cache" / "bws_cache.json.20260101",
            nested_root / "cache" / "bws_cache.enc.json",
            nested_root / "cache" / "bws_cache.enc.json.bak",
            nested_root / "oauth_creds.json",
            nested_root / "oauth_creds.json.tmp.4242.deadbeef",
            nested_root / ".claude.json",
            nested_root / ".env~",
            nested_root / "auth.json~",
            nested_root / "config.yaml~",
            nested_root / "config.yaml.corrupt.20260812-123456.bak",
            nested_root / "config.yaml.corrupt.20260812-123456.bak.copy",
            nested_root
            / "config.yaml.corrupt.20260812-123456.bak.backup-before-reset",
            nested_root / "config.yaml.corrupt.20260812-123456.bak.tmp.4242",
            nested_root / "config.yaml.corrupt.20260812-123456.bak~",
            nested_root / "deploy.ppk",
            nested_root / "deploy.ppk.bak",
            nested_root / "deploy.ppk~",
            nested_root / "private.pem",
            nested_root / "private.pem.bak",
            nested_root / "private.pem.20260101",
            nested_root / "private.pem~",
            nested_root / "late-private.pem",
        ]
        for path in nested_sensitive:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SENSITIVE\n")
        private_key = (
            "-----BEGIN ENCRYPTED PRIVATE KEY-----\nSENSITIVE\n"
            "-----END ENCRYPTED PRIVATE KEY-----\n"
        )
        (nested_root / "private.pem").write_text(private_key)
        (nested_root / "private.pem.bak").write_text(private_key)
        (nested_root / "private.pem.20260101").write_text(private_key)
        (nested_root / "private.pem~").write_text(private_key)
        (nested_root / "late-private.pem").write_text("x" * 9000 + private_key)
        public_cert = "-----BEGIN CERTIFICATE-----\nPUBLIC\n-----END CERTIFICATE-----\n"
        (nested_root / "public-ca.pem").write_text(public_cert)
        (nested_root / "public-ca.pem.bak").write_text(public_cert)
        (nested_root / "public-ca.pem~").write_text(public_cert)
        (nested_root / "README.md").write_text("safe\n")

        # A template dotenv remains exportable when it lives inside an
        # allow-listed root tree.
        template = default_home / "scripts" / "example" / ".env.example"
        template.parent.mkdir(parents=True)
        template.write_text("API_KEY=\n")

        canonical_tree_files = [
            default_home / "mcp-tokens" / "server.json",
            default_home / "mcp-tokens.bak" / "server.json",
            default_home / "pairing" / "device.json",
            default_home / "pairing.backup-20260101" / "device.json",
            default_home / "platforms" / "pairing" / "device.json",
            default_home
            / "platforms"
            / "pairing.backup-20260101"
            / "device.json",
        ]
        ordinary_tree_files = [
            nested_root / "pairing" / "README.md",
            nested_root / "pairing.backup-20260101" / "README.md",
            nested_root / "mcp-tokens" / "README.md",
            nested_root / "mcp-tokens.bak" / "README.md",
        ]
        for path in canonical_tree_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SENSITIVE\n")
        for path in ordinary_tree_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("safe\n")

        monkeypatch.setattr(
            "hermes_cli.profiles._get_default_hermes_home", lambda: default_home
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda n: default_home
        )
        monkeypatch.setattr("hermes_cli.profiles.validate_profile_name", lambda n: None)

        output = tmp_path / "export.tar.gz"
        result = export_profile("default", str(output))

        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()
            basenames = {n.rsplit("/", 1)[-1] for n in names}
            name_set = set(names)

        assert "config.yaml" in basenames
        assert "SOUL.md" in basenames
        assert "MEMORY.md" in basenames
        assert "default/.env.example" in names
        assert "default/.env.sample" in names
        assert "default/.env.template" in names
        assert "default/.env.dist" in names
        assert "README.md" in basenames
        assert "public-ca.pem" in basenames
        assert "public-ca.pem.bak" in basenames
        assert "public-ca.pem~" in basenames

        assert "auth.json" not in basenames
        assert "auth.lock" not in basenames
        assert "auth.json~" not in basenames
        assert ".env" not in basenames
        assert ".env.bak-kiro-20260529115545" not in basenames
        assert ".env.bak.example" not in basenames
        assert ".env~" not in basenames
        assert "config.yaml~" not in basenames
        assert "private.pem" not in basenames
        assert "private.pem.bak" not in basenames
        assert "private.pem.20260101" not in basenames
        assert "private.pem~" not in basenames
        assert "late-private.pem" not in basenames
        assert ".anthropic_oauth.json" not in basenames
        assert ".anthropic_oauth.json.bak" not in basenames
        assert "google_token.json" not in basenames
        assert "google_token.json.bak" not in basenames
        assert "google_oauth_pending.json" not in basenames
        assert "google_oauth_pending.json.backup-20260101" not in basenames
        assert "google_oauth.json" not in basenames
        assert "google_oauth.json.old" not in basenames
        assert "bws_cache.json" not in basenames
        assert "bws_cache.json.20260101" not in basenames
        assert "bws_cache.enc.json" not in basenames
        assert "bws_cache.enc.json.bak" not in basenames
        for path in canonical_tree_files:
            member = f"default/{path.relative_to(default_home).as_posix()}"
            assert member not in name_set, f"{member} must NOT be in export"
        for path in ordinary_tree_files:
            member = f"default/{path.relative_to(default_home).as_posix()}"
            assert member in name_set, f"{member} should be in export"
        assert not any(b.startswith("config.yaml.bak") for b in basenames)

    def test_named_profile_export_excludes_sqlite_sidecars_at_any_depth(
        self, tmp_path, monkeypatch
    ):
        """Only sidecars of header-confirmed databases are classified transient."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "testprofile"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")

        nested = profile_dir / "plugins" / "demo"
        metrics = profile_dir / "telemetry" / "shared_metrics"
        nested.mkdir(parents=True)
        metrics.mkdir(parents=True)
        database_paths = [
            profile_dir / "state.db",
            nested / "kanban.db",
            metrics / "metrics.sqlite3",
        ]
        sidecar_paths = [
            profile_dir / "state.db-shm",
            profile_dir / "state.db-wal",
            profile_dir / "state.db-journal",
            nested / "kanban.db-shm",
            nested / "kanban.db-wal",
            nested / "kanban.db-journal",
            metrics / "metrics.sqlite3-shm",
            metrics / "metrics.sqlite3-wal",
            metrics / "metrics.sqlite3-journal",
        ]
        for path in database_paths:
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE fixture (value TEXT)")
        for path in sidecar_paths:
            path.write_text("sqlite fixture\n")

        lookalike = nested / "notes.db-wal"
        lookalike.write_text("ordinary portable file\n")

        for directory in (profile_dir, nested, metrics):
            contents = [path.name for path in directory.iterdir()]
            ignored = _sqlite_sidecars_in_directory(str(directory), contents)
            for path in sidecar_paths:
                if path.parent == directory:
                    assert path.name in ignored
        assert lookalike.name not in _sqlite_sidecars_in_directory(
            str(nested), [path.name for path in nested.iterdir()]
        )

    def test_default_profile_export_excludes_nested_sqlite_sidecars(
        self, tmp_path, monkeypatch
    ):
        """Header detection also finds extensionless database sidecars."""
        default_home = tmp_path / ".hermes"
        default_home.mkdir(parents=True)
        (default_home / "config.yaml").write_text("model: test\n")

        nested = default_home / "plugins" / "demo"
        nested.mkdir(parents=True)
        database = nested / "kanban.db"
        sidecars = [
            nested / "kanban.db-shm",
            nested / "kanban.db-wal",
            nested / "kanban.db-journal",
        ]
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE fixture (value TEXT)")
        for path in sidecars:
            path.write_text("sqlite fixture\n")

        extensionless = nested / "state"
        with sqlite3.connect(extensionless) as connection:
            connection.execute("CREATE TABLE fixture (value TEXT)")
        extensionless_sidecar = nested / "state-wal"
        extensionless_sidecar.write_text("sqlite fixture\n")

        lookalike = nested / "notes.db-wal"
        lookalike.write_text("ordinary portable file\n")

        ignored = _sqlite_sidecars_in_directory(
            str(nested), [path.name for path in nested.iterdir()]
        )
        assert {path.name for path in sidecars} <= ignored
        assert extensionless_sidecar.name in ignored
        assert lookalike.name not in ignored

    @pytest.mark.parametrize(
        ("profile_name", "relative_db"),
        [
            ("testprofile", "telemetry/shared_metrics/metrics.sqlite3"),
            ("default", "plugins/demo/live.db"),
            ("question", "plugins/demo/question?mark.db"),
            ("hashmark", "plugins/demo/hash#mark.sqlite3"),
            ("legacy", "plugins/demo/legacy.sqlite"),
            ("data", "sessions/state.data"),
            ("extensionless", "sessions/state"),
        ],
    )
    def test_export_snapshots_rows_committed_only_to_active_wal(
        self, tmp_path, monkeypatch, profile_name, relative_db
    ):
        """Exports retain committed WAL rows while omitting live sidecars."""
        if profile_name == "default":
            profile_dir = tmp_path / ".hermes"
            monkeypatch.setattr(
                "hermes_cli.profiles._get_default_hermes_home",
                lambda: profile_dir,
            )
        else:
            profiles_root = tmp_path / "profiles"
            profile_dir = profiles_root / profile_name
            monkeypatch.setattr(
                "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
            )

        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        db_path = profile_dir / relative_db
        db_path.parent.mkdir(parents=True, exist_ok=True)

        writer = sqlite3.connect(db_path)
        try:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            writer.execute("CREATE TABLE items (value TEXT)")
            writer.execute("INSERT INTO items VALUES ('checkpointed')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            writer.execute("INSERT INTO items VALUES ('fresh-in-wal')")
            writer.commit()

            wal_path = Path(f"{db_path}-wal")
            shm_path = Path(f"{db_path}-shm")
            assert wal_path.exists() and wal_path.stat().st_size > 0
            assert shm_path.exists()

            # Prove the second committed row has not reached the main file.
            raw_main = tmp_path / f"raw-{profile_name}.db"
            raw_main.write_bytes(db_path.read_bytes())
            with sqlite3.connect(raw_main) as raw_conn:
                assert raw_conn.execute(
                    "SELECT value FROM items ORDER BY rowid"
                ).fetchall() == [
                    ("checkpointed",)
                ]

            monkeypatch.setattr(
                "hermes_cli.profiles.get_profile_dir", lambda n: profile_dir
            )
            monkeypatch.setattr(
                "hermes_cli.profiles.validate_profile_name", lambda n: None
            )
            result = export_profile(
                profile_name, str(tmp_path / f"{profile_name}.tar.gz")
            )

            for delimiter in ("?", "#"):
                if delimiter in db_path.name:
                    truncated_name = db_path.name.split(delimiter, 1)[0]
                    assert not db_path.with_name(truncated_name).exists()
        finally:
            writer.close()

        member = f"{profile_name}/{relative_db}"
        with tarfile.open(result, "r:gz") as tf:
            names = set(tf.getnames())
            archived_db = tmp_path / f"archived-{profile_name}.db"
            archived_db.write_bytes(tf.extractfile(member).read())

        with sqlite3.connect(archived_db) as archived_conn:
            assert archived_conn.execute(
                "SELECT value FROM items ORDER BY rowid"
            ).fetchall() == [
                ("checkpointed",),
                ("fresh-in-wal",),
            ]
        assert f"{member}-wal" not in names
        assert f"{member}-shm" not in names

    @pytest.mark.parametrize(
        ("relative_name", "content"),
        [
            ("auth.json", '{"opaque": "not-redactor-shaped"}'),
            (
                "config.yaml.corrupt.20260812-123456.bak",
                "api_key: not-redactor-shaped",
            ),
            (
                "config.yaml.corrupt.20260812-123456.bak.copy",
                "api_key: not-redactor-shaped",
            ),
            ("feishu_comment_pairing.json", '{"approved": {"ou_user": {}}}'),
            ("mcp-tokens/server.json", '{"opaque": "not-redactor-shaped"}'),
            (
                "mcp-tokens.bak-pre-migrate/server.json",
                '{"opaque": "not-redactor-shaped"}',
            ),
            (
                "pairing.backup-before-reset/device.json",
                '{"opaque": "not-redactor-shaped"}',
            ),
            ("home/.ssh/id_ed25519", "not-redactor-shaped"),
            ("home/.ssh.bak-before-rotation/id_ed25519", "not-redactor-shaped"),
            ("deploy.ppk", "PuTTY-User-Key-File-3: ssh-rsa\nPrivate-Lines: 1\nopaque"),
            ("private.pem", "-----BEGIN PRIVATE KEY-----\nopaque\n"),
        ],
    )
    def test_extra_files_cannot_reintroduce_sensitive_export_entries(
        self, tmp_path, monkeypatch, relative_name, content
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "extras"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "extras.tar.gz"
        with pytest.raises(ValueError, match="Refusing"):
            export_profile(
                "extras",
                str(output),
                extra_files={relative_name: content},
            )
        assert not output.exists()

    def test_safe_extra_file_remains_exportable(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "extras"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "extras",
            str(tmp_path / "extras.tar.gz"),
            extra_files={"desktop.json": '{"theme": "studio"}\n'},
        )
        with tarfile.open(result, "r:gz") as tf:
            member = tf.extractfile("extras/desktop.json")
            assert member is not None
            assert member.read().decode("utf-8") == '{"theme": "studio"}\n'


class TestExportSecretScrub:

    @pytest.mark.parametrize("profile_name", ["templated", "default"])
    def test_export_redacts_every_allowed_dotenv_template(
        self, tmp_path, monkeypatch, profile_name
    ):
        """Every portable dotenv template is scrubbed in both export paths."""
        template_names = (
            ".env.example",
            ".env.sample",
            ".env.template",
            ".env.dist",
        )
        if profile_name == "default":
            profile_dir = tmp_path / ".hermes"
            monkeypatch.setattr(
                "hermes_cli.profiles._get_default_hermes_home",
                lambda: profile_dir,
            )
        else:
            profiles_root = tmp_path / "profiles"
            profile_dir = profiles_root / profile_name
            monkeypatch.setattr(
                "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
            )

        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        for name in template_names:
            (profile_dir / name).write_text(f"API_KEY={_LEAKED_KEY}\n")

        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda n: profile_dir
        )
        monkeypatch.setattr("hermes_cli.profiles.validate_profile_name", lambda n: None)
        result = export_profile(profile_name, str(tmp_path / f"{profile_name}.tar.gz"))

        with tarfile.open(result, "r:gz") as tf:
            for name in template_names:
                member = f"{profile_name}/{name}"
                archived = tf.extractfile(member).read().decode("utf-8")
                assert _LEAKED_KEY not in archived
                assert _LEAKED_KEY in (profile_dir / name).read_text()

    def test_named_profile_export_redacts_secrets_in_text(self, tmp_path, monkeypatch):
        """Leaked keys in skills / SOUL / memories must not leave the archive."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "scrubme"
        profile_dir.mkdir(parents=True)

        soul = profile_dir / "SOUL.md"
        soul.write_text(f"My key is {_LEAKED_KEY}\n")

        skill_dir = profile_dir / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        skill = skill_dir / "SKILL.md"
        skill.write_text(
            "---\nname: demo\ndescription: Demo.\n---\n"
            f"Use OPENROUTER_API_KEY={_LEAKED_KEY}\n"
        )

        memories = profile_dir / "memories"
        memories.mkdir()
        memory = memories / "MEMORY.md"
        memory.write_text(f"token {_LEAKED_KEY}\n")

        (profile_dir / "config.yaml").write_text("model: gpt-4\n")

        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("scrubme", str(tmp_path / "scrubme.tar.gz"))

        with tarfile.open(result, "r:gz") as tf:
            members = {
                name: tf.extractfile(name).read().decode("utf-8")
                for name in tf.getnames()
                if name.endswith((".md", ".yaml"))
            }

        blob = "\n".join(members.values())
        assert _LEAKED_KEY not in blob
        assert any("SOUL.md" in n for n in members)
        assert any("SKILL.md" in n for n in members)
        assert any("MEMORY.md" in n for n in members)

        # Live profile must keep the original plaintext.
        assert _LEAKED_KEY in soul.read_text()
        assert _LEAKED_KEY in skill.read_text()
        assert _LEAKED_KEY in memory.read_text()

    def test_export_rejects_symlink_without_touching_source(
        self, tmp_path, monkeypatch
    ):
        """Symlinked skill text refuses export without touching its target."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "linkme"
        profile_dir.mkdir(parents=True)

        outside = tmp_path / "outside-skill.md"
        outside.write_text(f"secret {_LEAKED_KEY}\n")

        skill_dir = profile_dir / "skills" / "linked"
        skill_dir.mkdir(parents=True)
        link = skill_dir / "SKILL.md"
        link.symlink_to(outside)

        (profile_dir / "config.yaml").write_text("model: gpt-4\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "linkme.tar.gz"
        with pytest.raises(ValueError, match=r"skills/linked/SKILL\.md"):
            export_profile("linkme", str(output))

        assert not output.exists()
        assert _LEAKED_KEY in outside.read_text()
        assert link.is_symlink()


class TestExportSQLiteSecretInspection:
    @pytest.mark.parametrize(
        "fragment",
        [
            "password=",
            "API_KEY=",
            "postgresql://user:",
            '"apiKey": "',
            "-----BEGIN PRIVATE KEY-----",
        ],
    )
    def test_incomplete_secret_fragments_remain_exportable(
        self, tmp_path, monkeypatch, fragment
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "fragment"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute("INSERT INTO values_table VALUES (?)", (fragment,))
        before = database.read_bytes()
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("fragment", str(tmp_path / "fragment.tar.gz"))
        archived = tmp_path / "fragment-archived.db"
        with tarfile.open(result, "r:gz") as tf:
            archived.write_bytes(tf.extractfile("fragment/state.db").read())

        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT value FROM values_table"
            ).fetchone() == (fragment,)
        assert database.read_bytes() == before

    @pytest.mark.parametrize(
        "value",
        [
            "task-" + "A" * 20,
            "mask-" + "B" * 20,
            "ask-" + "C" * 20,
            "ask-" + "D" * 60,
            "xkey=value",
        ],
    )
    def test_framing_lookalikes_remain_exportable(
        self, tmp_path, monkeypatch, value
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "lookalike"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute("INSERT INTO values_table VALUES (?)", (value,))
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("lookalike", str(tmp_path / "lookalike.tar.gz"))
        archived = tmp_path / "lookalike-archived.db"
        with tarfile.open(result, "r:gz") as tf:
            archived.write_bytes(tf.extractfile("lookalike/state.db").read())
        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT value FROM values_table"
            ).fetchone() == (value,)

    @pytest.mark.parametrize(
        "value",
        [
            "password=actual-value-123",
            "API_KEY=actual-value-123",
            "postgresql://user:actual-value-123@host",
            '"apiKey": "actual-value-123"',
            "-----BEGIN PRIVATE KEY-----\nactual\n-----END PRIVATE KEY-----",
        ],
    )
    def test_complete_secret_evidence_refuses_export(
        self, tmp_path, monkeypatch, value
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "complete"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute("INSERT INTO values_table VALUES (?)", (value,))
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "complete.tar.gz"
        with pytest.raises(ValueError, match=r"secret-shaped content: state\.db"):
            export_profile("complete", str(output))
        assert not output.exists()

    @pytest.mark.parametrize(
        "schema_sql",
        [
            f"CREATE TABLE configured (value TEXT DEFAULT '{_LEAKED_KEY}')",
            f"CREATE INDEX configured_index ON items(value) WHERE value = '{_LEAKED_KEY}'",
            f"CREATE VIEW configured_view AS SELECT '{_LEAKED_KEY}' AS value",
            (
                "CREATE TRIGGER configured_trigger AFTER INSERT ON items BEGIN "
                f"INSERT INTO log VALUES ('{_LEAKED_KEY}'); END"
            ),
        ],
    )
    def test_secret_shaped_sqlite_schema_refuses_export(
        self, tmp_path, monkeypatch, schema_sql
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "schema"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE items (value TEXT)")
            connection.execute("CREATE TABLE log (value TEXT)")
            connection.execute(schema_sql)
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "schema.tar.gz"
        with pytest.raises(ValueError, match=r"secret-shaped content: state\.db"):
            export_profile("schema", str(output))
        assert not output.exists()

    def test_clean_table_index_view_and_trigger_schema_remains_exportable(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "schema"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE items (value TEXT DEFAULT 'safe')")
            connection.execute("CREATE TABLE log (value TEXT)")
            connection.execute("CREATE INDEX items_value ON items(value)")
            connection.execute("CREATE VIEW item_values AS SELECT value FROM items")
            connection.execute(
                "CREATE TRIGGER copy_item AFTER INSERT ON items BEGIN "
                "INSERT INTO log VALUES (NEW.value); END"
            )
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("schema", str(tmp_path / "clean-schema.tar.gz"))
        archived = tmp_path / "clean-schema.db"
        with tarfile.open(result, "r:gz") as tf:
            archived.write_bytes(tf.extractfile("schema/state.db").read())
        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT type FROM sqlite_schema "
                "WHERE name IN ('items', 'items_value', 'item_values', 'copy_item') "
                "ORDER BY type"
            ).fetchall() == [("index",), ("table",), ("trigger",), ("view",)]

    @pytest.mark.parametrize("storage", ["text", "blob"])
    def test_secret_shaped_sqlite_value_refuses_export_without_mutating_source(
        self, tmp_path, monkeypatch, storage
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "database"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        database = profile_dir / "state.db"
        value = _LEAKED_KEY if storage == "text" else sqlite3.Binary(
            _LEAKED_KEY.encode()
        )
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value)")
            connection.execute("INSERT INTO values_table VALUES (?)", (value,))
        before = database.read_bytes()
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / f"{storage}.tar.gz"
        with pytest.raises(ValueError, match=r"secret-shaped content: state\.db"):
            export_profile("database", str(output))

        assert not output.exists()
        assert database.read_bytes() == before
        with sqlite3.connect(database) as connection:
            stored = connection.execute("SELECT value FROM values_table").fetchone()[0]
        expected = _LEAKED_KEY if storage == "text" else _LEAKED_KEY.encode()
        assert stored == expected

    def test_undecodable_sqlite_blob_remains_exportable_and_unchanged(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "database"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        database = profile_dir / "state.db"
        binary = b"\xff\xfe\x00synthetic-binary\x80"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value BLOB)")
            connection.execute(
                "INSERT INTO values_table VALUES (?)", (sqlite3.Binary(binary),)
            )
        before = database.read_bytes()
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("database", str(tmp_path / "blob.tar.gz"))
        archived = tmp_path / "archived.db"
        with tarfile.open(result, "r:gz") as tf:
            archived.write_bytes(tf.extractfile("database/state.db").read())

        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT value FROM values_table"
            ).fetchone()[0] == binary
        assert database.read_bytes() == before

    @pytest.mark.parametrize(
        "blob",
        [
            b"\xff\xfe" + _LEAKED_KEY.encode(),
            _LEAKED_KEY.encode() + b"\xff\xfe",
        ],
    )
    def test_secret_in_arbitrary_sqlite_blob_refuses_export(
        self, tmp_path, monkeypatch, blob
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "database"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value BLOB)")
            connection.execute(
                "INSERT INTO values_table VALUES (?)", (sqlite3.Binary(blob),)
            )
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "arbitrary-blob.tar.gz"
        with pytest.raises(ValueError, match=r"secret-shaped content: state\.db"):
            export_profile("database", str(output))
        assert not output.exists()
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT value FROM values_table"
            ).fetchone()[0] == blob

    def test_clean_utf8_sqlite_blob_remains_exportable(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "database"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        blob = "plain UTF-8 café".encode()
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value BLOB)")
            connection.execute(
                "INSERT INTO values_table VALUES (?)", (sqlite3.Binary(blob),)
            )
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("database", str(tmp_path / "utf8-blob.tar.gz"))
        archived = tmp_path / "utf8-blob.db"
        with tarfile.open(result, "r:gz") as tf:
            archived.write_bytes(tf.extractfile("database/state.db").read())
        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT value FROM values_table"
            ).fetchone()[0] == blob

    @pytest.mark.parametrize("database_name", ["state.data", "state"])
    def test_header_identified_sqlite_secret_refuses_export(
        self, tmp_path, monkeypatch, database_name
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "header-db"
        profile_dir.mkdir(parents=True)
        database = profile_dir / database_name
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute("INSERT INTO values_table VALUES (?)", (_LEAKED_KEY,))
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / f"{database_name}.tar.gz"
        with pytest.raises(ValueError, match=database_name.replace(".", r"\.")):
            export_profile("header-db", str(output))
        assert not output.exists()

    @pytest.mark.parametrize(
        "url",
        [
            "https://x.test/cb?access_token=OpaqueCredential123456",
            "https://x.test/cb?access_token[]=OpaqueCredential123456",
            "https://x.test/cb?access_token%3DOpaqueCredential123456",
        ],
    )
    def test_url_credentials_in_sqlite_refuse_export(
        self, tmp_path, monkeypatch, url
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "url-db"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute("INSERT INTO values_table VALUES (?)", (url,))
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "url-db.tar.gz"
        with pytest.raises(ValueError, match=r"state\.db"):
            export_profile("url-db", str(output))
        assert not output.exists()

    def test_deleted_secret_residue_is_removed_without_changing_database_semantics(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "compact"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("PRAGMA user_version=42")
            connection.execute("PRAGMA application_id=12345")
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute(
                "CREATE TABLE keyed (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID"
            )
            connection.execute(
                "CREATE TABLE auto_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)"
            )
            connection.execute("CREATE TABLE shadowed (rowid TEXT, value TEXT)")
            connection.execute(
                "INSERT INTO values_table(rowid, value) VALUES(10, 'safe-one')"
            )
            connection.execute(
                "INSERT INTO values_table(rowid, value) VALUES(20, ?)",
                (_LEAKED_KEY,),
            )
            connection.execute(
                "INSERT INTO values_table(rowid, value) VALUES(30, 'safe-two')"
            )
            connection.execute("INSERT INTO keyed VALUES ('alpha', 'safe-keyed')")
            connection.execute("INSERT INTO auto_ids(value) VALUES ('safe-auto')")
            connection.execute(
                "INSERT INTO shadowed(_rowid_, rowid, value) "
                "VALUES(17, 'visible-rowid', 'safe-shadowed')"
            )
            connection.commit()
            connection.execute("DELETE FROM values_table WHERE rowid = 20")
            connection.commit()
        before = database.read_bytes()
        assert _LEAKED_KEY.encode() in before
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("compact", str(tmp_path / "compact.tar.gz"))
        archived = tmp_path / "compact-archived.db"
        with tarfile.open(result, "r:gz") as tf:
            archived.write_bytes(_archive_member_bytes(tf, "compact/state.db"))

        assert _LEAKED_KEY.encode() not in archived.read_bytes()
        assert database.read_bytes() == before
        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT rowid, value FROM values_table ORDER BY rowid"
            ).fetchall() == [(10, "safe-one"), (30, "safe-two")]
            assert connection.execute("PRAGMA user_version").fetchone() == (42,)
            assert connection.execute("PRAGMA application_id").fetchone() == (12345,)
            assert connection.execute("SELECT * FROM keyed").fetchall() == [
                ("alpha", "safe-keyed")
            ]
            assert connection.execute("SELECT * FROM auto_ids").fetchall() == [
                (1, "safe-auto")
            ]
            assert connection.execute(
                "SELECT name, seq FROM sqlite_sequence"
            ).fetchall() == [("auto_ids", 1)]
            assert connection.execute(
                "SELECT _rowid_, rowid, value FROM shadowed"
            ).fetchall() == [(17, "visible-rowid", "safe-shadowed")]

    @pytest.mark.parametrize(
        ("column_sql", "source_value", "compacted_value"),
        [
            ("value TEXT", "original", "changed"),
            ("value TEXT COLLATE NOCASE", "original", "ORIGINAL"),
            ("value", 1, 1.0),
        ],
    )
    def test_compaction_semantic_drift_fails_closed(
        self,
        tmp_path,
        column_sql,
        source_value,
        compacted_value,
    ):
        snapshot = tmp_path / "snapshot.db"
        compacted = tmp_path / "compacted.db"
        for path, value in (
            (snapshot, source_value),
            (compacted, compacted_value),
        ):
            with sqlite3.connect(path) as connection:
                connection.execute(f"CREATE TABLE values_table ({column_sql})")
                connection.execute(
                    "INSERT INTO values_table(rowid, value) VALUES(10, ?)",
                    (value,),
                )

        with pytest.raises(RuntimeError, match="rows changed"):
            _verify_compacted_sqlite_semantics(
                snapshot,
                compacted,
                Path("state.db"),
            )

    def test_compaction_handles_table_shadowing_every_rowid_alias(self, tmp_path):
        snapshot = tmp_path / "snapshot.db"
        compacted = tmp_path / "compacted.db"
        with sqlite3.connect(snapshot) as connection:
            connection.execute(
                "CREATE TABLE shadowed (rowid TEXT, _rowid_ TEXT, oid TEXT, value)"
            )
            connection.executemany(
                "INSERT INTO shadowed VALUES (?, ?, ?, ?)",
                [
                    ("r2", "u2", "o2", sqlite3.Binary(b"two")),
                    ("r1", "u1", "o1", 1.0),
                    ("r1", "u1", "o1", 1),
                    ("r1", "u1", "o1", None),
                ],
            )
            connection.commit()
            connection.execute("VACUUM INTO ?", (str(compacted),))

        _verify_compacted_sqlite_semantics(snapshot, compacted, Path("state.db"))

    @pytest.mark.parametrize("storage", ["text", "blob"])
    def test_deleted_sqlite_secret_residue_is_removed_without_mutating_source(
        self, tmp_path, monkeypatch, storage
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "deleted"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        value = _LEAKED_KEY if storage == "text" else sqlite3.Binary(
            _LEAKED_KEY.encode()
        )
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("CREATE TABLE values_table (value)")
            connection.execute("INSERT INTO values_table VALUES (?)", (value,))
            connection.commit()
            connection.execute("DELETE FROM values_table")
            connection.commit()
        before = database.read_bytes()
        assert _LEAKED_KEY.encode() in before
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("deleted", str(tmp_path / f"deleted-{storage}.tar.gz"))
        with tarfile.open(result, "r:gz") as archive:
            archived_bytes = _archive_member_bytes(archive, "deleted/state.db")
        archived = tmp_path / f"deleted-{storage}.db"
        archived.write_bytes(archived_bytes)

        assert _LEAKED_KEY.encode() not in archived_bytes
        assert database.read_bytes() == before
        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM values_table"
            ).fetchone() == (0,)

    @pytest.mark.parametrize("storage", ["text", "blob"])
    def test_deleted_long_prefix_secret_residue_is_removed(
        self, tmp_path, monkeypatch, storage
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "deleted-long"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        token = "sk-" + "A" * 80
        value = token if storage == "text" else sqlite3.Binary(token.encode())
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("CREATE TABLE values_table (value)")
            connection.execute("INSERT INTO values_table VALUES (?)", (value,))
            connection.commit()
            connection.execute("DELETE FROM values_table WHERE rowid = 1")
            connection.commit()
        before = database.read_bytes()
        marker_offset = before.index(token.encode())
        assert before[marker_offset - 2] & 0x80
        assert chr(before[marker_offset - 1]).isalnum()
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "deleted-long", str(tmp_path / f"deleted-long-{storage}.tar.gz")
        )
        with tarfile.open(result, "r:gz") as archive:
            archived_bytes = _archive_member_bytes(archive, "deleted-long/state.db")

        assert token.encode() not in archived_bytes
        assert database.read_bytes() == before

    @pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
    def test_utf16_encoded_sqlite_blob_refuses_export(
        self, tmp_path, monkeypatch, codec
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "utf16-blob"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        blob = "password=DefinitelySecret123".encode(codec)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value BLOB)")
            connection.execute(
                "INSERT INTO values_table VALUES (?)", (sqlite3.Binary(blob),)
            )
        before = database.read_bytes()
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / f"utf16-blob-{codec}.tar.gz"
        with pytest.raises(ValueError, match=r"secret-shaped content: state\.db"):
            export_profile("utf16-blob", str(output))

        assert not output.exists()
        assert database.read_bytes() == before

    @pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
    def test_deleted_utf16_encoded_sqlite_blob_residue_is_removed(
        self, tmp_path, monkeypatch, codec
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "deleted-utf16-blob"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        blob = "password=DefinitelySecret123".encode(codec)
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("CREATE TABLE values_table (value BLOB)")
            connection.execute(
                "INSERT INTO values_table VALUES (?)", (sqlite3.Binary(blob),)
            )
            connection.commit()
            connection.execute("DELETE FROM values_table WHERE rowid = 1")
            connection.commit()
        before = database.read_bytes()
        assert blob in before
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "deleted-utf16-blob",
            str(tmp_path / f"deleted-utf16-blob-{codec}.tar.gz"),
        )
        with tarfile.open(result, "r:gz") as archive:
            archived_bytes = _archive_member_bytes(
                archive, "deleted-utf16-blob/state.db"
            )

        assert blob not in archived_bytes
        assert database.read_bytes() == before

    @pytest.mark.parametrize(
        ("pragma_encoding", "codec"),
        [("UTF-16le", "utf-16-le"), ("UTF-16be", "utf-16-be")],
    )
    def test_deleted_utf16_sqlite_secret_residue_is_removed(
        self, tmp_path, monkeypatch, pragma_encoding, codec
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "deleted-utf16"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute(f"PRAGMA encoding='{pragma_encoding}'")
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute("INSERT INTO values_table VALUES (?)", (_LEAKED_KEY,))
            connection.commit()
            connection.execute("DELETE FROM values_table")
            connection.commit()
        before = database.read_bytes()
        marker = _LEAKED_KEY.encode(codec)
        assert marker in before
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "deleted-utf16", str(tmp_path / f"deleted-{pragma_encoding}.tar.gz")
        )
        with tarfile.open(result, "r:gz") as archive:
            archived_bytes = _archive_member_bytes(
                archive, "deleted-utf16/state.db"
            )

        assert marker not in archived_bytes
        assert database.read_bytes() == before

    def test_deleted_nonsecret_sqlite_content_remains_exportable(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "deleted-clean"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        deleted = "ordinary deleted content"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute("INSERT INTO values_table VALUES (?)", (deleted,))
            connection.commit()
            connection.execute("DELETE FROM values_table")
            connection.commit()
        before = database.read_bytes()
        assert deleted.encode() in before
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "deleted-clean", str(tmp_path / "deleted-clean.tar.gz")
        )
        archived = tmp_path / "deleted-clean-archived.db"
        with tarfile.open(result, "r:gz") as tf:
            archived.write_bytes(tf.extractfile("deleted-clean/state.db").read())
        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM values_table"
            ).fetchone()[0] == 0
        assert database.read_bytes() == before

    @pytest.mark.parametrize(
        "blob",
        [
            b"\xff\xfe" + _LEAKED_KEY.encode(),
            _LEAKED_KEY.encode() + b"\xff\xfe",
        ],
    )
    def test_deleted_secret_in_arbitrary_blob_is_removed(
        self, tmp_path, monkeypatch, blob
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "deleted-blob"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("CREATE TABLE values_table (value BLOB)")
            connection.execute(
                "INSERT INTO values_table VALUES (?)", (sqlite3.Binary(blob),)
            )
            connection.commit()
            connection.execute("DELETE FROM values_table")
            connection.commit()
        before = database.read_bytes()
        assert _LEAKED_KEY.encode() in before
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "deleted-blob", str(tmp_path / "deleted-arbitrary-blob.tar.gz")
        )
        with tarfile.open(result, "r:gz") as archive:
            archived_bytes = _archive_member_bytes(archive, "deleted-blob/state.db")

        assert _LEAKED_KEY.encode() not in archived_bytes
        assert database.read_bytes() == before

    def test_hint_dense_clean_sqlite_database_remains_exportable(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "hint-dense"
        profile_dir.mkdir(parents=True)
        database = profile_dir / "state.db"
        value = "key-safe-" * 4_000
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE values_table (value TEXT)")
            connection.execute("INSERT INTO values_table VALUES (?)", (value,))
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("hint-dense", str(tmp_path / "hint-dense.tar.gz"))
        archived = tmp_path / "hint-dense-archived.db"
        with tarfile.open(result, "r:gz") as tf:
            archived.write_bytes(tf.extractfile("hint-dense/state.db").read())
        with sqlite3.connect(archived) as connection:
            assert connection.execute(
                "SELECT value FROM values_table"
            ).fetchone() == (value,)

    def test_secret_committed_only_to_wal_refuses_export_without_mutation(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "database"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        database = profile_dir / "state.db"
        writer = sqlite3.connect(database)
        try:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            writer.execute("CREATE TABLE values_table (value TEXT)")
            writer.execute("INSERT INTO values_table VALUES ('safe')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            writer.execute("INSERT INTO values_table VALUES (?)", (_LEAKED_KEY,))
            writer.commit()
            wal = Path(f"{database}-wal")
            main_before = database.read_bytes()
            wal_before = wal.read_bytes()
            _patch_named_profile(monkeypatch, profiles_root, profile_dir)

            output = tmp_path / "wal.tar.gz"
            with pytest.raises(ValueError, match=r"state\.db"):
                export_profile("database", str(output))

            assert not output.exists()
            assert database.read_bytes() == main_before
            assert wal.read_bytes() == wal_before
            assert writer.execute(
                "SELECT value FROM values_table ORDER BY rowid"
            ).fetchall() == [("safe",), (_LEAKED_KEY,)]
        finally:
            writer.close()

    def test_malformed_recognized_sqlite_file_fails_closed(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "database"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        database = profile_dir / "broken.db"
        database.write_bytes(b"SQLite format 3\x00" + b"broken")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "broken.tar.gz"
        with pytest.raises(RuntimeError, match=r"broken\.db"):
            export_profile("database", str(output))
        assert not output.exists()
        assert database.read_bytes() == b"SQLite format 3\x00" + b"broken"


class TestExtensionIndependentExportScrub:
    @pytest.mark.parametrize("name", ["payload.data", "events.log", "notes"])
    def test_utf8_secret_is_redacted_independent_of_suffix(
        self, tmp_path, monkeypatch, name
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "text"
        profile_dir.mkdir(parents=True)
        source = profile_dir / name
        source.write_text(f"before {_LEAKED_KEY} after\n")
        source.chmod(0o640)
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("text", str(tmp_path / f"{name}.tar.gz"))
        with tarfile.open(result, "r:gz") as tf:
            member = tf.getmember(f"text/{name}")
            archived = tf.extractfile(member).read().decode()

        assert _LEAKED_KEY not in archived
        assert "before " in archived and " after" in archived
        assert member.mode & 0o777 == 0o640
        assert _LEAKED_KEY in source.read_text()

    def test_extra_files_are_redacted_independent_of_suffix(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "extras"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)
        extras = {
            name: f"before {_LEAKED_KEY} after\n"
            for name in ("payload.data", "events.log", "notes")
        }

        result = export_profile(
            "extras", str(tmp_path / "extras.tar.gz"), extra_files=extras
        )
        with tarfile.open(result, "r:gz") as tf:
            for name in extras:
                archived = tf.extractfile(f"extras/{name}").read().decode()
                assert _LEAKED_KEY not in archived
                assert "before " in archived and " after" in archived

    def test_url_credentials_are_scrubbed_from_copied_text_and_extras(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "urls"
        profile_dir.mkdir(parents=True)
        credential = "OpaqueCredential123456"
        query_url = f"https://example.test/callback?access_token={credential}&state=ok"
        userinfo_url = f"https://user:{credential}@example.test/path"
        source = profile_dir / "notes"
        source.write_text(f"{query_url}\n{userinfo_url}\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "urls",
            str(tmp_path / "urls.tar.gz"),
            extra_files={"desktop.json": f'{{"url": "{query_url}"}}'},
        )
        with tarfile.open(result, "r:gz") as tf:
            copied = _archive_member_bytes(tf, "urls/notes").decode()
            extra = _archive_member_bytes(tf, "urls/desktop.json").decode()

        assert credential not in copied
        assert credential not in extra
        assert credential in source.read_text()

    @pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
    def test_encoded_secret_file_fails_closed(self, tmp_path, monkeypatch, codec):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "encoded"
        profile_dir.mkdir(parents=True)
        source = profile_dir / "notes.data"
        source.write_bytes(f"password={_LEAKED_KEY}".encode(codec))
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / f"encoded-{codec}.tar.gz"
        with pytest.raises(ValueError, match=r"notes\.data"):
            export_profile("encoded", str(output))
        assert not output.exists()

    def test_safe_utf8_and_binary_files_remain_byte_identical(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "content"
        profile_dir.mkdir(parents=True)
        safe = profile_dir / "safe.data"
        binary = profile_dir / "binary.log"
        safe_bytes = "plain unicode text: café\r\n".encode()
        binary_bytes = b"\x00\xffsynthetic\r\n"
        safe.write_bytes(safe_bytes)
        binary.write_bytes(binary_bytes)
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("content", str(tmp_path / "content.tar.gz"))
        with tarfile.open(result, "r:gz") as tf:
            assert tf.extractfile("content/safe.data").read() == safe_bytes
            assert tf.extractfile("content/binary.log").read() == binary_bytes
        assert safe.read_bytes() == safe_bytes
        assert binary.read_bytes() == binary_bytes

    def test_secret_late_in_large_utf8_file_is_redacted(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "large"
        profile_dir.mkdir(parents=True)
        source = profile_dir / "events.log"
        source.write_text("safe\n" * 400_000 + f"key={_LEAKED_KEY}\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("large", str(tmp_path / "large.tar.gz"))
        with tarfile.open(result, "r:gz") as tf:
            archived = tf.extractfile("large/events.log").read().decode()
        assert _LEAKED_KEY not in archived
        assert archived.startswith("safe\n")
        assert _LEAKED_KEY in source.read_text()

    def test_secret_crossing_stream_chunk_is_redacted(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "boundary-text"
        profile_dir.mkdir(parents=True)
        source = profile_dir / "events.log"
        source.write_text("A" * (64 * 1024 - 4) + f" API_KEY={_LEAKED_KEY}\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "boundary-text", str(tmp_path / "boundary-text.tar.gz")
        )
        with tarfile.open(result, "r:gz") as tf:
            archived = _archive_member_bytes(tf, "boundary-text/events.log")

        assert _LEAKED_KEY.encode() not in archived
        assert _LEAKED_KEY in source.read_text()

    def test_control_split_secret_across_records_fails_closed(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "control-split"
        profile_dir.mkdir(parents=True)
        source = profile_dir / "events.log"
        payload = "sk-AAAA\n" + "B" * 20 + "\n"
        source.write_text(payload)
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "control-split.tar.gz"
        with pytest.raises(ValueError, match=r"events\.log"):
            export_profile("control-split", str(output))
        assert not output.exists()
        assert source.read_text() == payload

    def test_large_safe_binary_streams_without_mutation(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "large-binary"
        profile_dir.mkdir(parents=True)
        source = profile_dir / "payload.bin"
        binary = (b"\x00\xffsafe-binary-block" * 128 * 1024)[: 2 * 1024 * 1024]
        source.write_bytes(binary)
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile(
            "large-binary", str(tmp_path / "large-binary.tar.gz")
        )
        with tarfile.open(result, "r:gz") as tf:
            archived = _archive_member_bytes(tf, "large-binary/payload.bin")

        assert archived == binary
        assert source.read_bytes() == binary


class TestAtomicArchivePublication:
    def _profile(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "atomic"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: test\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

    def test_output_symlink_is_replaced_without_touching_target(
        self, tmp_path, monkeypatch
    ):
        self._profile(tmp_path, monkeypatch)
        external = tmp_path / "external.txt"
        external.write_text("do not alter")
        output = tmp_path / "atomic.tar.gz"
        try:
            output.symlink_to(external)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable on this platform: {exc}")

        result = export_profile("atomic", str(output))

        assert external.read_text() == "do not alter"
        assert result == output
        assert result.is_file() and not result.is_symlink()
        with tarfile.open(result, "r:gz") as tf:
            assert "atomic/config.yaml" in tf.getnames()

    def test_existing_regular_output_is_atomically_replaced(
        self, tmp_path, monkeypatch
    ):
        self._profile(tmp_path, monkeypatch)
        output = tmp_path / "atomic.tar.gz"
        output.write_bytes(b"old archive")

        result = export_profile("atomic", str(output))

        assert result == output and result.is_file()
        with tarfile.open(result, "r:gz") as tf:
            assert "atomic/config.yaml" in tf.getnames()

    def test_directory_output_fails_clearly(self, tmp_path, monkeypatch):
        self._profile(tmp_path, monkeypatch)
        output = tmp_path / "atomic.tar.gz"
        output.mkdir()

        with pytest.raises(IsADirectoryError, match="output is a directory"):
            export_profile("atomic", str(output))
        assert output.is_dir()

    def test_archive_creation_failure_is_not_published(
        self, tmp_path, monkeypatch
    ):
        self._profile(tmp_path, monkeypatch)
        output = tmp_path / "atomic.tar.gz"

        def fail_add(*args, **kwargs):
            raise RuntimeError("synthetic archive failure")

        monkeypatch.setattr(tarfile.TarFile, "add", fail_add)
        with pytest.raises(RuntimeError, match="synthetic archive failure"):
            export_profile("atomic", str(output))

        assert not output.exists()
        assert not list(tmp_path.glob(".atomic.tar.gz.*.tmp"))


class TestProfileExportSymlinkPolicy:
    @pytest.mark.parametrize("kind", ["absolute", "relative", "dangling"])
    def test_profile_symlink_refuses_export(self, tmp_path, monkeypatch, kind):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "links"
        profile_dir.mkdir(parents=True)
        link = profile_dir / "workspace" / "link"
        link.parent.mkdir()
        target = profile_dir / "workspace" / "target.txt"
        if kind == "absolute":
            target = tmp_path / "absolute-target.txt"
            target.write_text("synthetic")
            link.symlink_to(target)
        elif kind == "relative":
            target.write_text("synthetic")
            link.symlink_to("target.txt")
        else:
            link.symlink_to("missing.txt")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / f"{kind}.tar.gz"
        with pytest.raises(ValueError) as exc_info:
            export_profile("links", str(output))

        assert "workspace/link" in str(exc_info.value)
        assert str(target) not in str(exc_info.value)
        assert not output.exists()

    def test_symlink_in_excluded_credential_tree_does_not_block_export(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "links"
        profile_dir.mkdir(parents=True)
        outside = tmp_path / "outside-token-store"
        outside.write_text("DO_NOT_ARCHIVE_SYMLINK_TARGET")
        link = profile_dir / "mcp-tokens" / "server"
        link.parent.mkdir()
        link.symlink_to(outside)
        (profile_dir / "config.yaml").write_text("model: test\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("links", str(tmp_path / "filtered-link.tar.gz"))
        with tarfile.open(result, "r:gz") as tf:
            names = set(tf.getnames())
            content = b"\n".join(
                tf.extractfile(member).read()
                for member in tf.getmembers()
                if member.isfile()
            )

        assert not any("mcp-tokens" in name for name in names)
        assert b"DO_NOT_ARCHIVE_SYMLINK_TARGET" not in content
        assert link.is_symlink()

    def test_profile_root_symlink_refuses_export(self, tmp_path, monkeypatch):
        profiles_root = tmp_path / "profiles"
        target = tmp_path / "real-profile"
        target.mkdir()
        (target / "config.yaml").write_text("model: test\n")
        profile_dir = profiles_root / "linked-root"
        profiles_root.mkdir()
        profile_dir.symlink_to(target, target_is_directory=True)
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "linked-root.tar.gz"
        with pytest.raises(ValueError, match=r"profile export symlink: \.") as exc_info:
            export_profile("linked-root", str(output))

        assert str(target) not in str(exc_info.value)
        assert not output.exists()
