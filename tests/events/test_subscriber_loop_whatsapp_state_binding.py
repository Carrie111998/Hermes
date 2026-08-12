"""The subscriber poll loop must carry EVERY env-derived path, not just one.

``startup()`` already captures ``gateway_heartbeat_path()`` and carries it into
``_subscriber_poll_loop`` — its docstring spells out why: ``shutdown()``'s
``join(timeout=5)`` can expire on a loaded box, so a tick that lands after
teardown would otherwise follow ``HERMES_HOME`` into whatever the test restored.

The same loop still calls ``save_state(whatsapp_flush_state_path(), ...)`` —
resolved live, on the tick. ``save_state`` does ``parent.mkdir(parents=True)``
+ ``write_text`` + ``os.replace``, so that one line reintroduces exactly the
bug the carried heartbeat path was added to prevent.

See GBrain ``concepts/import-time-hermes-home-snapshot-bug``.
"""

import inspect
from pathlib import Path

from events import gateway_integration


def test_poll_loop_accepts_a_carried_whatsapp_state_path():
    """A caller must be able to bind the path, exactly as with the heartbeat."""
    params = inspect.signature(gateway_integration._subscriber_poll_loop).parameters

    assert "whatsapp_state_path" in params, (
        "_subscriber_poll_loop has no seam to carry the whatsapp flush state "
        "path — it can only resolve it live, on the tick"
    )


def test_poll_loop_does_not_resolve_the_whatsapp_state_path_live():
    """The loop body must use the carried value, never the live resolver."""
    src = inspect.getsource(gateway_integration._subscriber_poll_loop)

    assert "whatsapp_flush_state_path()" not in src, (
        "_subscriber_poll_loop still calls whatsapp_flush_state_path() inside "
        "the loop; a tick landing after monkeypatch teardown mkdirs and writes "
        "into the restored real ~/.hermes"
    )


def test_startup_captures_the_whatsapp_state_path():
    """Capture happens at startup, where the value's meaning is fixed."""
    src = inspect.getsource(gateway_integration.startup)

    assert "whatsapp_flush_state_path()" in src, (
        "startup() does not capture the whatsapp flush state path to carry "
        "into the subscriber thread"
    )


def test_poll_loop_accepts_a_carried_digest_state_path():
    """The digest state save has the identical defect, one branch over.

    ``save_state(digest_state_path(), _state)`` is a live per-tick resolve plus
    a write. The detector reports only the first env/write pair it finds per
    candidate, so this one hid behind the whatsapp hit.
    """
    params = inspect.signature(gateway_integration._subscriber_poll_loop).parameters

    # Deliberately NOT named `digest_state_path`: that would shadow the
    # module-level import of the same name inside a ~300-line function, so a
    # later edit calling digest_state_path() there would break silently.
    assert "digest_path" in params, (
        "_subscriber_poll_loop has no seam to carry the digest state path"
    )


def test_poll_loop_does_not_resolve_the_digest_state_path_live():
    src = inspect.getsource(gateway_integration._subscriber_poll_loop)

    assert "digest_state_path()" not in src, (
        "_subscriber_poll_loop still calls digest_state_path() inside the "
        "loop; the tick that lands after teardown writes the digest state "
        "into the restored real ~/.hermes"
    )


def test_direct_callers_still_resolve_live(tmp_path, monkeypatch):
    """Passing nothing keeps the correct behaviour for non-deferred callers."""
    home = tmp_path / "live_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    resolved = gateway_integration._resolve_whatsapp_state_path(None)

    assert isinstance(resolved, Path)
    assert str(home) in str(resolved), (
        "the lazy fallback must resolve against the live HERMES_HOME"
    )


def test_carried_path_wins_over_the_environment(tmp_path, monkeypatch):
    """The whole point: a bound path must not follow a later env change."""
    captured = tmp_path / "captured" / "whatsapp_flush_state.json"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "restored_home"))

    assert gateway_integration._resolve_whatsapp_state_path(captured) == captured
