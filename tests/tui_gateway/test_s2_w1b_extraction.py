"""Wave-1 god-file extraction regression tests (shard s2, clusters c6 + c17).

The change-watcher probes/broadcast loop and the display/agent config
loaders moved from ``tui_gateway/server.py`` into
``change_watcher_mixin.py`` / ``agent_config_loaders.py`` and are rebound
onto the server namespace at import time (see method_ctx.py).  These tests
pin the two things that must survive the move:

1. the names still resolve on the server module — entry.py imports
   ``resolve_skin`` directly and ws.py/compute_host.py/methods_* call the
   others through the ``server`` namespace, so a broken rebind breaks the
   gateway at import time;
2. the pure resolvers still return the same normalized values.
"""

import os
import threading

import pytest

from tui_gateway import server

_CHANGE_WATCHER_NAMES = (
    "resolve_skin",
    "_skin_sig",
    "_note_skin_broadcast",
    "_broadcast_skin_if_changed",
    "_watcher_home",
    "_pet_sig",
    "_pet_changed_payload",
    "_cron_sig",
    "_sessions_sig",
    "_platforms_sig",
    "_pairing_sig",
    "_broadcast_watched_changes",
    "_ensure_skin_watcher",
)
_CONFIG_LOADER_NAMES = (
    "_load_approval_mode",
    "_coerce_statusbar",
    "_display_mouse_tracking",
    "_load_reasoning_config",
    "_load_service_tier",
    "_load_provider_routing",
    "_load_show_reasoning",
    "_load_memory_notifications",
    "_load_tool_progress_mode",
    "_load_enabled_toolsets",
)


def test_moved_functions_rebound_onto_server_namespace():
    """Bodies moved to the mixin modules, but names still resolve on server."""
    for name in _CHANGE_WATCHER_NAMES:
        fn = getattr(server, name)
        assert callable(fn)
        assert fn.__code__.co_filename.endswith("change_watcher_mixin.py"), name
        assert fn.__globals__ is server.__dict__, name
    for name in _CONFIG_LOADER_NAMES:
        fn = getattr(server, name)
        assert callable(fn)
        assert fn.__code__.co_filename.endswith("agent_config_loaders.py"), name
        assert fn.__globals__ is server.__dict__, name
    # cluster-private constants moved along and stay visible on server
    assert server._STATUSBAR_MODES == frozenset({"off", "top", "bottom"})
    assert server._MOUSE_TRACKING_ALIASES["click"] == "buttons"
    assert server._APPROVAL_MODES == frozenset({"manual", "smart", "off"})


def test_entry_and_ws_still_import_moved_names():
    """entry.py imports resolve_skin by name; ws.py calls server.* directly."""
    from tui_gateway import entry, ws  # noqa: F401

    assert entry.resolve_skin is server.resolve_skin
    assert ws.server._ensure_skin_watcher is server._ensure_skin_watcher


def test_coerce_statusbar():
    assert server._coerce_statusbar(False) == "off"
    assert server._coerce_statusbar("TOP") == "top"
    assert server._coerce_statusbar("bottom") == "bottom"
    assert server._coerce_statusbar(" off ") == "off"
    assert server._coerce_statusbar(None) == "top"
    assert server._coerce_statusbar("side") == "top"


def test_display_mouse_tracking():
    assert server._display_mouse_tracking({}) == "all"  # legacy default True
    assert server._display_mouse_tracking(None) == "all"
    assert server._display_mouse_tracking({"mouse_tracking": False}) == "off"
    assert server._display_mouse_tracking({"mouse_tracking": "wheel"}) == "wheel"
    assert server._display_mouse_tracking({"mouse_tracking": "click"}) == "buttons"
    assert server._display_mouse_tracking({"mouse_tracking": "full"}) == "all"
    assert server._display_mouse_tracking({"mouse_tracking": "bogus"}) == "all"
    assert server._display_mouse_tracking({"tui_mouse": False}) == "off"
    assert server._display_mouse_tracking({"tui_mouse": "scroll"}) == "wheel"


def test_load_service_tier(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {"service_tier": "priority"}})
    assert server._load_service_tier() == "priority"
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {"service_tier": "FAST"}})
    assert server._load_service_tier() == "priority"
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {"service_tier": "standard"}})
    assert server._load_service_tier() is None
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {}})
    assert server._load_service_tier() is None


