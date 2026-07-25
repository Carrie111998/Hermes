"""Unit tests for workspace_runtime.discovery.

Five states are exercised against a temporary filesystem plus the real
canonical workspace at `/home/taras/projects`.

The verdict encoder is exercised for byte-stability: the same verdict
must produce a byte-identical block on every call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workspace_runtime.discovery import (
    DISCOVERY_THRESHOLD,
    SIGNAL_PATHS,
    TELEMETRY_DIRNAME,
    TELEMETRY_FILENAME,
    DiscoveryVerdict,
    VerdictState,
    _rank_candidates,
    _signal_count,
    _signal_present,
    _present_signals,
    _validate_bootstrap,
    _walk_up,
    discover,
    render_verdict_block,
    write_telemetry,
)


# -----------------------------------------------------------------------------
# Fixture helpers
# -----------------------------------------------------------------------------


def _populate_full_workspace(root: Path) -> None:
    """Write all 4 canonical signals + an AUTHORITY-MODEL.md at root."""
    (root / "IDENTITY.md").write_text("# identity stub\n" * 5, encoding="utf-8")
    (root / "ARCHITECTURE.md").write_text("# architecture stub\n" * 5, encoding="utf-8")
    (root / "GOVERNANCE").mkdir(parents=True, exist_ok=True)
    (root / "GOVERNANCE" / "BOOTSTRAP.md").write_text("# bootstrap stub\n" * 5, encoding="utf-8")
    (root / "CONTEXT").mkdir(parents=True, exist_ok=True)
    (root / "CONTEXT" / "workspace-index.json").write_text(
        json.dumps({"schema_version": 3.0, "items": []}),
        encoding="utf-8",
    )
    (root / "GOVERNANCE" / "AUTHORITY-MODEL.md").write_text(
        "# authority stub\n" * 5, encoding="utf-8"
    )


def _populate_partial_workspace(root: Path) -> None:
    """Write only IDENTITY.md and ARCHITECTURE.md."""
    (root / "IDENTITY.md").write_text("# identity\n", encoding="utf-8")
    (root / "ARCHITECTURE.md").write_text("# architecture\n", encoding="utf-8")


# -----------------------------------------------------------------------------
# Tests for _signal_present / _present_signals
# -----------------------------------------------------------------------------


def test_signal_present_with_real_file(tmp_path: Path):
    """A regular non-empty file counts as present."""
    (tmp_path / "IDENTITY.md").write_text("hello\n", encoding="utf-8")
    assert _signal_present(tmp_path, "identity") is True


def test_signal_present_with_missing_file(tmp_path: Path):
    """A file that does not exist does not count as present."""
    assert _signal_present(tmp_path, "identity") is False


def test_signal_present_with_empty_file(tmp_path: Path):
    """Empty files do NOT count as canonical signals."""
    (tmp_path / "IDENTITY.md").write_text("", encoding="utf-8")
    assert _signal_present(tmp_path, "identity") is False


def test_signal_present_with_directory(tmp_path: Path):
    """Directories at the signal path do NOT count."""
    (tmp_path / "IDENTITY.md").mkdir()
    assert _signal_present(tmp_path, "identity") is False


def test_signal_present_with_broken_symlink(tmp_path: Path):
    """Broken symlinks do NOT count."""
    real = tmp_path / "real.md"
    (tmp_path / "IDENTITY.md").symlink_to(real)
    assert _signal_present(tmp_path, "identity") is False


def test_signal_present_with_valid_symlink(tmp_path: Path):
    """Valid symlinks do count."""
    real = tmp_path / "real.md"
    real.write_text("hello\n", encoding="utf-8")
    (tmp_path / "IDENTITY.md").symlink_to(real)
    assert _signal_present(tmp_path, "identity") is True


def test_present_signals_returns_sorted_tuples(tmp_path: Path):
    """Order is the canonical SIGNAL_PATHS order."""
    _populate_partial_workspace(tmp_path)
    present, missing = _present_signals(tmp_path)
    # identity and architecture exist in SIGNAL_PATHS order before bootstrap/workspace_index.
    assert present == ("identity", "architecture")
    assert missing == ("bootstrap_md", "workspace_index")


# -----------------------------------------------------------------------------
# Tests for walk-up
# -----------------------------------------------------------------------------


def test_walk_up_at_exact_root_with_all_signals(tmp_path: Path):
    """cwd IS the workspace root → detected."""
    _populate_full_workspace(tmp_path)
    matches = _walk_up(tmp_path)
    assert matches == [tmp_path]


def test_walk_up_from_subdirectory_of_workspace(tmp_path: Path):
    """cwd is below the workspace → root detected."""
    _populate_full_workspace(tmp_path)
    sub = tmp_path / "deep" / "nested" / "project"
    sub.mkdir(parents=True)
    matches = _walk_up(sub)
    assert tmp_path in matches
    # The deepest match is the workspace root, not the cwd.
    assert matches[0] == tmp_path


def test_walk_up_no_signals(tmp_path: Path):
    """No signals at any level → empty list."""
    matches = _walk_up(tmp_path)
    assert matches == []


# -----------------------------------------------------------------------------
# Tests for ranking
# -----------------------------------------------------------------------------


def test_rank_candidates_deepest_wins(tmp_path: Path):
    """Deeper match beats shallower."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    outer.mkdir(parents=True)
    inner.mkdir(parents=True)
    _populate_full_workspace(outer)
    _populate_full_workspace(inner)
    ranked = _rank_candidates([outer, inner])
    assert ranked[0] == inner


