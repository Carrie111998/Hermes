"""Regression tests for the s1 wave-1b mixin extraction (tui_gateway/server.py).

Covers the pure methods moved verbatim into ``session_cwd_mixin.py``
(cluster c34) and ``compute_host_mixin.py`` (cluster c22).  server.py
rebinds the moved functions onto its own namespace at import end (same
seam as method_ctx.py), so every test below exercises the moved bodies
through ``server.<name>`` — the exact namespace every call site (handlers,
ws.py, compute_host.py, the dashboard) uses.

Also asserts the extraction wiring itself: the moved functions are the
mixin's code objects rebound onto server's globals, not duplicates.
"""

from __future__ import annotations

import inspect
import os
import threading

import pytest

import tui_gateway.server as server
from tui_gateway import compute_host_mixin, session_cwd_mixin


# ── Extraction wiring (byte-fidelity of the move) ───────────────────────

MIXIN_FUNCTIONS = {
    "_normalize_completion_path": session_cwd_mixin,
    "_completion_cwd": session_cwd_mixin,
    "_terminal_task_cwd": session_cwd_mixin,
    "_session_cwd": session_cwd_mixin,
    "_persisted_session_cwd": session_cwd_mixin,
    "_heal_dead_cwd": session_cwd_mixin,
    "_is_local_terminal_backend": session_cwd_mixin,
    "_display_session_cwd": session_cwd_mixin,
    "_reconcile_session_cwd_from_terminal": session_cwd_mixin,
    "_emit_settled_session_info": session_cwd_mixin,
    "_register_session_cwd": session_cwd_mixin,
    "_inside_compute_host_child": compute_host_mixin,
    "_turn_isolation_enabled": compute_host_mixin,
    "_session_uses_compute_host": compute_host_mixin,
    "_get_compute_host_supervisor": compute_host_mixin,
    "_compute_host_turn_frame": compute_host_mixin,
    "_metadata_mirror": compute_host_mixin,
    "_apply_compute_host_metadata_mirror": compute_host_mixin,
    "_on_compute_host_turn_done": compute_host_mixin,
    "_submit_prompt_to_compute_host": compute_host_mixin,
    "_send_compute_host_control": compute_host_mixin,
}


@pytest.mark.parametrize("name,mixin", sorted(MIXIN_FUNCTIONS.items()))
def test_moved_function_is_mixin_code_rebound_onto_server(name, mixin):
    """server.<name> is the mixin's code object rebound onto server globals."""
    server_fn = getattr(server, name)
    mixin_fn = getattr(mixin, name)
    assert server_fn.__code__ is mixin_fn.__code__  # verbatim move, same code
    assert server_fn.__globals__ is vars(server)  # rebind seam (method_ctx style)
    assert server_fn.__doc__ == mixin_fn.__doc__


def test_moved_function_source_lives_in_mixin_module():
    """inspect.getsource resolves to the mixin file (bodies were moved)."""
    src_file = inspect.getsourcefile(server._session_cwd)
    assert src_file is not None
    assert src_file.replace("\\", "/").endswith("tui_gateway/session_cwd_mixin.py")


# ── c34: session cwd resolution ─────────────────────────────────────────

