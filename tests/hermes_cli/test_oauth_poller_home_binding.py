"""Dashboard OAuth device-code pollers must stay bound to the request's home.

``_start_device_code_flow`` answers an HTTP request by spawning a daemon
thread that polls the provider for up to ~15 minutes and then writes the AUTH
STORE via ``hermes_cli/auth.py``. The write is wrapped in
``_profile_scope(_oauth_session_profile(sid))`` — but when the request names no
profile, ``_oauth_profile_name`` returns None and ``_profile_scope`` installs
NO override, so the auth-store write resolves ``HERMES_HOME`` whenever the
poller happens to finish.

The thread's lifetime is bounded by the device-code flow, not by the request
that started it, so a completion landing after ``HERMES_HOME`` moves writes
credentials into the restored home — under pytest, the real ``~/.hermes``.

The rule (GBrain ``concepts/import-time-hermes-home-snapshot-bug``): resolve at
the moment the value's meaning is fixed, then CARRY it. That moment is request
time, and the shape to copy is right next door — ``_run_dashboard_mcp_oauth``
captures ``flow.hermes_home`` and re-applies it in the worker via
``set_hermes_home_override``.
"""

import inspect

import pytest

from hermes_cli import web_server


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    return home_a, home_b


def _resolved(path):
    return str(path.expanduser().resolve(strict=False))


# ---------------------------------------------------------------------------
# The capture: request time is when the home's meaning is fixed.
# ---------------------------------------------------------------------------


def test_new_oauth_session_captures_the_request_home(homes):
    home_a, _ = homes

    sid, sess = web_server._new_oauth_session("nous", "device_code")

    try:
        assert sess.get("hermes_home") == _resolved(home_a), (
            "the OAuth session did not capture the home it was created under, "
            "so its poller has nothing to carry and must resolve live"
        )
        assert web_server._oauth_session_home(sid) == _resolved(home_a)
    finally:
        with web_server._oauth_sessions_lock:
            web_server._oauth_sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# The carry: _profile_scope must honour a captured home for the "current" case.
# ---------------------------------------------------------------------------


def test_profile_scope_pins_a_captured_home_when_no_profile_is_named(homes, monkeypatch):
    from hermes_constants import get_hermes_home

    home_a, home_b = homes
    monkeypatch.setenv("HERMES_HOME", str(home_b))

    with web_server._profile_scope(None, hermes_home=_resolved(home_a)):
        seen = _resolved(get_hermes_home())

    assert seen == _resolved(home_a), (
        "_profile_scope ignored the captured home — the auth-store write would "
        "follow HERMES_HOME into the restored (real) home"
    )
    assert _resolved(get_hermes_home()) == _resolved(home_b), (
        "_profile_scope leaked its override past the with-block"
    )


def test_profile_scope_still_resolves_live_without_a_captured_home(homes, monkeypatch):
    from hermes_constants import get_hermes_home

    _, home_b = homes
    monkeypatch.setenv("HERMES_HOME", str(home_b))

    with web_server._profile_scope(None):
        assert _resolved(get_hermes_home()) == _resolved(home_b)


def test_an_explicit_profile_still_wins_over_the_captured_home(homes, monkeypatch, tmp_path):
    """A named profile is a stronger statement of intent than the capture."""
    from hermes_constants import get_hermes_home

    home_a, _ = homes
    named = tmp_path / "profiles" / "work"
    named.mkdir(parents=True)
    monkeypatch.setattr(web_server, "_resolve_profile_dir", lambda _n: named)

    with web_server._profile_scope("work", hermes_home=_resolved(home_a)):
        assert _resolved(get_hermes_home()) == _resolved(named)


# ---------------------------------------------------------------------------
# End to end: a poller that finishes after the env moved writes to home A.
# ---------------------------------------------------------------------------


def test_nous_poller_saves_credentials_into_the_captured_home(homes, monkeypatch):
    """The representative poller, driven to completion with the env moved."""
    from hermes_constants import get_hermes_home
    from hermes_cli import auth as auth_mod

    home_a, home_b = homes
    saved_under = {}

    sid, sess = web_server._new_oauth_session("nous", "device_code")
    sess.update({
        "portal_base_url": "https://portal.invalid",
        "client_id": "cid",
        "device_code": "dev",
        "interval": 1,
        "scope": "openid",
        "expires_at": __import__("time").time() + 600,
    })

    monkeypatch.setattr(
        auth_mod, "_poll_for_token",
        lambda **kw: {"access_token": "tok", "refresh_token": "ref", "expires_in": 60},
    )
    monkeypatch.setattr(
        auth_mod, "refresh_nous_oauth_from_state", lambda state, **kw: state
    )
    monkeypatch.setattr(
        auth_mod, "persist_nous_credentials",
        lambda state: saved_under.update(home=_resolved(get_hermes_home())),
    )

    # The moment monkeypatch teardown restores the env under the thread.
    monkeypatch.setenv("HERMES_HOME", str(home_b))

    try:
        web_server._nous_poller(sid)

        assert sess["status"] == "approved", (
            f"poller did not complete: {sess.get('error_message')!r}"
        )
        assert saved_under.get("home") == _resolved(home_a), (
            "the poller wrote credentials into the home HERMES_HOME was "
            "restored to — on a real run that is ~/.hermes/auth.json"
        )
    finally:
        with web_server._oauth_sessions_lock:
            web_server._oauth_sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# None of the four may be left behind.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "poller",
    ["_nous_poller", "_minimax_poller", "_xai_device_poller", "_codex_full_login_worker"],
)
def test_every_device_code_poller_carries_the_captured_home(poller):
    src = inspect.getsource(getattr(web_server, poller))

    assert "_profile_scope(" in src, f"{poller} no longer scopes its auth write"
    scope_call = src[src.index("_profile_scope(") :]
    scope_call = scope_call[: scope_call.index(")\n") + 1]

    assert "hermes_home=" in scope_call, (
        f"{poller} scopes its auth-store write without carrying the home "
        "captured at request time — it will resolve HERMES_HOME up to ~15 "
        "minutes later, after the request scope is gone"
    )


def test_the_synchronous_pkce_handler_still_resolves_live():
    """_submit_anthropic_pkce runs inside the request — live resolve is correct."""
    src = inspect.getsource(web_server._submit_anthropic_pkce)

    assert "_profile_scope(_oauth_session_profile(session_id, profile))" in src, (
        "the synchronous PKCE handler was changed to carry a captured home; it "
        "runs on the request thread, where resolving live is already right"
    )
