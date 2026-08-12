"""HERMES_HOME binding for the TUI gateway's module-level home snapshot.

``tui_gateway/server.py`` resolved ``_hermes_home = get_hermes_home()`` at
module import — the original form of the bug class that
``concepts/import-time-hermes-home-snapshot-bug`` tracks, and the very constant
``_CRASH_LOG`` used to be built from.  ``tests/conftest.py``'s autouse
``_hermetic_environment`` fixture redirects ``HERMES_HOME`` to a per-test
tempdir only AFTER collection has imported this module, so the snapshot held
the developer's real ``~/.hermes`` before the first test ran.

Four of its use sites are genuine writes:

* ``_save_cfg`` -> ``atomic_config_write(<home>/config.yaml)`` — the user's LIVE
  config, reachable from every ``config.set`` branch via ``_write_config_key``;
* ``clipboard.paste`` and ``_queue_attached_image`` -> ``<home>/images/`` mkdir
  plus a PNG write;
* ``paste.collapse`` -> ``<home>/pastes/`` mkdir plus a ``.txt`` write.

Existing protection was per-test and incidental: some callers patch
``_hermes_home``, others stub ``_write_config_key``.  Nothing structural stopped
a newly added test from writing the developer's real config.yaml.

See GBrain ``concepts/import-time-hermes-home-snapshot-bug`` (original class).
"""

import base64
import os
from pathlib import Path

import pytest

from tui_gateway import server


def _live_home() -> Path:
    """The home the hermetic fixture points this test at."""
    return Path(os.environ["HERMES_HOME"])


def _effective_home() -> Path:
    """Where a write would actually land, on a fixed OR an unfixed tree."""
    resolver = getattr(server, "_resolve_hermes_home", None)
    if resolver is not None:
        return Path(resolver())
    return Path(server._hermes_home)  # pre-fix: the import-time snapshot


@pytest.fixture(autouse=True)
def _refuse_to_touch_the_real_home():
    """Fail loudly instead of writing, if resolution escapes the test home.

    These tests drive real write paths — ``_save_cfg`` replaces a whole
    ``config.yaml``.  On an unfixed tree (a bisect, a reverted seam) the
    import-time snapshot points at the developer's live ``~/.hermes``, and
    running this file there would destroy their config.  That happened once,
    on 2026-08-11, during the audit that produced this file.  A guard is
    cheaper than the recovery.
    """
    effective = _effective_home().resolve()
    for forbidden in (Path.home() / ".hermes", Path.home() / ".hermes" / "profiles" / "main"):
        if effective == forbidden.resolve():
            pytest.fail(
                f"refusing to run: _hermes_home resolves to the real home {effective}. "
                "Run with HERMES_HOME pointed at a throwaway directory."
            )
    yield


def test_hermes_home_is_not_baked_at_import():
    """The invariant the whole bug class turns on.

    A non-None ``_hermes_home`` at import means the path was fixed before
    conftest could redirect ``HERMES_HOME``.
    """
    assert server._hermes_home is None


def test_resolver_follows_the_live_hermes_home():
    assert server._resolve_hermes_home() == _live_home()
    assert server._resolve_hermes_home() != Path.home() / ".hermes"


def test_resolver_tracks_a_later_hermes_home_change(monkeypatch, tmp_path):
    """Resolution happens per call, not once — the seam has no cache."""
    other = tmp_path / "another-home"
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert server._resolve_hermes_home() == other


def test_explicit_hermes_home_override_still_wins(monkeypatch, tmp_path):
    """Backward compatibility for the 23 ``monkeypatch.setattr`` call sites."""
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    assert server._resolve_hermes_home() == tmp_path


def test_string_override_is_coerced_to_path(monkeypatch, tmp_path):
    """Callers pin a ``str`` in places; the seam must still return a ``Path``.

    ``_hermes_home`` is a bare ``Path`` (unlike ``_CRASH_LOG``, which is a
    ``str``), and its use sites do ``_resolve_hermes_home() / "config.yaml"``,
    which would raise on a ``str``.
    """
    monkeypatch.setattr(server, "_hermes_home", str(tmp_path))
    resolved = server._resolve_hermes_home()
    assert isinstance(resolved, Path)
    assert resolved == tmp_path


def test_resolver_ignores_a_task_scoped_profile_override(monkeypatch, tmp_path):
    """``get_process_hermes_home()``, deliberately — not ``get_hermes_home()``.

    The import-time constant was captured with no context override active, so
    the launch home is exactly what it held.  Every use site here runs on the
    RPC dispatch thread, outside the per-turn ``set_hermes_home_override``
    scope, so preserving that is behaviour-preserving rather than a guess.
    ``_profile_home`` in particular *must* compare against the launch home:
    resolving through the override would make it report "already the launch
    profile" for whatever profile the caller was scoped to.
    """
    token = server.set_hermes_home_override(str(tmp_path / "some-profile"))
    try:
        assert server._resolve_hermes_home() == _live_home()
    finally:
        server.reset_hermes_home_override(token)


# ── The write paths, exercised for real ──────────────────────────────


def test_save_cfg_targets_the_live_home_without_writing(monkeypatch):
    """The highest-value site, asserted by SPY rather than by writing.

    ``_save_cfg`` replaces a whole ``config.yaml``.  Spying
    ``atomic_config_write`` proves which path it was handed while guaranteeing
    that a regression in the seam can never cost anyone their config — the
    assertion is on the target, so it is strictly stronger than inspecting the
    file afterwards anyway.
    """
    seen: list[Path] = []
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "atomic_config_write", lambda path, data, **kw: seen.append(Path(path))
    )

    server._save_cfg({"model": "binding/probe"})

    assert seen == [_live_home() / "config.yaml"]


def test_save_cfg_write_lands_under_the_live_home():
    """And the real write, once the autouse guard has cleared the home."""
    server._save_cfg({"model": "binding/probe"})

    written = _live_home() / "config.yaml"
    assert written.exists(), "_save_cfg wrote outside the per-test home"
    assert "binding/probe" in written.read_text(encoding="utf-8")


def test_queue_attached_image_writes_under_the_live_home():
    png = base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    session: dict = {}
    written = server._queue_attached_image(session, png, ".png", prefix="probe")

    assert Path(written).parent == _live_home() / "images"
    assert Path(written).exists()


def test_paste_collapse_writes_under_the_live_home():
    """No test patched ``_hermes_home`` around this handler before the fix."""
    result = server._methods["paste.collapse"](1, {"text": "binding probe"})

    path = Path(result["result"]["path"])
    assert path.parent == _live_home() / "pastes"
    assert path.read_text(encoding="utf-8") == "binding probe"


def test_no_use_site_still_reads_the_bare_constant():
    """Every one of the ten use sites must go through the seam.

    A single missed site keeps writing to the import-time home, and the suite
    would not necessarily notice — the pre-fix run leaked nothing on its own.
    """
    source = Path(server.__file__).read_text(encoding="utf-8")

    # The seam's own body legitimately reads the constant (that IS the
    # override check), so grade everything after it instead of the whole file.
    marker = "def _resolve_hermes_home() -> Path:"
    assert marker in source, "the resolver seam is gone"
    body_start = source.index(marker)
    after = source[source.index("load_hermes_dotenv(", body_start) :]

    for banned in (
        "_hermes_home / ",
        "Path(_hermes_home)",
        "str(_hermes_home)",
        "hermes_home=_hermes_home",
    ):
        assert banned not in after, f"a use site still reads the bare constant: {banned}"

    # Nine use sites plus the import-time dotenv load.
    assert after.count("_resolve_hermes_home()") >= 10
