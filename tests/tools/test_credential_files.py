"""Tests for credential file passthrough and skills directory mounting."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.credential_files import (
    clear_credential_files,
    get_credential_file_mounts,
    get_cache_directory_mounts,
    get_skills_directory_mount,
    iter_cache_files,
    iter_skills_files,
    map_cache_path_to_container,
    register_credential_file,
    register_credential_files,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset module state between tests.

    ``clear_credential_files`` now also drops the per-home config cache, so it
    is the single reset both surfaces need.
    """
    clear_credential_files()
    yield
    clear_credential_files()


class TestRegisterCredentialFiles:
    def test_dict_with_path_key(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "token.json").write_text("{}", encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            missing = register_credential_files([{"path": "token.json"}])

        assert missing == []
        mounts = get_credential_file_mounts()
        assert len(mounts) == 1
        assert mounts[0]["host_path"] == str(hermes_home / "token.json")
        assert mounts[0]["container_path"] == "/root/.hermes/token.json"


    def test_path_takes_precedence_over_name(self, tmp_path):
        """When both path and name are present, path wins."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "real.json").write_text("{}", encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            missing = register_credential_files([
                {"path": "real.json", "name": "wrong.json"},
            ])

        assert missing == []
        mounts = get_credential_file_mounts()
        assert "real.json" in mounts[0]["container_path"]


class TestSkillsDirectoryMount:
    def test_returns_mount_when_skills_dir_exists(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        skills_dir = hermes_home / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "test-skill").mkdir()
        (skills_dir / "test-skill" / "SKILL.md").write_text("# test", encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            mounts = get_skills_directory_mount()

        assert len(mounts) >= 1
        assert mounts[0]["host_path"] == str(skills_dir)
        assert mounts[0]["container_path"] == "/root/.hermes/skills"


    def test_custom_container_base(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        (hermes_home / "skills").mkdir(parents=True)

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            mounts = get_skills_directory_mount(container_base="/home/user/.hermes")

        assert mounts[0]["container_path"] == "/home/user/.hermes/skills"

    def test_symlinks_are_sanitized(self, tmp_path):
        """Symlinks in skills dir should be excluded from the mount."""
        hermes_home = tmp_path / ".hermes"
        skills_dir = hermes_home / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "legit.md").write_text("# real skill", encoding="utf-8")
        # Create a symlink pointing outside the skills tree
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        (skills_dir / "evil_link").symlink_to(secret)

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            mounts = get_skills_directory_mount()

        assert len(mounts) >= 1
        mount = mounts[0]
        # The mount path should be a sanitized copy, not the original
        safe_path = Path(mount["host_path"])
        assert safe_path != skills_dir
        # Legitimate file should be present
        assert (safe_path / "legit.md").exists()
        assert (safe_path / "legit.md").read_text(encoding="utf-8") == "# real skill"
        # Symlink should NOT be present
        assert not (safe_path / "evil_link").exists()

    def test_no_symlinks_returns_original_dir(self, tmp_path):
        """When no symlinks exist, the original dir is returned (no copy)."""
        hermes_home = tmp_path / ".hermes"
        skills_dir = hermes_home / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "skill.md").write_text("ok", encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            mounts = get_skills_directory_mount()

        assert mounts[0]["host_path"] == str(skills_dir)


class TestIterSkillsFiles:
    def test_returns_files_skipping_symlinks(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        skills_dir = hermes_home / "skills"
        (skills_dir / "cat" / "myskill").mkdir(parents=True)
        (skills_dir / "cat" / "myskill" / "SKILL.md").write_text("# skill", encoding="utf-8")
        (skills_dir / "cat" / "myskill" / "scripts").mkdir()
        (skills_dir / "cat" / "myskill" / "scripts" / "run.sh").write_text("#!/bin/bash", encoding="utf-8")
        # Add a symlink that should be filtered
        secret = tmp_path / "secret"
        secret.write_text("nope", encoding="utf-8")
        (skills_dir / "cat" / "myskill" / "evil").symlink_to(secret)

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            files = iter_skills_files()

        paths = {f["container_path"] for f in files}
        assert "/root/.hermes/skills/cat/myskill/SKILL.md" in paths
        assert "/root/.hermes/skills/cat/myskill/scripts/run.sh" in paths
        # Symlink should be excluded
        assert not any("evil" in f["container_path"] for f in files)

    def test_empty_when_no_skills_dir(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            assert iter_skills_files() == []

class TestPathTraversalSecurity:
    """Path traversal and absolute path rejection.

    A malicious skill could declare::

        required_credential_files:
          - path: '../../.ssh/id_rsa'

    Without containment checks, this would mount the host's SSH private key
    into the container sandbox, leaking it to the skill's execution environment.
    """

    def test_dotdot_traversal_rejected(self, tmp_path, monkeypatch):
        """'../sensitive' must not escape HERMES_HOME."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir()

        # Create a sensitive file one level above hermes_home
        sensitive = tmp_path / "sensitive.json"
        sensitive.write_text('{"secret": "value"}', encoding="utf-8")

        result = register_credential_file("../sensitive.json")

        assert result is False
        assert get_credential_file_mounts() == []

    def test_deep_traversal_rejected(self, tmp_path, monkeypatch):
        """'../../etc/passwd' style traversal must be rejected."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        # Create a fake sensitive file outside hermes_home
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")

        result = register_credential_file("../../.ssh/id_rsa")

        assert result is False
        assert get_credential_file_mounts() == []

    def test_absolute_path_rejected(self, tmp_path, monkeypatch):
        """Absolute paths must be rejected regardless of whether they exist."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        # Create a file at an absolute path
        sensitive = tmp_path / "absolute.json"
        sensitive.write_text("{}", encoding="utf-8")

        result = register_credential_file(str(sensitive))

        assert result is False
        assert get_credential_file_mounts() == []


    def test_nested_subdir_inside_hermes_home_allowed(self, tmp_path, monkeypatch):
        """Files in subdirectories of HERMES_HOME must be allowed."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        subdir = hermes_home / "creds"
        subdir.mkdir()
        (subdir / "oauth.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        result = register_credential_file("creds/oauth.json")

        assert result is True

    def test_symlink_traversal_rejected(self, tmp_path, monkeypatch):
        """A symlink inside HERMES_HOME pointing outside must be rejected."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        # Create a sensitive file outside hermes_home
        sensitive = tmp_path / "sensitive.json"
        sensitive.write_text('{"secret": "value"}', encoding="utf-8")

        # Create a symlink inside hermes_home pointing outside
        symlink = hermes_home / "evil_link.json"
        try:
            symlink.symlink_to(sensitive)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this platform")

        result = register_credential_file("evil_link.json")

        # The resolved path escapes HERMES_HOME — must be rejected
        assert result is False
        assert get_credential_file_mounts() == []


# ---------------------------------------------------------------------------
# Config-based credential files — same containment checks
# ---------------------------------------------------------------------------

class TestConfigPathTraversal:
    """terminal.credential_files in config.yaml must also reject traversal."""

    def _write_config(self, hermes_home: Path, cred_files: list):
        import yaml
        config_path = hermes_home / "config.yaml"
        config_path.write_text(yaml.dump({"terminal": {"credential_files": cred_files}}), encoding="utf-8")

    def test_config_traversal_rejected(self, tmp_path, monkeypatch):
        """'../secret' in config.yaml must not escape HERMES_HOME."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        sensitive = tmp_path / "secret.json"
        sensitive.write_text("{}", encoding="utf-8")
        self._write_config(hermes_home, ["../secret.json"])

        mounts = get_credential_file_mounts()
        host_paths = [m["host_path"] for m in mounts]
        assert str(sensitive) not in host_paths
        assert str(sensitive.resolve()) not in host_paths

    def test_config_absolute_path_rejected(self, tmp_path, monkeypatch):
        """Absolute paths in config.yaml must be rejected."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        sensitive = tmp_path / "abs.json"
        sensitive.write_text("{}", encoding="utf-8")
        self._write_config(hermes_home, [str(sensitive)])

        mounts = get_credential_file_mounts()
        assert mounts == []

    def test_config_legitimate_file_works(self, tmp_path, monkeypatch):
        """Normal files inside HERMES_HOME via config must still mount."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        (hermes_home / "oauth.json").write_text("{}", encoding="utf-8")
        self._write_config(hermes_home, ["oauth.json"])

        mounts = get_credential_file_mounts()
        assert len(mounts) == 1
        assert "oauth.json" in mounts[0]["container_path"]


class TestConfigMasterStoreDenylist:
    """terminal.credential_files must run through the same master-store
    deny-list as skill registration, so a config entry cannot mount a
    credential store the agent is denied from reading (#84270)."""

    def _write_config(self, hermes_home: Path, cred_files: list):
        import yaml
        config_path = hermes_home / "config.yaml"
        config_path.write_text(yaml.dump({"terminal": {"credential_files": cred_files}}), encoding="utf-8")

    @pytest.mark.parametrize("master_store", ["auth.json", ".env", "mcp-tokens/x.json"])
    def test_config_master_store_never_mounts(self, tmp_path, monkeypatch, master_store):
        """auth.json / .env / mcp-tokens/* declared in config must be refused,
        even though they sit inside HERMES_HOME and pass containment."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        target = hermes_home / master_store
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret", encoding="utf-8")
        self._write_config(hermes_home, [master_store])

        mounts = get_credential_file_mounts()
        host_paths = [m["host_path"] for m in mounts]
        assert str(target.resolve()) not in host_paths
        assert mounts == []

    def test_config_service_token_still_mounts(self, tmp_path, monkeypatch):
        """A safe operator-approved service token (not a master store) must
        still mount — the deny-list only blocks the master key files."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        (hermes_home / "google_token.json").write_text("{}", encoding="utf-8")
        self._write_config(hermes_home, ["google_token.json"])

        mounts = get_credential_file_mounts()
        assert len(mounts) == 1
        assert "google_token.json" in mounts[0]["container_path"]

    def test_config_and_skill_share_one_policy(self, tmp_path, monkeypatch):
        """The config path and the skill path must agree: a master store is
        refused by both surfaces (regression against config-path drift)."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        (hermes_home / "auth.json").write_text("secret", encoding="utf-8")

        # Skill path already refuses it.
        assert register_credential_file("auth.json") is False

        # Config path must refuse it too.
        self._write_config(hermes_home, ["auth.json"])
        assert get_credential_file_mounts() == []


class TestConfigCacheProfileBoundary:
    """The config-file cache must not transplant one profile's approved
    credential path into another profile's sandbox. In a multiplexed gateway
    the active HERMES_HOME is profile-scoped while the module-global cache
    outlives any single scope, so the cache is keyed by resolved home."""

    def _write_config(self, hermes_home: Path, cred_files: list):
        import yaml
        config_path = hermes_home / "config.yaml"
        config_path.write_text(yaml.dump({"terminal": {"credential_files": cred_files}}), encoding="utf-8")

    def _profile(self, tmp_path: Path, name: str, token: str) -> Path:
        home = tmp_path / name / ".hermes"
        home.mkdir(parents=True)
        (home / token).write_text("{}", encoding="utf-8")
        self._write_config(home, [token])
        return home

    def test_second_profile_never_receives_first_profiles_token(self, tmp_path, monkeypatch):
        """Profile A then profile B in the same process, no manual cache clear:
        B must see only B's token and never A's host path."""
        home_a = self._profile(tmp_path, "a", "a_token.json")
        home_b = self._profile(tmp_path, "b", "b_token.json")

        monkeypatch.setenv("HERMES_HOME", str(home_a))
        mounts_a = get_credential_file_mounts()
        assert [m["host_path"] for m in mounts_a] == [str((home_a / "a_token.json").resolve())]

        # Switch to profile B WITHOUT clearing the cache.
        monkeypatch.setenv("HERMES_HOME", str(home_b))
        mounts_b = get_credential_file_mounts()
        host_paths_b = [m["host_path"] for m in mounts_b]
        assert host_paths_b == [str((home_b / "b_token.json").resolve())]
        assert str((home_a / "a_token.json").resolve()) not in host_paths_b

    def test_refresh_boundary_observes_config_change(self, tmp_path, monkeypatch):
        """clear_credential_files() is the declared refresh boundary: after a
        config entry is removed, the next load past the boundary drops it."""
        home = self._profile(tmp_path, "p", "svc.json")
        monkeypatch.setenv("HERMES_HOME", str(home))

        assert len(get_credential_file_mounts()) == 1

        # Remove the entry from config; the cached snapshot is still stale...
        self._write_config(home, [])
        assert len(get_credential_file_mounts()) == 1

        # ...until the declared refresh boundary re-reads config.
        clear_credential_files()
        assert get_credential_file_mounts() == []

    def test_retargeted_alias_home_never_serves_prior_targets_token(self, tmp_path, monkeypatch):
        """A stable alias path (e.g. ``profiles/current``) retargeted from home A
        to home B while the active override string stays constant must NOT serve
        A's cached host path. The cache is keyed by the *canonical* home identity
        (symlink-resolved), so the retarget changes the key even though the raw
        ``HERMES_HOME`` spelling does not."""
        home_a = self._profile(tmp_path, "a", "a_token.json")
        home_b = self._profile(tmp_path, "b", "b_token.json")

        # The active HERMES_HOME is a stable alias whose *string* never changes.
        alias = tmp_path / "profiles" / "current"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(home_a, target_is_directory=True)
        monkeypatch.setenv("HERMES_HOME", str(alias))

        mounts_a = get_credential_file_mounts()
        assert [m["host_path"] for m in mounts_a] == [str((home_a / "a_token.json").resolve())]

        # Atomically retarget the alias A -> B WITHOUT clearing the cache; the
        # override string handed to the loader is byte-for-byte identical.
        alias.unlink()
        alias.symlink_to(home_b, target_is_directory=True)

        mounts_b = get_credential_file_mounts()
        host_paths_b = [m["host_path"] for m in mounts_b]
        assert host_paths_b == [str((home_b / "b_token.json").resolve())]
        assert str((home_a / "a_token.json").resolve()) not in host_paths_b

    def test_intra_load_alias_retarget_never_crosses_home(self, tmp_path, monkeypatch):
        """The race is *inside* a single cache miss, not just between two loads.

        The key is captured from the canonical home, but the config read and
        every admission independently re-resolve ``HERMES_HOME``. If the alias
        is retargeted A -> B after key capture yet before those run, B's config
        and B's token path could be realized and cached under A's key — the same
        cross-home credential transplant, only within one load. The loader pins
        the resolved home snapshot for the whole miss, so both the raw-config
        read and admission observe home A regardless of the live alias.
        """
        home_a = self._profile(tmp_path, "a", "a_token.json")
        home_b = self._profile(tmp_path, "b", "b_token.json")

        alias = tmp_path / "profiles" / "current"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(home_a, target_is_directory=True)
        monkeypatch.setenv("HERMES_HOME", str(alias))

        import hermes_cli.config as hermes_config
        real_read_raw_config = hermes_config.read_raw_config

        def _retarget_then_read(*args, **kwargs):
            # Fire exactly once, mid-load: flip the alias A -> B *after* the
            # loader captured its canonical key but *before* it realizes any
            # path. A snapshot-blind loader would now read B under A's key.
            if alias.readlink() == home_a:
                alias.unlink()
                alias.symlink_to(home_b, target_is_directory=True)
            return real_read_raw_config(*args, **kwargs)

        monkeypatch.setattr(hermes_config, "read_raw_config", _retarget_then_read)

        mounts = get_credential_file_mounts()
        host_paths = [m["host_path"] for m in mounts]

        # The load must have stayed on home A end-to-end; B's token must never
        # be realized under A's captured key.
        assert host_paths == [str((home_a / "a_token.json").resolve())]
        assert str((home_b / "b_token.json").resolve()) not in host_paths

        # Point the alias back to A: the canonical-A cache entry must still hold
        # A's own token (never poisoned with B during the interleaved miss).
        alias.unlink()
        alias.symlink_to(home_a, target_is_directory=True)
        assert [m["host_path"] for m in get_credential_file_mounts()] == [
            str((home_a / "a_token.json").resolve())
        ]


# ---------------------------------------------------------------------------
# Cache directory mounts
# ---------------------------------------------------------------------------

class TestCacheDirectoryMounts:
    """Tests for get_cache_directory_mounts() and iter_cache_files()."""

    def test_returns_existing_cache_dirs(self, tmp_path, monkeypatch):
        """Existing cache dirs are returned with correct container paths."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "cache" / "documents").mkdir(parents=True)
        (hermes_home / "cache" / "audio").mkdir(parents=True)
        (hermes_home / "cache" / "videos").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mounts = get_cache_directory_mounts()
        paths = {m["container_path"] for m in mounts}
        assert "/root/.hermes/cache/documents" in paths
        assert "/root/.hermes/cache/audio" in paths
        assert "/root/.hermes/cache/videos" in paths


    def test_legacy_dir_names_resolved(self, tmp_path, monkeypatch):
        """Old-style dir names (e.g. document_cache) are resolved correctly.

        Populates the legacy dirs with a sentinel file so they count as
        ``has content`` for ``get_hermes_dir``'s populated-legacy check
        (see #27602 — empty legacy stubs are no longer honoured).
        """
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        # Use legacy dir name with content — get_hermes_dir prefers
        # populated old over new.
        legacy_doc = hermes_home / "document_cache"
        legacy_img = hermes_home / "image_cache"
        legacy_doc.mkdir()
        legacy_img.mkdir()
        (legacy_doc / "cached.txt").write_bytes(b"x")
        (legacy_img / "cached.png").write_bytes(b"x")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mounts = get_cache_directory_mounts()
        host_paths = {m["host_path"] for m in mounts}
        assert str(hermes_home / "document_cache") in host_paths
        assert str(hermes_home / "image_cache") in host_paths
        # Container paths always use the new layout
        container_paths = {m["container_path"] for m in mounts}
        assert "/root/.hermes/cache/documents" in container_paths
        assert "/root/.hermes/cache/images" in container_paths

    def test_empty_hermes_home(self, tmp_path, monkeypatch):
        """Empty home → every staging dir is created and mounted (#76577).

        Docker snapshots the mount list at container creation; skipping
        not-yet-existing dirs meant the first attachment/clipboard file after
        container start dangled forever. All _CACHE_DIRS entries mount."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mounts = get_cache_directory_mounts()
        container_paths = {m["container_path"] for m in mounts}
        assert "/root/.hermes/attachments" in container_paths
        assert "/root/.hermes/images" in container_paths
        assert "/root/.hermes/cache/images" in container_paths
        for mount in mounts:
            assert Path(mount["host_path"]).is_dir()

    def test_images_upload_dir_is_mounted(self, tmp_path, monkeypatch):
        """The flat top-level ``images/`` upload dir is mounted (#69575).

        Desktop / clipboard / PDF uploads land in ``HERMES_HOME/images``, not
        under ``cache/``. Without this entry vision_analyze on a desktop upload
        fails because the file is not reachable inside the sandbox.
        """
        hermes_home = tmp_path / ".hermes"
        (hermes_home / "images").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mounts = get_cache_directory_mounts()
        by_container = {m["container_path"]: m["host_path"] for m in mounts}
        assert "/root/.hermes/images" in by_container
        assert by_container["/root/.hermes/images"] == str(hermes_home / "images")

    def test_images_upload_file_maps_into_container(self, tmp_path, monkeypatch):
        """A concrete upload under ``images/`` maps to its container path.

        This is the reverse mapping vision uses to translate a container-visible
        path back to the host mount; it must recognise the ``images/`` dir.
        """
        hermes_home = tmp_path / ".hermes"
        (hermes_home / "images").mkdir(parents=True)
        upload = hermes_home / "images" / "upload_20260722_181019_1.png"
        upload.write_bytes(bytes.fromhex("89504e470d0a1a0a"))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        assert (
            map_cache_path_to_container(str(upload))
            == "/root/.hermes/images/upload_20260722_181019_1.png"
        )


class TestMapCachePathToContainer:
    """Tests for map_cache_path_to_container() — the backend-agnostic mapper."""

    def test_maps_path_under_cache_dir(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        img_dir = hermes_home / "cache" / "images"
        img_dir.mkdir(parents=True)
        host_path = str(img_dir / "generated.png")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        assert (
            map_cache_path_to_container(host_path)
            == "/root/.hermes/cache/images/generated.png"
        )


    def test_maps_path_even_when_cache_dir_missing(self, tmp_path, monkeypatch):
        """Missing staging dirs are auto-created at mount-list time (#76577):
        Docker snapshots mounts at container creation, so a dir that appears
        later would dangle for the container's whole life. The map must
        therefore succeed (and the dir exist) even before first use."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mapped = map_cache_path_to_container(str(hermes_home / "cache" / "images" / "x.png"))
        assert mapped == "/root/.hermes/cache/images/x.png"
        assert (hermes_home / "cache" / "images").is_dir()


class TestToAgentVisiblePathPerBackend:
    """#76577 follow-up: translation covers every backend that relocates the
    Hermes cache — not just docker — and skips the ones where the host path
    stays correct (local; singularity auto-binds the host home)."""

    def _staged(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        (hermes_home / "attachments").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        return str(hermes_home / "attachments" / "drop.zip")

    def test_docker_maps_to_root_hermes(self, tmp_path, monkeypatch):
        staged = self._staged(tmp_path, monkeypatch)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        from tools.credential_files import to_agent_visible_cache_path
        assert to_agent_visible_cache_path(staged) == "/root/.hermes/attachments/drop.zip"

    def test_ssh_maps_to_tilde_hermes(self, tmp_path, monkeypatch):
        staged = self._staged(tmp_path, monkeypatch)
        monkeypatch.setenv("TERMINAL_ENV", "ssh")
        from tools.credential_files import to_agent_visible_cache_path
        assert to_agent_visible_cache_path(staged) == "~/.hermes/attachments/drop.zip"

    @pytest.mark.parametrize("backend", ["local", "singularity", ""])
    def test_untranslated_backends_keep_host_path(self, tmp_path, monkeypatch, backend):
        staged = self._staged(tmp_path, monkeypatch)
        monkeypatch.setenv("TERMINAL_ENV", backend)
        from tools.credential_files import to_agent_visible_cache_path
        assert to_agent_visible_cache_path(staged) == staged

    def test_non_cache_path_passes_through(self, tmp_path, monkeypatch):
        self._staged(tmp_path, monkeypatch)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        from tools.credential_files import to_agent_visible_cache_path
        assert to_agent_visible_cache_path("/etc/hosts") == "/etc/hosts"


class TestIterCacheFiles:
    """Tests for iter_cache_files()."""

    def test_enumerates_files(self, tmp_path, monkeypatch):
        """Regular files in cache dirs are returned."""
        hermes_home = tmp_path / ".hermes"
        doc_dir = hermes_home / "cache" / "documents"
        doc_dir.mkdir(parents=True)
        (doc_dir / "upload.zip").write_bytes(b"PK\x03\x04")
        (doc_dir / "report.pdf").write_bytes(b"%PDF-1.4")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        entries = iter_cache_files()
        names = {Path(e["container_path"]).name for e in entries}
        assert "upload.zip" in names
        assert "report.pdf" in names

    def test_skips_symlinks(self, tmp_path, monkeypatch):
        """Symlinks inside cache dirs are skipped."""
        hermes_home = tmp_path / ".hermes"
        doc_dir = hermes_home / "cache" / "documents"
        doc_dir.mkdir(parents=True)
        real_file = doc_dir / "real.txt"
        real_file.write_text("content", encoding="utf-8")
        (doc_dir / "link.txt").symlink_to(real_file)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        entries = iter_cache_files()
        names = [Path(e["container_path"]).name for e in entries]
        assert "real.txt" in names
        assert "link.txt" not in names


    def test_empty_cache(self, tmp_path, monkeypatch):
        """No cache dirs → empty list."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        assert iter_cache_files() == []


class TestMasterCredentialStoresAreNeverMountable:
    """Containment is not enough — HERMES_HOME *is* where the keys live.

    ``required_credential_files`` is skill-declared frontmatter, and skills are
    installed from the hub. The traversal guard already stops
    ``../../.ssh/id_rsa`` from escaping HERMES_HOME, but every master
    credential store sits *inside* it: a one-line declaration would otherwise
    bind-mount ``.env`` (every provider key) or ``auth.json`` (all provider
    tokens and OAuth grants) read-only into the sandbox the skill's own code
    runs in.

    The bar is the canonical read deny-list: whatever the agent is forbidden to
    ``read_file`` must not be mountable either, so the mount surface can't
    grant what the read surface denies.
    """

    @staticmethod
    def _home(tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / ".env").write_text("OPENAI_API_KEY=sk-proj-REAL\n", encoding="utf-8")
        (home / "auth.json").write_text('{"providers":{}}', encoding="utf-8")
        (home / ".anthropic_oauth.json").write_text('{"refresh_token":"rt"}', encoding="utf-8")
        (home / "webhook_subscriptions.json").write_text("{}", encoding="utf-8")
        (home / "cache").mkdir()
        (home / "cache" / "bws_cache.json").write_text("{}", encoding="utf-8")
        (home / "mcp-tokens").mkdir()
        (home / "mcp-tokens" / "srv.json").write_text('{"access_token":"t"}', encoding="utf-8")
        (home / "google_token.json").write_text("{}", encoding="utf-8")
        return home

    @pytest.mark.parametrize(
        "rel_path",
        [
            ".env",
            "auth.json",
            ".anthropic_oauth.json",
            "webhook_subscriptions.json",
            "cache/bws_cache.json",
            "mcp-tokens/srv.json",
        ],
    )
    def test_master_credential_store_is_refused(self, tmp_path, rel_path):
        home = self._home(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            assert register_credential_file(rel_path) is False, (
                f"{rel_path} would be bind-mounted into the sandbox"
            )
            assert get_credential_file_mounts() == []

    def test_per_service_token_still_mounts(self, tmp_path):
        """The module's legitimate purpose must keep working."""
        home = self._home(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            assert register_credential_file("google_token.json") is True
            mounts = get_credential_file_mounts()
        assert [m["container_path"] for m in mounts] == [
            "/root/.hermes/google_token.json"
        ]

    def test_refused_entry_does_not_block_the_rest_of_the_batch(self, tmp_path):
        home = self._home(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            missing = register_credential_files([".env", "google_token.json"])
            mounts = get_credential_file_mounts()

        paths = [m["container_path"] for m in mounts]
        assert "/root/.hermes/google_token.json" in paths
        assert "/root/.hermes/.env" not in paths
        assert ".env" in missing, "a refused store is reported back to the skill"

    def test_traversal_guard_still_applies(self, tmp_path):
        """The pre-existing containment check is untouched."""
        home = self._home(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            assert register_credential_file("../../.ssh/id_rsa") is False
            assert register_credential_file("/etc/passwd") is False

    def test_missing_guard_fails_closed_with_error_log(self, tmp_path, caplog):
        """If agent.file_safety can't be imported the mount is refused loudly.

        The fail-closed path must be observable (#67665): a silent deny with
        no diagnostic reproduces the trust gap the deny-list was added to fix.
        """
        import tools.credential_files as cf

        home = self._home(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}), \
                patch.object(cf, "get_read_block_error", None):
            with caplog.at_level("ERROR", logger="tools.credential_files"):
                assert cf.register_credential_file("google_token.json") is False
            assert cf.get_credential_file_mounts() == []
        assert any("deny-list cannot be consulted" in r.message for r in caplog.records)

    def test_guard_exception_fails_closed_with_traceback(self, tmp_path, caplog):
        """A raising guard refuses the mount and logs the stack trace."""
        import tools.credential_files as cf

        home = self._home(tmp_path)

        def _boom(path):
            raise RuntimeError("guard exploded")

        with patch.dict(os.environ, {"HERMES_HOME": str(home)}), \
                patch.object(cf, "get_read_block_error", _boom):
            with caplog.at_level("ERROR", logger="tools.credential_files"):
                assert cf.register_credential_file("google_token.json") is False
            assert cf.get_credential_file_mounts() == []
        rec = next(r for r in caplog.records if "read guard raised" in r.message)
        assert rec.exc_info is not None, "traceback must be attached (logger.exception)"
