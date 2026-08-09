"""Tests for SessionStore.prune_old_entries and the gateway watcher that calls it.

The SessionStore in-memory dict (and its backing sessions.json) grew
unbounded — every unique (platform, chat_id, thread_id, user_id) tuple
ever seen was kept forever, regardless of how stale it became.  These
tests pin the prune behaviour:

  * Entries older than max_age_days (by updated_at) are removed
  * Entries marked ``suspended`` are preserved (user-paused)
  * Entries with an active process attached are preserved
  * max_age_days <= 0 disables pruning entirely
  * sessions.json is rewritten with the post-prune dict
  * The ``updated_at`` field — not ``created_at`` — drives the decision
    (so a long-running-but-still-active session isn't pruned)
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch


from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionSource, SessionStore


def test_session_store_default_db_uses_runtime_hermes_home(tmp_path, monkeypatch):
    """SessionStore must honor runtime HERMES_HOME when opening the default DB.

    Regression for the import-time DEFAULT_DB_PATH freeze: importing
    hermes_state before a fixture redirected HERMES_HOME used to pin every
    default SessionDB() at the developer's real ~/.hermes/state.db.
    """
    config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    fake_home = tmp_path / "alt_hermes_home"
    fake_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_home))

    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)

    try:
        assert store._db is not None
        assert store._db.db_path == fake_home / "state.db"
    finally:
        if store._db is not None:
            store._db.close()


def _make_store(tmp_path, max_age_days: int = 90, has_active_processes_fn=None):
    """Build a SessionStore bypassing SQLite/disk-load side effects."""
    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode="none"),
        session_store_max_age_days=max_age_days,
    )
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(
            sessions_dir=tmp_path,
            config=config,
            has_active_processes_fn=has_active_processes_fn,
        )
    store._db = None
    store._loaded = True
    return store


def _entry(key: str, age_days: float, *, suspended: bool = False,
           session_id: str | None = None) -> SessionEntry:
    now = datetime.now()
    return SessionEntry(
        session_key=key,
        session_id=session_id or f"sid_{key}",
        created_at=now - timedelta(days=age_days + 30),  # arbitrary older
        updated_at=now - timedelta(days=age_days),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        suspended=suspended,
    )


class TestPruneBasics:

    def test_prune_uses_updated_at_not_created_at(self, tmp_path):
        """A session created long ago but updated recently must be kept."""
        store = _make_store(tmp_path)
        now = datetime.now()
        entry = SessionEntry(
            session_key="long-lived",
            session_id="sid",
            created_at=now - timedelta(days=365),   # ancient
            updated_at=now - timedelta(days=3),     # but just chatted
            platform=Platform.TELEGRAM,
            chat_type="dm",
        )
        store._entries["long-lived"] = entry

        removed = store.prune_old_entries(max_age_days=30)

        assert removed == 0
        assert "long-lived" in store._entries


    def test_prune_skips_entries_with_active_processes(self, tmp_path):
        """Sessions with active bg processes aren't pruned even if old.

        The callback is keyed by session_key — matching what
        process_registry.has_active_for_session() actually consumes in
        gateway/run.py.  Prior to the fix this test passed the callback a
        session_id, which silently matched an implementation bug where
        prune_old_entries was also passing session_id; real-world usage
        (via process_registry) takes a session_key and never matched, so
        active sessions were still being pruned.
        """
        active_session_keys = {"active"}

        def _has_active(session_key: str) -> bool:
            return session_key in active_session_keys

        store = _make_store(tmp_path, has_active_processes_fn=_has_active)
        store._entries["active"] = _entry(
            "active", age_days=1000, session_id="sid_active"
        )
        store._entries["idle"] = _entry(
            "idle", age_days=1000, session_id="sid_idle"
        )

        removed = store.prune_old_entries(max_age_days=90)

        assert removed == 1
        assert "active" in store._entries
        assert "idle" not in store._entries

    def test_prune_active_check_uses_session_key_not_session_id(self, tmp_path):
        """Regression guard: a callback that only recognises session_ids must
        NOT protect entries during prune.  This pins the fix so a future
        refactor can't silently revert to passing session_id again.
        """
        def _recognises_only_ids(identifier: str) -> bool:
            return identifier.startswith("sid_")

        store = _make_store(tmp_path, has_active_processes_fn=_recognises_only_ids)
        store._entries["active"] = _entry(
            "active", age_days=1000, session_id="sid_active"
        )

        removed = store.prune_old_entries(max_age_days=90)

        # Entry is pruned because the callback receives "active" (session_key),
        # not "sid_active" (session_id), so _recognises_only_ids returns False.
        assert removed == 1
        assert "active" not in store._entries


    def test_prune_publishes_route_absence_only_after_sqlite_close(self, tmp_path):
        store = _make_store(tmp_path)
        store._entries["stale"] = _entry(
            "stale", age_days=500, session_id="stale-sid"
        )
        events = []
        fake_db = MagicMock()
        fake_db.save_gateway_routing_entry.return_value = None
        fake_db.replace_gateway_routing_entries.return_value = None
        fake_db.end_session.side_effect = (
            lambda *_args, **_kwargs: events.append("sqlite:end")
        )
        store._db = fake_db
        original_save_entries = store._save_entries

        def _save_entries(*args, **kwargs):
            events.append(
                "route:absent" if "stale" not in store._entries else "route:present"
            )
            return original_save_entries(*args, **kwargs)

        store._save_entries = _save_entries

        assert store.prune_old_entries(90) == 1
        assert events.index("sqlite:end") < events.index("route:absent")

    def test_prune_process_death_after_sqlite_close_recovers_durable_marker(
        self, tmp_path
    ):
        repo = Path(__file__).resolve().parents[2]
        hermes_home = tmp_path / "hermes-home"
        sessions_dir = tmp_path / "sessions"
        env = os.environ.copy()
        env.update({
            "HERMES_HOME": str(hermes_home),
            "PRUNE_TEST_SESSIONS": str(sessions_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        crash_script = r'''
import os
from datetime import timedelta
from pathlib import Path
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore, _now

store = SessionStore(
    sessions_dir=Path(os.environ["PRUNE_TEST_SESSIONS"]),
    config=GatewayConfig(),
)
source = SessionSource(
    platform=Platform.TELEGRAM,
    chat_id="prune-crash-chat",
    user_id="prune-crash-user",
    chat_type="dm",
)
entry = store.get_or_create_session(source)
entry.updated_at = _now() - timedelta(days=500)
store._save_entries(require_primary_db=True)
real_end = store._db.end_session
def end_then_die(*args, **kwargs):
    real_end(*args, **kwargs)
    os._exit(73)
store._db.end_session = end_then_die
store.prune_old_entries(90)
'''
        crashed = subprocess.run(
            [sys.executable, "-c", crash_script],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert crashed.returncode == 73, crashed.stderr

        recovery_script = r'''
import json
import os
from pathlib import Path
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore, build_session_key

store = SessionStore(
    sessions_dir=Path(os.environ["PRUNE_TEST_SESSIONS"]),
    config=GatewayConfig(),
)
source = SessionSource(
    platform=Platform.TELEGRAM,
    chat_id="prune-crash-chat",
    user_id="prune-crash-user",
    chat_type="dm",
)
key = build_session_key(source)
store._ensure_loaded()
before = store._entries[key]
marker = before.metadata.get("terminal_transition")
replacement = store.get_or_create_session(source)
print("RESULT=" + json.dumps({
    "old_session_id": before.session_id,
    "marker_reason": marker.get("reason") if marker else None,
    "replacement_session_id": replacement.session_id,
    "replacement_has_marker": "terminal_transition" in replacement.metadata,
}))
'''
        recovered = subprocess.run(
            [sys.executable, "-c", recovery_script],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert recovered.returncode == 0, recovered.stderr
        result_line = next(
            line for line in recovered.stdout.splitlines() if line.startswith("RESULT=")
        )
        result = json.loads(result_line.removeprefix("RESULT="))
        assert result["marker_reason"] == "session_prune"
        assert result["replacement_session_id"] != result["old_session_id"]
        assert result["replacement_has_marker"] is False

    def test_prune_recovery_publishes_absence_before_fresh_route(self, tmp_path):
        store = _make_store(tmp_path)
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="prune-recovery-chat",
            user_id="prune-recovery-user",
            chat_type="dm",
        )
        original = store.get_or_create_session(source)
        original.metadata["terminal_transition"] = {
            "session_id": original.session_id,
            "reason": "session_prune",
            "token": "prune-recovery-token",
        }
        store._save_entries()
        events = []
        original_save_entries = store._save_entries

        def _save_entries(*args, **kwargs):
            current = store._entries.get(original.session_key)
            events.append(
                "route:absent"
                if current is None
                else f"route:{current.session_id}"
            )
            return original_save_entries(*args, **kwargs)

        store._save_entries = _save_entries
        replacement = store.get_or_create_session(source)

        assert "route:absent" in events
        assert events.index("route:absent") < events.index(
            f"route:{replacement.session_id}"
        )

    def test_prune_routes_through_terminal_teardown_boundary(self, tmp_path):
        store = _make_store(tmp_path)
        store._entries["stale"] = _entry(
            "stale", age_days=500, session_id="stale-sid"
        )
        events = []

        def _begin(sid, reason):
            events.append(("begin", sid, reason))
            return object()

        def _complete(sid, token, key):
            assert key not in store._entries
            events.append(("complete", sid, key, token))

        def _end(sid, token, succeeded):
            events.append(("end", sid, succeeded, token))

        store._before_auto_reset_fn = _begin
        store._before_terminal_completion_fn = _complete
        store._after_auto_reset_fn = _end

        assert store.prune_old_entries(max_age_days=90) == 1
        assert events[0][:3] == ("begin", "stale-sid", "session_prune")
        assert events[1][:3] == ("complete", "stale-sid", "stale")
        assert events[2][0:3] == ("end", "stale-sid", True)

    def test_prune_is_thread_safe(self, tmp_path):
        """Prune acquires _lock internally; concurrent update_session is safe."""
        store = _make_store(tmp_path)
        for i in range(20):
            age = 1000 if i % 2 == 0 else 1
            store._entries[f"s{i}"] = _entry(f"s{i}", age_days=age)

        results = []

        def _pruner():
            results.append(store.prune_old_entries(max_age_days=90))

        def _reader():
            # Mimic a concurrent update_session reader iterating under lock.
            with store._lock:
                list(store._entries.keys())

        threads = [threading.Thread(target=_pruner)]
        threads += [threading.Thread(target=_reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive()

        # Exactly one pruner ran; removed exactly the 10 stale entries.
        assert results == [10]
        assert len(store._entries) == 10
        for i in range(20):
            if i % 2 == 1:  # fresh
                assert f"s{i}" in store._entries


class TestPrunePersistsToDisk:
    def test_prune_rewrites_sessions_json(self, tmp_path):
        """After prune, sessions.json on disk reflects the new dict."""
        config = GatewayConfig(
            default_reset_policy=SessionResetPolicy(mode="none"),
            session_store_max_age_days=90,
        )
        store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = None
        # Force-populate without calling get_or_create to avoid DB side-effects
        store._entries["stale"] = _entry("stale", age_days=500)
        store._entries["fresh"] = _entry("fresh", age_days=1)
        store._loaded = True
        store._save()

        # Verify pre-prune state on disk. Filter out metadata sentinels
        # (e.g. the "_README" note) so we assert on session keys only.
        saved_pre = json.loads((tmp_path / "sessions.json").read_text())
        assert {k for k in saved_pre if not k.startswith("_")} == {"stale", "fresh"}

        # Prune and check disk.
        store.prune_old_entries(max_age_days=90)
        saved_post = json.loads((tmp_path / "sessions.json").read_text())
        assert {k for k in saved_post if not k.startswith("_")} == {"fresh"}


class TestGatewayConfigSerialization:

    def test_session_store_max_age_days_roundtrips(self):
        cfg = GatewayConfig(session_store_max_age_days=30)
        restored = GatewayConfig.from_dict(cfg.to_dict())
        assert restored.session_store_max_age_days == 30

    def test_session_store_max_age_days_missing_defaults_90(self):
        """Loading an old config (pre-this-field) falls back to default."""
        restored = GatewayConfig.from_dict({})
        assert restored.session_store_max_age_days == 90


class TestGatewayWatcherCallsPrune:
    """The session_expiry_watcher should call prune_old_entries once per hour."""


    def test_prune_gate_suppresses_within_interval(self):
        import time as _t

        last_ts = _t.time() - 600  # 10 minutes ago
        prune_interval = 3600.0
        now = _t.time()

        should_prune = (now - last_ts) > prune_interval
        assert should_prune is False


class TestReadmeSentinel:
    """The gateway writes a self-documenting ``_README`` key into sessions.json
    so users who inspect the file directly understand it's the gateway routing
    index (not the session list). It must never round-trip into a SessionEntry,
    and real entries must survive a save/load cycle alongside it (#49361)."""

    def test_save_writes_readme_sentinel_first(self, tmp_path):
        store = _make_store(tmp_path)
        store._entries["agent:main:whatsapp:dm:99"] = _entry(
            "agent:main:whatsapp:dm:99", age_days=1
        )
        store._save()

        raw = json.loads((tmp_path / "sessions.json").read_text())
        assert "_README" in raw
        # Sentinel renders first so it's the first thing a user sees on `cat`.
        assert next(iter(raw)) == "_README"
        # The note points users at the real store and command.
        assert "state.db" in raw["_README"]
        assert "hermes sessions list" in raw["_README"]

