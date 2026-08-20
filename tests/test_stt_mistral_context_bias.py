"""Voxtral requests carry the declared language and the vocabulary bias.

Pins three things the Mistral STT path got wrong before:

* ``context_bias`` (the real Voxtral parameter) is resolved from config and
  sent; ``prompt`` (which the SDK signature does not accept) never is;
* a server refusing the vocabulary costs a retry, not the transcription;
* a genuine outage is reported as such, without a pointless second call.
"""
import sys
import types

import pytest

import tools.transcription_tools as tt


class _FakeComplete:
    def __init__(self, failures=()):
        self.calls = []
        self._failures = list(failures)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self._failures:
            err = self._failures.pop(0)
            if err:
                raise err
        return types.SimpleNamespace(text="bonjour")


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a stub ``mistralai.client`` and return the recorded calls."""
    complete = _FakeComplete()

    class _Client:
        def __init__(self, api_key=None):
            self.audio = types.SimpleNamespace(
                transcriptions=types.SimpleNamespace(complete=complete)
            )

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    module = types.ModuleType("mistralai.client")
    module.Mistral = _Client
    monkeypatch.setitem(sys.modules, "mistralai", types.ModuleType("mistralai"))
    monkeypatch.setitem(sys.modules, "mistralai.client", module)
    monkeypatch.setattr(tt, "_resolve_provider_key", lambda *a, **k: "key")
    return complete


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"\x00")
    return str(path)


def _config(monkeypatch, **stt):
    monkeypatch.setattr(tt, "_load_stt_config", lambda: stt)


def test_language_and_vocabulary_reach_the_request(fake_sdk, audio, monkeypatch):
    _config(monkeypatch, language="fr", mistral={"context_bias": ["Soundboks", "Connemara"]})
    result = tt._transcribe_mistral(audio, "voxtral-mini-latest")
    assert result["success"] is True
    (call,) = fake_sdk.calls
    assert call["language"] == "fr"
    assert call["context_bias"] == ["Soundboks", "Connemara"]


def test_dead_prompt_parameter_is_never_sent(fake_sdk, audio, monkeypatch):
    _config(monkeypatch, language="fr")
    tt._transcribe_mistral(audio, "m", prompt="du contexte")
    (call,) = fake_sdk.calls
    assert "prompt" not in call        # the SDK signature would raise TypeError
    assert "context_bias" not in call  # nothing configured → nothing sent


def test_global_context_bias_is_the_fallback_source(fake_sdk, audio, monkeypatch):
    _config(monkeypatch, context_bias=["LJT"], mistral={})
    tt._transcribe_mistral(audio, "m")
    assert fake_sdk.calls[0]["context_bias"] == ["LJT"]


def test_refused_vocabulary_is_retried_without_it(fake_sdk, audio, monkeypatch, caplog):
    _config(monkeypatch, language="fr", mistral={"context_bias": ["Le Jockey Tricolore"]})
    fake_sdk._failures = [Exception(
        "API error occurred: Status 400. Body: {\"message\":\"Context bias item "
        "'Le Jockey Tricolore' must not contain commas or whitespace\"}"
    )]
    result = tt._transcribe_mistral(audio, "m")
    assert result["success"] is True and result["transcript"] == "bonjour"
    assert len(fake_sdk.calls) == 2
    assert "context_bias" not in fake_sdk.calls[1]
    assert fake_sdk.calls[1]["language"] == "fr"  # the language survives the retry


def test_real_outage_is_not_blamed_on_the_vocabulary(fake_sdk, audio, monkeypatch):
    _config(monkeypatch, mistral={"context_bias": ["LJT"]})
    fake_sdk._failures = [Exception("API error occurred: Status 503. Body: upstream down")]
    result = tt._transcribe_mistral(audio, "m")
    assert result["success"] is False
    assert len(fake_sdk.calls) == 1  # no second call on a genuine failure