def test_rank_candidates_more_signals_win(tmp_path: Path):
    """At equal depth, more signals wins."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _populate_full_workspace(a)  # 4 signals
    _populate_partial_workspace(b)  # 2 signals
    ranked = _rank_candidates([b, a])
    assert ranked[0] == a


def test_rank_candidates_governance_bonus_wins(tmp_path: Path):
    """At equal depth and signals, governance bonus breaks tie."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _populate_full_workspace(a)
    _populate_full_workspace(b)
    (a / "GOVERNANCE" / "AMENDMENTS.md").write_text("x\n", encoding="utf-8")
    ranked = _rank_candidates([a, b])
    # a has governance_bonus=1, b has 0. a ranks first.
    assert ranked[0] == a


def test_rank_candidates_lexical_tiebreak(tmp_path: Path):
    """At full tie, alphabetical path wins."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _populate_full_workspace(a)
    _populate_full_workspace(b)
    ranked = _rank_candidates([a, b])
    assert ranked[0] == a


def test_rank_candidates_deterministic():
    """Same input → same output, always."""
    p1 = Path("/some/workspace")
    p2 = Path("/other/workspace")
    ranked1 = _rank_candidates([p1, p2])
    ranked2 = _rank_candidates([p2, p1])
    assert ranked1 == ranked2


# -----------------------------------------------------------------------------
# Tests for bootstrap validation
# -----------------------------------------------------------------------------


def test_bootstrap_validation_5_of_5(tmp_path: Path):
    """All 4 bootstrap signals + AUTHORITY-MODEL.md → 5/5."""
    _populate_full_workspace(tmp_path)
    ans, unans = _validate_bootstrap(tmp_path)
    assert ans == 5
    assert unans == ()


def test_bootstrap_validation_4_of_5(tmp_path: Path):
    """4 bootstrap signals but no AUTHORITY-MODEL.md → 4/5."""
    _populate_partial_workspace(tmp_path)
    # Add bootstrap_md and workspace_index but NOT authority model
    (tmp_path / "GOVERNANCE").mkdir(exist_ok=True)
    (tmp_path / "GOVERNANCE" / "BOOTSTRAP.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "CONTEXT").mkdir(exist_ok=True)
    (tmp_path / "CONTEXT" / "workspace-index.json").write_text("{}", encoding="utf-8")
    ans, unans = _validate_bootstrap(tmp_path)
    assert ans == 4
    assert unans == (5,)


def test_bootstrap_validation_2_of_5(tmp_path: Path):
    """Only 2 bootstrap signals → 2/5; q3, q4, q5 unanswerable."""
    _populate_partial_workspace(tmp_path)
    ans, unans = _validate_bootstrap(tmp_path)
    assert ans == 2
    assert set(unans) == {3, 4, 5}


# -----------------------------------------------------------------------------
# Tests for the public discover() entry point
# -----------------------------------------------------------------------------


def test_discover_inside_workspace(tmp_path: Path):
    """cwd IS a full workspace → INSIDE."""
    _populate_full_workspace(tmp_path)
    v = discover(tmp_path)
    assert v.state == VerdictState.INSIDE
    assert v.root == tmp_path
    assert v.bootstrap_validation == "passed"
    assert v.questions_answerable == 5
    assert v.unanswerable_questions == ()
    assert "identity" in v.present
    assert "architecture" in v.present
    assert "bootstrap_md" in v.present
    assert "workspace_index" in v.present


def test_discover_partial_workspace(tmp_path: Path):
    """cwd has only 2 of 4 canonical signals → PARTIAL."""
    _populate_partial_workspace(tmp_path)
    v = discover(tmp_path)
    assert v.state == VerdictState.PARTIAL
    assert v.root == tmp_path
    assert v.bootstrap_validation == "partial"
    assert v.questions_answerable == 2
    assert "bootstrap_md" in v.missing
    assert "workspace_index" in v.missing


def test_discover_multi_workspace(tmp_path: Path):
    """Two candidate roots → MULTI with deterministic ranking."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    _populate_full_workspace(outer)
    # 'inner' also has a partial set of signals so it's a second candidate
    (inner / "IDENTITY.md").write_text("x\n", encoding="utf-8")
    (inner / "ARCHITECTURE.md").write_text("x\n", encoding="utf-8")

    # cwd at inner so walk-up finds both inner and outer.
    v = discover(inner)
    assert v.state == VerdictState.MULTI
    # inner is deeper; verify it ranks first.
    assert v.candidates[0] == inner
    assert outer in v.candidates


