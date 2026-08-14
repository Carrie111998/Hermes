"""Canonical conversation ownership — the durable cross-process authority.

``gateway/turn_lease.py`` serialises turns *inside one process*; its own
docstring names the hole it cannot close: "A CLI process sharing the session
via CLI-continuity is outside any in-process lock — that pair needs a DB-level
lease (separate design)." This is that lease.

The unit of ownership is the **conversation root** (``get_conversation_root``),
not the session id: context compression rotates ``session_id`` to a fresh
segment mid-turn and delegate subagents hang off their parent, so a session-id
lock would hand the same conversation to two owners the moment it rotated.

The root is *mutable* — deleting an ancestor NULLs its children's
``parent_session_id`` and re-roots them — so a grant pins the root it captured
at acquire time and fenced writes validate that pinned identity rather than
recomputing it.
"""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB

from agent.session_ownership import (
    ConversationOwnershipConflict,
    ConversationOwnershipError,
    ConversationOwnershipUnavailable,
    StaleConversationOwnershipError,
    new_holder_id,
    own_conversation,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_public_exception_text_does_not_leak_authority_diagnostics():
    """Generic adapter stringification must not expose roots, hosts or paths."""
    secret_root = "private-session-root"
    secret_holder = "host=WORKSTATION:pid=4321:start=123:tid=99:nonce=secret"
    secret_path = r"C:\\Users\\private\\state.db"

    conflict = ConversationOwnershipConflict(
        secret_root,
        holder=secret_holder,
        surface="cli",
        session_id="private-child",
        fence_token=77,
    )
    stale = StaleConversationOwnershipError(
        secret_root,
        expected_fence_token=76,
        actual_fence_token=77,
        actual_holder=secret_holder,
    )
    unavailable = ConversationOwnershipUnavailable(
        secret_root, sqlite3.OperationalError(f"unable to open {secret_path}")
    )

    projected = "\n".join(map(str, (conflict, stale, unavailable)))
    for secret in (
        secret_root,
        secret_holder,
        secret_path,
        "WORKSTATION",
        "4321",
        "private-child",
        "77",
    ):
        assert secret not in projected
    assert conflict.holder == secret_holder
    assert unavailable.cause.args[0].endswith(secret_path)


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


# ── the plan's Slice A step 1: two PROCESSES, one canonical conversation ────

_CHILD_SOURCE = r'''
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_state import SessionDB
from agent.session_ownership import new_holder_id

db_path, handshake, release_flag = sys.argv[2], sys.argv[3], sys.argv[4]
db = SessionDB(Path(db_path))
holder = new_holder_id(surface="child-process")
grant = db.try_acquire_conversation_ownership(
    "conv-root", holder, ttl_seconds=120.0, surface="child-process",
    session_id="conv-root",
)
with open(handshake, "w", encoding="utf-8") as fh:
    fh.write(str(grant.fence_token))
deadline = time.time() + 60
while time.time() < deadline and not os.path.exists(release_flag):
    time.sleep(0.05)
db.release_conversation_ownership(grant)
db.close()
'''


def _spawn_holder(tmp_path, db_path):
    handshake = tmp_path / "acquired.txt"
    release_flag = tmp_path / "please-release.txt"
    env = dict(os.environ)
    # The autouse HERMES_HOME isolation in tests/conftest.py does not reach a
    # spawned child; pass it explicitly so the child cannot touch a real home.
    env["HERMES_HOME"] = os.environ.get("HERMES_HOME", str(tmp_path / "home"))
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SOURCE, str(REPO_ROOT), str(db_path),
         str(handshake), str(release_flag)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if handshake.exists() and handshake.read_text(encoding="utf-8").strip():
            return child, handshake, release_flag
        if child.poll() is not None:
            out, err = child.communicate()
            raise AssertionError(
                "ownership holder child exited before acquiring:\n"
                f"stdout={out.decode(errors='replace')}\n"
                f"stderr={err.decode(errors='replace')}"
            )
        time.sleep(0.05)
    child.kill()
    raise AssertionError("ownership holder child never acquired")


def test_second_process_cannot_own_the_same_canonical_conversation(tmp_path):
    """The core invariant: one live owner per conversation, across processes."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("conv-root", source="cli")
    db.close()

    child, _handshake, release_flag = _spawn_holder(tmp_path, db_path)
    try:
        db = SessionDB(db_path)
        with pytest.raises(ConversationOwnershipConflict) as excinfo:
            db.try_acquire_conversation_ownership(
                "conv-root",
                new_holder_id(surface="test-process"),
                ttl_seconds=120.0,
                surface="test-process",
                session_id="conv-root",
            )
        # The conflict must be projectable — a surface has to be able to tell
        # the user who holds it without reading the table itself.
        assert excinfo.value.conversation_root == "conv-root"
        assert excinfo.value.surface == "child-process"
        assert excinfo.value.holder
        db.close()
    finally:
        release_flag.write_text("go", encoding="utf-8")
        child.wait(timeout=60)

    # After the holder releases, the conversation is acquirable again — and the
    # fence token has advanced, so the previous holder's token is now stale.
    db = SessionDB(db_path)
    grant = db.try_acquire_conversation_ownership(
        "conv-root",
        new_holder_id(surface="test-process"),
        ttl_seconds=120.0,
        surface="test-process",
        session_id="conv-root",
    )
    assert grant.fence_token >= 2
    db.close()


# ── canonical identity ─────────────────────────────────────────────────────


def test_ownership_is_keyed_by_conversation_root_not_session_id(db):
    """A compression child and its parent are ONE conversation, one owner."""
    db.create_session("root", source="cli")
    db.create_session("child", source="compression", parent_session_id="root")

    assert db.get_conversation_root("child") == "root"

    grant = db.try_acquire_conversation_ownership(
        db.get_conversation_root("root"),
        new_holder_id(surface="cli"),
        surface="cli",
        session_id="root",
    )
    assert grant.conversation_root == "root"

    with pytest.raises(ConversationOwnershipConflict):
        db.try_acquire_conversation_ownership(
            db.get_conversation_root("child"),
            new_holder_id(surface="gateway"),
            surface="gateway",
            session_id="child",
        )


def test_grant_pins_the_root_it_captured(db):
    """The grant and every fenced write use the identity captured at admission."""
    db.create_session("root", source="cli")
    db.create_session("child", source="compression", parent_session_id="root")

    grant = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="cli"), surface="cli", session_id="child",
    )

    assert db.get_conversation_root("child") == "root"
    assert grant.conversation_root == "root"
    assert db.execute_fenced_write(grant, lambda conn: "ok") == "ok"


def test_live_owner_blocks_ancestor_delete_from_splitting_the_authority(tmp_path):
    """A lineage rewrite must not create a second acquirable ownership key.

    This is deliberately two-handle evidence.  A process-local grant fallback
    can keep the old writer fenced against ``root``, but it cannot stop another
    process from resolving the orphaned child as ``child`` and acquiring that
    new key.  The smallest safe policy is to refuse the re-rooting delete while
    the covering grant is live.
    """
    db_path = tmp_path / "state.db"
    owner_db = SessionDB(db_path)
    contender_db = SessionDB(db_path)
    try:
        owner_db.create_session("root", source="cli")
        owner_db.create_session(
            "child", source="compression", parent_session_id="root"
        )
        grant = owner_db.try_acquire_conversation_ownership(
            "root",
            new_holder_id(surface="cli"),
            surface="cli",
            session_id="child",
            ttl_seconds=120.0,
        )

        with pytest.raises(ConversationOwnershipConflict):
            contender_db.delete_session("root")

        assert contender_db.get_conversation_root("child") == "root"
        with pytest.raises(ConversationOwnershipConflict):
            contender_db.try_acquire_conversation_ownership(
                contender_db.get_conversation_root("child"),
                new_holder_id(surface="gateway"),
                surface="gateway",
                session_id="child",
            )

        owner_db.release_conversation_ownership(grant)
        assert contender_db.delete_session("root") is True
        assert contender_db.get_conversation_root("child") == "child"
    finally:
        contender_db.close()
        owner_db.close()


# ── fencing ────────────────────────────────────────────────────────────────


def test_fence_token_increments_per_owner_handover(db):
    db.create_session("root", source="cli")
    first = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="cli"), surface="cli", session_id="root",
    )
    db.release_conversation_ownership(first)
    second = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="tui"), surface="tui", session_id="root",
    )
    assert second.fence_token == first.fence_token + 1


def test_fenced_write_rejects_a_stale_token(db):
    """The whole point of fencing: a slow writer cannot publish after handover."""
    db.create_session("root", source="cli")
    stale = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="cli"), surface="cli", session_id="root",
    )
    db.release_conversation_ownership(stale)
    db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="tui"), surface="tui", session_id="root",
    )

    calls = []
    with pytest.raises(StaleConversationOwnershipError) as excinfo:
        db.execute_fenced_write(stale, lambda conn: calls.append(1))
    assert calls == [], "the mutation must not run at all, not run-then-fail"
    assert excinfo.value.expected_fence_token == stale.fence_token


def test_fenced_write_runs_the_mutation_in_the_same_transaction(db):
    db.create_session("root", source="cli")
    grant = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="cli"), surface="cli", session_id="root",
    )

    def _mutate(conn):
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("fenced", "root"))
        return "done"

    assert db.execute_fenced_write(grant, _mutate) == "done"
    assert db.get_session("root")["title"] == "fenced"


def test_fenced_write_refuses_an_expired_grant_without_handover(db):
    """Passing TTL invalidates the grant even before another holder arrives."""
    db.create_session("root", source="cli")
    grant = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="cli"), surface="cli", session_id="root",
        ttl_seconds=0.001,
    )
    time.sleep(0.05)

    calls = []
    with pytest.raises(StaleConversationOwnershipError):
        db.execute_fenced_write(grant, lambda conn: calls.append(1))
    assert calls == []


# ── release / takeover ─────────────────────────────────────────────────────


def test_release_is_holder_and_fence_scoped(db):
    """A late unwind must never free a newer owner's grant (#28686 lesson)."""
    db.create_session("root", source="cli")
    stale = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="cli"), surface="cli", session_id="root",
    )
    db.release_conversation_ownership(stale)
    live = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="tui"), surface="tui", session_id="root",
    )

    db.release_conversation_ownership(stale)  # idempotent, and not the owner

    owner = db.get_conversation_owner("root")
    assert owner is not None
    assert owner["holder"] == live.holder
    assert owner["fence_token"] == live.fence_token


