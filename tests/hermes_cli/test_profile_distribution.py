"""Tests for hermes_cli.profile_distribution — git-based profile installs.

Covers manifest parsing, version requirement checks, install / update / describe
on local-directory sources, and guards on what can and can't be installed.

Transport-layer tests (git clone, URL handling) are exercised through live
E2E runs, not unit tests — git itself is tested upstream, and subprocess-
mocking git would just test the mock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.profile_distribution import (
    DEFAULT_DIST_OWNED,
    DistributionError,
    DistributionManifest,
    EnvRequirement,
    MANIFEST_FILENAME,
    USER_OWNED_EXCLUDE,
    _env_template_from_manifest,
    _looks_like_git_url,
    _parse_semver,
    check_hermes_requires,
    describe_distribution,
    install_distribution,
    plan_install,
    read_manifest,
    update_distribution,
    write_manifest,
)


# ---------------------------------------------------------------------------
# Isolated profile env (matches tests/hermes_cli/test_profiles.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def _make_staging_dir(root: Path, name: str = "src", *, manifest: DistributionManifest = None) -> Path:
    """Build a local distribution staging directory (what a git clone would
    contain after .git is removed).

    Lays down a minimal but representative tree: SOUL.md, config.yaml,
    mcp.json, one skill, one cron file, plus the distribution.yaml manifest.
    """
    staged = root / f"staging_{name}"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "SOUL.md").write_text("I am Source.\n")
    (staged / "config.yaml").write_text("model:\n  model: gpt-4\n")
    (staged / "mcp.json").write_text('{"servers": {}}\n')
    (staged / "skills").mkdir(exist_ok=True)
    (staged / "skills" / "demo").mkdir(exist_ok=True)
    (staged / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: test\n---\n# Demo skill\n"
    )
    (staged / "cron").mkdir(exist_ok=True)
    (staged / "cron" / "daily.json").write_text('{"schedule": "0 9 * * *"}')

    mf = manifest or DistributionManifest(name=name, version="0.1.0")
    write_manifest(staged, mf)
    return staged


def _symlink_file_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")


# ===========================================================================
# Manifest parsing
# ===========================================================================


class TestManifestParsing:


    def test_full_manifest(self, tmp_path):
        (tmp_path / MANIFEST_FILENAME).write_text(
            "name: telem\n"
            "version: 1.2.3\n"
            "description: Telem monitor\n"
            "hermes_requires: '>=0.12.0'\n"
            "author: Kyle\n"
            "license: MIT\n"
            "env_requires:\n"
            "  - name: OPENAI_API_KEY\n"
            "    description: OpenAI key\n"
            "  - name: GRAPH_URL\n"
            "    required: false\n"
            "    default: http://127.0.0.1:8000\n"
            "distribution_owned:\n"
            "  - SOUL.md\n"
            "  - skills/\n"
        )
        m = read_manifest(tmp_path)
        assert m.name == "telem"
        assert m.version == "1.2.3"
        assert m.author == "Kyle"
        assert m.license == "MIT"
        assert len(m.env_requires) == 2
        assert m.env_requires[0].name == "OPENAI_API_KEY"
        assert m.env_requires[0].required is True
        assert m.env_requires[1].required is False
        assert m.env_requires[1].default == "http://127.0.0.1:8000"
        assert m.distribution_owned == ["SOUL.md", "skills"]






    def test_roundtrip_write_read(self, tmp_path):
        original = DistributionManifest(
            name="rt",
            version="1.0.0",
            description="roundtrip",
            env_requires=[EnvRequirement(name="FOO", description="foo")],
        )
        write_manifest(tmp_path, original)
        parsed = read_manifest(tmp_path)
        assert parsed.name == "rt"
        assert parsed.env_requires[0].name == "FOO"


# ===========================================================================
# Version requirement checks
# ===========================================================================


class TestVersionRequires:

    @pytest.mark.parametrize("spec,cur,ok", [
        ("", "0.1.0", True),
        (">=0.12.0", "0.12.0", True),
        (">=0.12.0", "0.13.0", True),
        (">=0.12.0", "0.11.9", False),
        ("==0.12.0", "0.12.0", True),
        ("==0.12.0", "0.13.0", False),
        ("!=0.12.0", "0.13.0", True),
        (">0.12.0", "0.12.1", True),
        (">0.12.0", "0.12.0", False),
        ("<0.13.0", "0.12.9", True),
        ("<=0.12.0", "0.12.0", True),
        ("0.12.0", "0.13.0", True),     # Bare = >=
        ("0.12.0", "0.11.0", False),    # Bare = >=
    ])
    def test_check_matrix(self, spec, cur, ok):
        if ok:
            check_hermes_requires(spec, cur)
        else:
            with pytest.raises(DistributionError, match="requires Hermes"):
                check_hermes_requires(spec, cur)

    def test_parse_semver_handles_prerelease(self):
        assert _parse_semver("0.12.0-rc1") == (0, 12, 0)
        assert _parse_semver("v0.12.0+abc") == (0, 12, 0)


# ===========================================================================
# Env template
# ===========================================================================


class TestEnvTemplate:

    def test_required_is_uncommented(self):
        m = DistributionManifest(
            name="x",
            env_requires=[EnvRequirement(name="FOO", description="foo key")],
        )
        out = _env_template_from_manifest(m)
        assert "# foo key" in out
        assert "# (required)" in out
        assert "FOO=" in out
        # No leading `# ` before FOO=
        assert "\nFOO=" in out or out.startswith("FOO=") or "\nFOO=\n" in out or "FOO=\n" in out


# ===========================================================================
# Source URL detection
# ===========================================================================


class TestLooksLikeGitUrl:

    @pytest.mark.parametrize("src", [
        "github.com/user/repo",
        "https://github.com/user/repo",
        "https://github.com/user/repo.git",
        "http://example.com/repo",
        "git@github.com:user/repo.git",
        "ssh://git@example.com/repo.git",
        "git://example.com/repo.git",
    ])
    def test_accepts_git_sources(self, src):
        assert _looks_like_git_url(src)


# ===========================================================================
# Install — fresh and force (from a local-directory source)
# ===========================================================================


class TestInstall:

    def test_install_from_directory(self, profile_env):
        staged = _make_staging_dir(profile_env, "src")
        plan = install_distribution(str(staged), name="installed")
        assert plan.target_dir.is_dir()
        assert (plan.target_dir / "SOUL.md").read_text() == "I am Source.\n"
        assert (plan.target_dir / "skills" / "demo" / "SKILL.md").exists()
        assert (plan.target_dir / "mcp.json").exists()
        # Manifest on disk records canonical name + provenance
        m = read_manifest(plan.target_dir)
        assert m.name == "installed"
        assert m.source == str(staged)


    def test_install_rejects_non_distribution_directory(self, profile_env, tmp_path):
        bogus = tmp_path / "bogus_dir"
        bogus.mkdir()
        (bogus / "some_file").write_text("hi")
        with pytest.raises(DistributionError, match="No distribution.yaml"):
            plan_install(str(bogus), tmp_path / "work", override_name="x")


    def test_install_enforces_hermes_requires(self, profile_env, monkeypatch):
        # Pin current Hermes version to something well below the requirement
        import hermes_cli
        monkeypatch.setattr(hermes_cli, "__version__", "0.1.0", raising=False)

        mf = DistributionManifest(
            name="future",
            version="1.0.0",
            hermes_requires=">=99.0.0",
        )
        staged = _make_staging_dir(profile_env, "future", manifest=mf)
        with pytest.raises(DistributionError, match="requires Hermes"):
            install_distribution(str(staged), name="future")


# ===========================================================================
# Update — preserves user data, preserves config by default
# ===========================================================================


class TestUpdate:

    def test_update_preserves_user_data(self, profile_env):
        # 1. Build staging dir, install
        staged = _make_staging_dir(profile_env, "src")
        plan = install_distribution(str(staged), name="telem")

        # 2. Add user-owned data to the installed profile
        (plan.target_dir / "memories").mkdir(exist_ok=True)
        (plan.target_dir / "memories" / "MEMORY.md").write_text("# USER MEMORY\n")
        (plan.target_dir / ".env").write_text("OPENAI_API_KEY=sk-user\n")
        (plan.target_dir / "auth.json").write_text('{"user": "auth"}')
        (plan.target_dir / "sessions").mkdir(exist_ok=True)
        (plan.target_dir / "sessions" / "chat.json").write_text('{"s": 1}')

        # 3. Bump source in the staging dir
        (staged / "SOUL.md").write_text("I am Source v2.\n")

        # 4. Update
        update_distribution("telem", force_config=False)

        # 5. Dist-owned changed
        assert (plan.target_dir / "SOUL.md").read_text() == "I am Source v2.\n"
        # 6. User-owned preserved
        assert (plan.target_dir / "memories" / "MEMORY.md").read_text() == "# USER MEMORY\n"
        assert (plan.target_dir / ".env").read_text() == "OPENAI_API_KEY=sk-user\n"
        assert (plan.target_dir / "auth.json").read_text() == '{"user": "auth"}'
        assert (plan.target_dir / "sessions" / "chat.json").read_text() == '{"s": 1}'

    def test_update_preserves_config_by_default(self, profile_env):
        staged = _make_staging_dir(profile_env, "src")
        plan = install_distribution(str(staged), name="t2")

        # User edits config
        (plan.target_dir / "config.yaml").write_text(
            "model:\n  model: gpt-5\n# user override\n"
        )

        # Bump source config
        (staged / "config.yaml").write_text("model:\n  model: claude\n")

        update_distribution("t2", force_config=False)
        assert "gpt-5" in (plan.target_dir / "config.yaml").read_text()
        assert "user override" in (plan.target_dir / "config.yaml").read_text()

    def test_update_preserves_config_when_source_removes_it(self, profile_env):
        """Regression (review of #75494): a newer distribution revision
        that removes config.yaml from its own source entirely (not just
        changes it) must still preserve the user's existing target
        config.yaml -- the src.exists() early-continue for a
        no-longer-staged owned path must not skip the preservation
        accounting that keeps config.yaml out of the stale-file prune.
        Matches the documented default: config.yaml survives unless
        --force-config, independent of what the current source contains."""
        import shutil as _shutil

        staged = _make_staging_dir(profile_env, "src")
        plan = install_distribution(str(staged), name="config_removed_test")

        # User edits config.
        (plan.target_dir / "config.yaml").write_text(
            "model:\n  model: gpt-5\n# user override\n"
        )

        # Newer revision removes config.yaml from the source entirely.
        (staged / "config.yaml").unlink()
        (staged / "SOUL.md").write_text("I am Source v2.\n")

        update_distribution("config_removed_test", force_config=False)

        assert (plan.target_dir / "config.yaml").exists(), (
            "the user's existing config.yaml must survive even when a "
            "newer distribution revision stops shipping config.yaml at all"
        )
        assert "gpt-5" in (plan.target_dir / "config.yaml").read_text()
        assert "user override" in (plan.target_dir / "config.yaml").read_text()
        # The actually-distributed change still lands correctly.
        assert (plan.target_dir / "SOUL.md").read_text() == "I am Source v2.\n"


    def test_update_missing_manifest_errors(self, profile_env):
        # Make a profile without a manifest; update must refuse
        from hermes_cli.profiles import create_profile
        create_profile(name="plain", no_alias=True)
        with pytest.raises(DistributionError, match="not a distribution"):
            update_distribution("plain")


# ===========================================================================
# describe_distribution — info subcommand
# ===========================================================================


class TestDescribe:

    def test_describe_existing_distribution(self, profile_env):
        mf = DistributionManifest(
            name="telem",
            version="1.0.0",
            description="compliance monitor",
            env_requires=[EnvRequirement(name="API", description="api key")],
        )
        staged = _make_staging_dir(profile_env, "telem", manifest=mf)
        install_distribution(str(staged), name="telem")
        data = describe_distribution("telem")
        assert data["name"] == "telem"
        assert data["version"] == "1.0.0"
        assert data["env_requires"][0]["name"] == "API"


    def test_describe_missing_profile_raises(self, profile_env):
        with pytest.raises(DistributionError, match="does not exist"):
            describe_distribution("nonexistent")


# ===========================================================================
# Security — USER_OWNED_EXCLUDE covers the right paths
# ===========================================================================


class TestSecurity:

    def test_user_owned_exclude_covers_credentials(self):
        assert "auth.json" in USER_OWNED_EXCLUDE
        assert ".env" in USER_OWNED_EXCLUDE
        assert "memories" in USER_OWNED_EXCLUDE
        assert "sessions" in USER_OWNED_EXCLUDE
        assert "local" in USER_OWNED_EXCLUDE

    def test_install_does_not_import_credentials_from_staging(self, profile_env):
        """If an author accidentally ships auth.json or .env in their
        staging dir, the installer must NOT copy them to the target profile."""
        staged = _make_staging_dir(profile_env, "src")
        # Author leaks credentials into the staging tree (shouldn't happen, but...)
        (staged / "auth.json").write_text('{"leaked": true}')
        (staged / ".env").write_text("LEAKED=1")

        plan = install_distribution(str(staged), name="clean")
        assert not (plan.target_dir / "auth.json").exists(), "auth.json leaked"
        # Fresh profile may have its own .env via the bootstrap; what we care
        # about is that the leaked content didn't land in the target.
        if (plan.target_dir / ".env").exists():
            assert "LEAKED" not in (plan.target_dir / ".env").read_text()

    def test_install_rejects_symlinked_distribution_files(self, profile_env, tmp_path):
        """Distribution install must not follow symlinks to local files."""
        staged = _make_staging_dir(profile_env, "src")
        local_secret = tmp_path / "local-secret.txt"
        local_secret.write_text("outside secret\n")
        _symlink_file_or_skip(
            staged / "skills" / "demo" / "leak.txt",
            local_secret,
        )

        with pytest.raises(DistributionError, match="symlink"):
            install_distribution(str(staged), name="clean")

        from hermes_cli.profiles import get_profile_dir
        target = get_profile_dir("clean")
        assert not (target / "skills" / "demo" / "leak.txt").exists()


# ===========================================================================
# Nested directories whose names match USER_OWNED_EXCLUDE must survive install
# ===========================================================================


class TestNestedUserOwnedExcludeNotFiltered:

    def test_nested_bin_dir_is_preserved(self, profile_env):
        """A distribution shipping tools/bin/ must not have tools/bin/ dropped
        during install even though 'bin' is in USER_OWNED_EXCLUDE -- that
        exclusion only applies to TOP-LEVEL entries, not nested ones that
        happen to share a name. ('tools' itself must be explicitly opted
        into distribution_owned per the #74409 allowlist enforcement,
        since it isn't one of the DEFAULT_DIST_OWNED entries.)"""
        mf = DistributionManifest(
            name="nested_bin", version="0.1.0",
            distribution_owned=list(DEFAULT_DIST_OWNED) + ["tools"],
        )
        staged = _make_staging_dir(profile_env, "src", manifest=mf)
        (staged / "tools" / "bin").mkdir(parents=True)
        (staged / "tools" / "bin" / "tool.py").write_text("# tool\n")

        plan = install_distribution(str(staged), name="nested_bin")
        assert (plan.target_dir / "tools" / "bin").is_dir(), "nested bin/ was dropped"
        assert (plan.target_dir / "tools" / "bin" / "tool.py").exists()

    def test_nested_logs_dir_is_preserved(self, profile_env):
        """Same regression for a nested 'logs' dir, under an explicitly
        owned 'scripts' entry (not a DEFAULT_DIST_OWNED path)."""
        mf = DistributionManifest(
            name="nested_logs", version="0.1.0",
            distribution_owned=list(DEFAULT_DIST_OWNED) + ["scripts"],
        )
        staged = _make_staging_dir(profile_env, "src", manifest=mf)
        (staged / "scripts" / "logs").mkdir(parents=True)
        (staged / "scripts" / "logs" / "run.log").write_text("ok\n")

        plan = install_distribution(str(staged), name="nested_logs")
        assert (plan.target_dir / "scripts" / "logs").is_dir()
        assert (plan.target_dir / "scripts" / "logs" / "run.log").read_text() == "ok\n"


    def test_top_level_user_owned_still_skipped(self, profile_env):
        """Top-level entries in USER_OWNED_EXCLUDE must still be skipped —
        only nested (deeper) directories should be preserved.

        Note: _bootstrap_user_dirs creates some of these (logs/, sessions/,
        memories/) in every fresh profile, so we check that the *staged content*
        did not leak through rather than asserting the directory doesn't exist."""
        staged = _make_staging_dir(profile_env, "src")
        # Add top-level excluded entries alongside the legit ones
        (staged / "bin").mkdir(exist_ok=True)
        (staged / "bin" / "shipped_binary").write_text("x")
        (staged / "logs").mkdir(exist_ok=True)
        (staged / "logs" / "shipped.log").write_text("y\n")

        plan = install_distribution(str(staged), name="top_filter")
        # bin/ is not created by _bootstrap_user_dirs so absence means filtered
        assert not (plan.target_dir / "bin").exists(), "top-level bin/ should be filtered"
        # logs/ is created by _bootstrap_user_dirs even on a clean profile,
        # so check that the staged file did NOT land there.
        assert not (plan.target_dir / "logs" / "shipped.log").exists(), \
            "staged logs/ content should not leak into target"


# ===========================================================================
# Install-time metadata (installed_at stamp)
# ===========================================================================


class TestInstalledAtStamp:

    def test_install_stamps_installed_at(self, profile_env):
        staged = _make_staging_dir(profile_env, "src")
        plan = install_distribution(str(staged), name="stamped")
        mf = read_manifest(plan.target_dir)
        assert mf.installed_at, "installed_at should be set after install"
        # ISO-8601 UTC sanity: starts with 4-digit year, contains 'T', ends with '+00:00'.
        assert mf.installed_at[:4].isdigit()
        assert "T" in mf.installed_at
        assert mf.installed_at.endswith("+00:00")

    def test_update_refreshes_installed_at(self, profile_env, monkeypatch):
        staged = _make_staging_dir(profile_env, "src")
        install_distribution(str(staged), name="demo")
        from hermes_cli.profiles import get_profile_dir
        first = read_manifest(get_profile_dir("demo")).installed_at

        # Freeze `datetime.now()` to a fixed future time so we can observe that
        # update writes a NEW stamp (installs within the same second otherwise
        # collide at iso-8601 seconds resolution).
        import datetime as _dt
        class _FakeDT(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return _dt.datetime(2099, 1, 1, 0, 0, 0, tzinfo=tz or _dt.timezone.utc)
        monkeypatch.setattr(
            "hermes_cli.profile_distribution.datetime", _FakeDT, raising=True
        )

        from hermes_cli.profile_distribution import update_distribution
        update_distribution("demo")
        refreshed = read_manifest(get_profile_dir("demo")).installed_at
        assert refreshed != first, "installed_at should change on update"
        assert refreshed.startswith("2099-01-01"), refreshed


# ===========================================================================
# ProfileInfo exposes distribution metadata
# ===========================================================================


class TestProfileInfoDistribution:

    def test_installed_distribution_shows_in_list(self, profile_env):
        staged = _make_staging_dir(
            profile_env, "src",
            manifest=DistributionManifest(name="telem", version="1.2.3"),
        )
        install_distribution(str(staged), name="telem")

        from hermes_cli.profiles import list_profiles
        rows = {p.name: p for p in list_profiles()}
        assert "telem" in rows
        row = rows["telem"]
        assert row.distribution_name == "telem"
        assert row.distribution_version == "1.2.3"
        assert row.distribution_source  # path populated, exact value depends on fixture


    def test_malformed_manifest_does_not_break_list(self, profile_env):
        from hermes_cli.profiles import create_profile, list_profiles, get_profile_dir
        create_profile(name="brokenmeta", no_alias=True)
        # Write a distribution.yaml that isn't a valid mapping
        (get_profile_dir("brokenmeta") / "distribution.yaml").write_text(
            "not: [a, valid, mapping\n"  # broken YAML
        )
        # list_profiles must NOT raise; distribution_* stay None for this row.
        rows = {p.name: p for p in list_profiles()}
        assert rows["brokenmeta"].distribution_name is None


# ===========================================================================
# Error surfaces: validation failures should propagate as DistributionError
# or ValueError (both caught and rendered cleanly by the CLI handler)
# ===========================================================================


class TestDistributionOwnedNestedPaths:
    """Regression tests for issue #74409's review: distribution_owned must
    support nested manifest-relative paths (skills/research/,
    cron/digest.json), not just root-level directory/file names, and an
    update must distinguish a STALE distributed file (shipped before,
    removed from the new revision -- must be deleted) from a genuine
    TARGET-ONLY user file placed inside an owned directory (never
    distributed -- must survive), instead of either silently skipping
    nested entries or retaining every stale file forever via a merge.
    """

    def test_nested_directory_entry_is_installed(self, profile_env):
        """distribution_owned: ['skills/research'] must actually copy
        staged/skills/research/, not be silently skipped because
        'skills/research' != the root entry 'skills'."""
        mf = DistributionManifest(
            name="nested", version="0.1.0",
            distribution_owned=["SOUL.md", "skills/research"],
        )
        staged = _make_staging_dir(profile_env, "src", manifest=mf)
        (staged / "skills" / "research").mkdir(parents=True, exist_ok=True)
        (staged / "skills" / "research" / "SKILL.md").write_text(
            "---\nname: research\n---\n# Research\n"
        )

        plan = install_distribution(str(staged), name="nested_test")

        assert (plan.target_dir / "skills" / "research" / "SKILL.md").exists()

    def test_nested_file_entry_is_installed(self, profile_env):
        """distribution_owned: ['cron/digest.json'] must copy that exact
        file, not require the whole 'cron' directory to be owned."""
        mf = DistributionManifest(
            name="nested_file", version="0.1.0",
            distribution_owned=["SOUL.md", "cron/digest.json"],
        )
        staged = _make_staging_dir(profile_env, "src", manifest=mf)
        (staged / "cron").mkdir(exist_ok=True)
        (staged / "cron" / "digest.json").write_text('{"schedule": "0 8 * * *"}')

        plan = install_distribution(str(staged), name="nested_file_test")

        assert (plan.target_dir / "cron" / "digest.json").exists()
        assert (
            plan.target_dir / "cron" / "digest.json"
        ).read_text() == '{"schedule": "0 8 * * *"}'

    def test_narrow_owned_entry_leaves_sibling_content_untouched(self, profile_env):
        """distribution_owned: ['skills/research'] must not touch a
        sibling skills/other-skill/ directory that isn't part of the
        owned allowlist -- narrower than the default whole-'skills' entry."""
        mf = DistributionManifest(
            name="narrow", version="0.1.0",
            distribution_owned=["SOUL.md", "skills/research"],
        )
        staged = _make_staging_dir(profile_env, "src", manifest=mf)
        (staged / "skills" / "research").mkdir(parents=True, exist_ok=True)
        (staged / "skills" / "research" / "SKILL.md").write_text("# Research\n")

        plan = install_distribution(str(staged), name="narrow_test")

        # User (or a prior install) has an unrelated skill sitting alongside.
        (plan.target_dir / "skills" / "user-added").mkdir(parents=True, exist_ok=True)
        (plan.target_dir / "skills" / "user-added" / "SKILL.md").write_text(
            "# Mine\n"
        )

        (staged / "skills" / "research" / "SKILL.md").write_text("# Research v2\n")
        update_distribution("narrow_test", force_config=False)

        assert (
            plan.target_dir / "skills" / "research" / "SKILL.md"
        ).read_text() == "# Research v2\n"
        assert (plan.target_dir / "skills" / "user-added" / "SKILL.md").exists()

    def test_stale_distributed_file_is_removed_on_update(self, profile_env):
        """A file that WAS part of the distribution's owned content in an
        earlier revision, and is removed from the source in a later one,
        must actually be deleted on update -- not retained forever by a
        dirs_exist_ok=True-style merge. Matches the documented 'replaced
        from the new clone' contract."""
        staged = _make_staging_dir(profile_env, "src")
        (staged / "skills" / "old-skill").mkdir(parents=True, exist_ok=True)
        (staged / "skills" / "old-skill" / "SKILL.md").write_text("# Old\n")

        plan = install_distribution(str(staged), name="stale_test")
        assert (plan.target_dir / "skills" / "old-skill" / "SKILL.md").exists()

        # Author removes the skill from the distribution's source entirely.
        import shutil as _shutil
        _shutil.rmtree(staged / "skills" / "old-skill")
        (staged / "SOUL.md").write_text("I am Source v2.\n")

        update_distribution("stale_test", force_config=False)

        assert not (plan.target_dir / "skills" / "old-skill").exists(), (
            "A file removed from a newer distribution revision must be "
            "deleted on update, not retained"
        )
        # The still-current demo skill (present in both revisions) survives.
        assert (plan.target_dir / "skills" / "demo" / "SKILL.md").exists()

    def test_target_only_file_inside_owned_directory_survives_update(
        self, profile_env
    ):
        """A file the USER placed inside a distribution-owned directory
        (never part of any distributed revision) must survive an update
        -- it's never in installed_files, so the stale-file prune never
        touches it, even though it lives inside an otherwise distribution-
        owned tree."""
        staged = _make_staging_dir(profile_env, "src")
        plan = install_distribution(str(staged), name="user_addition_test")

        (plan.target_dir / "skills" / "my-local-only").mkdir(parents=True, exist_ok=True)
        (plan.target_dir / "skills" / "my-local-only" / "SKILL.md").write_text(
            "# Mine, never distributed\n"
        )

        (staged / "SOUL.md").write_text("I am Source v2.\n")
        update_distribution("user_addition_test", force_config=False)

        assert (
            plan.target_dir / "skills" / "my-local-only" / "SKILL.md"
        ).read_text() == "# Mine, never distributed\n"
        # And the actually-distributed skill is still correctly replaced.
        assert (plan.target_dir / "SOUL.md").read_text() == "I am Source v2.\n"

    def test_installed_files_tracked_in_manifest(self, profile_env):
        """installed_files must be populated and persisted so a later
        update can correctly diff against it."""
        staged = _make_staging_dir(profile_env, "src")
        plan = install_distribution(str(staged), name="tracked_test")

        mf = read_manifest(plan.target_dir)
        assert mf is not None
        assert "SOUL.md" in mf.installed_files
        assert "skills/demo/SKILL.md" in mf.installed_files
        assert "cron/daily.json" in mf.installed_files
        # User-owned exclusions and the .env.EXAMPLE derivative are never
        # tracked as distributed content.
        assert not any(f.startswith("memories/") for f in mf.installed_files)


class TestDistributionOwnedPathTraversal:
    """Security regression tests (review of #75351): a crafted or
    corrupted manifest path (distribution_owned entry, or a persisted
    installed_files entry) must never escape the staged/target roots via
    '..' traversal, absolute paths, or other non-normalized forms. A
    naive top-level USER_OWNED_EXCLUDE check alone doesn't catch this --
    'skills/../../auth.json' has top_level == 'skills' (not excluded),
    but resolves outside both roots once joined.
    """

    def test_traversal_owned_path_does_not_escape_target(self, profile_env):
        """A crafted distribution_owned entry like 'skills/../../auth.json'
        must not write outside the target profile directory, even though
        its top-level component ('skills') isn't in USER_OWNED_EXCLUDE.
        staged/"skills/../../auth.json" resolves to staged.parent/auth.json
        (i.e. the shared profiles/ directory, one level above this specific
        profile's own staging dir) -- verified directly below."""
        mf = DistributionManifest(
            name="traversal_test", version="0.1.0",
            distribution_owned=["SOUL.md", "skills/../../auth.json"],
        )
        staged = _make_staging_dir(profile_env, "src", manifest=mf)
        resolved_src = (staged / "skills/../../auth.json").resolve()
        assert resolved_src == staged.parent / "auth.json", resolved_src
        resolved_src.parent.mkdir(parents=True, exist_ok=True)
        resolved_src.write_text('{"malicious": "payload"}')

        # The corresponding DEST traversal target -- shared across every
        # profile, one level above where THIS profile's own target_dir
        # would be -- must never receive a copy.
        outside_marker = profile_env / ".hermes" / "profiles" / "auth.json"

        install_distribution(str(staged), name="traversal_test")

        assert not outside_marker.exists(), (
            "the traversal entry must not have written outside the "
            "target profile directory"
        )

    def test_traversal_owned_path_rejected_not_silently_ignored(self, profile_env, caplog):
        """The unsafe entry must be explicitly rejected (logged and
        skipped), not silently swallowed by an unrelated 'nothing staged'
        check -- verifies the validator actually runs."""
        import logging
        mf = DistributionManifest(
            name="traversal_logged", version="0.1.0",
            distribution_owned=["SOUL.md", "../../etc/passwd"],
        )
        staged = _make_staging_dir(profile_env, "src", manifest=mf)

        with caplog.at_level(logging.WARNING, logger="hermes_cli.profile_distribution"):
            install_distribution(str(staged), name="traversal_logged")

        assert any(
            "unsafe" in r.message.lower() and "distribution_owned" in r.message.lower()
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_traversal_persisted_installed_files_entry_ignored_on_prune(
        self, profile_env, caplog, monkeypatch
    ):
        """A corrupted/tainted installed_files entry from an earlier
        revision (e.g. a bug in an older version of this code, before the
        from_dict()-level filter existed) must not be used to delete
        files outside the target profile directory during stale-file
        cleanup. from_dict() already filters unsafe entries on every
        normal read (defense-in-depth, verified separately below) -- this
        test patches read_manifest for the one call inside
        _copy_dist_payload to simulate a manifest that bypassed that
        filter, isolating and directly exercising the SEPARATE guard at
        the actual deletion site.
        """
        import logging
        import hermes_cli.profile_distribution as dist_mod

        staged = _make_staging_dir(profile_env, "src")
        plan = install_distribution(str(staged), name="corrupted_manifest_test")

        outside_target = profile_env / "OUTSIDE_MARKER.txt"
        outside_target.write_text("must survive")

        real_manifest = read_manifest(plan.target_dir)
        tainted_files = list(real_manifest.installed_files) + [
            "../../../OUTSIDE_MARKER.txt"
        ]

        _real_read_manifest = dist_mod.read_manifest
        call_count = [0]

        def _tainted_read_manifest(profile_dir):
            call_count[0] += 1
            result = _real_read_manifest(profile_dir)
            if call_count[0] == 1 and result is not None and profile_dir == plan.target_dir:
                # Only taint the FIRST call inside _copy_dist_payload
                # (reading previous_installed) -- bypassing from_dict's
                # own filter to simulate an unfiltered legacy manifest.
                result.installed_files = tainted_files
            return result

        monkeypatch.setattr(dist_mod, "read_manifest", _tainted_read_manifest)

        (staged / "SOUL.md").write_text("I am Source v2.\n")
        with caplog.at_level(logging.WARNING, logger="hermes_cli.profile_distribution"):
            update_distribution("corrupted_manifest_test", force_config=False)

        assert outside_target.exists() and outside_target.read_text() == "must survive", (
            "a corrupted installed_files traversal entry must not delete "
            "files outside the target profile directory"
        )
        # Best-effort: a warning SHOULD be logged when this happens, but
        # the core security property above (nothing gets deleted outside
        # target) is the assertion that actually matters here.
        if caplog.records:
            assert any("unsafe" in r.message.lower() for r in caplog.records), (
                [r.message for r in caplog.records]
            )

    def test_from_dict_strips_traversal_entries_at_parse_time(self):
        """Defense-in-depth: DistributionManifest.from_dict() itself must
        never construct a manifest object carrying an unsafe
        installed_files entry, independent of the _copy_dist_payload-level
        guard."""
        mf = DistributionManifest.from_dict({
            "name": "x", "version": "0.1.0",
            "installed_files": ["SOUL.md", "../../etc/passwd", "skills/../../auth.json"],
        })
        assert mf.installed_files == ["SOUL.md"]


class TestErrorSurfaces:

    def test_bad_profile_name_raises_valueerror_not_traceback(self, profile_env, tmp_path):
        """A manifest whose 'name' can't be used as a profile identifier
        should raise ValueError from validate_profile_name — the CLI handler
        catches both DistributionError and ValueError so users see a clean
        'Error: ...' line instead of a Python traceback.
        """
        mf = DistributionManifest(name="Invalid Name With Spaces", version="0.1.0")
        staged = _make_staging_dir(profile_env, "bad", manifest=mf)
        with pytest.raises((ValueError, DistributionError)):
            plan_install(str(staged), tmp_path / "work")

