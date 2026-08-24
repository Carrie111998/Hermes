"""A root shadow whose profile database lost the session is ABSENT, not ambiguous.

Root rows with ``source='session_bridge_profile'`` are pointers: the real
session lives in a profile database. Measured 2026-08-24 on the live box, 3 of
the 38 such shadows resolve to NO profile database at all (cron_* rows from
2026-07-27/29). Both resolution sites reported that as "identity is ambiguous",
which sends a reader hunting for a second copy that does not exist. The
genuine-ambiguity guard (two profiles holding one id) must survive the change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB
from session_bridge.context_pack import ContextPackBuilder, ContextPackRequest
from session_bridge.models import Provider
from session_bridge.store import SessionBridgeStore


SHADOW_SOURCE = "session_bridge_profile"


def _shadow(database: SessionDB, session_id: str) -> None:
    """A root pointer row: no transcript of its own, source marks it a shadow."""

    database.ensure_session(session_id, source=SHADOW_SOURCE, cwd="C:/workspace")


def _real(database: SessionDB, session_id: str, *, marker: str) -> None:
    database.ensure_session(session_id, source="cli", cwd="C:/workspace")
    database.append_message(session_id, "user", content=f"{marker} first")
    database.append_message(session_id, "assistant", content=f"{marker} latest")


@pytest.fixture
def store(tmp_path: Path):
    root_path = tmp_path / "state.db"
    main_path = tmp_path / "profiles" / "main" / "state.db"
    other_path = tmp_path / "profiles" / "other" / "state.db"
    main_path.parent.mkdir(parents=True)
    other_path.parent.mkdir(parents=True)

    root = SessionDB(root_path)
    main = SessionDB(main_path)
    other = SessionDB(other_path)
    try:
        _shadow(root, "dangling")   # no profile carries this one
        _shadow(root, "resolvable")  # exactly one profile carries it
        _shadow(root, "ambiguous")   # two profiles carry it

        _real(main, "resolvable", marker="main")
        _real(main, "ambiguous", marker="main")
        _real(other, "ambiguous", marker="other")

        yield SessionBridgeStore(root)
    finally:
        root.close()
        main.close()
        other.close()


def test_dangling_shadow_raises_key_error_not_ambiguity(store) -> None:
    with pytest.raises(KeyError):
        store.get_sidebar_preview_source("dangling")


def test_dangling_shadow_is_indistinguishable_from_an_absent_session(store) -> None:
    """An absent root row already raised KeyError; a dangling shadow now matches."""

    with pytest.raises(KeyError):
        store.get_sidebar_preview_source("no-such-session")


def test_a_shadow_resolving_to_one_profile_still_reads(store) -> None:
    snapshot = store.get_sidebar_preview_source("resolvable")

    assert snapshot["source_session_id"] == "resolvable"
    assert [message["content"] for message in snapshot["messages"]] == [
        "main first",
        "main latest",
    ]


def test_two_profiles_holding_one_id_is_still_ambiguous(store) -> None:
    """Genuine ambiguity must not be swallowed by the absent-source fix."""

    with pytest.raises(ValueError, match="ambiguous"):
        store.get_sidebar_preview_source("ambiguous")


def _pack_request(session_id: str) -> ContextPackRequest:
    return ContextPackRequest(
        source_session_id=session_id,
        target_provider=Provider.CODEX,
        bridge_id="bridge-1",
        source_cursor="cursor-1",
        source_hash="sha256:1",
        budget_chars=8000,
        stale=False,
        diverged=False,
    )


def test_context_pack_dangling_shadow_raises_key_error(store) -> None:
    builder = ContextPackBuilder(store.db, store)

    with pytest.raises(KeyError):
        builder.build(_pack_request("dangling"))


def test_context_pack_two_profiles_holding_one_id_is_still_ambiguous(store) -> None:
    builder = ContextPackBuilder(store.db, store)

    with pytest.raises(ValueError, match="ambiguous"):
        builder.build(_pack_request("ambiguous"))
