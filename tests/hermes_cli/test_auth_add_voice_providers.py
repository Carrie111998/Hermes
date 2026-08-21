"""`hermes auth add` must accept the voice-only provider ids (#90956).

`resolve_provider_secret()` step 3 reads the credential pool by the
``tts.provider`` / ``stt.provider`` id (elevenlabs, bare "openai", groq,
mistral), but the auth-add validation only accepted ``PROVIDER_REGISTRY``
(inference ids), so the pool was unwritable for exactly the providers the
read side was written for — "Unknown provider: elevenlabs".
"""

import types

import pytest

from hermes_cli import auth_commands
from hermes_cli.auth_commands import VOICE_CREDENTIAL_PROVIDER_IDS, auth_add_command


def _args(provider: str, api_key: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        provider=provider, auth_type="api-key", api_key=api_key, label=None
    )


class _FakePool:
    def __init__(self):
        self.entries_list = []

    def entries(self):
        return self.entries_list

    def add_entry(self, entry):
        self.entries_list.append(entry)


@pytest.mark.parametrize("provider", sorted(VOICE_CREDENTIAL_PROVIDER_IDS))
def test_auth_add_accepts_voice_only_providers(monkeypatch, provider):
    """Each voice-only id passes validation and lands in the pool under its
    own id — the exact key `resolve_provider_secret()` step 3 looks up."""
    fake = _FakePool()
    monkeypatch.setattr(auth_commands, "load_pool", lambda _p: fake)
    # Non-interactive path: label falls back to the default (no stdin prompt).
    monkeypatch.setattr(auth_commands.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))

    auth_add_command(_args(provider, "test-key-placeholder-not-real"))

    assert len(fake.entries_list) == 1
    assert fake.entries_list[0].provider == provider


def test_unknown_provider_still_rejected(monkeypatch):
    """Fail-closed guard on the carve-out: ids outside both the registry and
    the voice set keep being rejected."""
    fake = _FakePool()
    monkeypatch.setattr(auth_commands, "load_pool", lambda _p: fake)

    with pytest.raises(SystemExit, match="Unknown provider"):
        auth_add_command(_args("not-a-real-provider", "test-key-placeholder-not-real"))
    assert fake.entries_list == []


def test_voice_ids_are_config_legal_voice_provider_values():
    """Consistency contract: every id we accept must be a value the voice
    config actually uses (a key under the tts/stt sections), otherwise the
    pool key could never be read back by the tools."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    tts_ids = set(DEFAULT_CONFIG.get("tts", {}).keys())
    stt_ids = set(DEFAULT_CONFIG.get("stt", {}).keys())
    for pid in VOICE_CREDENTIAL_PROVIDER_IDS:
        assert pid in tts_ids or pid in stt_ids, (
            f"{pid!r} is not a configured tts/stt provider — the pool entry "
            f"would be unreadable by resolve_provider_secret()"
        )