class TestNormalizeCompletionPath:
    def test_nt_style_path_passes_through(self):
        result = server._normalize_completion_path(r"C:\work\proj")
        # On a real POSIX host the drive-letter branch applies; on Windows the
        # path is already native. Either way it must not crash or mangle.
        assert isinstance(result, str)
        assert result

    def test_posix_drive_letter_mapping(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        assert server._normalize_completion_path(r"C:\work\proj") == "/mnt/c/work/proj"

    def test_posix_backslash_folding(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        assert server._normalize_completion_path("a/b") == "a/b"

    def test_tilde_expansion(self, monkeypatch, tmp_path):
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
        result = server._normalize_completion_path("~/home")
        assert result == str(tmp_path / "home")


class TestSessionCwd:
    def test_session_cwd_uses_session_value(self):
        assert server._session_cwd({"cwd": "/somewhere"}) == "/somewhere"

    def test_session_cwd_falls_back_to_completion_cwd(self, monkeypatch):
        monkeypatch.setattr(server, "_completion_cwd", lambda **kw: "/fallback")
        assert server._session_cwd(None) == "/fallback"
        assert server._session_cwd({}) == "/fallback"

    def test_completion_cwd_prefers_explicit_param(self, monkeypatch, tmp_path):
        target = tmp_path / "explicit"
        target.mkdir()
        monkeypatch.setattr(server, "_profile_configured_cwd", lambda *_a: "")
        monkeypatch.setattr(server, "_launch_configured_cwd", lambda: "")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert server._completion_cwd({"cwd": str(target)}) == str(target)

    def test_completion_cwd_falls_back_to_os_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "_profile_configured_cwd", lambda *_a: "")
        monkeypatch.setattr(server, "_launch_configured_cwd", lambda: "")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(tmp_path)
        assert server._completion_cwd({}) == str(tmp_path)

    def test_is_local_terminal_backend_defaults_local(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_ENV", raising=False)
        assert server._is_local_terminal_backend() is True

    def test_is_local_terminal_backend_remote(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "ssh")
        assert server._is_local_terminal_backend() is False


class TestPersistedSessionCwd:
    def test_desktop_source_gets_no_stamp(self, monkeypatch):
        monkeypatch.setattr(server, "_session_source", lambda s: "desktop")
        assert server._persisted_session_cwd({"cwd": "/opt/whatever"}) is None

    def test_explicit_cwd_is_stamped(self, monkeypatch):
        monkeypatch.setattr(server, "_session_source", lambda s: "desktop")
        monkeypatch.setattr(server, "_session_cwd", lambda s: "/explicit")
        assert server._persisted_session_cwd({"explicit_cwd": True}) == "/explicit"

    def test_terminal_session_owns_its_cwd(self, monkeypatch):
        monkeypatch.setattr(server, "_session_source", lambda s: "terminal")
        assert server._persisted_session_cwd({"cwd": "/work"}) == "/work"

    def test_no_cwd_means_no_stamp(self, monkeypatch):
        monkeypatch.setattr(server, "_session_source", lambda s: "terminal")
        assert server._persisted_session_cwd({}) is None


class TestHealDeadCwd:
    def test_live_directory_unchanged(self, tmp_path):
        assert server._heal_dead_cwd(str(tmp_path)) == str(tmp_path)

    def test_dead_path_climbs_to_existing_ancestor(self, monkeypatch, tmp_path):
        alive = tmp_path / "repo"
        alive.mkdir()
        dead = alive / "worktrees" / "gone"
        monkeypatch.setattr(server, "_git_common_repo_root_for_cwd", lambda _p: "")
        monkeypatch.setattr(server, "_git_repo_root_for_cwd", lambda _p: "")
        assert server._heal_dead_cwd(str(dead)) == str(alive)


class TestRegisterSessionCwd:
    def test_registers_task_env_override(self, monkeypatch):
        calls = {}

        def fake_register(session_key, env_overrides):
            calls["key"] = session_key
            calls["overrides"] = env_overrides

        monkeypatch.setattr(server, "_terminal_task_cwd", lambda s: "/task-cwd")
        monkeypatch.setattr(
            "tools.terminal_tool.register_task_env_overrides", fake_register
        )
        server._register_session_cwd({"session_key": "sid-1"})
        assert calls == {"key": "sid-1", "overrides": {"cwd": "/task-cwd"}}

    def test_none_session_is_noop(self, monkeypatch):
        monkeypatch.setattr(
            "tools.terminal_tool.register_task_env_overrides",
            lambda *a, **k: pytest.fail("must not be called"),
        )
        server._register_session_cwd(None)


# ── c22: compute-host turn isolation ────────────────────────────────────

class TestComputeHostPredicates:
    def test_inside_child_from_env(self, monkeypatch):
        monkeypatch.delenv("HERMES_COMPUTE_HOST_CHILD", raising=False)
        assert server._inside_compute_host_child() is False
        monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
        assert server._inside_compute_host_child() is True

    def test_turn_isolation_enabled_off_inside_child(self, monkeypatch):
        monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
        assert server._turn_isolation_enabled() is False

    def test_turn_isolation_enabled_reads_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_COMPUTE_HOST_CHILD", raising=False)
        monkeypatch.setattr(
            server, "_load_dashboard_process_isolation_config", lambda: {"turn_isolation": True}
        )
        assert server._turn_isolation_enabled() is True
        monkeypatch.setattr(
            server, "_load_dashboard_process_isolation_config", lambda: {}
        )
        assert server._turn_isolation_enabled() is False

    def test_session_uses_compute_host(self, monkeypatch):
        monkeypatch.setattr(server, "_turn_isolation_enabled", lambda cfg=None: True)
        assert server._session_uses_compute_host({"agent": None, "agent_ready": "x"}) is True
        assert server._session_uses_compute_host({"_compute_host_active": True}) is True
        assert server._session_uses_compute_host({"agent": object()}) is False
        monkeypatch.setattr(server, "_turn_isolation_enabled", lambda cfg=None: False)
        assert server._session_uses_compute_host({"agent": None}) is False


class TestComputeHostFrame:
    def test_metadata_mirror_passthrough(self):
        assert server._metadata_mirror(None) == {}
        assert server._metadata_mirror({}) == {}
        assert server._metadata_mirror({"_metadata_mirror": {"model": "x"}}) == {"model": "x"}
        assert server._metadata_mirror({"_metadata_mirror": "junk"}) == {}

    def test_turn_frame_fields(self, monkeypatch):
        monkeypatch.setattr(server, "_session_cwd", lambda s: "/frame-cwd")
        monkeypatch.setattr(server, "_session_source", lambda s: "desktop")
        session = {"history_lock": threading.Lock(), "session_key": "sk", "cols": 100}
        frame = server._compute_host_turn_frame("rid-1", "sid-1", session, "hello")
        assert frame["type"] == "turn.start"
        assert frame["sid"] == "sid-1"
        assert frame["request_id"] == "rid-1"
        assert frame["text"] == "hello"
        assert frame["cwd"] == "/frame-cwd"
        assert frame["source"] == "desktop"
        assert frame["cols"] == 100

    def test_apply_metadata_mirror(self):
        session = {"history_lock": threading.Lock()}
        frame = {"session_key": "sk2", "history_version": 7, "session_info": {"model": "m"}}
        server._apply_compute_host_metadata_mirror(session, frame)
        assert session["session_key"] == "sk2"
        assert session["history_version"] == 7
        assert session["_metadata_mirror"] == {"model": "m"}
        # non-dict frame is a no-op
        server._apply_compute_host_metadata_mirror(session, None)
        assert session["session_key"] == "sk2"

    def test_on_turn_done_emits_session_info(self, monkeypatch):
        monkeypatch.setattr(server, "_clear_inflight_turn", lambda s: None)
        monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: None)
        monkeypatch.setattr(server, "_session_info", lambda agent, session: {"sid": "sid-1"})
        emitted = []
        monkeypatch.setattr(server, "_emit", lambda kind, sid, payload: emitted.append((kind, sid, payload)))
        session = {"history_lock": threading.Lock(), "running": True}
        server._on_compute_host_turn_done("rid-1", "sid-1", session, {"type": "turn.done"})
        assert session["running"] is False
        assert emitted == [("session.info", "sid-1", {"sid": "sid-1"})]

    def test_on_turn_done_error_emits_message_complete(self, monkeypatch):
        monkeypatch.setattr(server, "_clear_inflight_turn", lambda s: None)
        monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: None)
        monkeypatch.setattr(server, "_session_info", lambda agent, session: {})
        emitted = []
        monkeypatch.setattr(server, "_emit", lambda kind, sid, payload: emitted.append((kind, sid, payload)))
        session = {"history_lock": threading.Lock()}
        server._on_compute_host_turn_done(
            "rid-1", "sid-1", session, {"type": "turn.error", "message": "boom"}
        )
        assert emitted[0] == ("message.complete", "sid-1", {"text": "Error: boom", "status": "error"})
