"""``gateway/run.py`` must resolve HERMES_HOME at CALL time, not at import.

``_hermes_home = get_hermes_home()`` used to run at module import — i.e. at test
COLLECTION time, before ``tests/conftest.py``'s autouse fixture redirects
HERMES_HOME to a per-test tempdir. Every unpatched use site therefore pointed at
the user's real ``~/.hermes``, and the write sites (voice-mode state, the
``.clean_shutdown`` marker, the update/restart markers, and ``mark_seen`` ->
``atomic_config_write`` on ``config.yaml``) wrote there.

The same bug class destroyed the user's real ``~/.hermes/config.yaml`` on
2026-08-11 via ``tui_gateway/server.py::_save_cfg``. Three constants were derived
from this one — a CHAIN of import-time snapshots — so fixing the root alone would
have fixed nothing.

Resolver choice is deliberate: ``get_process_hermes_home()``, NOT
``get_hermes_home()``. No context override is ever active at module import, so
the process-level resolver is the exact live equivalent of what the old constant
captured. It also keeps ``_gateway_config_home()`` meaningful — that helper
layers ``get_hermes_home_override()`` on top of this value, which only makes
sense if this value is itself override-free.
"""

import os
from pathlib import Path

import pytest

import gateway.run as run


# --------------------------------------------------------------------------
# SAFETY GUARD — this bug class cannot be tested safely without it.
#
# For this defect "the test fails" and "the user's live file is gone" are the
# SAME EVENT: the whole point is that the constant holds the real home. An
# unguarded red run destroyed the real ~/.hermes/config.yaml on 2026-08-11.
#
# Reads through the seam when it exists and the bare constant when it does not,
# so the guard still protects a bisect back onto the unfixed tree.
# --------------------------------------------------------------------------
def _effective_home() -> Path:
    resolver = getattr(run, "_resolve_hermes_home", None)
    if callable(resolver):
        return Path(resolver())
    return Path(getattr(run, "_hermes_home"))


@pytest.fixture(autouse=True)
def _never_touch_the_real_hermes_home():
    real = Path(os.path.expanduser("~")) / ".hermes"
    forbidden = {real.resolve(strict=False), (real / "profiles" / "main").resolve(strict=False)}
    try:
        effective = _effective_home().resolve(strict=False)
    except Exception as exc:  # pragma: no cover — a broken resolver is also unsafe
        pytest.fail(f"Could not resolve the effective Hermes home: {exc!r}")
    if effective in forbidden:
        pytest.fail(
            "REFUSING TO RUN: gateway.run resolves to the real Hermes home "
            f"({effective}). This test writes gateway state; running it here "
            "would clobber live files. Export a throwaway HERMES_HOME first."
        )
    yield


class TestRootSeam:
    def test_no_import_time_snapshot(self):
        """The module constant is a None sentinel, not a baked-in Path."""
        assert run._hermes_home is None, (
            "gateway.run._hermes_home holds a Path at import — that is the bug. "
            "It must default to None so resolution happens at call time."
        )

    def test_resolver_follows_a_live_env_flip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert run._resolve_hermes_home() == tmp_path

    def test_module_override_still_wins(self, tmp_path, monkeypatch):
        """The ~260 existing monkeypatch sites must keep working."""
        monkeypatch.setattr(run, "_hermes_home", tmp_path)
        assert run._resolve_hermes_home() == tmp_path

    def test_override_coerces_str_to_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run, "_hermes_home", str(tmp_path))
        assert run._resolve_hermes_home() == tmp_path
        assert isinstance(run._resolve_hermes_home(), Path)


