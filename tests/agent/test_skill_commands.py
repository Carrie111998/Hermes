"""Tests for agent/skill_commands.py — skill slash command scanning and platform filtering."""

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tools.skills_tool as skills_tool_module
from agent.skill_commands import (
    build_preloaded_skills_prompt,
    build_skill_invocation_message,
    get_skill_commands,
    resolve_skill_command_key,
    scan_skill_commands,
)


def _make_skill(
    skills_dir, name, frontmatter_extra="", body="Do the thing.", category=None
):
    """Helper to create a minimal skill directory with SKILL.md."""
    if category:
        skill_dir = skills_dir / category / name
    else:
        skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"""\
---
name: {name}
description: Description for {name}.
{frontmatter_extra}---

# {name}

{body}
"""
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


def _symlink_category(skills_dir: Path, linked_root: Path, category: str) -> Path:
    """Create a category symlink under skills_dir pointing outside the tree."""
    external_category = linked_root / category
    external_category.mkdir(parents=True, exist_ok=True)
    symlink_path = skills_dir / category
    try:
        symlink_path.symlink_to(external_category, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")
    return external_category


class TestScanSkillCommands:
    def test_live_profile_root_and_external_dirs_are_part_of_catalog_scope(
        self, tmp_path
    ):
        """A long-lived process must not reuse another profile's slash map."""
        import agent.skill_commands as sc_mod
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        external_a = tmp_path / "external-a"
        external_b = tmp_path / "external-b"
        for home, external, local_name, external_name in (
            (home_a, external_a, "a-local", "a-external"),
            (home_b, external_b, "b-local", "b-external"),
        ):
            _make_skill(home / "skills", local_name)
            _make_skill(external, external_name)
            (home / "config.yaml").write_text(
                f"skills:\n  external_dirs:\n    - {external}\n", encoding="utf-8"
            )

        with (
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
            patch.object(sc_mod, "_skill_commands_environment", None),
            patch.object(sc_mod, "_skill_commands_roots", None),
            patch.object(sc_mod, "_resolve_skill_commands_platform", return_value=None),
            patch.object(sc_mod, "_resolve_skill_commands_environment", return_value=()),
        ):
            token = set_hermes_home_override(str(home_a))
            try:
                commands_a = dict(get_skill_commands())
            finally:
                reset_hermes_home_override(token)

            token = set_hermes_home_override(str(home_b))
            try:
                commands_b = dict(get_skill_commands())
            finally:
                reset_hermes_home_override(token)

            token = set_hermes_home_override(str(home_a))
            try:
                commands_a_again = dict(get_skill_commands())
            finally:
                reset_hermes_home_override(token)

        assert set(commands_a) == {"/a-local", "/a-external"}
        assert set(commands_b) == {"/b-local", "/b-external"}
        assert commands_a_again == commands_a

    def test_failed_scan_in_another_profile_never_returns_or_commits_old_catalog(
        self, tmp_path
    ):
        import agent.skill_commands as sc_mod
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        _make_skill(home_a / "skills", "a-only")
        _make_skill(home_b / "skills", "b-only")

        with (
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
            patch.object(sc_mod, "_skill_commands_environment", None),
            patch.object(sc_mod, "_skill_commands_roots", None),
            patch.object(sc_mod, "_resolve_skill_commands_platform", return_value=None),
            patch.object(sc_mod, "_resolve_skill_commands_environment", return_value=()),
        ):
            token = set_hermes_home_override(str(home_a))
            try:
                commands_a = dict(get_skill_commands())
            finally:
                reset_hermes_home_override(token)

            token = set_hermes_home_override(str(home_b))
            try:
                with patch(
                    "agent.skill_utils.iter_skill_index_files",
                    side_effect=OSError("temporary profile-b failure"),
                ):
                    assert get_skill_commands() == {}
            finally:
                reset_hermes_home_override(token)

            # The global cache remains the known-good profile-A snapshot, but
            # the profile-B request was deliberately denied that stale value.
            assert sc_mod._skill_commands == commands_a
            token = set_hermes_home_override(str(home_a))
            try:
                assert dict(get_skill_commands()) == commands_a
            finally:
                reset_hermes_home_override(token)

    def test_concurrent_profile_scans_do_not_cross_commit_catalogs(self, tmp_path):
        """Context-local profiles may race, but each caller receives its own map."""
        import agent.skill_commands as sc_mod
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        _make_skill(home_a / "skills", "a-only")
        _make_skill(home_b / "skills", "b-only")
        results = {}
        errors = []

        def load_for_profile(label, home):
            token = set_hermes_home_override(str(home))
            try:
                results[label] = dict(get_skill_commands())
            except BaseException as exc:  # assert after both workers join
                errors.append(exc)
            finally:
                reset_hermes_home_override(token)

        with (
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
            patch.object(sc_mod, "_skill_commands_environment", None),
            patch.object(sc_mod, "_skill_commands_roots", None),
            patch.object(sc_mod, "_resolve_skill_commands_platform", return_value=None),
            patch.object(sc_mod, "_resolve_skill_commands_environment", return_value=()),
        ):
            workers = [
                threading.Thread(target=load_for_profile, args=("a", home_a)),
                threading.Thread(target=load_for_profile, args=("b", home_b)),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

        assert errors == []
        assert set(results["a"]) == {"/a-only"}
        assert set(results["b"]) == {"/b-only"}

    def test_finds_skills(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "my-skill")
            result = scan_skill_commands()
        assert "/my-skill" in result
        assert result["/my-skill"]["name"] == "my-skill"

    @pytest.mark.parametrize(
        "broken",
        (
            "---\nname: steals-valid\n# no closing fence\n",
            "---\n- steals-valid\n---\n",
        ),
        ids=("unclosed-fence", "non-mapping-yaml"),
    )
    def test_invalid_fenced_skill_refuses_partial_slash_catalog(self, tmp_path, broken):
        """A corrupt package cannot claim a name or hide a complete scan."""
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "valid")
            corrupt = _make_skill(tmp_path, "corrupt") / "SKILL.md"
            corrupt.write_text(broken, encoding="utf-8")
            result = scan_skill_commands()

        assert result == {}
        assert "/steals-valid" not in result
        assert "/valid" not in result

    def test_empty_dir(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = scan_skill_commands()
        assert result == {}

    def test_root_scan_failure_preserves_last_good_catalog(self, tmp_path, caplog):
        import agent.skill_commands as sc_mod

        previous = {
            "/stable": {
                "name": "stable",
                "description": "Stable.",
                "skill_md_path": "/stable/SKILL.md",
                "skill_dir": "/stable",
            }
        }
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch.object(sc_mod, "_skill_commands", previous),
            patch.object(sc_mod, "_skill_commands_roots", (str(tmp_path.resolve()),)),
            patch(
                "agent.skill_utils.iter_skill_index_files",
                side_effect=OSError("temporary scan failure"),
            ),
            caplog.at_level("WARNING", logger="agent.skill_commands"),
        ):
            result = scan_skill_commands()

        assert result == previous
        assert any("keeping the previous catalog" in r.message for r in caplog.records)

    def test_first_scan_primary_root_permission_failure_retries_without_external_subset(
        self, monkeypatch, tmp_path
    ):
        """``stat`` errors on the primary root are not silently treated as empty."""
        import agent.skill_commands as sc_mod

        primary_root = tmp_path / "skills"
        external_root = tmp_path / "external"
        _make_skill(primary_root, "local")
        _make_skill(external_root, "external")
        roots = (str(primary_root.resolve()), str(external_root.resolve()))

        monkeypatch.setattr(
            sc_mod,
            "_resolve_skill_command_roots",
            lambda: (primary_root, (external_root,), roots),
        )
        original_stat = Path.stat
        deny_primary = True

        def deny_primary_root(path, *args, **kwargs):
            if deny_primary and path == primary_root:
                raise PermissionError("simulated primary root denial")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", deny_primary_root)
        monkeypatch.setattr(sc_mod, "_skill_commands", {})
        monkeypatch.setattr(sc_mod, "_skill_commands_platform", None)
        monkeypatch.setattr(sc_mod, "_skill_commands_environment", None)
        monkeypatch.setattr(sc_mod, "_skill_commands_roots", None)
        monkeypatch.setattr(sc_mod, "_resolve_skill_commands_platform", lambda: None)
        monkeypatch.setattr(sc_mod, "_resolve_skill_commands_environment", lambda: ())

        assert get_skill_commands() == {}
        assert sc_mod._skill_commands == {}

        deny_primary = False
        assert set(get_skill_commands()) == {"/local", "/external"}

    @pytest.mark.parametrize("root_kind", ["missing", "file"])
    def test_cold_start_invalid_external_root_refuses_local_only_catalog(
        self, monkeypatch, tmp_path, root_kind
    ):
        import agent.skill_commands as sc_mod

        primary_root = tmp_path / "skills"
        external_root = tmp_path / "configured-external"
        _make_skill(primary_root, "local")
        if root_kind == "file":
            external_root.write_text("not a directory", encoding="utf-8")
        roots = (str(primary_root.resolve()), str(external_root.resolve()))

        monkeypatch.setattr(
            sc_mod,
            "_resolve_skill_command_roots",
            lambda: (primary_root, (external_root,), roots),
        )
        monkeypatch.setattr(sc_mod, "_skill_commands", {})
        monkeypatch.setattr(sc_mod, "_skill_commands_platform", None)
        monkeypatch.setattr(sc_mod, "_skill_commands_environment", None)
        monkeypatch.setattr(sc_mod, "_skill_commands_roots", None)
        monkeypatch.setattr(sc_mod, "_resolve_skill_commands_platform", lambda: None)
        monkeypatch.setattr(sc_mod, "_resolve_skill_commands_environment", lambda: ())

        assert get_skill_commands() == {}
        assert sc_mod._skill_commands == {}
        assert sc_mod._skill_commands_roots is None

    def test_deleted_external_root_refuses_old_scope_then_rebuilds_new_scope(
        self, monkeypatch, tmp_path
    ):
        """A removed configured external root cannot be hidden by a cached map."""
        import agent.skill_commands as sc_mod

        primary_root = tmp_path / "skills"
        external_root = tmp_path / "external"
        _make_skill(primary_root, "local")
        external_skill_dir = _make_skill(external_root, "external")
        current_external_roots = (external_root,)

        def resolve_roots():
            roots = (primary_root, *current_external_roots)
            return (
                primary_root,
                current_external_roots,
                tuple(str(root.resolve()) for root in roots),
            )

        monkeypatch.setattr(sc_mod, "_resolve_skill_command_roots", resolve_roots)
        monkeypatch.setattr(sc_mod, "_skill_commands", {})
        monkeypatch.setattr(sc_mod, "_skill_commands_platform", None)
        monkeypatch.setattr(sc_mod, "_skill_commands_environment", None)
        monkeypatch.setattr(sc_mod, "_skill_commands_roots", None)
        monkeypatch.setattr(sc_mod, "_resolve_skill_commands_platform", lambda: None)
        monkeypatch.setattr(sc_mod, "_resolve_skill_commands_environment", lambda: ())

        assert set(get_skill_commands()) == {"/local", "/external"}

        (external_skill_dir / "SKILL.md").unlink()
        external_skill_dir.rmdir()
        external_root.rmdir()
        assert get_skill_commands() == {}
        assert set(sc_mod._skill_commands) == {"/local", "/external"}

        # The following root-config resolution defines a new scope.  It can
        # build the still-available local root without reusing old external
        # entries.
        current_external_roots = ()
        assert set(get_skill_commands()) == {"/local"}

    def test_unreadable_skill_preserves_last_good_catalog(
        self, tmp_path, caplog
    ):
        import agent.skill_commands as sc_mod

        previous = {
            "/stable": {
                "name": "stable",
                "description": "Stable.",
                "skill_md_path": "/stable/SKILL.md",
                "skill_dir": "/stable",
            }
        }
        unreadable = tmp_path / "missing" / "SKILL.md"
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch.object(sc_mod, "_skill_commands", previous),
            patch.object(sc_mod, "_skill_commands_roots", (str(tmp_path.resolve()),)),
            patch(
                "agent.skill_utils.iter_skill_index_files",
                return_value=iter([unreadable]),
            ),
            caplog.at_level("WARNING", logger="agent.skill_commands"),
        ):
            result = scan_skill_commands()

        assert result == previous
        assert any("scan was incomplete" in r.message for r in caplog.records)

    def test_incomplete_first_scan_does_not_cache_partial_catalog(
        self, tmp_path, caplog
    ):
        import agent.skill_commands as sc_mod

        readable = _make_skill(tmp_path, "readable")
        unreadable = tmp_path / "missing" / "SKILL.md"
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch.object(sc_mod, "_skill_commands", {}),
            patch(
                "agent.skill_utils.iter_skill_index_files",
                return_value=iter([readable, unreadable]),
            ),
            caplog.at_level("WARNING", logger="agent.skill_commands"),
        ):
            result = scan_skill_commands()

        assert result == {}
        assert any("scan was incomplete" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        ("old_platform", "new_platform", "old_environment", "new_environment"),
        [
            ("telegram", "discord", (), ()),
            (
                None,
                None,
                (("kanban", True),),
                (("kanban", False),),
            ),
        ],
    )
    def test_failed_cross_scope_scan_never_returns_stale_catalog(
        self,
        tmp_path,
        old_platform,
        new_platform,
        old_environment,
        new_environment,
    ):
        import agent.skill_commands as sc_mod

        previous = {
            "/scope-only": {
                "name": "scope-only",
                "description": "Only valid in the previous scope.",
                "skill_md_path": "/old/SKILL.md",
                "skill_dir": "/old",
            }
        }
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch.object(sc_mod, "_skill_commands", previous),
            patch.object(sc_mod, "_skill_commands_platform", old_platform),
            patch.object(
                sc_mod,
                "_skill_commands_environment",
                old_environment,
            ),
            patch.object(
                sc_mod,
                "_resolve_skill_commands_platform",
                return_value=new_platform,
            ),
            patch.object(
                sc_mod,
                "_resolve_skill_commands_environment",
                return_value=new_environment,
            ),
            patch(
                "agent.skill_utils.iter_skill_index_files",
                side_effect=OSError("temporary scan failure"),
            ),
        ):
            result = get_skill_commands()
            assert sc_mod._skill_commands == previous
            assert sc_mod._skill_commands_platform == old_platform
            assert sc_mod._skill_commands_environment == old_environment

        assert result == {}

    def test_partial_walk_failure_is_not_cached_and_retries_cross_scope(
        self, tmp_path, monkeypatch
    ):
        import agent.skill_commands as sc_mod
        from agent import skill_utils

        good = _make_skill(tmp_path, "good")
        bad = tmp_path / "bad"
        bad.mkdir()
        previous = {
            "/scope-only": {
                "name": "scope-only",
                "description": "Only valid in the previous scope.",
                "skill_md_path": "/old/SKILL.md",
                "skill_dir": "/old",
            }
        }
        bad_stat_attempts = 0

        def fake_walk(root, *, followlinks, onerror):
            assert root == str(tmp_path)
            assert followlinks is True
            assert callable(onerror)
            yield str(tmp_path), ["good", "bad"], []
            yield str(good), [], ["SKILL.md"]
            yield str(bad), [], []

        def fake_stat(path, *, follow_symlinks):
            nonlocal bad_stat_attempts
            if path == str(bad):
                bad_stat_attempts += 1
                raise OSError("temporary subtree failure")
            return os.stat(path, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(
            skill_utils,
            "os",
            SimpleNamespace(walk=fake_walk, stat=fake_stat, path=os.path),
        )
        monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", tmp_path)
        monkeypatch.setattr(sc_mod, "_skill_commands", previous)
        monkeypatch.setattr(sc_mod, "_skill_commands_platform", "telegram")
        monkeypatch.setattr(sc_mod, "_skill_commands_environment", ())
        monkeypatch.setattr(
            sc_mod, "_resolve_skill_commands_platform", lambda: "discord"
        )
        monkeypatch.setattr(
            sc_mod, "_resolve_skill_commands_environment", lambda: ()
        )

        assert get_skill_commands() == {}
        assert get_skill_commands() == {}
        assert bad_stat_attempts == 2
        assert sc_mod._skill_commands == previous
        assert sc_mod._skill_commands_platform == "telegram"
        assert sc_mod._skill_commands_environment == ()

    def test_excludes_incompatible_platform(self, tmp_path):
        """macOS-only skills should not register slash commands on Linux."""
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("agent.skill_utils.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            _make_skill(tmp_path, "imessage", frontmatter_extra="platforms: [macos]\n")
            _make_skill(tmp_path, "web-search")
            result = scan_skill_commands()
        assert "/web-search" in result
        assert "/imessage" not in result





    def test_loads_skill_invocation_from_symlinked_skill_dir(self, tmp_path):
        """Slash commands should load skills symlinked under the local skills dir."""
        external_root = tmp_path / "external"
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        real_skill_dir = _make_skill(
            external_root,
            "impeccable",
            body="Apply impeccable design craft.",
        )
        symlink_path = skills_root / "impeccable"
        try:
            symlink_path.symlink_to(real_skill_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable in test environment: {exc}")

        with patch("tools.skills_tool.SKILLS_DIR", skills_root):
            result = scan_skill_commands()
            message = build_skill_invocation_message("/impeccable")

        assert "/impeccable" in result
        assert message is not None
        assert "Apply impeccable design craft." in message

    def test_get_skill_commands_rescans_when_platform_scope_changes(self, tmp_path):
        """Platform-specific disabled-skill caches must not leak across platforms.

        Regression test for #14536: a gateway process serving Telegram
        and Discord concurrently would seed the process-global cache
        with whichever platform scanned first, and subsequent
        ``get_skill_commands()`` calls from the other platform silently
        inherited that filter.
        """
        import agent.skill_commands as sc_mod
        from agent.skill_commands import get_skill_commands

        def _disabled_skills():
            platform = os.getenv("HERMES_PLATFORM")
            if platform == "telegram":
                return {"telegram-only"}
            if platform == "discord":
                return {"discord-only"}
            return set()

        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._get_disabled_skill_names", side_effect=_disabled_skills),
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
        ):
            _make_skill(tmp_path, "shared")
            _make_skill(tmp_path, "telegram-only")
            _make_skill(tmp_path, "discord-only")

            with patch.dict(os.environ, {"HERMES_PLATFORM": "telegram"}):
                telegram_commands = dict(get_skill_commands())

            assert "/shared" in telegram_commands
            assert "/discord-only" in telegram_commands
            assert "/telegram-only" not in telegram_commands

            with patch.dict(os.environ, {"HERMES_PLATFORM": "discord"}):
                discord_commands = dict(get_skill_commands())

            assert "/shared" in discord_commands
            assert "/telegram-only" in discord_commands
            assert "/discord-only" not in discord_commands

            # Switching back to telegram must also rescan — not re-serve
            # the discord view that was just cached.
            with patch.dict(os.environ, {"HERMES_PLATFORM": "telegram"}):
                telegram_again = dict(get_skill_commands())

            assert "/telegram-only" not in telegram_again
            assert "/discord-only" in telegram_again

    def test_get_skill_commands_rescans_when_session_platform_changes(self, tmp_path):
        """``HERMES_SESSION_PLATFORM`` from the gateway session context must
        also trigger a rescan, not just ``HERMES_PLATFORM`` (#14536).

        Exercises the real ContextVar path: the gateway sets the active
        adapter via ``set_session_vars(platform=...)`` and the resolver
        reads it via ``get_session_env``. Setting ``HERMES_SESSION_PLATFORM``
        in ``os.environ`` would only test ``get_session_env``'s legacy
        env-var fallback — a regression that swapped ``get_session_env``
        for plain ``os.getenv`` would still pass while breaking concurrent
        gateway sessions, which is the bug the ContextVar plumbing exists
        to prevent in the first place.
        """
        import agent.skill_commands as sc_mod
        from agent.skill_commands import get_skill_commands
        from gateway.session_context import (
            clear_session_vars,
            get_session_env,
            set_session_vars,
        )

        def _disabled_skills():
            platform = (
                os.getenv("HERMES_PLATFORM")
                or get_session_env("HERMES_SESSION_PLATFORM")
            )
            if platform == "telegram":
                return {"telegram-only"}
            if platform == "discord":
                return {"discord-only"}
            return set()

        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._get_disabled_skill_names", side_effect=_disabled_skills),
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
        ):
            _make_skill(tmp_path, "shared")
            _make_skill(tmp_path, "telegram-only")
            _make_skill(tmp_path, "discord-only")

            # First simulated gateway request: telegram handler.
            tokens = set_session_vars(platform="telegram")
            try:
                telegram_commands = dict(get_skill_commands())
            finally:
                clear_session_vars(tokens)

            assert "/shared" in telegram_commands
            assert "/discord-only" in telegram_commands
            assert "/telegram-only" not in telegram_commands

            # Second simulated gateway request: discord handler. The cache
            # was just populated for telegram; the rescan trigger must fire
            # off the ContextVar change, not just an env-var change.
            tokens = set_session_vars(platform="discord")
            try:
                discord_commands = dict(get_skill_commands())
            finally:
                clear_session_vars(tokens)

            assert "/shared" in discord_commands
            assert "/telegram-only" in discord_commands
            assert "/discord-only" not in discord_commands

    def test_get_skill_commands_rescans_when_leaving_platform_scope(self, tmp_path, monkeypatch):
        """Returning to no-platform-scope (CLI / cron / RL) after a gateway
        session must rescan so the unfiltered view is repopulated (#14536).

        A long-lived process running both gateway sessions and bare CLI
        invocations would otherwise stay stuck on whichever platform's
        filter was last applied.
        """
        import agent.skill_commands as sc_mod
        from agent.skill_commands import get_skill_commands

        def _disabled_skills():
            if os.getenv("HERMES_PLATFORM") == "telegram":
                return {"telegram-only"}
            return set()

        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._get_disabled_skill_names", side_effect=_disabled_skills),
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
        ):
            _make_skill(tmp_path, "shared")
            _make_skill(tmp_path, "telegram-only")

            monkeypatch.setenv("HERMES_PLATFORM", "telegram")
            telegram_commands = dict(get_skill_commands())
            assert "/telegram-only" not in telegram_commands

            # Drop back to no platform scope — bare CLI / cron / RL rollouts.
            monkeypatch.delenv("HERMES_PLATFORM", raising=False)
            bare_commands = dict(get_skill_commands())

            assert "/telegram-only" in bare_commands
            assert sc_mod._skill_commands_platform is None


    def test_get_skill_commands_rescans_when_kanban_environment_changes(
        self, tmp_path, monkeypatch
    ):
        import agent.skill_commands as sc_mod
        from agent.skill_commands import get_skill_commands

        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
            patch.object(sc_mod, "_skill_commands_environment", None),
            patch(
                "tools.kanban_tools._profile_has_kanban_toolset",
                return_value=False,
            ),
        ):
            _make_skill(
                tmp_path,
                "kanban-only",
                frontmatter_extra="environments: [kanban]\n",
            )
            assert "/kanban-only" not in get_skill_commands()

            monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
            assert "/kanban-only" in get_skill_commands()

            monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
            assert "/kanban-only" not in get_skill_commands()





    # -- core-command collision guard (#31204 / #53450) ---------------------




    # -- inter-skill slug collision dedup (#50304 / #63305) ------------------

    def test_slug_collision_keeps_first_skill(self, tmp_path):
        """Two skills whose names normalize to the same slug do not clobber.

        ``git_helper`` and ``git-helper`` are distinct frontmatter names but
        both reduce to the ``/git-helper`` command. The first one scanned must
        keep the command rather than being silently overwritten by the second.
        """
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            # ``a-first`` sorts before ``z-second`` so the index walk visits the
            # underscore-named skill first; that one must win the slash command.
            first = tmp_path / "a-first"
            first.mkdir()
            (first / "SKILL.md").write_text(
                "---\nname: git_helper\ndescription: First skill.\n---\n\nBody.\n"
            )
            second = tmp_path / "z-second"
            second.mkdir()
            (second / "SKILL.md").write_text(
                "---\nname: git-helper\ndescription: Second skill.\n---\n\nBody.\n"
            )
            result = scan_skill_commands()
        assert "/git-helper" in result
        # First-wins: the entry resolves to the first skill, not the shadowing one.
        assert result["/git-helper"]["name"] == "git_helper"
        assert result["/git-helper"]["skill_dir"] == str(first)

    def test_slug_collision_warns(self, tmp_path, caplog):
        """A slug collision emits a warning so the user can diagnose the
        shadowed skill."""
        import logging as _logging
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            first = tmp_path / "a-first"
            first.mkdir()
            (first / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: First.\n---\n\nBody.\n"
            )
            second = tmp_path / "z-second"
            second.mkdir()
            (second / "SKILL.md").write_text(
                "---\nname: my_skill\ndescription: Second.\n---\n\nBody.\n"
            )
            with caplog.at_level(_logging.WARNING, logger="agent.skill_commands"):
                scan_skill_commands()
        assert any("already claimed" in r.message for r in caplog.records)


