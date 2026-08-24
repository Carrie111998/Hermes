"""The root/profile split duplicates sessions; the catalog must survive it.

One Hermes session legitimately lives in both the root ``state.db`` and a
``profiles/<name>/state.db``. Measured on the live box 2026-08-23, 3,190 of
the 5,543 sessions in ``profiles/main`` were also in the root database. Every
merge point in the catalog used to raise ``duplicate native Hermes session
identity across profiles`` on that, so ``session_search`` failed on EVERY
query and ``session_get`` failed on all 3,202 duplicated ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB
from session_bridge.catalog import UnifiedCatalog
from session_bridge.store import SessionBridgeStore, _dedupe_native_session_copies


SHARED = "claude:006cc84a-614b-482f-b946-76e5a3ab502b"


def _seed(database: SessionDB, session_id: str, *, messages: int, marker: str) -> None:
    database.ensure_session(session_id, source="claude", cwd="C:/workspace/project")
    for index in range(messages):
        database.append_message(
            session_id,
            "user" if index % 2 == 0 else "assistant",
            content=f"{marker} kaleidoscopeprobe message {index}",
        )


@pytest.fixture
def catalog(tmp_path: Path):
    root_path = tmp_path / "state.db"
    profile_path = tmp_path / "profiles" / "main" / "state.db"
    profile_path.parent.mkdir(parents=True)

    root = SessionDB(root_path)
    profile = SessionDB(profile_path)
    try:
        # The same session in both databases, plus one unique to each side.
        _seed(root, SHARED, messages=2, marker="root")
        _seed(root, "root-only", messages=2, marker="root")
        _seed(profile, SHARED, messages=6, marker="profile")
        _seed(profile, "profile-only", messages=2, marker="profile")

        store = SessionBridgeStore(root)
        yield UnifiedCatalog(root, store)
    finally:
        root.close()
        profile.close()


def test_search_survives_a_session_present_in_two_databases(catalog) -> None:
    result = catalog.search(query="kaleidoscopeprobe", limit=50)

    ids = [entry["session_id"] for entry in result["results"]]
    assert sorted(ids) == sorted([SHARED, "profile-only", "root-only"])
    assert len(ids) == len(set(ids))


def test_browse_survives_a_session_present_in_two_databases(catalog) -> None:
    result = catalog.search(query="", limit=50)

    ids = [entry["session_id"] for entry in result["results"]]
    assert sorted(ids) == sorted([SHARED, "profile-only", "root-only"])


def test_search_keeps_the_copy_that_carries_the_transcript(catalog) -> None:
    """The two copies are not identical -- one side is often a stub row."""

    result = catalog.search(query="kaleidoscopeprobe", limit=50)

    shared = next(e for e in result["results"] if e["session_id"] == SHARED)
    assert shared["message_count"] == 6  # the profile copy, not the 2-message root one


def test_get_reads_a_session_present_in_two_databases(catalog) -> None:
    result = catalog.get(SHARED, window=50)

    assert result["session_id"] == SHARED
    assert result["message_count"] == 6
    assert "profile kaleidoscopeprobe message 0" in result["messages"][0]["content"]


def test_get_still_raises_key_error_for_an_unknown_session(catalog) -> None:
    with pytest.raises(KeyError):
        catalog.get("no-such-session", window=5)


def test_dedupe_prefers_the_first_copy_when_no_richness_is_given() -> None:
    items = [("root", 1), ("profile", 9)]

    kept = _dedupe_native_session_copies(items, identity=lambda item: "same")

    assert kept == [("root", 1)]


def test_dedupe_preserves_first_appearance_order() -> None:
    items = [("a", 1), ("b", 1), ("a", 5), ("c", 1)]

    kept = _dedupe_native_session_copies(
        items,
        identity=lambda item: item[0],
        richness=lambda item: item[1],
    )

    assert kept == [("a", 5), ("b", 1), ("c", 1)]
