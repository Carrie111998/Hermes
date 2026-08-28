"""The dashboard OAuth login must not create duplicate pool rows for one
single-use Anthropic refresh token.

Anthropic OAuth refresh tokens are single-use. The dashboard save path used to
insert an explicit ``manual:dashboard_pkce`` pool row *in addition to* the
``hermes_pkce`` row that ``load_pool`` seeds from
~/.hermes/.anthropic_oauth.json. Both rows carried the same refresh token, so
the first to refresh rotated the pair and the stale twin's next refresh 401'd
("refresh_token_reused") and revoked the login.

Uses a real tmp HERMES_HOME so the pool + OAuth file resolve to disk exactly as
in production; anthropic is marked as the explicitly-configured provider so the
credential_pool auto-discovery gate (PR #4210) lets the file-seed through.
"""
import importlib

import pytest


@pytest.fixture
def anthropic_home(monkeypatch, tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    import agent.anthropic_adapter as aa
    # External Claude Code creds on the dev/CI box are a separate credential —
    # not part of the dashboard flow under test.
    monkeypatch.setattr(aa, "read_claude_code_credentials", lambda: None)

    import agent.credential_pool as cp
    importlib.reload(cp)
    monkeypatch.setattr(cp, "read_claude_code_credentials", lambda: None, raising=False)
    monkeypatch.setattr(cp, "load_env", lambda: {}, raising=False)
    # The user has chosen Anthropic (dashboard model picker) — this is what
    # gates the pool's OAuth auto-discovery.
    import hermes_cli.auth as auth
    monkeypatch.setattr(auth, "is_provider_explicitly_configured", lambda p: True)
    return home, cp


def _anthropic_oauth_rows(cp, refresh_token):
    pool = cp.load_pool("anthropic")
    return [
        e for e in pool.entries()
        if e.auth_type == cp.AUTH_TYPE_OAUTH and e.refresh_token == refresh_token
    ]


def test_dashboard_login_creates_single_row_for_the_oauth_token(anthropic_home):
    home, cp = anthropic_home
    from hermes_cli.web_server import _save_anthropic_oauth_creds

    _save_anthropic_oauth_creds("access-T1", "refresh-T1", 1_000_000)

    rows = _anthropic_oauth_rows(cp, "refresh-T1")
    sources = sorted(e.source for e in rows)
    assert len(rows) == 1, (
        f"dashboard login created {len(rows)} pool rows sharing one single-use "
        f"refresh token: {sources}"
    )
    assert rows[0].source == "hermes_pkce"


def test_dashboard_relogin_does_not_accumulate_rows(anthropic_home):
    home, cp = anthropic_home
    from hermes_cli.web_server import _save_anthropic_oauth_creds

    # A second login (e.g. re-auth after expiry) rotates the token; the pool
    # must still hold exactly one OAuth row, now with the new token.
    _save_anthropic_oauth_creds("access-T1", "refresh-T1", 1_000_000)
    _save_anthropic_oauth_creds("access-T2", "refresh-T2", 2_000_000)

    assert _anthropic_oauth_rows(cp, "refresh-T1") == []
    rows = _anthropic_oauth_rows(cp, "refresh-T2")
    assert len(rows) == 1
    assert rows[0].source == "hermes_pkce"