class TestResolveSkillCommandKey:
    """Telegram bot-command names disallow hyphens, so the menu registers
    skills with hyphens swapped for underscores. When Telegram autocomplete
    sends the underscored form back, we need to find the hyphenated key.
    """

    def test_hyphenated_form_matches_directly(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "claude-code")
            scan_skill_commands()
            assert resolve_skill_command_key("claude-code") == "/claude-code"



    def test_unknown_command_returns_none(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "claude-code")
            scan_skill_commands()
            assert resolve_skill_command_key("does_not_exist") is None
            assert resolve_skill_command_key("does-not-exist") is None




class TestBuildPreloadedSkillsPrompt:
    def test_builds_prompt_for_multiple_named_skills(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "first-skill")
            _make_skill(tmp_path, "second-skill")
            prompt, loaded, missing = build_preloaded_skills_prompt(
                ["first-skill", "second-skill"]
            )

        assert missing == []
        assert loaded == ["first-skill", "second-skill"]
        assert "first-skill" in prompt
        assert "second-skill" in prompt
        assert "preloaded" in prompt.lower()


    def test_skips_disabled_skill(self, tmp_path, monkeypatch):
        """A globally-disabled skill must not be force-loaded via -s /
        HERMES_TUI_SKILLS preloading (mirrors the bundle gate, #59156)."""
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "enabled-skill", body="Enabled content.")
            _make_skill(tmp_path, "disabled-skill", body="SECRET DISABLED CONTENT.")

            import agent.skill_utils as su_module
            monkeypatch.setattr(
                su_module, "get_disabled_skill_names", lambda platform=None: {"disabled-skill"}
            )

            prompt, loaded, missing = build_preloaded_skills_prompt(
                ["enabled-skill", "disabled-skill"]
            )

        assert loaded == ["enabled-skill"]
        assert missing == ["disabled-skill"]
        assert "SECRET DISABLED CONTENT." not in prompt
        assert "enabled-skill" in prompt



