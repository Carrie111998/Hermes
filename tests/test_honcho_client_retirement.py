"""Reachability-based Honcho retirement and identity-safe SDK cache tests."""

from __future__ import annotations

import gc
import sys
import threading
import types
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from honcho import Honcho as RealHoncho
from honcho.peer import Peer as RealPeer
from honcho.session import Session as RealSession

from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho import client as client_module
from plugins.memory.honcho import session as session_module
from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client, reset_honcho_client
from plugins.memory.honcho.session import HonchoSession, HonchoSessionManager


class _Transport:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


class _Context:
    messages = []
    summary = None


class _Peer:
    def __init__(self, honcho, peer_id):
        self._honcho = honcho
        self.peer_id = peer_id

    def message(self, content):
        return content


class _Session:
    def __init__(self, honcho, session_id):
        self._honcho = honcho
        self.session_id = session_id

    def add_peers(self, peers):
        return None

    def get_peer_configuration(self, peer):
        return SimpleNamespace(observe_me=None, observe_others=None)

    def context(self, **kwargs):
        return _Context()


class _Honcho:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._http = _Transport()
        self._async_http = None


    def peer(self, peer_id):
        return _Peer(self, peer_id)

    def session(self, session_id):
        return _Session(self, session_id)


@pytest.fixture(autouse=True)
def _clean_client_cache(monkeypatch):
    reset_honcho_client()

    fake_honcho = types.ModuleType("honcho")
    fake_honcho.Honcho = _Honcho
    monkeypatch.setitem(sys.modules, "honcho", fake_honcho)
    fake_session = types.ModuleType("honcho.session")
    fake_session.SessionPeerConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "honcho.session", fake_session)
    monkeypatch.setattr(client_module, "_resolve_timeout_from_sources", lambda cfg: 30.0)
    yield
    reset_honcho_client()
    gc.collect()


def _cfg(workspace="ws"):
    return HonchoClientConfig(
        api_key="test", workspace_id=workspace, write_frequency="turn"
    )


def test_real_sdk_private_transport_is_finalized_without_async_creation():
    client = RealHoncho(
        base_url="http://127.0.0.1:9",
        workspace_id="finalizer-shape-probe",
    )
    transport = client._http
    client_ref = weakref.ref(client)

    assert client_module._sync_http_transport(client) is transport
    assert client._async_http is None
    client_module._register_client_finalizer(client)

    del client
    gc.collect()

    assert client_ref() is None
    assert transport._client.is_closed


def test_reset_waits_for_final_strong_reference_and_closes_sync_once():
    client = get_honcho_client(_cfg())
    transport = client._http
    ref = weakref.ref(client)

    reset_honcho_client()
    gc.collect()
    assert transport.close_count == 0
    assert ref() is client

    del client
    gc.collect()
    assert ref() is None
    assert transport.close_count == 1


def test_profile_finalizers_are_isolated_and_async_transport_stays_lazy():
    first = get_honcho_client(_cfg("first"))
    second = get_honcho_client(_cfg("second"))
    first_transport = first._http
    second_transport = second._http

    assert first._async_http is None
    assert second._async_http is None
    reset_honcho_client()
    del first
    gc.collect()
    assert first_transport.close_count == 1
    assert second_transport.close_count == 0
    assert second._async_http is None

    del second
    gc.collect()
    assert second_transport.close_count == 1


def test_cached_sdk_objects_delay_retirement_until_manager_refresh_and_release():
    cfg = _cfg()
    manager = HonchoSessionManager(config=cfg)
    old = manager.honcho
    old_ref = weakref.ref(old)
    transport = old._http
    peer = manager._get_or_create_peer("user")
    sdk_session, _ = manager._get_or_create_honcho_session("session", peer, peer)

    reset_honcho_client()
    new = manager.honcho
    assert new is not old
    assert manager._peers_cache == {}
    assert manager._sessions_cache == {}
    del old
    gc.collect()
    assert transport.close_count == 0

    del peer, sdk_session
    gc.collect()
    assert old_ref() is None
    assert transport.close_count == 1
    assert new._async_http is None


def _manager_with_switchable_clients(monkeypatch, first, current):
    cfg = _cfg()
    monkeypatch.setattr(session_module, "get_honcho_client", lambda bound: current[0])
    manager = HonchoSessionManager(config=cfg)
    manager._honcho = first
    return manager


