"""critic's on-disk paths: same location in production, isolated under pytest.

Identical defect and identical fix to ``graphs/jobflow.py`` (see
``tests/graphs/test_jobflow_paths.py``): eight module-level constants built from
``Path.home()``, so (a) they bypass the ``HERMES_HOME`` redirect ``conftest.py``
installs, and (b) they are resolved at IMPORT time, before conftest's autouse
fixture runs.

The stakes are higher here than in jobflow. ``_bump_skill_metadata`` writes
**skill confidence scores** to ``~/.hermes/skills/<skill>/metadata.json``, and this
box has 50 real skills installed. A test reaching that write corrupts live
skill-selection data, and nothing about the resulting file would look wrong.

Resolver is ``get_default_hermes_root()``, not ``get_hermes_home()``: every path
here is ``<root>/profiles/...``, ``<root>/mailbox/...`` or ``<root>/skills/...``,
i.e. rooted at ``~/.hermes`` itself. ``get_hermes_home()`` returns
``<root>/profiles/<name>`` and would produce ``…/profiles/main/profiles/critic/…``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_constants
from graphs import critic

# (accessor, path segments under the hermes root)
PATHS = [
    ("diff_reports_dir", ("profiles", "matcher-shadow", "workspace", "diff-reports")),
    ("allowed_knobs_path", ("profiles", "critic", "allowed_knobs.json")),
    ("changelog_path", ("profiles", "critic", "workspace", "changelog.jsonl")),
    ("reversals_dir", ("profiles", "critic", "workspace", "reversals")),
    ("retros_dir", ("profiles", "critic", "workspace", "retros")),
    ("whatsapp_queue_path", ("profiles", "critic", "workspace", "whatsapp_queue.jsonl")),
    ("proposal_mailbox_path", ("mailbox", "main", "inbox")),
]


def _expected(root: Path, segments) -> Path:
    for s in segments:
        root = root / s
    return root


# --- the "keep the current paths" contract -------------------------------


@pytest.mark.parametrize("accessor,segments", PATHS)
def test_production_paths_are_identical_to_the_old_constants(accessor, segments, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    if hermes_constants.get_default_hermes_root() != Path.home() / ".hermes":
        pytest.skip("platform default home is not ~/.hermes")

    assert getattr(critic, accessor)() == _expected(Path.home() / ".hermes", segments)


def test_skill_metadata_path_is_identical_to_the_old_inline_expression(monkeypatch):
    """The one that actually WRITES, and the one a module-scope audit misses.

    It was built inline inside a function body, not as a module constant.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    if hermes_constants.get_default_hermes_root() != Path.home() / ".hermes":
        pytest.skip("platform default home is not ~/.hermes")

    assert critic.skill_metadata_path("autocontext") == (
        Path.home() / ".hermes" / "skills" / "autocontext" / "metadata.json"
    )


def test_a_profile_scoped_hermes_home_does_NOT_double_up_the_profiles_segment(monkeypatch):
    """The get_hermes_home() trap, which here would be visibly absurd.

    These paths already contain a ``profiles/`` segment. Resolving them from
    get_hermes_home() (= <root>/profiles/main) would yield
    ``…/profiles/main/profiles/critic/…``.
    """
    root = Path.home() / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "main"))

    resolved = critic.allowed_knobs_path()

    assert resolved == root / "profiles" / "critic" / "allowed_knobs.json"
    assert "profiles" not in resolved.relative_to(root).parts[1:], (
        f"the profiles segment got doubled: {resolved}"
    )


# --- the isolation the fix buys ------------------------------------------


@pytest.mark.parametrize("accessor,segments", PATHS)
def test_paths_follow_an_isolated_hermes_home(accessor, segments, tmp_path, monkeypatch):
    fake_root = tmp_path / "isolated"
    monkeypatch.setenv("HERMES_HOME", str(fake_root))

    resolved = getattr(critic, accessor)()

    assert resolved == _expected(fake_root, segments)
    # Guard the real ~/.hermes specifically -- on Windows tmp_path itself lives
    # under the home directory, so "not under Path.home()" is false for every
    # correctly-isolated path.
    assert (Path.home() / ".hermes") not in resolved.parents


def test_skill_metadata_write_target_follows_an_isolated_home(tmp_path, monkeypatch):
    """50 real skills live under ~/.hermes/skills; none may be reachable."""
    fake_root = tmp_path / "isolated"
    monkeypatch.setenv("HERMES_HOME", str(fake_root))

    resolved = critic.skill_metadata_path("autocontext")

    assert resolved == fake_root / "skills" / "autocontext" / "metadata.json"
    assert (Path.home() / ".hermes") not in resolved.parents


# --- the import-time half ------------------------------------------------


def test_paths_are_resolved_lazily_not_snapshotted_at_import(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"

    monkeypatch.setenv("HERMES_HOME", str(first))
    assert critic.changelog_path().is_relative_to(first)

    monkeypatch.setenv("HERMES_HOME", str(second))
    assert critic.changelog_path().is_relative_to(second), (
        "path did not follow the env change -- it is snapshotted, not resolved per call"
    )


# --- regression guards ----------------------------------------------------


def test_the_module_contains_no_Path_home_call_at_all():
    source = Path(critic.__file__).read_text(encoding="utf-8")

    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(source.splitlines(), 1)
        if "Path.home()" in line and not line.lstrip().startswith(("#", "*", '"'))
    ]

    assert not offenders, (
        "graphs/critic.py resolves the real user home again; use "
        "get_default_hermes_root():\n  " + "\n  ".join(offenders)
    )


def test_the_old_module_level_constants_are_gone():
    for dead in (
        "HERMES", "DIFF_REPORTS_DIR", "ALLOWED_KNOBS_PATH", "CHANGELOG_PATH",
        "REVERSALS_DIR", "RETROS_DIR", "WHATSAPP_QUEUE", "PROPOSAL_MAILBOX",
    ):
        assert not hasattr(critic, dead), (
            f"{dead} still exists as a module attribute; it would bind at import "
            "time and bypass HERMES_HOME again"
        )