def test_expired_grant_is_taken_over_with_a_new_fence(db):
    db.create_session("root", source="cli")
    expired = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="cli"), surface="cli", session_id="root",
        ttl_seconds=0.001,
    )
    time.sleep(0.05)
    taken = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="tui"), surface="tui", session_id="root",
    )
    assert taken.fence_token == expired.fence_token + 1
    with pytest.raises(StaleConversationOwnershipError):
        db.execute_fenced_write(expired, lambda conn: None)


def test_acquire_samples_lease_time_after_write_authority(db, monkeypatch):
    """SQLite lock patience must not consume the lease before it is published."""
    db.create_session("root", source="cli")
    original_execute_write = db._execute_write

    def _delayed_execute_write(fn, *args, **kwargs):
        time.sleep(0.08)
        return original_execute_write(fn, *args, **kwargs)

    monkeypatch.setattr(db, "_execute_write", _delayed_execute_write)
    before = time.time()
    grant = db.try_acquire_conversation_ownership(
        "root",
        new_holder_id(surface="cli"),
        surface="cli",
        session_id="root",
        ttl_seconds=0.5,
    )

    # The delayed authority acquisition must be reflected in the published
    # timestamp. Compare timestamps instead of requiring a tiny post-return TTL,
    # which would make slow Windows fsync itself a source of test flakiness.
    assert grant.expires_at >= before + 0.55
    assert db.get_conversation_owner("root") is not None


