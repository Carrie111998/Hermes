"""Regression tests for #93862: profile resolution for symlink-overlay homes.

Multi-agent orchestration platforms isolate each task's sessions/logs/cache in
a per-task HERMES_HOME while sharing a named profile's skills/plugins/cron/
SOUL via symlinks::

    HERMES_HOME=/workspaces/<task>/hermes-home
      ├── SOUL.md   -> <root>/profiles/workflow_evaluator/SOUL.md
      ├── plugins/  -> <root>/profiles/workflow_evaluator/plugins
      └── cron/     -> <root>/profiles/workflow_evaluator/cron

The profile name is baked into every symlink target, but resolution only
pattern-matched the home path itself, so overlay homes reported "default".

Contract under test:
- direct profile dir still resolves (``<root>/profiles/X`` -> X)
- overlay home with symlinked members resolves to the backing profile
- overlay with plain COPIED files (no symlinks) stays "default"
- root home stays "default"
- both consumers (system prompt hint, cross-profile write guard) agree
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink creation requires elevated privileges on Windows",
)


@pytest.fixture()
def profile_farm(tmp_path, monkeypatch):
    """A fake hermes root with a named profile and an overlay home."""
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "workflow_evaluator"
    (profile / "plugins").mkdir(parents=True)
    (profile / "cron").mkdir()
    (profile / "SOUL.md").write_text("# soul\n")

    overlay = tmp_path / "workspaces" / "task-1" / "hermes-home"
    overlay.mkdir(parents=True)
    (overlay / "SOUL.md").symlink_to(profile / "SOUL.md")
    (overlay / "plugins").symlink_to(profile / "plugins")
    (overlay / "cron").symlink_to(profile / "cron")
    # Non-shared, task-local state alongside the symlinks:
    (overlay / "sessions").mkdir()

    # Make the fake root authoritative for get_default_hermes_root():
    # HERMES_HOME outside the native home, parent not named "profiles",
    # so the env path itself is treated as the root... except we want the
    # overlay as HOME. get_default_hermes_root() derives the root from
    # HERMES_HOME, so for overlay layouts the recovered name comes from
    # the symlink targets, not the root relation — which is the point.
    monkeypatch.setenv("HERMES_HOME", str(overlay))
    # Reset memos that cache root/home resolution.
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    return root, profile, overlay


def test_direct_profile_dir_resolves(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_constants import profile_name_for_home

    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "milo"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    assert profile_name_for_home(profile) == "milo"


def test_overlay_home_recovers_profile_from_symlinks(profile_farm):
    from hermes_constants import profile_name_for_home

    _root, _profile, overlay = profile_farm
    assert profile_name_for_home(overlay) == "workflow_evaluator"


def test_overlay_with_copied_files_stays_default(tmp_path, monkeypatch):
    """Plain copies identify nothing — no symlink, no recovery."""
    import hermes_constants
    from hermes_constants import profile_name_for_home

    overlay = tmp_path / "copied-home"
    (overlay / "plugins").mkdir(parents=True)
    (overlay / "SOUL.md").write_text("# soul\n")
    monkeypatch.setenv("HERMES_HOME", str(overlay))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    assert profile_name_for_home(overlay) == "default"


def test_root_home_stays_default(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_constants import profile_name_for_home

    root = tmp_path / "hermes-root"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    assert profile_name_for_home(root) == "default"


def test_symlinked_home_itself_still_resolves(tmp_path, monkeypatch):
    """A HERMES_HOME that is itself a symlink TO a profile dir resolves via
    path resolution (pre-existing behavior, must not regress)."""
    import hermes_constants
    from hermes_constants import profile_name_for_home

    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "milo"
    profile.mkdir(parents=True)
    link = tmp_path / "home-link"
    link.symlink_to(profile)
    monkeypatch.setenv("HERMES_HOME", str(link))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    assert profile_name_for_home(link) == "milo"


def test_system_prompt_and_file_safety_agree_on_overlay(profile_farm, monkeypatch):
    """Both consumers of profile resolution must report the same name for an
    overlay home: the prompt hint (#93862's symptom) and the cross-profile
    write guard (same bug class — a mismatch would misclassify the session's
    own profile writes)."""
    _root, _profile, overlay = profile_farm

    from agent.system_prompt import _profile_name_for_home as prompt_name
    import agent.file_safety as fs

    monkeypatch.setattr(fs, "_hermes_home_path", lambda: overlay)

    assert prompt_name(overlay) == "workflow_evaluator"
    assert fs._resolve_active_profile_name() == "workflow_evaluator"


def test_disagreeing_symlinks_stay_default(profile_farm, tmp_path):
    """If overlay members point into DIFFERENT profiles, the overlay is
    ambiguous (or crafted) and the conservative answer is default — a single
    stale or malicious link must not reassign the guard's active side."""
    from hermes_constants import profile_name_for_home

    root, _profile, overlay = profile_farm
    other = root / "profiles" / "victim"
    (other / "skills").mkdir(parents=True)
    (overlay / "skills").symlink_to(other / "skills")

    assert profile_name_for_home(overlay) == "default"


def test_broken_symlink_identifies_nothing(tmp_path, monkeypatch):
    """A dangling symlink must not supply a profile name (resolve must be
    strict)."""
    import hermes_constants
    from hermes_constants import profile_name_for_home

    overlay = tmp_path / "broken-home"
    overlay.mkdir()
    (overlay / "SOUL.md").symlink_to(
        tmp_path / "hermes-root" / "profiles" / "ghost" / "SOUL.md"
    )
    monkeypatch.setenv("HERMES_HOME", str(overlay))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    assert profile_name_for_home(overlay) == "default"


def test_nested_profiles_dir_in_path_identifies_nothing(tmp_path, monkeypatch):
    """Only the exact tail ``profiles/<name>/<member>`` identifies a profile.
    A ``profiles`` directory elsewhere in the target path must not — e.g.
    ``.../profiles/archive/root/plugins`` is not profile "archive"."""
    import hermes_constants
    from hermes_constants import profile_name_for_home

    deep = tmp_path / "profiles" / "archive" / "root" / "plugins"
    deep.mkdir(parents=True)
    overlay = tmp_path / "nested-home"
    overlay.mkdir()
    (overlay / "plugins").symlink_to(deep)
    monkeypatch.setenv("HERMES_HOME", str(overlay))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    # Target tail is root/plugins under .../profiles/archive/..., NOT
    # profiles/<name>/plugins — identifies nothing.
    assert profile_name_for_home(overlay) == "default"


class TestOverlayCrossProfileGuardEndToEnd:
    """The guard itself, not just name agreement: an overlay session must be
    able to write its OWN backing profile unwarned, while writes into other
    profiles' scoped areas are still classified — even though the env-derived
    root is the overlay dir, not the backing root (#93862)."""

    @pytest.fixture()
    def guard(self, profile_farm, monkeypatch):
        import agent.file_safety as fs

        _root, _profile, overlay = profile_farm
        monkeypatch.setattr(fs, "_hermes_home_path", lambda: overlay)
        return fs

    def test_own_backing_profile_write_is_in_profile(self, guard, profile_farm):
        _root, profile, _overlay = profile_farm
        target = profile / "skills" / "new-skill" / "SKILL.md"
        assert guard.classify_cross_profile_target(str(target)) is None

    def test_other_profile_write_is_flagged(self, guard, profile_farm):
        root, _profile, _overlay = profile_farm
        other = root / "profiles" / "victim" / "skills" / "x.md"
        result = guard.classify_cross_profile_target(str(other))
        assert result is not None
        assert result["active_profile"] == "workflow_evaluator"
        assert result["target_profile"] == "victim"
        assert result["area"] == "skills"

    def test_default_profile_area_write_is_flagged(self, guard, profile_farm):
        root, _profile, _overlay = profile_farm
        default_area = root / "skills" / "x.md"
        result = guard.classify_cross_profile_target(str(default_area))
        assert result is not None
        assert result["active_profile"] == "workflow_evaluator"
        assert result["target_profile"] == "default"

    def test_unrelated_path_is_not_classified(self, guard, tmp_path):
        assert guard.classify_cross_profile_target(str(tmp_path / "code" / "a.py")) is None


class TestMixedOverlayPinnedBehavior:
    """Pin the intended semantics for imperfect overlays: NOISE (dangling or
    malformed links) is ignored when the valid links are unanimous — overlay
    decay is legitimate — while a valid CONFLICT always resolves to default.
    """

    def test_valid_links_plus_dangling_still_resolve(self, profile_farm):
        """A dangling extra link (e.g. a pruned profile area) must not nuke
        an otherwise-unanimous identity."""
        from hermes_constants import profile_name_for_home

        root, _profile, overlay = profile_farm
        (overlay / "memories").symlink_to(
            root / "profiles" / "workflow_evaluator" / "memories"  # does not exist
        )
        assert profile_name_for_home(overlay) == "workflow_evaluator"

    def test_valid_links_plus_malformed_target_still_resolve(self, profile_farm, tmp_path):
        """A link whose target exists but has no profiles/<name>/<member>
        tail identifies nothing and is ignored."""
        from hermes_constants import profile_name_for_home

        _root, _profile, overlay = profile_farm
        elsewhere = tmp_path / "shared-scratch"
        elsewhere.mkdir()
        (overlay / "hooks").symlink_to(elsewhere)
        assert profile_name_for_home(overlay) == "workflow_evaluator"

    def test_valid_conflict_beats_noise_and_resolves_default(self, profile_farm, tmp_path):
        """Conflicting VALID identities resolve to default even alongside
        additional noise links."""
        from hermes_constants import profile_name_for_home

        root, _profile, overlay = profile_farm
        other = root / "profiles" / "victim"
        (other / "skills").mkdir(parents=True)
        (overlay / "skills").symlink_to(other / "skills")
        (overlay / "memories").symlink_to(root / "nowhere")  # dangling noise
        assert profile_name_for_home(overlay) == "default"
