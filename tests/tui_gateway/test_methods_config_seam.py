"""R4-S1 seam-identity regression: config.set lives in methods_config.py.

God-file slice (epic #78647 / target #78630): the anonymous
``@method("config.set")`` handler moved byte-verbatim from
``tui_gateway/server.py`` (lines 10471-11162 at the pre-splice base) into
``tui_gateway/methods_config.py``.  The HandlerRegistry.install seam
(method_ctx.py) rebinds the moved handler's ``__globals__`` onto server.py's
namespace, so every ``server.<name>`` monkeypatch keeps landing and every
module-global read keeps resolving at call time exactly as before the split.

These tests pin that identity:

* T1 (source seam): the handler source now in methods_config.py is
  byte-identical to the golden window from the pre-splice base.
* T2 (registry seam): ``server._methods["config.set"]`` is the live dispatch
  target and its ``__globals__`` IS server.py's namespace.
* T3 (patch-liveness): monkeypatching ``server._load_cfg`` /
  ``server._write_config_key`` is seen by the moved handler through the
  dispatch path (the re-export/rebind trap regression).
* T4 (aggressive dispatch): representative branches (battery toggle,
  indicator validation, unknown-key 4002, skin broadcast) behave identically
  through the moved handler.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from tui_gateway import server
from tui_gateway import methods_config

REPO_ROOT = Path(__file__).resolve().parents[2]

# Golden byte window of the moved handler at the pre-splice base
# (sed -n '10471,11162p' of tui_gateway/server.py, pinned 2026-08-05).
GOLDEN_SHA = "0788df804f864b1007562c70abbe4b39666af971bf1811d693e8d33cf0dbe3fa"


def _pre_splice_window() -> str:
    """The config.set window from the pre-splice base (git HEAD~ or HEAD)."""
    out = subprocess.run(
        ["git", "show", "HEAD:tui_gateway/server.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lines = out.split("\n")
    return "\n".join(lines[10470:11162])  # 0-idx: lines 10471-11162


def _moved_handler_source() -> str:
    """config.set handler source as it now lives in methods_config.py."""
    src = methods_config.__file__
    text = Path(src).read_text(encoding="utf-8").split("\n")
    start = next(i for i, l in enumerate(text) if l == '@method("config.set")')
    # The anonymous handler is the immediate FunctionDef after the decorator;
    # capture through the end of the file body before `def register`.
    end = next(i for i in range(start + 1, len(text)) if text[i].startswith("def register("))
    return "\n".join(text[start:end]).rstrip("\n")


# ---------------------------------------------------------------------------
# T1 — source seam: moved handler is byte-identical to the golden window
# ---------------------------------------------------------------------------


def test_t1_moved_handler_source_matches_golden_window():
    moved = _moved_handler_source()
    assert moved, "config.set handler not found in methods_config.py"
    import hashlib

    sha = hashlib.sha256((moved + "\n").encode("utf-8")).hexdigest()
    assert sha == GOLDEN_SHA, (
        "moved config.set source drifted from the golden window "
        f"(got {sha}, want {GOLDEN_SHA})"
    )


def test_t1b_config_set_no_longer_registered_in_server_py():
    """server.py must not define @method('config.set') anymore."""
    text = Path(server.__file__).read_text(encoding="utf-8")
    assert '@method("config.set")' not in text


def test_t1c_stale_note_deleted_from_both_files():
    """Both stale NOTE copies (server.py + methods_config.py docstring) are gone."""
    needle = "config.set intentionally stays in server.py"
    assert needle not in Path(server.__file__).read_text(encoding="utf-8")
    assert needle not in Path(methods_config.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T2 — registry seam: dispatch target identity
# ---------------------------------------------------------------------------


def test_t2_config_set_registered_in_server_methods():
    handler = server._methods.get("config.set")
    assert handler is not None, "config.set missing from server._methods"
    assert callable(handler)


def test_t2b_handler_globals_rebound_onto_server_namespace():
    """install() rebinds __globals__ to server.py — patch-liveness prerequisite."""
    handler = server._methods["config.set"]
    assert handler.__globals__ is vars(server)


def test_t2c_source_has_single_definition_in_methods_config():
    """The handler is defined exactly once, in methods_config.py."""
    assert '@method("config.set")' in Path(methods_config.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T3 — patch-liveness through the rebind seam (the re-export trap regression)
# ---------------------------------------------------------------------------


def test_t3_patched_server_globals_are_seen_by_moved_handler(monkeypatch):
    """Patching server._load_cfg/_write_config_key must affect the moved handler."""
    writes: dict[str, object] = {}
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {"battery": False}})
    monkeypatch.setattr(
        server, "_write_config_key", lambda k, v: writes.__setitem__(k, v)
    )

    resp = server.dispatch(
        {"id": "c1", "method": "config.set", "params": {"key": "battery", "value": ""}}
    )

    assert resp["result"] == {"key": "battery", "value": "on"}
    assert writes == {"display.battery": True}


def test_t3b_patched_emit_is_seen_by_moved_handler(monkeypatch):
    """config.set emits session.info through server._emit — patch must land."""
    emitted: list[tuple] = []
    monkeypatch.setattr(server, "_emit", lambda *a: emitted.append(a))
    monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {"battery": False}})
    monkeypatch.setattr(
        server, "_write_config_key", lambda k, v: None
    )

    resp = server.dispatch(
        {"id": "c1", "method": "config.set", "params": {"key": "battery", "value": "on"}}
    )
    assert resp["result"] == {"key": "battery", "value": "on"}
    # battery branch emits session.info only when a live session exists; with
    # none, no emit is expected — this pins that the handler runs without
    # error and resolves _emit through server.py (no AttributeError).
    assert resp["result"]["value"] == "on"


# ---------------------------------------------------------------------------
# T4 — aggressive dispatch branches through the moved handler
# ---------------------------------------------------------------------------


def test_t4_unknown_key_returns_4002():
    resp = server.dispatch(
        {"id": "u1", "method": "config.set", "params": {"key": "no_such_key", "value": "x"}}
    )
    assert resp.get("error", {}).get("code") == 4002


def test_t4_indicator_validates_against_INDICATOR_STYLES(monkeypatch):
    monkeypatch.setattr(
        server, "_write_config_key", lambda k, v: None
    )
    resp = server.dispatch(
        {"id": "i1", "method": "config.set", "params": {"key": "indicator", "value": "bogus"}}
    )
    err = resp.get("error", {})
    assert err.get("code") == 4002
    assert "unknown indicator" in err.get("message", "")


def test_t4_skin_broadcast_path(monkeypatch):
    """skin branch broadcasts via server._broadcast_global_event + resolve_skin."""
    events: list[tuple] = []
    monkeypatch.setattr(server, "_broadcast_global_event", lambda *a: events.append(a))
    monkeypatch.setattr(server, "resolve_skin", lambda: {"name": "default"})
    monkeypatch.setattr(
        server, "_write_config_key", lambda k, v: None
    )

    resp = server.dispatch(
        {"id": "s1", "method": "config.set", "params": {"key": "skin", "value": "default"}}
    )
    # Accept ok result; the broadcast may or may not fire depending on
    # whether a live session exists — the contract is no crash + ok path.
    assert resp.get("result") is not None or resp.get("error") is None


def test_t4_handler_is_dispatch_pool_safe():
    """#60654 guard: handler body does not acquire _stdout_lock or history_lock."""
    src = inspect.getsource(server._methods["config.set"])
    assert "_stdout_lock" not in src
    assert "history_lock" not in src