class TestBuildSkillInvocationMessage:




    def test_uses_shared_skill_loader_for_secure_setup(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TENOR_API_KEY", raising=False)
        calls = []

        def fake_secret_callback(var_name, prompt, metadata=None):
            calls.append((var_name, prompt, metadata))
            os.environ[var_name] = "stored-in-test"
            return {
                "success": True,
                "stored_as": var_name,
                "validated": False,
                "skipped": False,
            }

        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            fake_secret_callback,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "test-skill",
                frontmatter_extra=(
                    "required_environment_variables:\n"
                    "  - name: TENOR_API_KEY\n"
                    "    prompt: Tenor API key\n"
                ),
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/test-skill", "do stuff")

        assert msg is not None
        assert "test-skill" in msg
        assert len(calls) == 1
        assert calls[0][0] == "TENOR_API_KEY"

    def test_gateway_still_loads_skill_but_returns_setup_guidance(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("TENOR_API_KEY", raising=False)

        def fail_if_called(var_name, prompt, metadata=None):
            raise AssertionError(
                "gateway flow should not try secure in-band secret capture"
            )

        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            fail_if_called,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            from gateway.session_context import clear_session_vars, set_session_vars

            tokens = set_session_vars(platform="telegram")
            try:
                _make_skill(
                    tmp_path,
                    "test-skill",
                    frontmatter_extra=(
                        "required_environment_variables:\n"
                        "  - name: TENOR_API_KEY\n"
                        "    prompt: Tenor API key\n"
                    ),
                )
                scan_skill_commands()
                msg = build_skill_invocation_message("/test-skill", "do stuff")
            finally:
                clear_session_vars(tokens)

        assert msg is not None
        assert "local cli" in msg.lower()


    def test_supporting_file_hint_uses_file_path_argument(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "test-skill")
            references = skill_dir / "references"
            references.mkdir()
            (references / "api.md").write_text("reference")
            scan_skill_commands()
            msg = build_skill_invocation_message("/test-skill", "do stuff")

        assert msg is not None
        assert 'file_path="<path>"' in msg


class TestSkillDirectoryHeader:
    """Bound slash payloads must not advertise a mutable executable path."""

    def test_header_contains_absolute_skill_dir(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "abs-dir-skill")
            scan_skill_commands()
            msg = build_skill_invocation_message("/abs-dir-skill", "go")

        assert msg is not None
        assert f"[Skill directory: {skill_dir}]" not in msg
        assert "Resolve any relative paths" not in msg

    def test_supporting_files_shown_with_absolute_paths(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "scripted-skill")
            (skill_dir / "scripts").mkdir()
            (skill_dir / "scripts" / "run.js").write_text("console.log('hi')")
            scan_skill_commands()
            msg = build_skill_invocation_message("/scripted-skill")

        assert msg is not None
        # Bound packages expose only relative support paths.  A subsequent
        # skill_view request obtains a fresh checked snapshot rather than
        # executing a path that may have been replaced since activation.
        assert "scripts/run.js" in msg
        assert str(skill_dir / "scripts" / "run.js") not in msg
        assert f"node {skill_dir}/scripts/foo.js" not in msg

    def test_supporting_file_preview_is_bounded(self, tmp_path):
        limit = skills_tool_module.MAX_LINKED_FILES_PER_CATEGORY
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "asset-heavy")
            assets = skill_dir / "assets"
            assets.mkdir()
            for index in range(limit + 5):
                (assets / f"{index:03d}.txt").write_text("asset")
            scan_skill_commands()
            msg = build_skill_invocation_message("/asset-heavy")

        assert msg is not None
        assert "Supporting-file preview truncated" in msg
        assert f"assets/{limit - 1:03d}.txt" in msg
        assert f"assets/{limit + 4:03d}.txt" not in msg


class TestTemplateVarSubstitution:
    """``${HERMES_SKILL_DIR}`` and ``${HERMES_SESSION_ID}`` in SKILL.md body
    are replaced before the agent sees the content."""

    def test_substitutes_skill_dir(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(
                tmp_path,
                "templated",
                body="Run: node ${HERMES_SKILL_DIR}/scripts/foo.js",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/templated")

        assert msg is not None
        assert f"node {skill_dir}/scripts/foo.js" in msg
        # The literal template token must not leak through.
        assert "${HERMES_SKILL_DIR}" not in msg.split("[Skill directory:")[0]



    def test_disable_template_vars_via_config(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_preprocessing.load_skills_config",
                return_value={"template_vars": False},
            ),
        ):
            _make_skill(
                tmp_path,
                "no-sub",
                body="Run: node ${HERMES_SKILL_DIR}/scripts/foo.js",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/no-sub")

        assert msg is not None
        # Template token must survive when substitution is disabled.
        assert "${HERMES_SKILL_DIR}/scripts/foo.js" in msg


class TestInlineShellExpansion:
    def test_quoted_false_does_not_enable_inline_shell(self):
        from agent.skill_preprocessing import preprocess_skill_content

        result = preprocess_skill_content(
            "Value: !`printf EXECUTED`",
            None,
            skills_cfg={"inline_shell": "false"},
        )

        assert result == "Value: !`printf EXECUTED`"

    def test_inline_shell_is_off_by_default(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "dyn-default-off",
                body="Today is !`echo INLINE_RAN`.",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/dyn-default-off")

        assert msg is not None
        # Default config has inline_shell=False — snippet must stay literal.
        assert "!`echo INLINE_RAN`" in msg
        assert "Today is INLINE_RAN." not in msg

    def test_inline_shell_runs_when_enabled(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_preprocessing.load_skills_config",
                return_value={"template_vars": True, "inline_shell": True,
                              "inline_shell_timeout": 5},
            ),
        ):
            _make_skill(
                tmp_path,
                "dyn-on",
                body="Marker: !`echo INLINE_RAN`.",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/dyn-on")

        assert msg is not None
        assert "Marker: INLINE_RAN." in msg
        assert "!`echo INLINE_RAN`" not in msg

    def test_inline_shell_runs_in_skill_directory(self, tmp_path):
        """Inline snippets get the skill dir as CWD so relative paths work."""
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_preprocessing.load_skills_config",
                return_value={"template_vars": True, "inline_shell": True,
                              "inline_shell_timeout": 5},
            ),
        ):
            skill_dir = _make_skill(
                tmp_path,
                "dyn-cwd",
                body="Here: !`pwd`",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/dyn-cwd")

        assert msg is not None
        assert f"Here: {skill_dir}" in msg

    def test_inline_shell_timeout_does_not_break_message(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_preprocessing.load_skills_config",
                return_value={"template_vars": True, "inline_shell": True,
                              "inline_shell_timeout": 1},
            ),
        ):
            _make_skill(
                tmp_path,
                "dyn-slow",
                body="Slow: !`sleep 5 && printf DYN_MARKER`",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/dyn-slow")

        assert msg is not None
        # Timeout is surfaced as a marker instead of propagating as an error,
        # and the rest of the skill message still renders.
        assert "inline-shell timeout" in msg
        # The command's intended stdout never made it through — only the
        # timeout marker (which echoes the command text) survives.
        assert "DYN_MARKER" not in msg.replace("sleep 5 && printf DYN_MARKER", "")


class TestBoundInvocationRendering:
    """Slash-family callers must never re-open a package by its old path."""

    def _make_swappable_skill(self, tmp_path, name="bound"):
        skills_root = tmp_path / "skills"
        packages = tmp_path / "packages"
        original_category = packages / "original"
        replacement_category = packages / "replacement"
        skills_root.mkdir()
        original = _make_skill(
            original_category, name, body="Marker: !`cat marker.txt`"
        )
        replacement = replacement_category / name
        replacement.mkdir(parents=True)
        # Same SKILL.md identity keeps discovery stable; only the package
        # support file differs, which is what the inline shell observes.
        os.link(original / "SKILL.md", replacement / "SKILL.md")
        (original / "marker.txt").write_text("ORIGINAL", encoding="utf-8")
        (replacement / "marker.txt").write_text("REPLACEMENT", encoding="utf-8")
        category_link = skills_root / "linked"
        try:
            category_link.symlink_to(original_category, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        return skills_root, category_link, replacement_category

    def _swap_config(self, category_link, replacement_category):
        swapped = False

        def config():
            nonlocal swapped
            if not swapped:
                swapped = True
                category_link.unlink()
                category_link.symlink_to(replacement_category, target_is_directory=True)
            return {
                "template_vars": True,
                "inline_shell": True,
                "inline_shell_timeout": 5,
            }

        return config, lambda: swapped

    def test_slash_inline_shell_stays_in_discovered_package(self, tmp_path):
        skills_root, category_link, replacement_category = self._make_swappable_skill(
            tmp_path, "slash-bound"
        )
        config, was_swapped = self._swap_config(category_link, replacement_category)
        with (
            patch("tools.skills_tool.SKILLS_DIR", skills_root),
            patch("agent.skill_utils.get_external_skills_dirs", return_value=[]),
            patch("agent.skill_preprocessing.load_skills_config", side_effect=config),
        ):
            scan_skill_commands()
            msg = build_skill_invocation_message("/slash-bound")

        assert was_swapped() is True
        assert "Marker: ORIGINAL" in msg
        assert "REPLACEMENT" not in msg

    def test_stacked_inline_shell_stays_in_discovered_package(self, tmp_path):
        from agent.skill_commands import build_stacked_skill_invocation_message

        skills_root, category_link, replacement_category = self._make_swappable_skill(
            tmp_path, "stacked-bound"
        )
        _make_skill(skills_root, "plain", body="Plain guidance.")
        config, was_swapped = self._swap_config(category_link, replacement_category)
        with (
            patch("tools.skills_tool.SKILLS_DIR", skills_root),
            patch("agent.skill_utils.get_external_skills_dirs", return_value=[]),
            patch("agent.skill_preprocessing.load_skills_config", side_effect=config),
        ):
            scan_skill_commands()
            result = build_stacked_skill_invocation_message(
                ["/stacked-bound", "/plain"]
            )

        assert result is not None
        assert was_swapped() is True
        assert "Marker: ORIGINAL" in result[0]
        assert "REPLACEMENT" not in result[0]

    def test_preload_inline_shell_stays_in_discovered_package(self, tmp_path):
        skills_root, category_link, replacement_category = self._make_swappable_skill(
            tmp_path, "preload-bound"
        )
        config, was_swapped = self._swap_config(category_link, replacement_category)
        with (
            patch("tools.skills_tool.SKILLS_DIR", skills_root),
            patch("agent.skill_utils.get_external_skills_dirs", return_value=[]),
            patch("agent.skill_preprocessing.load_skills_config", side_effect=config),
        ):
            prompt, loaded, missing = build_preloaded_skills_prompt(["preload-bound"])

        assert was_swapped() is True
        assert loaded == ["preload-bound"]
        assert missing == []
        assert "Marker: ORIGINAL" in prompt
        assert "REPLACEMENT" not in prompt


class TestStackedSkillCommands:
    """Stacked slash-skill invocations — inspired by Claude Code v2.1.199."""

    def _setup_three_skills(self, tmp_path):
        _make_skill(tmp_path, "skill-a", body="Body A.")
        _make_skill(tmp_path, "skill-b", body="Body B.")
        _make_skill(tmp_path, "skill-c", body="Body C.")


    def test_split_stops_at_non_skill_token(self, tmp_path):
        from agent.skill_commands import split_stacked_skill_commands
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            self._setup_three_skills(tmp_path)
            scan_skill_commands()
            keys, instruction = split_stacked_skill_commands(
                "/skill-b /not-a-skill /skill-c hello"
            )
        assert keys == ["/skill-b"]
        # Parsing stops at the first unresolvable token; everything from
        # there on is the user instruction (slash included).
        assert instruction == "/not-a-skill /skill-c hello"



    def test_split_caps_at_five_total(self, tmp_path):
        from agent.skill_commands import split_stacked_skill_commands
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            for i in range(7):
                _make_skill(tmp_path, f"stk-{i}")
            scan_skill_commands()
            rest = " ".join(f"/stk-{i}" for i in range(1, 7)) + " run"
            keys, instruction = split_stacked_skill_commands(rest)
        # First skill was already consumed by the caller — split returns at
        # most 4 extras so the total stays at 5.
        assert len(keys) == 4
        assert instruction.startswith("/stk-5")



    def test_stacked_message_skips_missing_skills(self, tmp_path):
        from agent.skill_commands import build_stacked_skill_invocation_message
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            self._setup_three_skills(tmp_path)
            scan_skill_commands()
            result = build_stacked_skill_invocation_message(
                ["/skill-a", "/gone"], "go"
            )
        assert result is not None
        msg, loaded, missing = result
        assert loaded == ["skill-a"]
        assert missing == ["gone"]
        assert "Skills missing (skipped): gone" in msg