def test_refresh_samples_lease_time_after_write_authority(db, monkeypatch):
    """A delayed refresh must publish a fresh TTL, not an already-old one."""
    db.create_session("root", source="cli")
    grant = db.try_acquire_conversation_ownership(
        "root",
        new_holder_id(surface="cli"),
        surface="cli",
        session_id="root",
        ttl_seconds=120.0,
    )
    original_execute_write = db._execute_write

    def _delayed_execute_write(fn, *args, **kwargs):
        time.sleep(0.08)
        return original_execute_write(fn, *args, **kwargs)

    monkeypatch.setattr(db, "_execute_write", _delayed_execute_write)
    before = time.time()
    assert db.refresh_conversation_ownership(grant, ttl_seconds=0.5) is True
    # The delayed authority acquisition must be reflected in the published
    # timestamp. This compares timestamps rather than a tiny post-return TTL,
    # which would make slow Windows fsync itself a source of test flakiness.
    assert grant.expires_at >= before + 0.55
    assert db.get_conversation_owner("root") is not None


def test_root_resolution_failure_fails_closed(db, monkeypatch):
    """An identity lookup failure must never mint a lease on the child id."""
    db.create_session("root", source="cli")
    db.create_session("child", source="compression", parent_session_id="root")

    def _raise(_session_id):
        raise sqlite3.OperationalError("identity authority unavailable")

    monkeypatch.setattr(db, "get_conversation_root", _raise)
    with pytest.raises(ConversationOwnershipUnavailable):
        with own_conversation(db, "child", surface="cli"):
            pass
    assert db.get_conversation_owner("child") is None


