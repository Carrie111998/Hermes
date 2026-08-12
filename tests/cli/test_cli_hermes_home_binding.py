"""``cli.py`` must resolve HERMES_HOME at call time, not at module import.

``cli.py`` used to do ``_hermes_home = get_hermes_home()`` at module scope.
Under pytest the module is imported during collection — *before*
``tests/conftest.py``'s autouse ``_hermetic_environment`` fixture redirects
``HERMES_HOME`` to a per-test tempdir — so the snapshot baked in the user's
real ``~/.hermes``.  Every write built from it (``config.yaml`` via
``save_config_value``/``mark_seen``, ``.hermes_history``, ``pastes/``,
``interrupt_debug.log``) therefore landed on live user files no matter what
the fixture did.  On 2026-08-11 the sibling instance in ``tui_gateway/server.py``
destroyed the user's real ``~/.hermes/config.yaml`` this way.

These tests pin the seam: ``_hermes_home`` is a ``None`` sentinel, and
``_resolve_hermes_home()`` resolves live unless a test overrides it.
"""

import inspect
import os
from pathlib import Path

import pytest

import cli


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------

def _effective_home() -> Path:
    """The home this module would write to right now.

    Reads through the seam when it exists and the bare constant when it does
    not, so the guard still fires on a bisect onto the unfixed tree.
    """
    resolver = getattr(cli, "_resolve_hermes_home", None)
    if callable(resolver):
        return Path(resolver())
    return Path(cli._hermes_home)


@pytest.fixture(autouse=True)
def _never_target_the_real_hermes_home():
    """Abort rather than let a failing test write the user's live files.

    For this bug class "the test fails" and "the user's live config.yaml is
    gone" are the same event, so this runs *before* every test body.
    """
    real_root = Path.home() / ".hermes"
    forbidden = {real_root.resolve(strict=False),
                 (real_root / "profiles" / "main").resolve(strict=False)}
    effective = _effective_home().resolve(strict=False)
    if effective in forbidden:
        pytest.fail(
            f"REFUSING TO RUN: cli.py would write to the real Hermes home "
            f"({effective}). Point HERMES_HOME at a throwaway directory."
        )
    yield


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

def test_module_constant_is_a_none_sentinel_not_an_import_time_snapshot():
    """A non-None module default would be a snapshot taken at import."""
    source = inspect.getsource(cli).split("\n")
    decl = [ln for ln in source if ln.startswith("_hermes_home")]
    assert decl == ["_hermes_home: Optional[Path] = None"], decl


def test_resolver_follows_hermes_home_set_after_import(tmp_path, monkeypatch):
    """The whole defect: a post-import HERMES_HOME change must be honored."""
    later = tmp_path / "moved_after_import"
    later.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(later))

    assert cli._resolve_hermes_home() == later


def test_resolver_tracks_further_changes_rather_than_caching(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(first))
    assert cli._resolve_hermes_home() == first
    monkeypatch.setenv("HERMES_HOME", str(second))
    assert cli._resolve_hermes_home() == second