def test_discover_not_a_workspace(tmp_path: Path):
    """cwd with no signals → NOT_FOUND."""
    bare = tmp_path / "scratch"
    bare.mkdir()
    v = discover(bare)
    assert v.state == VerdictState.NOT_FOUND
    assert v.root is None
    assert v.questions_answerable == 0


def test_discover_invalid_path_returns_not_found():
    """A non-existent cwd → NOT_FOUND, not crash."""
    v = discover(Path("/nonexistent/path/that/never/exists"))
    assert v.state == VerdictState.NOT_FOUND


# -----------------------------------------------------------------------------
# Verdict-block encoder tests
# -----------------------------------------------------------------------------


def test_render_block_byte_stable(tmp_path: Path):
    """Same verdict → same block, every time."""
    _populate_full_workspace(tmp_path)
    v = discover(tmp_path)
    a = render_verdict_block(v)
    b = render_verdict_block(v)
    assert a == b
    # Stable prefixes
    assert a.startswith("<workspace-runtime-verdict")
    assert a.endswith("</workspace-runtime-verdict>")


def test_render_block_contains_signals(tmp_path: Path):
    """The block carries each signal-present key."""
    _populate_full_workspace(tmp_path)
    v = discover(tmp_path)
    block = render_verdict_block(v)
    assert 'state="inside_workspace"' in block
    assert 'present="identity architecture bootstrap_md workspace_index"' in block
    assert 'missing=""' not in block  # not emitted when empty


def test_render_block_partial(tmp_path: Path):
    """Partial block lists missing signals."""
    _populate_partial_workspace(tmp_path)
    v = discover(tmp_path)
    block = render_verdict_block(v)
    assert 'state="partial_workspace"' in block
    assert "bootstrap_md" in block
    assert "workspace_index" in block


def test_render_block_not_a_workspace(tmp_path: Path):
    """Not-a-workspace block contains no fake root claim."""
    v = discover(tmp_path / "scratch")
    block = render_verdict_block(v)
    assert 'state="not_a_workspace"' in block
    assert 'root="' not in block  # no root attribute when not found
    assert "Workspace discovery did NOT locate" in block


# -----------------------------------------------------------------------------
# Telemetry tests
# -----------------------------------------------------------------------------


def test_telemetry_written(tmp_path: Path, monkeypatch):
    """Telemetry is written when given a target path."""
    _populate_full_workspace(tmp_path)
    v = discover(tmp_path)
    target = tmp_path / "telem.json"
    out = write_telemetry(v, session_id="test-session", path=target)
    assert out == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["state"] == "inside_workspace"
    assert payload["session_id"] == "test-session"
    assert payload["cwd"] == str(tmp_path)


