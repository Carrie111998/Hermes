def test_build_codex_probe_client_none_without_creds(monkeypatch):
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "_try_codex", lambda: (None, None))
    assert ac.build_codex_probe_client() is None


def test_build_codex_probe_client_unwraps_raw_openai(monkeypatch):
    import agent.auxiliary_client as ac
    from openai import OpenAI
    raw = OpenAI(api_key="tok.eyJ.sig", base_url=ac._CODEX_AUX_BASE_URL)
    monkeypatch.setattr(
        ac, "_try_codex",
        lambda: (ac.CodexAuxiliaryClient(raw, ac._CODEX_AUX_MODEL), ac._CODEX_AUX_MODEL),
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