def test_unrelated_write_does_not_borrow_the_only_thread_grant(db):
    """A grant on A cannot authorize or stale-fail a mutation on unrelated B."""
    db.create_session("a", source="cli")
    db.create_session("b", source="cli")
    with own_conversation(db, "a", surface="cli"):
        db.append_message("b", role="user", content="independent")
    assert [m["content"] for m in db.get_messages("b")] == ["independent"]


def test_transactional_and_admission_root_resolvers_are_equivalent(db):
    """Both authority-key walks agree on normal, missing, and cyclic lineage."""
    db.create_session("root", source="cli")
    db.create_session("child", source="compression", parent_session_id="root")
    db.create_session("cycle-a", source="cli")
    db.create_session("cycle-b", source="cli", parent_session_id="cycle-a")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
            ("cycle-b", "cycle-a"),
        )
    )

    for session_id in ("root", "child", "missing", "cycle-a", "cycle-b"):
        public_root = db.get_conversation_root(session_id)
        transaction_root = db._execute_write(
            lambda conn, sid=session_id: db._conversation_root_in_transaction(
                conn, sid
            )
        )
        assert transaction_root == public_root


def test_dead_holder_process_is_reclaimed_before_ttl(db):
    """A killed CLI must not strand the conversation for the full TTL."""
    db.create_session("root", source="cli")
    dead_pid = _find_dead_pid()
    holder = f"host={_hostname()}:pid={dead_pid}:start=0:nonce=deadbeef"
    db.try_acquire_conversation_ownership(
        "root", holder, surface="cli", session_id="root", ttl_seconds=3600.0,
    )
    taken = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="tui"), surface="tui", session_id="root",
    )
    assert taken.holder != holder


def test_refresh_only_extends_our_own_grant(db):
    db.create_session("root", source="cli")
    stale = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="cli"), surface="cli", session_id="root",
    )
    db.release_conversation_ownership(stale)
    live = db.try_acquire_conversation_ownership(
        "root", new_holder_id(surface="tui"), surface="tui", session_id="root",
    )
    assert db.refresh_conversation_ownership(stale) is False
    assert db.refresh_conversation_ownership(live) is True


# ── fail closed ────────────────────────────────────────────────────────────


def test_authority_failure_never_silently_grants_ownership(db, monkeypatch):
    """Unlike the compression lock, this authority must not fail open.

    ``try_acquire_compression_lock`` returns False on ``sqlite3.Error`` because
    skipping compression is safe. Skipping *ownership* is not — it would let
    both racers proceed. A broken authority raises a typed error the surface
    projects as a refusal.
    """
    import sqlite3

    db.create_session("root", source="cli")

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "_execute_write", _boom)
    with pytest.raises(ConversationOwnershipError):
        db.try_acquire_conversation_ownership(
            "root", new_holder_id(surface="cli"), surface="cli",
            session_id="root",
        )


# ── helpers ────────────────────────────────────────────────────────────────


def _hostname() -> str:
    import socket

    return socket.gethostname()


def _find_dead_pid() -> int:
    """A pid that is not currently running on this host."""
    try:
        import psutil

        live = set(psutil.pids())
    except Exception:  # pragma: no cover - psutil is a hard dep in practice
        live = set()
    for candidate in range(999000, 999500):
        if candidate not in live:
            return candidate
    raise AssertionError("no free pid found")
