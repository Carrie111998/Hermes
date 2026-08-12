"""HERMES_HOME binding for ``gateway/run.py``'s module-level home snapshot.

``gateway/run.py`` resolved ``_hermes_home = get_hermes_home()`` at module
import — the original form of the bug class that
``concepts/import-time-hermes-home-snapshot-bug`` tracks.  ``tests/conftest.py``'s
autouse ``_hermetic_environment`` fixture redirects ``HERMES_HOME`` to a per-test
tempdir only AFTER collection has imported this module, so the snapshot held the
developer's real ``~/.hermes`` before the first test ran.

This is the largest instance on the repo: 65 use sites, and — unlike
``tui_gateway/server.py`` — it derives FURTHER module-level constants from the
snapshot, each of which is a second snapshot in its own right:

* ``_env_path = _hermes_home / '.env'`` (import-scoped; read by nothing, but
  seven existing tests ``monkeypatch.setattr`` it, so the name must survive);
* ``_config_path = _hermes_home / 'config.yaml'`` (import-scoped: consumed three
  lines later by the config->env bridge and never read again);
* ``GatewayRunner._VOICE_MODE_PATH`` (a genuine runtime second snapshot, read
  AND written long after import).

The destructive use sites are worse than the page's summary recorded.  Besides
``(_hermes_home / ".clean_shutdown").touch()`` and the ``.update_response``
unlinks, there are THREE ``mark_seen(_hermes_home / "config.yaml", FLAG)`` calls
— ``mark_seen`` is ``yaml.safe_load`` + ``atomic_config_write``, the exact writer
that destroyed the user's live ``~/.hermes/config.yaml`` on 2026-08-11.

``gateway/slash_commands.py`` copies the snapshot across the module boundary at
ten sites (``from gateway.run import _hermes_home``), the same shape ``entry.py``
had with ``_CRASH_LOG``.

Existing protection is ~150 ``monkeypatch``/``patch`` sites across ~40 files.
That is incidental, not structural: it is the same pattern that masked
``_save_cfg`` right up until that bug destroyed the live config.

See GBrain ``concepts/import-time-hermes-home-snapshot-bug`` (original class).
"""

import inspect
import os
import re
from pathlib import Path

import pytest

from gateway import run


def _live_home() -> Path:
    """The home the hermetic fixture points this test at."""
    return Path(os.environ["HERMES_HOME"])


def _effective_home() -> Path:
    """Where a write would actually land, on a fixed OR an unfixed tree."""
    resolver = getattr(run, "_resolve_hermes_home", None)
    if resolver is not None:
        return Path(resolver())
    return Path(run._hermes_home)  # pre-fix: the import-time snapshot


@pytest.fixture(autouse=True)
def _refuse_to_touch_the_real_home():
    """Fail loudly instead of writing, if resolution escapes the test home.

    These tests drive real write paths.  On an unfixed tree (a bisect, a
    reverted seam) the import-time snapshot points at the developer's live
    ``~/.hermes``, and running this file there would write into it.  For this
    bug class "the test fails" and "the user's live file is gone" are the same
    event — that happened on 2026-08-11.  A guard is cheaper than the recovery.
    """
    effective = _effective_home().resolve()
    for forbidden in (Path.home() / ".hermes", Path.home() / ".hermes" / "profiles" / "main"):
        if effective == forbidden.resolve():
            pytest.fail(
                f"refusing to run: the gateway home resolves to the real home {effective}. "
                "Run with HERMES_HOME pointed at a throwaway directory."
            )
    yield


# ── The seam ─────────────────────────────────────────────────────────


def test_hermes_home_is_not_baked_at_import():
    """The invariant the whole bug class turns on.

    A non-None ``_hermes_home`` at import means the path was fixed before
    conftest could redirect ``HERMES_HOME``.
    """
    assert run._hermes_home is None


def test_resolver_follows_the_live_hermes_home():
    assert run._resolve_hermes_home() == _live_home()
    assert run._resolve_hermes_home() != Path.home() / ".hermes"


def test_resolver_tracks_a_later_hermes_home_change(monkeypatch, tmp_path):
    """Resolution happens per call, not once — the seam has no cache."""
    other = tmp_path / "another-home"
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert run._resolve_hermes_home() == other


def test_explicit_hermes_home_override_still_wins(monkeypatch, tmp_path):
    """Backward compatibility for the ~150 existing patch sites.

    Roughly 40 test files across ``tests/gateway``, ``tests/cli`` and others do
    ``patch("gateway.run._hermes_home", tmp_path)``.  All of them must keep
    working unchanged, or this fix trades one breakage for forty.
    """
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    assert run._resolve_hermes_home() == tmp_path