def test_explicit_override_still_wins(tmp_path, monkeypatch):
    """Existing ``monkeypatch.setattr("cli._hermes_home", ...)`` sites keep working."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env_home"))
    pinned = tmp_path / "pinned"
    monkeypatch.setattr(cli, "_hermes_home", pinned)

    assert cli._resolve_hermes_home() == pinned


def test_override_given_as_a_string_is_coerced_to_path(tmp_path, monkeypatch):
    """Callers pin a ``str`` in places; the resolver must still return a Path."""
    pinned = tmp_path / "pinned_str"
    monkeypatch.setattr(cli, "_hermes_home", str(pinned))

    resolved = cli._resolve_hermes_home()
    assert isinstance(resolved, Path)
    assert resolved == pinned


def test_resolver_ignores_the_context_local_profile_override(tmp_path, monkeypatch):
    """``get_process_hermes_home()`` is the deliberate choice, not ``get_hermes_home()``.

    The constant this replaced was evaluated at import, when no context-local
    override is ever active.  ``save_config_value`` is called from gateway code
    that runs inside ``_profile_runtime_scope`` on the multiplexed inbound path;
    following that override would redirect config writes into a secondary
    profile's home — a behavior change, not a bug fix.
    """
    from hermes_constants import (
        set_hermes_home_override,
        reset_hermes_home_override,
    )

    process_home = tmp_path / "launch_home"
    process_home.mkdir()
    other_profile = tmp_path / "profiles" / "secondary"
    other_profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(process_home))

    token = set_hermes_home_override(str(other_profile))
    try:
        assert cli._resolve_hermes_home() == process_home
    finally:
        reset_hermes_home_override(token)


# ---------------------------------------------------------------------------
# No second snapshot survives in the module body
# ---------------------------------------------------------------------------

def test_no_body_reference_bypasses_the_seam():
    """Every use site must go through the resolver.

    Sliced after the resolver definition: ``Path(_hermes_home)`` inside the
    resolver's own override branch is the fix, not a violation.
    """
    source = inspect.getsource(cli)
    body = source[source.index("def _resolve_hermes_home") :]
    body = body[body.index("\n\n\n") :]  # drop the resolver itself

    assert "_hermes_home /" not in body
    assert "_hermes_home)" not in body


def test_no_derived_module_level_constant_snapshots_the_home():
    """``gateway/run.py`` derives ``_env_path``/``_config_path`` from its snapshot.

    A derived module-level constant is a second snapshot and would defeat the
    seam; cli.py must not grow one.
    """
    import ast

    tree = ast.parse(inspect.getsource(cli))
    offenders = []
    for node in tree.body:  # module scope only
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        names = {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name)
        }
        calls = {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        target_names = {t.id for t in targets if isinstance(t, ast.Name)}
        if target_names == {"_hermes_home"}:
            continue
        if "_hermes_home" in names or "_resolve_hermes_home" in calls:
            offenders.append((sorted(target_names), node.lineno))
    assert offenders == [], f"derived import-time snapshot(s): {offenders}"


# ---------------------------------------------------------------------------
# The write paths
# ---------------------------------------------------------------------------

def test_save_config_value_writes_under_the_live_home(tmp_path, monkeypatch):
    """Spying the writer is strictly safer than inspecting the file afterwards."""
    home = tmp_path / "live_home"
    home.mkdir()
    (home / "config.yaml").write_text("display:\n  skin: default\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    seen = []
    import utils

    monkeypatch.setattr(
        utils,
        "atomic_roundtrip_yaml_update",
        lambda path, key, value: seen.append(Path(path)),
    )

    assert cli.save_config_value("display.skin", "mono") is True
    assert seen == [home / "config.yaml"]


def test_save_config_value_does_not_touch_the_import_time_home(tmp_path, monkeypatch):
    """End-to-end: the file that actually changes is the live home's."""
    import_time_home = tmp_path / "import_time"
    import_time_home.mkdir()
    stale = "display:\n  skin: STALE\n"
    (import_time_home / "config.yaml").write_text(stale, encoding="utf-8")

    live_home = tmp_path / "live"
    live_home.mkdir()
    (live_home / "config.yaml").write_text("display:\n  skin: default\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(live_home))

    assert cli.save_config_value("display.skin", "mono") is True

    assert "mono" in (live_home / "config.yaml").read_text(encoding="utf-8")
    assert (import_time_home / "config.yaml").read_text(encoding="utf-8") == stale


def test_load_cli_config_reads_the_live_home(tmp_path, monkeypatch):
    home = tmp_path / "read_home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "display:\n  skin: from_live_home\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    cfg = cli.load_cli_config()
    assert (cfg.get("display") or {}).get("skin") == "from_live_home"


def test_prefill_relative_path_resolves_against_the_live_home(tmp_path, monkeypatch):
    home = tmp_path / "prefill_home"
    home.mkdir()
    (home / "prefill.json").write_text(
        '[{"role": "user", "content": "hi"}]', encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert cli._load_prefill_messages("prefill.json") == [
        {"role": "user", "content": "hi"}
    ]


def test_history_file_binds_to_the_live_home(tmp_path, monkeypatch):
    """``HermesCLI.__init__`` builds ``.hermes_history`` from the same seam."""
    home = tmp_path / "history_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    src = inspect.getsource(cli.HermesCLI.__init__)
    assert 'self._history_file = _resolve_hermes_home() / ".hermes_history"' in src
    assert cli._resolve_hermes_home() / ".hermes_history" == home / ".hermes_history"


def test_conftest_redirect_is_what_the_seam_now_follows():
    """Sanity: under the hermetic fixture the resolver lands in the tempdir."""
    assert cli._resolve_hermes_home() == Path(os.environ["HERMES_HOME"])
