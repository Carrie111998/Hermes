"""Regression coverage: the curator's bundled-skill write guard must honour
``curator.prune_builtins``.

``skill_usage.is_curation_eligible()`` returns ``_prune_builtins_enabled()``
for a bundled skill, so with ``curator.prune_builtins: true`` the curator's
candidate enumeration hands the review fork every bundled built-in. The write
guard used to refuse all of them unconditionally, so each attempt was denied
and the tool-loop guard eventually aborted the whole consolidation pass.

The guard must also not fall through to the ``created_by`` curator-managed
check for bundled skills: they ship with no usage record, so that check would
reject them even when pruning is enabled. ``skill_usage.adopt_skill()``
refuses bundled skills for precisely this reason -- they are governed by
``curator.prune_builtins``, not by the ownership marker.

Guards that must stay intact regardless of the flag: pinned, protected
built-ins, hub-installed, and user-owned (``created_by`` unset) skills.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from tools import skill_manager_tool as smt
from tools import skill_provenance, skill_usage


BUNDLED = "docx"


def _guard(
    name,
    *,
    prune_builtins,
    record=None,
    bundled=True,
    protected=False,
    hub=False,
):
    """Invoke the write guard as the background review fork would."""
    stack = [
        patch.object(skill_provenance, "is_background_review", lambda: True),
        patch.object(smt, "is_background_review", lambda: True, create=True),
        patch.object(
            skill_usage, "_prune_builtins_enabled", lambda: prune_builtins
        ),
        patch.object(skill_usage, "is_bundled", lambda n: bundled),
        patch.object(skill_usage, "is_protected_builtin", lambda n: protected),
        patch.object(skill_usage, "is_hub_installed", lambda n: hub),
        patch.object(skill_usage, "get_record", lambda n: record or {}),
    ]
    for ctx in stack:
        ctx.start()
    try:
        return smt._background_review_write_guard(name, Path("."), "patch")
    finally:
        for ctx in reversed(stack):
            ctx.stop()


def test_bundled_allowed_when_prune_builtins_enabled():
    """With pruning on, a bundled skill must clear the guard.

    Regression: the guard refused unconditionally, so every enumerated
    bundled candidate was denied and the consolidation pass aborted.
    """
    assert _guard(BUNDLED, prune_builtins=True) is None


def test_bundled_refused_when_prune_builtins_disabled():
    """With pruning off, a bundled skill must still be refused."""
    result = _guard(BUNDLED, prune_builtins=False)
    assert result is not None
    assert result["success"] is False
    assert "prune_builtins is disabled" in result["error"]


def test_bundled_does_not_fall_through_to_created_by_check():
    """Bundled skills have no usage record; that must not block them.

    They are governed by ``curator.prune_builtins`` rather than by the
    ``created_by`` ownership marker, so the guard must decide and return
    within the bundled branch instead of reaching the curator-managed check.
    """
    with patch.object(skill_usage, "load_usage", lambda: {}):
        assert _guard(BUNDLED, prune_builtins=True) is None


def test_pinned_bundled_still_refused():
    """Pin outranks prune_builtins (issue #25839)."""
    result = _guard(BUNDLED, prune_builtins=True, record={"pinned": True})
    assert result is not None
    assert "pinned" in result["error"]


@pytest.mark.parametrize(
    "kwarg, needle",
    [
        ("protected", "protected"),
        ("hub", "hub-installed"),
    ],
)
def test_protected_and_hub_still_refused(kwarg, needle):
    """prune_builtins must not widen the protected/hub guards."""
    result = _guard(BUNDLED, prune_builtins=True, **{kwarg: True})
    assert result is not None
    assert needle in result["error"]


def test_user_owned_skill_still_refused():
    """A non-bundled skill without ``created_by`` stays off-limits."""
    with patch.object(skill_usage, "load_usage", lambda: {"mine": {}}):
        result = _guard("mine", prune_builtins=True, bundled=False)
    assert result is not None
    assert "not curator-managed" in result["error"]