def test_string_override_is_coerced_to_path(monkeypatch, tmp_path):
    """Callers pin a ``str`` in places; the seam must still return a ``Path``.

    Every use site does ``_resolve_hermes_home() / "config.yaml"``, which would
    raise on a ``str``.
    """
    monkeypatch.setattr(run, "_hermes_home", str(tmp_path))
    resolved = run._resolve_hermes_home()
    assert isinstance(resolved, Path)
    assert resolved == tmp_path


def test_resolver_ignores_a_task_scoped_profile_override(monkeypatch, tmp_path):
    """``get_process_hermes_home()``, deliberately — not ``get_hermes_home()``.

    Decisive here in a way it was not for the earlier instances: THIS module is
    the one that installs the override.  ``_profile_runtime_scope`` wraps every
    multiplexed inbound turn in ``set_hermes_home_override(profile_home)``, and
    use sites such as the three ``mark_seen(config.yaml)`` calls run inside that
    scope.  Following the override would redirect a routed profile's turn into
    that profile's config.yaml instead of the launch profile's — a product
    change, not an audit fix.

    ``_gateway_config_home()`` settles it independently: it exists precisely to
    layer an explicit override check ON TOP of the bare constant, which is only
    meaningful if the bare constant is override-blind.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(tmp_path / "some-profile"))
    try:
        assert run._resolve_hermes_home() == _live_home()
    finally:
        reset_hermes_home_override(token)


def test_gateway_config_home_still_honors_an_explicit_override(tmp_path):
    """The one caller that DOES opt into the override must keep doing so."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    profile = tmp_path / "some-profile"
    token = set_hermes_home_override(str(profile))
    try:
        assert run._gateway_config_home() == profile
    finally:
        reset_hermes_home_override(token)

    assert run._gateway_config_home() == _live_home()


# ── The derived constants ────────────────────────────────────────────


def test_voice_mode_path_is_not_baked_at_import():
    """The derived constant that is read AND written long after import."""
    assert run.GatewayRunner._VOICE_MODE_PATH is None


def test_voice_mode_write_lands_under_the_live_home():
    """A real write, driven with no path patched in — the uncovered case."""
    runner = object.__new__(run.GatewayRunner)
    runner._voice_mode = {"telegram:123": "all"}

    runner._save_voice_modes()

    written = _live_home() / "gateway_voice_mode.json"
    assert written.exists(), "_save_voice_modes wrote outside the per-test home"
    assert "telegram:123" in written.read_text(encoding="utf-8")


def test_voice_mode_instance_override_still_wins(tmp_path):
    """Existing tests set this on the INSTANCE, so a property would break them.

    ``tests/gateway/test_voice_command.py`` does ``runner._VOICE_MODE_PATH =
    tmp_path / ...`` and ``test_voice_mode_platform_isolation.py`` does
    ``patch.object(runner, "_VOICE_MODE_PATH", voice_path)``.  Neither works
    against a data descriptor.
    """
    runner = object.__new__(run.GatewayRunner)
    runner._voice_mode = {"telegram:9": "off"}
    runner._VOICE_MODE_PATH = tmp_path / "voice.json"

    runner._save_voice_modes()

    assert (tmp_path / "voice.json").exists()
    assert not (_live_home() / "gateway_voice_mode.json").exists()


def test_env_path_name_survives_for_the_tests_that_patch_it():
    """Seven tests ``monkeypatch.setattr(gateway_run, "_env_path", ...)``.

    ``monkeypatch.setattr`` raises when the attribute is absent, so removing
    this vestigial name — nothing in ``gateway/run.py`` reads it — would break
    ``test_discord_channel_prompts``, ``test_fast_command`` and
    ``test_reasoning_command``.
    """
    assert isinstance(run._env_path, Path)


def test_env_path_is_never_read_at_runtime():
    """Keeping the vestigial name is only safe while nothing READS it.

    A runtime read would silently reintroduce the import-time snapshot this
    seam exists to remove — the value is resolved once, at import, under
    whatever home the process was started with.
    """
    source = Path(run.__file__).read_text(encoding="utf-8")
    reads = [
        line.strip()
        for line in source.splitlines()
        if "_env_path" in line
        and not line.lstrip().startswith("#")
        and "_env_path = " not in line
    ]
    assert not reads, f"_env_path is read at runtime: {reads}"


# ── Every use site must go through the seam ──────────────────────────


def _source_without_the_seam() -> str:
    """``gateway/run.py`` minus the resolver's own body.

    The seam legitimately reads the bare constant — that IS the override check —
    so grading the whole file would fail on the FIXED tree.
    """
    source = Path(run.__file__).read_text(encoding="utf-8")
    seam = inspect.getsource(run._resolve_hermes_home)
    assert seam in source, "the resolver seam is not defined in gateway/run.py"
    source = source.replace(seam, "")
    # The sentinel declaration itself also names the constant.
    return re.sub(r"^_hermes_home:.*$", "", source, flags=re.MULTILINE)