def test_load_show_reasoning(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {}})
    assert server._load_show_reasoning() is True
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {"show_reasoning": False}})
    assert server._load_show_reasoning() is False


def test_load_memory_notifications(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {}})
    assert server._load_memory_notifications() == "on"
    monkeypatch.setattr(
        server, "_load_cfg", lambda: {"display": {"memory_notifications": False}}
    )
    assert server._load_memory_notifications() == "off"
    monkeypatch.setattr(
        server, "_load_cfg", lambda: {"display": {"memory_notifications": "verbose"}}
    )
    assert server._load_memory_notifications() == "verbose"


def test_load_tool_progress_mode(monkeypatch):
    monkeypatch.delenv("HERMES_TUI_TOOL_PROGRESS", raising=False)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {}})
    assert server._load_tool_progress_mode() == "all"
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {"tool_progress": False}})
    assert server._load_tool_progress_mode() == "off"
    monkeypatch.setenv("HERMES_TUI_TOOL_PROGRESS", "verbose")
    assert server._load_tool_progress_mode() == "verbose"


def test_load_approval_mode(monkeypatch):
    import tools.approval as approval

    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "smart")
    assert server._load_approval_mode() == "smart"
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "off")
    assert server._load_approval_mode() == "off"
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "bogus")
    assert server._load_approval_mode() == "manual"


def test_load_enabled_toolsets_explicit_env(monkeypatch):
    monkeypatch.setenv("HERMES_TUI_TOOLSETS", "web, terminal")
    result = server._load_enabled_toolsets()
    assert result == ["web", "terminal"]


def test_skin_sig_and_watcher_home(monkeypatch, tmp_path):
    (tmp_path / "skins").mkdir()
    (tmp_path / "skins" / "custom.yaml").write_text("name: custom\n")
    monkeypatch.setattr(server, "_hermes_home", str(tmp_path))
    monkeypatch.setattr(server, "get_hermes_home_override", lambda: None)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {"skin": "custom"}})

    name, mtime = server._skin_sig()
    assert name == "custom"
    assert mtime is not None
    assert server._watcher_home() == tmp_path


def test_change_watcher_wiring_after_move(monkeypatch, tmp_path):
    """The extracted broadcast pass still drives events through server state."""
    monkeypatch.setattr(server, "_hermes_home", str(tmp_path))
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_change_sigs", {})
    monkeypatch.setattr(server, "_change_checked_at", {})
    monkeypatch.setattr(server, "_change_broadcast_at", {})
    events = []
    monkeypatch.setattr(
        server, "_broadcast_global_event", lambda ev, payload=None: events.append(ev)
    )
    state_db = tmp_path / "state.db"
    state_db.write_text("v1")
    # Pin explicit mtimes: rapid write_text calls can land on the same NTFS
    # timestamp tick, which would make the signature comparison a no-op.
    os.utime(state_db, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    server._broadcast_watched_changes(now=0.0)  # first sighting seeds silently
    assert events == []

    state_db.write_text("v2")
    os.utime(state_db, ns=(1_700_000_000_000_100_000, 1_700_000_000_000_100_000))
    server._broadcast_watched_changes(now=1.0)  # a move broadcasts once
    assert events == ["sessions.changed"]

    state_db.write_text("v3")
    os.utime(state_db, ns=(1_700_000_000_000_200_000, 1_700_000_000_000_200_000))
    server._broadcast_watched_changes(now=2.5)  # inside the 2s floor: suppressed
    assert events == ["sessions.changed"]

    server._broadcast_watched_changes(now=4.0)  # floor window open: trailing edge
    assert events == ["sessions.changed", "sessions.changed"]


def test_ensure_skin_watcher_idempotent(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, name=None, daemon=None):
            self.target = target

        def start(self):
            started.append(self)

    monkeypatch.setattr(server, "_skin_watcher_started", False)
    monkeypatch.setattr(server, "_note_skin_broadcast", lambda: None)
    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    server._ensure_skin_watcher()
    server._ensure_skin_watcher()

    assert len(started) == 1
    assert server._skin_watcher_started is True


def test_resolve_skin_returns_dict(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {}})
    result = server.resolve_skin()
    assert isinstance(result, dict)
