"""Tests for the public probe-client builder functions (SR-470 canary seams).

These verify that build_codex_probe_client and build_anthropic_probe_client
return RAW SDK clients (not the shim wrappers) and handle the no-creds path
gracefully.
"""


def test_build_codex_probe_client_none_without_token(monkeypatch):
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "_read_codex_access_token", lambda: None)
    assert ac.build_codex_probe_client() is None


def test_build_codex_probe_client_returns_raw_openai_client(monkeypatch):
    import agent.auxiliary_client as ac
    from openai import OpenAI
    monkeypatch.setattr(ac, "_read_codex_access_token", lambda: "tok.eyJ.sig")
    built = ac.build_codex_probe_client()
    assert built is not None
    client, model = built
    # RAW OpenAI client (NOT the CodexAuxiliaryClient shim) so the canary can
    # stream and inspect the raw response.completed snapshot.
    assert isinstance(client, OpenAI)
    assert model == ac._CODEX_AUX_MODEL
    assert ac._CODEX_AUX_BASE_URL in str(client.base_url)


def test_build_anthropic_probe_client_smoke(monkeypatch):
    # Smoke: must return None or a (client, model) tuple, never raise, even with
    # no Anthropic creds configured. Force the no-creds path.
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "_select_pool_entry", lambda provider: (False, None))
    try:
        import agent.anthropic_adapter as aa
        monkeypatch.setattr(aa, "resolve_anthropic_token", lambda: "")
    except Exception:
        pass
    result = ac.build_anthropic_probe_client()
    assert result is None or (isinstance(result, tuple) and len(result) == 2)