class TestDerivedConstants:
    """Each derived constant was a SECOND snapshot and needed its own seam."""

    def _voice_prop(self):
        return run.GatewayRunner.__dict__["_VOICE_MODE_PATH"]

    def test_voice_mode_path_follows_a_live_env_flip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        class _Dummy(run.GatewayRunner):
            # Bypass the real (heavy) __init__ — we only need the property and
            # the one method under test.
            def __init__(self):
                pass

        got = self._voice_prop().fget(_Dummy())
        assert got == tmp_path / "gateway_voice_mode.json"

    def test_voice_mode_path_instance_override_still_works(self, tmp_path):
        """test_voice_command.py assigns this attribute directly."""

        class _Dummy(run.GatewayRunner):
            # Bypass the real (heavy) __init__ — we only need the property and
            # the one method under test.
            def __init__(self):
                pass

        d = _Dummy()
        custom = tmp_path / "custom_voice.json"
        self._voice_prop().fset(d, custom)
        assert self._voice_prop().fget(d) == custom

    def test_voice_mode_path_survives_patch_object_roundtrip(self, tmp_path, monkeypatch):
        """``patch.object`` restores a CLASS attribute with delattr, not setattr.

        test_voice_mode_platform_isolation.py uses this idiom. Without a
        deleter on the property its teardown raises
        "property has no deleter" — which is how the first version of this fix
        broke three tests.
        """
        from unittest.mock import patch

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        class _Dummy(run.GatewayRunner):
            def __init__(self):
                pass

        d = _Dummy()
        custom = tmp_path / "patched_voice.json"
        with patch.object(d, "_VOICE_MODE_PATH", custom):
            assert d._VOICE_MODE_PATH == custom
        # After teardown it must fall back to live resolution, not explode.
        assert d._VOICE_MODE_PATH == tmp_path / "gateway_voice_mode.json"

    def test_saving_voice_modes_writes_to_the_live_home(self, tmp_path, monkeypatch):
        """The real write, end to end — it must land in the live home."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        class _Dummy(run.GatewayRunner):
            # Bypass the real (heavy) __init__ — we only need the property and
            # the one method under test.
            def __init__(self):
                pass

        d = _Dummy()
        d._voice_mode = {"telegram:123": "all"}
        run.GatewayRunner._save_voice_modes(d)

        written = tmp_path / "gateway_voice_mode.json"
        assert written.exists(), "voice-mode state did not land in the live home"
        assert "telegram:123" in written.read_text()

    def test_planned_restart_marker_path_follows_the_live_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert run._planned_restart_notification_path() == tmp_path / ".restart_pending.json"

    def test_no_module_level_path_is_derived_from_the_snapshot(self):
        """No `X = _hermes_home / ...` may survive at module or class scope.

        Slices off the seam's own body first — the resolver legitimately
        contains ``Path(_hermes_home)``, so grading the whole file would fail on
        the FIXED tree.
        """
        src = Path(run.__file__).read_text(encoding="utf-8")
        marker = "def _resolve_hermes_home("
        assert marker in src, "seam is missing"
        head, _, tail = src.partition(marker)
        body_after_seam = tail.split("\n\n\n", 1)[-1]
        graded = head + body_after_seam

        offenders = [
            line.strip()
            for line in graded.splitlines()
            if "= _hermes_home /" in line or "= _hermes_home\n" in line
        ]
        assert not offenders, f"import-time snapshot derivations remain: {offenders}"

    def test_no_module_level_constant_is_built_from_the_resolver(self):
        """The seam must not be re-frozen into a new column-0 constant.

        Calling ``_resolve_hermes_home()`` at module scope re-creates exactly
        the bug this fix removes — the value is correct at import and stale
        forever after.

        Graded against the module's actual attributes rather than its source,
        so it cannot be fooled by call syntax (``f(home=_resolve_hermes_home())``
        is fine) and correctly accepts an import-time local that is ``del``'d
        once consumed.
        """
        survivors = {
            name: value
            for name, value in vars(run).items()
            if isinstance(value, Path) and name not in ("_hermes_home", "_env_path")
        }
        assert not survivors, (
            "module-level Path constants survived import; each is a fresh "
            f"import-time snapshot of the Hermes home: {survivors}"
        )
        # Consumed inside the import-time config->env bridge, then dropped.
        assert not hasattr(run, "_config_path")

    def test_env_path_name_survives_for_the_tests_that_patch_it(self):
        """``_env_path`` is deliberately KEPT even though nothing reads it.

        Deleting it as "dead code" broke seven tests that do
        ``monkeypatch.setattr(gateway_run, "_env_path", ...)`` — monkeypatch
        raises when the attribute is absent. It is safe to keep precisely
        because no runtime code reads it, so it cannot leak a stale home.
        """
        assert hasattr(run, "_env_path"), (
            "_env_path was removed; that breaks the 7 tests in "
            "test_discord_channel_prompts / test_fast_command / "
            "test_reasoning_command that monkeypatch it"
        )
        assert isinstance(run._env_path, Path)

    def test_env_path_is_never_read_at_runtime(self):
        """Keeping the name is only safe while nothing reads it.

        A runtime read would silently reintroduce the import-time snapshot.
        """
        src = Path(run.__file__).read_text(encoding="utf-8")
        reads = [
            line.strip()
            for line in src.splitlines()
            if "_env_path" in line
            and not line.lstrip().startswith("#")
            and "_env_path = " not in line
        ]
        assert not reads, f"_env_path is read at runtime: {reads}"


class TestResolverChoiceIsDeliberate:
    """``get_process_hermes_home`` vs ``get_hermes_home`` is a real decision."""

    def test_seam_ignores_a_context_override(self, tmp_path, monkeypatch):
        """A per-turn profile override must NOT redirect gateway state.

        ``_gateway_config_home()`` layers the override on top of this value. If
        the seam honored the override itself, that layering would double up and
        ~60 unrelated write sites would silently start following per-turn
        profile scope.
        """
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        other = tmp_path / "some_other_profile"
        other.mkdir()

        token = set_hermes_home_override(str(other))
        try:
            assert run._resolve_hermes_home() == tmp_path, (
                "the seam followed a context override — it must resolve the "
                "PROCESS home so _gateway_config_home()'s layering stays correct"
            )
            assert run._gateway_config_home() == other, (
                "_gateway_config_home() must still honor the override"
            )
        finally:
            reset_hermes_home_override(token)