def test_telemetry_uses_default_under_hermes_home(tmp_path: Path, monkeypatch):
    """When no path is given, default is $HERMES_HOME/workspace_runtime/last_discovery.json."""
    hermes_home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _populate_full_workspace(tmp_path)
    v = discover(tmp_path)
    out = write_telemetry(v, session_id="auto")
    assert out is not None
    expected = hermes_home / TELEMETRY_DIRNAME / TELEMETRY_FILENAME
    assert out == expected
    assert expected.exists()


def test_telemetry_overwrites_per_session(tmp_path: Path):
    """Each call overwrites the previous telemetry record."""
    target = tmp_path / "telem.json"
    _populate_partial_workspace(tmp_path)
    v1 = discover(tmp_path)
    write_telemetry(v1, session_id="first", path=target)
    payload1 = json.loads(target.read_text(encoding="utf-8"))
    assert payload1["session_id"] == "first"

    _populate_full_workspace(tmp_path)
    v2 = discover(tmp_path)
    write_telemetry(v2, session_id="second", path=target)
    payload2 = json.loads(target.read_text(encoding="utf-8"))
    assert payload2["session_id"] == "second"
    assert payload1 != payload2


# -----------------------------------------------------------------------------
# Real-environment smoke test against canonical Workspace OS workspace
# -----------------------------------------------------------------------------


CANONICAL = Path("/home/taras/projects")


@pytest.mark.skipif(
    not CANONICAL.exists(),
    reason="Canonical Workspace OS root is not present on this machine.",
)
def test_canonical_workspace_is_inside():
    """The canonical Workspace is detected as inside_workspace."""
    canonical_index = CANONICAL / "CONTEXT" / "workspace-index.json"
    if not canonical_index.exists():
        pytest.skip("canonical CONTEXT/workspace-index.json not present")

    v = discover(CANONICAL)
    assert v.state == VerdictState.INSIDE
    assert v.root == CANONICAL
    assert v.bootstrap_validation in {"passed", "almost_passed"}
    # Canonical has all 4 bootstrap signals
    for sig in ("identity", "architecture", "bootstrap_md", "workspace_index"):
        assert sig in v.present, f"{sig} should be present"


@pytest.mark.skipif(
    not CANONICAL.exists(),
    reason="Canonical Workspace OS root is not present on this machine.",
)
def test_canonical_subdirectory_still_resolves_to_root():
    """cwd inside a subdir of the canonical Workspace still detects /home/taras/projects."""
    canonical_index = CANONICAL / "CONTEXT" / "workspace-index.json"
    if not canonical_index.exists():
        pytest.skip("canonical CONTEXT/workspace-index.json not present")

    sub = CANONICAL / "workspace-os" / "src" / "workspace_os"
    if not sub.exists():
        sub = CANONICAL / ".project-state"
        if not sub.exists():
            pytest.skip("a known subdirectory of the canonical Workspace is not present")

    v = discover(sub)
    assert v.state == VerdictState.INSIDE
    assert v.root == CANONICAL


def test_bare_tmp_directory_is_not_a_workspace(tmp_path: Path):
    """An empty tmp dir is correctly classified as not_a_workspace."""
    v = discover(tmp_path)
    assert v.state == VerdictState.NOT_FOUND


def test_discover_contains_cwd_resolution_failure(monkeypatch):
    """A cwd lookup failure returns ERROR without retrying the broken call."""
    import workspace_runtime.discovery as mod

    monkeypatch.delenv("PWD", raising=False)
    monkeypatch.setattr(mod.Path, "cwd", classmethod(lambda cls: (_ for _ in ()).throw(OSError("cwd gone"))))
    v = mod.discover()
    assert v.state == VerdictState.ERROR
    assert v.cwd == Path("/")
    assert "cwd gone" in (v.error_message or "")


# -----------------------------------------------------------------------------
# Robustness / error path tests
# -----------------------------------------------------------------------------


def test_discover_returns_error_verdict_on_missing_module(tmp_path: Path, monkeypatch):
    """If _signal_present itself raises, the verdict is the ERROR state."""
    # Force _walk_up to raise by substituting _signal_present with a raising function.
    import workspace_runtime.discovery as mod

    def boom(*_a, **_kw):
        raise RuntimeError("synthetic fault for test")

    monkeypatch.setattr(mod, "_signal_present", boom)
    v = mod.discover(tmp_path)
    assert v.state == mod.VerdictState.ERROR
    assert v.error_message is not None
    assert "synthetic fault" in v.error_message