def test_peer_finishing_after_refresh_is_not_published(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class BlockingHoncho(_Honcho):
        def peer(self, peer_id):
            entered.set()
            assert release.wait(5)
            return super().peer(peer_id)

    old = BlockingHoncho(workspace_id="old")
    new = _Honcho(workspace_id="new")
    current = [old]
    manager = _manager_with_switchable_clients(monkeypatch, old, current)
    result = []
    worker = threading.Thread(target=lambda: result.append(manager._get_or_create_peer("p")))
    worker.start()
    assert entered.wait(5)
    current[0] = new
    assert manager.honcho is new
    release.set()
    worker.join(5)

    assert result[0]._honcho is old
    assert "p" not in manager._peers_cache


def test_session_finishing_after_refresh_is_not_published(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class BlockingHoncho(_Honcho):
        def session(self, session_id):
            entered.set()
            assert release.wait(5)
            return super().session(session_id)

    old = BlockingHoncho(workspace_id="old")
    new = _Honcho(workspace_id="new")
    current = [old]
    manager = _manager_with_switchable_clients(monkeypatch, old, current)
    peer = _Peer(old, "p")
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            manager._get_or_create_honcho_session("s", peer, peer)[0]
        )
    )
    worker.start()
    assert entered.wait(5)
    current[0] = new
    assert manager.honcho is new
    release.set()
    worker.join(5)

    assert result[0]._honcho is old
    assert "s" not in manager._sessions_cache


def test_direct_session_cache_path_refreshes_before_use(monkeypatch):
    old = _Honcho(workspace_id="old")
    new = _Honcho(workspace_id="new")
    current = [old]
    manager = _manager_with_switchable_clients(monkeypatch, old, current)
    local = HonchoSession("key", "user", "ai", "sid")
    stale = _Session(old, "sid")
    manager._cache["key"] = local
    manager._sessions_cache["sid"] = stale
    manager._session_cache_owners["sid"] = old

    current[0] = new
    result = manager.get_session_context("key")
    assert result == {"representation": "", "card": []}
    assert manager._sessions_cache == {}


def test_new_session_does_not_hold_cache_lock_while_refreshing(monkeypatch):
    old_client = _Honcho(workspace_id="old")
    current = [old_client]
    manager = _manager_with_switchable_clients(
        monkeypatch, old_client, current
    )
    old_session = HonchoSession("key", "user", "ai", "old-sid")
    manager._cache["key"] = old_session
    entered_resolution = threading.Event()
    original_resolve_user = manager._resolve_user_peer_id

    def signal_resolution(key):
        entered_resolution.set()
        return original_resolve_user(key)

    monkeypatch.setattr(manager, "_resolve_user_peer_id", signal_resolution)
    manager._client_refresh_lock.acquire()
    result = []
    worker = threading.Thread(
        target=lambda: result.append(manager.new_session("key"))
    )
    worker.start()
    try:
        assert entered_resolution.wait(5)
        assert manager._cache_lock.acquire(timeout=1)
        try:
            assert manager._cache["key"] is old_session
        finally:
            manager._cache_lock.release()
    finally:
        manager._client_refresh_lock.release()
    worker.join(5)

    assert not worker.is_alive()
    assert result[0] is manager._cache["key"]
    assert result[0] is not old_session


