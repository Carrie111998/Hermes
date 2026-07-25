import json

import pytest


def test_build_codex_probe_client_none_without_creds(monkeypatch):
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "_build_codex_client", lambda model=None: (None, None))
    assert ac.build_codex_probe_client() is None


def test_build_codex_probe_client_unwraps_raw_openai(monkeypatch):
    import agent.auxiliary_client as ac
    from openai import OpenAI
    raw = OpenAI(api_key="tok.eyJ.sig", base_url=ac._CODEX_AUX_BASE_URL)
    monkeypatch.setattr(
        ac, "_build_codex_client",
        lambda model=None: (ac.CodexAuxiliaryClient(raw, ac._CODEX_AUX_MODEL), ac._CODEX_AUX_MODEL),
    )
    built = ac.build_codex_probe_client()
    assert built is not None
    client, model = built
    assert client is raw                       # shim unwrapped to the RAW client
    assert isinstance(client, OpenAI)
    assert model == ac._CODEX_AUX_MODEL


def test_build_anthropic_probe_client_none_without_creds(monkeypatch):
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "_try_anthropic", lambda: (None, None))
    assert ac.build_anthropic_probe_client() is None


def test_build_anthropic_probe_client_unwraps_raw(monkeypatch):
    import agent.auxiliary_client as ac

    sentinel = object()

    class _FakeWrapped:
        _real_client = sentinel

    monkeypatch.setattr(
        ac, "_try_anthropic",
        lambda: (_FakeWrapped(), "claude-haiku-4-5-20251001"),
    )
    built = ac.build_anthropic_probe_client()
    assert built is not None
    client, model = built
    assert client is sentinel
    assert model == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Root-mode probe credential resolution (2026-07-25 regression).
#
# The SR-470 canary runs from a Scheduled Task that deliberately leaves
# HERMES_HOME UNSET so events.paths resolves the cross-profile root (~/.hermes)
# for the event bus + sentinel.  The gateway, by contrast, runs profile-scoped,
# so the live Codex credential lives ONLY in <root>/profiles/<active>/auth.json.
#
# hermes_cli.auth._global_auth_file_path() makes the store fallback ONE-WAY
# (profile -> root), so a root-mode process has no path to the profile
# credential: the canary reported "no Codex token configured" the moment a
# duplicate copy at the root was (correctly) pruned.  These tests pin the
# profile-aware credential lookup AND the invariant that it must not drag the
# event bus / sentinel off the root.
# ---------------------------------------------------------------------------
@pytest.fixture
def root_mode_home(tmp_path, monkeypatch):
    """A root-mode HERMES_HOME whose Codex credential lives only in a profile."""
    import agent.credential_pool as cp

    root = tmp_path / ".hermes"
    (root / "profiles" / "main").mkdir(parents=True)
    (root / "active_profile").write_text("main", encoding="utf-8")

    # Root store: other providers, but deliberately NO openai-codex — this is
    # the post-prune shape that broke the canary.
    (root / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {"xai-oauth": {"tokens": {"token_type": "Bearer", "expires_in": 3600}}},
        "credential_pool": {},
    }), encoding="utf-8")

    # Profile store: the ONE live Codex credential.
    (root / "profiles" / "main" / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": "profile-codex-access",
                    "refresh_token": "profile-codex-refresh",
                },
                "auth_mode": "oauth",
            }
        },
        "credential_pool": {},
    }), encoding="utf-8")

    # Root mode == HERMES_HOME is the root itself (what the canary task does).
    monkeypatch.setenv("HERMES_HOME", str(root))
    cp.invalidate_pool_cache()
    yield root
    cp.invalidate_pool_cache()


def test_codex_probe_client_resolves_active_profile_store_from_root_mode(root_mode_home):
    """The canary must find the credential the gateway actually uses."""
    import agent.auxiliary_client as ac

    built = ac.build_codex_probe_client()
    assert built is not None, "root-mode probe failed to reach the active profile's Codex credential"
    client, _model = built
    assert client.api_key == "profile-codex-access"


def test_probe_credential_scope_leaves_event_bus_and_sentinel_on_the_root(root_mode_home):
    """CRITICAL: profile-aware credentials must NOT relocate bus/sentinel state.

    ~/.hermes/ops/canary/canary-backend-conformance.ps1 leaves HERMES_HOME unset
    on purpose (CLAUDE.md notification-layer invariant).  The credential scope
    must therefore be context-local and never touch os.environ.
    """
    import os

    import agent.auxiliary_client as ac
    from events import paths as event_paths
    from hermes_constants import get_default_hermes_root
    from obs.backend_conformance_canary import _sentinel_path

    before_env = os.environ.get("HERMES_HOME")
    with ac.probe_credential_scope():
        assert os.environ.get("HERMES_HOME") == before_env, "scope mutated os.environ"
        assert get_default_hermes_root() == root_mode_home
        assert event_paths.events_db_path() == root_mode_home / "events" / "event_bus.db"
        assert _sentinel_path() == root_mode_home / "canary" / "backend_conformance.json"
    assert os.environ.get("HERMES_HOME") == before_env


def test_probe_credential_scope_is_a_noop_in_profile_mode(tmp_path, monkeypatch):
    """A profile-scoped process (the gateway) must not be redirected anywhere."""
    import agent.auxiliary_client as ac
    from hermes_cli.config import get_hermes_home

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "main"
    profile.mkdir(parents=True)
    (root / "active_profile").write_text("main", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))

    with ac.probe_credential_scope():
        assert get_hermes_home() == profile


def test_probe_credential_scope_is_a_noop_without_an_active_profile(tmp_path, monkeypatch):
    """Classic mode (no profile, or 'default') keeps reading the root store."""
    import agent.auxiliary_client as ac
    from hermes_cli.config import get_hermes_home

    root = tmp_path / ".hermes"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    with ac.probe_credential_scope():
        assert get_hermes_home() == root

    (root / "active_profile").write_text("default", encoding="utf-8")
    with ac.probe_credential_scope():
        assert get_hermes_home() == root