def test_no_use_site_still_reads_the_bare_constant():
    """A single missed site keeps writing to the import-time home.

    The suite would not necessarily notice: the pre-fix run leaks nothing on its
    own, exactly as it did not for ``_CRASH_LOG`` or ``tui_gateway``.
    """
    after = _source_without_the_seam()

    for banned in (
        "_hermes_home / ",
        "Path(_hermes_home)",
        "str(_hermes_home)",
        "hermes_home=_hermes_home",
        "return _hermes_home",
    ):
        assert banned not in after, f"a use site still reads the bare constant: {banned}"


def test_every_config_yaml_marker_write_goes_through_the_seam():
    """``mark_seen`` is ``yaml.safe_load`` + ``atomic_config_write``.

    That is the exact writer that replaced the user's live ``config.yaml`` with
    22 bytes on 2026-08-11, and it rewrites through ``safe_load`` so it also
    strips every comment.  ``gateway/run.py`` calls it three times.
    """
    # The seam's docstring names ``mark_seen(config.yaml)`` in prose, which the
    # call-site regex would happily match — grade the file without it.
    calls = re.findall(r"mark_seen\(\s*([^,]+),", _source_without_the_seam())

    assert len(calls) == 3, f"expected three mark_seen call sites, found {len(calls)}"
    for target in calls:
        assert target.strip() == '_resolve_hermes_home() / "config.yaml"', (
            f"mark_seen is handed {target.strip()!r}, not the resolved home"
        )


def test_slash_commands_does_not_copy_the_snapshot_across_the_boundary():
    """``from gateway.run import _hermes_home`` is a second snapshot.

    Ten function-local imports in ``gateway/slash_commands.py`` bind the module
    global by value at call time; under the ``None`` sentinel every one of them
    would produce ``TypeError: unsupported operand type(s) for /: 'NoneType'``.
    They must import the seam instead — the same fix ``entry.py`` needed for
    ``_CRASH_LOG``.
    """
    source = (Path(run.__file__).parent / "slash_commands.py").read_text(encoding="utf-8")

    assert "import _hermes_home" not in source
    assert "_hermes_home / " not in source
    assert source.count("_resolve_hermes_home") >= 10


# ── Ported from tests/gateway/test_hermes_home_binding.py ────────────
#
# That file was this one's twin: two sessions fixed the same defect the same
# day, each with its own suite.  `313546825` deleted the twin in favour of this
# file, calling it a superset and porting `test_env_path_is_never_read_at_runtime`
# forward as "the one guard only mine had".  Two more were only in the twin, and
# accepting the delete unchanged would have dropped them silently.


def test_voice_mode_path_survives_patch_object_roundtrip(tmp_path, monkeypatch):
    """``patch.object`` restores a CLASS attribute with delattr, not setattr.

    test_voice_mode_platform_isolation.py uses this idiom. A property without a
    deleter raises "property has no deleter" on teardown — which is how an
    earlier version of this fix broke three tests. The plain attribute has no
    such failure mode; assert the round trip anyway so a future move back to a
    descriptor cannot regress it silently.

    ``test_voice_mode_instance_override_still_wins`` above covers the instance
    override but never tears one down, so it cannot catch this.
    """
    from unittest.mock import patch

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    runner = object.__new__(run.GatewayRunner)
    custom = tmp_path / "patched_voice.json"
    with patch.object(runner, "_VOICE_MODE_PATH", custom):
        assert runner._voice_mode_path() == custom
    # After teardown it must fall back to live resolution, not explode.
    assert runner._voice_mode_path() == tmp_path / "gateway_voice_mode.json"


def test_planned_restart_marker_path_follows_the_live_home(tmp_path, monkeypatch):
    """The restart marker is a write site, and this is its only coverage."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert run._planned_restart_notification_path() == tmp_path / ".restart_pending.json"


def test_no_module_level_constant_is_built_from_the_resolver():
    """The seam must not be re-frozen into a new column-0 constant.

    Calling ``_resolve_hermes_home()`` at module scope re-creates exactly the
    bug this fix removes — the value is correct at import and stale forever
    after.  ``test_no_use_site_still_reads_the_bare_constant`` cannot catch it:
    that one bans textual reads of ``_hermes_home``, and a re-freeze names the
    RESOLVER instead.

    Graded against the module's actual attributes rather than its source, so it
    cannot be fooled by call syntax (``f(home=_resolve_hermes_home())`` is fine)
    and correctly accepts an import-time local that is ``del``'d once consumed.
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