def test_concurrent_first_creation_returns_published_winner_with_messages(monkeypatch):
    manager = HonchoSessionManager(
        honcho=cast(Any, _Honcho(workspace_id="ws")), config=_cfg()
    )
    first_entered = threading.Event()
    second_entered = threading.Event()
    message_added = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def controlled_session_build(session_id, user_peer, assistant_peer, owner=None):
        nonlocal call_count
        with call_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_entered.set()
            assert second_entered.wait(5)
        else:
            second_entered.set()
            assert message_added.wait(5)
        return _Session(owner, session_id), []

    monkeypatch.setattr(
        manager, "_get_or_create_honcho_session", controlled_session_build
    )
    results = []

    def first_creator():
        session = manager.get_or_create("key")
        session.messages.append({"role": "user", "content": "preserved"})
        results.append(session)
        message_added.set()

    def second_creator():
        results.append(manager.get_or_create("key"))

    first_thread = threading.Thread(target=first_creator)
    first_thread.start()
    assert first_entered.wait(5)
    second_thread = threading.Thread(target=second_creator)
    second_thread.start()
    first_thread.join(5)
    second_thread.join(5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert results[0] is results[1]
    assert results[0].messages == [{"role": "user", "content": "preserved"}]
    assert manager._cache["key"] is results[0]


def test_old_flush_cannot_overwrite_completed_rotation():
    owner = _Honcho(workspace_id="ws")
    manager = HonchoSessionManager(honcho=cast(Any, owner), config=_cfg())
    old = HonchoSession(
        "key",
        "user",
        "assistant",
        "old-session",
        messages=[{"role": "user", "content": "pending", "_synced": False}],
    )
    manager._cache["key"] = old
    flush_entered = threading.Event()
    release_flush = threading.Event()

    class BlockingSession(_Session):
        def add_messages(self, messages):
            flush_entered.set()
            assert release_flush.wait(5)

    old_sdk_session = BlockingSession(owner, old.honcho_session_id)
    manager._sessions_cache[old.honcho_session_id] = old_sdk_session
    manager._session_cache_owners[old.honcho_session_id] = owner
    flush_result = []
    flush_thread = threading.Thread(
        target=lambda: flush_result.append(manager._flush_session(old))
    )
    flush_thread.start()
    assert flush_entered.wait(5)

    rotated = manager.new_session("key")
    assert manager._cache["key"] is rotated
    release_flush.set()
    flush_thread.join(5)

    assert not flush_thread.is_alive()
    assert flush_result == [True]
    assert manager._cache["key"] is rotated
    assert manager._cache["key"] is not old


def test_frozen_clock_rotations_use_distinct_session_keys(monkeypatch):
    monkeypatch.setattr("time.time_ns", lambda: 123)
    manager = HonchoSessionManager(
        honcho=cast(Any, _Honcho(workspace_id="ws")), config=_cfg()
    )
    manager.get_or_create("key")

    first = manager.new_session("key")
    second = manager.new_session("key")

    assert first is not second
    assert first.key == "key:123"
    assert second.key == "key:124"
    assert first.honcho_session_id != second.honcho_session_id


def test_real_sdk_private_child_owners_are_rejected_without_sidecars(monkeypatch):
    old = RealHoncho(base_url="http://127.0.0.1:9", workspace_id="old")
    new = _Honcho(workspace_id="new")
    current = [new]

    peer_manager = _manager_with_switchable_clients(monkeypatch, new, current)
    stale_peer = RealPeer("peer", old)
    peer_manager._peers_cache["peer"] = stale_peer
    replacement_peer = peer_manager._get_or_create_peer("peer")
    assert session_module._sdk_owner(stale_peer) is old
    assert replacement_peer._honcho is new

    session_manager = _manager_with_switchable_clients(monkeypatch, new, current)
    stale_session = RealSession("session", old)
    session_manager._sessions_cache["session"] = stale_session
    replacement_session, _ = session_manager._get_or_create_honcho_session(
        "session", new.peer("user"), new.peer("assistant")
    )
    assert session_module._sdk_owner(stale_session) is old
    assert replacement_session._honcho is new

    old._http.close()


def test_failed_in_place_oauth_rotation_rebuilds_current_acquisition(monkeypatch):
    from plugins.memory.honcho import oauth

    cfg = _cfg()
    first = get_honcho_client(cfg)
    refresh_results = iter(
        [("new-token", True), ("new-token", False), ("new-token", False)]
    )
    monkeypatch.setattr(
        oauth, "ensure_fresh_token", lambda path, host: next(refresh_results)
    )
    monkeypatch.setattr(oauth, "apply_token_to_client", lambda client, token: False)

    replacement = get_honcho_client(cfg)

    assert replacement is not first
    assert get_honcho_client(cfg) is replacement
    assert getattr(replacement, "kwargs")["api_key"] == "new-token"


def test_factory_finishing_after_global_reset_retries_current_slot(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    built = []
    build_lock = threading.Lock()

    class BlockingHoncho(_Honcho):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            with build_lock:
                built.append(self)
                build_number = len(built)
            if build_number == 1:
                entered.set()
                assert release.wait(5)

    monkeypatch.setattr(sys.modules["honcho"], "Honcho", BlockingHoncho)
    cfg = _cfg()
    worker_result = []
    worker = threading.Thread(
        target=lambda: worker_result.append(get_honcho_client(cfg))
    )
    worker.start()
    assert entered.wait(5)

    reset_honcho_client()
    replacement = get_honcho_client(cfg)
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert len(built) == 2
    assert worker_result == [replacement]
    assert replacement is built[1]
    assert get_honcho_client(cfg) is replacement


def test_explicit_configs_with_different_timeouts_use_distinct_slots(monkeypatch):
    cfg_a = _cfg()
    cfg_a.timeout = 10
    cfg_b = _cfg()
    cfg_b.timeout = 20
    monkeypatch.setattr(
        client_module,
        "_resolve_timeout_from_sources",
        lambda config: config.timeout if config is not None else 30.0,
    )
    start = threading.Event()
    results = []
    result_lock = threading.Lock()

    def acquire(config):
        assert start.wait(5)
        client = get_honcho_client(config)
        with result_lock:
            results.append(client)

    threads = [
        threading.Thread(target=acquire, args=(cfg_a,)),
        threading.Thread(target=acquire, args=(cfg_b,)),
    ]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert results[0] is not results[1]
    assert {getattr(client, "kwargs")["timeout"] for client in results} == {10, 20}
    assert client_module._client_cache_key(cfg_a) != client_module._client_cache_key(
        cfg_b
    )


def test_detached_factories_with_different_timeouts_stay_isolated(monkeypatch):
    cfg_a = _cfg()
    cfg_a.timeout = 10
    cfg_b = _cfg()
    cfg_b.timeout = 20
    entered_first_build = threading.Event()
    release_first_build = threading.Event()
    build_lock = threading.Lock()
    build_timeouts = []
    build_count = 0
    monkeypatch.setattr(
        client_module,
        "_resolve_timeout_from_sources",
        lambda config: config.timeout if config is not None else 30.0,
    )

    class BlockingHoncho(_Honcho):
        def __init__(self, **kwargs):
            nonlocal build_count
            with build_lock:
                build_count += 1
                number = build_count
                build_timeouts.append(kwargs.get("timeout"))
            if number == 1:
                entered_first_build.set()
                assert release_first_build.wait(5)
            super().__init__(**kwargs)

    fake_honcho = types.ModuleType("honcho")
    setattr(fake_honcho, "Honcho", BlockingHoncho)
    monkeypatch.setitem(sys.modules, "honcho", fake_honcho)
    worker_result = []
    worker = threading.Thread(
        target=lambda: worker_result.append(get_honcho_client(cfg_a))
    )
    worker.start()
    assert entered_first_build.wait(5)

    reset_honcho_client()
    replacement = get_honcho_client(cfg_b)
    release_first_build.set()
    worker.join(5)

    assert not worker.is_alive()
    assert build_timeouts == [10, 20, 10]
    assert len(worker_result) == 1
    assert worker_result[0] is not replacement
    assert getattr(worker_result[0], "kwargs")["timeout"] == 10
    assert getattr(replacement, "kwargs")["timeout"] == 20
    key_a = client_module._client_cache_key(cfg_a)
    key_b = client_module._client_cache_key(cfg_b)
    assert key_a != key_b
    assert client_module._cached_timeouts[key_a] == 10
    assert client_module._cached_timeouts[key_b] == 20


def test_bound_oauth_path_survives_ambient_profile_change(monkeypatch):
    from plugins.memory.honcho import oauth

    cfg = _cfg()
    bound_path = Path("/profiles/named/honcho.json")
    cfg.config_path = bound_path
    observed_paths = []
    monkeypatch.setattr(
        client_module, "resolve_config_path", lambda: Path("/profiles/default/honcho.json")
    )
    monkeypatch.setattr(
        oauth,
        "ensure_fresh_token",
        lambda path, host: (observed_paths.append(path) or None, False),
    )

    first = get_honcho_client(cfg)
    second = get_honcho_client(cfg)

    assert first is second
    assert observed_paths == [bound_path, bound_path]


def test_bound_profile_home_scopes_worker_config_fallback(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home
    from hermes_cli import config as hermes_config

    cfg = _cfg()
    cfg.timeout = None
    cfg.base_url = None
    cfg.hermes_home = tmp_path / "named-profile"
    observed_homes = []

    def load_bound_config():
        observed_homes.append(get_hermes_home())
        return {
            "honcho": {
                "timeout": 17,
                "base_url": "http://127.0.0.1:38000/v3",
            }
        }

    monkeypatch.setattr(hermes_config, "load_config", load_bound_config)
    result = []
    worker = threading.Thread(target=lambda: result.append(get_honcho_client(cfg)))
    worker.start()
    worker.join(5)

    assert not worker.is_alive()
    assert len(result) == 1
    assert observed_homes == [cfg.hermes_home]
    assert getattr(result[0], "kwargs")["timeout"] == 17
    assert getattr(result[0], "kwargs")["base_url"] == "http://127.0.0.1:38000"


def test_cached_value_reset_in_same_slot_retries_before_return(monkeypatch):
    cfg = _cfg()
    first = get_honcho_client(cfg)
    key = client_module._client_cache_key(cfg)
    slot = client_module._client_slot_for(key)
    acquired_old = threading.Event()
    release = threading.Event()
    original_get_from_slot = client_module._get_honcho_client_from_slot
    worker_result = []
    worker_ident = []

    def pause_worker_after_acquire(config, cache_key, current_slot):
        result = original_get_from_slot(config, cache_key, current_slot)
        if threading.get_ident() == worker_ident[0]:
            acquired_old.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(
        client_module, "_get_honcho_client_from_slot", pause_worker_after_acquire
    )

    def acquire_in_worker():
        worker_ident.append(threading.get_ident())
        worker_result.append(get_honcho_client(cfg))

    worker = threading.Thread(target=acquire_in_worker)
    worker.start()
    assert acquired_old.wait(5)
    slot.reset()
    replacement = get_honcho_client(cfg)
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert replacement is not first
    assert worker_result == [replacement]


def test_global_reset_cannot_split_map_and_slot_validation(monkeypatch):
    cfg = _cfg()
    old = get_honcho_client(cfg)
    key = client_module._client_cache_key(cfg)
    slot = client_module._client_slot_for(key)
    contains_entered = threading.Event()
    release_contains = threading.Event()
    reset_started = threading.Event()
    reset_done = threading.Event()
    worker_ident = []
    worker_result = []
    original_contains = type(slot).contains

    def blocking_contains(self, value):
        if self is slot and threading.get_ident() == worker_ident[0]:
            contains_entered.set()
            assert release_contains.wait(5)
        return original_contains(self, value)

    monkeypatch.setattr(type(slot), "contains", blocking_contains)

    def acquire_cached():
        worker_ident.append(threading.get_ident())
        worker_result.append(get_honcho_client(cfg))

    def reset_cache():
        reset_started.set()
        reset_honcho_client()
        reset_done.set()

    worker = threading.Thread(target=acquire_cached)
    worker.start()
    assert contains_entered.wait(5)
    resetter = threading.Thread(target=reset_cache)
    resetter.start()
    assert reset_started.wait(5)
    assert not reset_done.wait(0.05)

    release_contains.set()
    worker.join(5)
    resetter.join(5)

    assert not worker.is_alive()
    assert not resetter.is_alive()
    assert worker_result == [old]
    assert reset_done.is_set()
    assert get_honcho_client(cfg) is not old


def test_get_or_create_uses_one_client_snapshot_across_refresh(monkeypatch):
    first_peer_created = threading.Event()
    release = threading.Event()

    class RecordingHoncho(_Honcho):
        def __init__(self, *, block_first_peer=False, **kwargs):
            super().__init__(**kwargs)
            self.block_first_peer = block_first_peer
            self.peers = []
            self.sessions = []

        def peer(self, peer_id):
            peer = super().peer(peer_id)
            self.peers.append(peer)
            if self.block_first_peer and len(self.peers) == 1:
                first_peer_created.set()
                assert release.wait(5)
            return peer

        def session(self, session_id):
            session = super().session(session_id)
            self.sessions.append(session)
            return session

    old = RecordingHoncho(workspace_id="old", block_first_peer=True)
    new = RecordingHoncho(workspace_id="new")
    current = [old]
    manager = _manager_with_switchable_clients(monkeypatch, old, current)
    worker_result = []
    worker = threading.Thread(
        target=lambda: worker_result.append(manager.get_or_create("key"))
    )
    worker.start()
    assert first_peer_created.wait(5)

    current[0] = new
    assert manager.honcho is new
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert len(old.peers) == 2
    assert len(old.sessions) == 1
    assert new.peers == []
    assert new.sessions == []
    assert worker_result[0].key == "key"


def test_native_init_threads_preserve_profile_home_context(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
    configs = [_cfg("a"), _cfg("b")]
    seen = []
    seen_lock = threading.Lock()

    def record_init(self, cfg, session_id, **kwargs):
        with seen_lock:
            seen.append((cfg, get_hermes_home()))
        self._session_initialized = True

    monkeypatch.setattr(HonchoMemoryProvider, "_do_session_init", record_init)
    providers = []
    for cfg, home in zip(configs, homes):
        provider = HonchoMemoryProvider()
        provider._config = cfg
        provider._lazy_init_kwargs = {}
        provider._lazy_init_session_id = cfg.workspace_id
        token = set_hermes_home_override(home)
        try:
            provider._start_session_init_background()
        finally:
            reset_hermes_home_override(token)
        providers.append(provider)

    for provider in providers:
        provider._init_thread.join(5)
    assert sorted((cfg.workspace_id, home) for cfg, home in seen) == [
        ("a", homes[0]),
        ("b", homes[1]),
    ]
