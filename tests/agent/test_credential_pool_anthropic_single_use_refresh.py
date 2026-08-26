"""Anthropic OAuth must get the single-use-refresh-token protections.

``agent/anthropic_adapter.py`` documents that Claude Code / subscription OAuth
refresh tokens are single-use: a successful refresh rotates the pair and
invalidates the old refresh token. ``openai-codex`` and ``xai-oauth`` have the
same semantics and are serialized through the cross-process auth-store flock
before the refresh POST, with an in-lock re-read that adopts a pair another
process already rotated.

``anthropic`` was absent from that allow-list, so two profiles sharing one
credential could both POST the same refresh token and the loser would get
``refresh_token_reused`` / ``invalid_grant``. The entries carry the same
``expires_at_ms``, so ``_entry_needs_refresh`` fires for all of them at once —
the race is the normal case, not a rare interleaving.

These tests assert the behaviour contract (serialize + adopt-instead-of-POST),
not the literal contents of the provider tuple.
"""

import threading

import pytest

from agent import credential_pool as CP
from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    CredentialPool,
    PooledCredential,
)


def _entry(
    *,
    id: str = "anthropic-1",
    access_token: str = "acc-old",
    refresh_token: str = "ref-old",
    source: str = "manual:hermes_pkce",
    expires_at_ms: int = 1_000,
) -> PooledCredential:
    return PooledCredential(
        provider="anthropic",
        id=id,
        label="anthropic-oauth-1",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source=source,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=expires_at_ms,
    )


@pytest.fixture
def pool(monkeypatch):
    """A CredentialPool for anthropic that never touches the real auth store."""
    monkeypatch.setattr(CP, "read_credential_pool", lambda provider=None: [])
    monkeypatch.setattr(CP, "write_credential_pool", lambda *a, **k: None)
    return CredentialPool("anthropic", [_entry()])


def test_refresh_takes_the_cross_process_lock(pool, monkeypatch):
    """The refresh POST must happen while the auth-store flock is held.

    Without this, two processes both POST the same single-use token and the
    loser is left holding a revoked refresh token.
    """
    events = []

    class _Lock:
        def __enter__(self):
            events.append("lock-acquired")
            return self

        def __exit__(self, *exc):
            events.append("lock-released")
            return False

    monkeypatch.setattr(CP, "_auth_store_lock", lambda **kwargs: _Lock())
    monkeypatch.setattr(
        CredentialPool,
        "_refresh_entry_impl",
        lambda self, entry, *, force: (events.append("refresh-post"), entry)[1],
    )

    pool._refresh_entry(pool._entries[0], force=True)

    assert "refresh-post" in events, "refresh never ran"
    assert events.index("lock-acquired") < events.index("refresh-post") < events.index(
        "lock-released"
    ), f"refresh POST ran outside the cross-process lock: {events}"


def test_rotated_pair_is_adopted_instead_of_posting_a_revoked_token(pool, monkeypatch):
    """A waiter must adopt the winner's rotated pair, not POST the stale one."""
    rotated = {
        "id": "anthropic-1",
        "label": "anthropic-oauth-1",
        "auth_type": AUTH_TYPE_OAUTH,
        "priority": 0,
        "source": "manual:hermes_pkce",
        "access_token": "acc-new",
        "refresh_token": "ref-new",
        "expires_at_ms": 9_999_999,
    }
    monkeypatch.setattr(CP, "read_credential_pool", lambda provider=None: [rotated])

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(CP, "_auth_store_lock", lambda **kwargs: _Lock())

    posted = []
    monkeypatch.setattr(
        CredentialPool,
        "_refresh_entry_impl",
        lambda self, entry, *, force: (posted.append(entry.refresh_token), entry)[1],
    )

    result = pool._refresh_entry(pool._entries[0], force=True)

    assert posted == [], (
        "posted an already-rotated (revoked) refresh token instead of adopting "
        f"the persisted pair: {posted}"
    )
    assert result is not None
    assert result.access_token == "acc-new"
    assert result.refresh_token == "ref-new"


def test_claude_code_entries_still_sync_from_the_credentials_file(pool, monkeypatch):
    """``claude_code`` keeps its canonical store (~/.claude/.credentials.json).

    The Claude Code CLI writes that file too, so those entries must not be
    switched over to the pool-store sync.
    """
    entry = _entry(source="claude_code")
    pool._entries = [entry]
    called = []

    monkeypatch.setattr(
        CredentialPool,
        "_sync_anthropic_entry_from_credentials_file",
        lambda self, e: (called.append(e.id), e)[1],
    )

    pool._sync_anthropic_entry_from_pool_store(entry)

    assert called == [entry.id], "claude_code entry bypassed the credentials-file sync"


def test_concurrent_refresh_posts_the_token_only_once(monkeypatch):
    """Two pools racing on one credential must produce exactly one POST.

    This is the end-to-end shape of the bug: same credential, same expiry, two
    profiles entering the refresh window together.
    """
    store = {
        "rows": [
            {
                "id": "anthropic-1",
                "label": "anthropic-oauth-1",
                "auth_type": AUTH_TYPE_OAUTH,
                "priority": 0,
                "source": "manual:hermes_pkce",
                "access_token": "acc-old",
                "refresh_token": "ref-old",
                "expires_at_ms": 1_000,
            }
        ]
    }
    posts = []
    real_lock = threading.Lock()

    class _Lock:
        def __enter__(self):
            real_lock.acquire()
            return self

        def __exit__(self, *exc):
            real_lock.release()
            return False

    monkeypatch.setattr(CP, "_auth_store_lock", lambda **kwargs: _Lock())
    monkeypatch.setattr(CP, "read_credential_pool", lambda provider=None: store["rows"])
    monkeypatch.setattr(CP, "write_credential_pool", lambda *a, **k: None)

    def fake_impl(self, entry, *, force):
        posts.append(entry.refresh_token)
        rotated = {**store["rows"][0], "access_token": "acc-new", "refresh_token": "ref-new"}
        store["rows"] = [rotated]
        return PooledCredential.from_dict("anthropic", rotated)

    monkeypatch.setattr(CredentialPool, "_refresh_entry_impl", fake_impl)

    def worker():
        p = CredentialPool("anthropic", [_entry()])
        p._refresh_entry(p._entries[0], force=True)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(posts) == 1, (
        f"single-use refresh token was POSTed {len(posts)} times: {posts} — "
        "the loser would get refresh_token_reused"
    )
