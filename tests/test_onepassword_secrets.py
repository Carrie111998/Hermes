"""Hermetic tests for the 1Password Secrets Manager integration.

We never hit the real 1Password API — the ``onepassword`` SDK package is
faked via ``sys.modules`` injection so the suite stays fast and offline-safe.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources import onepassword as op  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    op._reset_cache_for_tests()
    yield
    op._reset_cache_for_tests()


# ---------------------------------------------------------------------------
# _field_label_to_env_name / _is_valid_env_name — ASCII-only (P2 finding)
# ---------------------------------------------------------------------------


def test_field_label_to_env_name_ascii_basic():
    assert op._field_label_to_env_name("api key") == "API_KEY"
    assert op._field_label_to_env_name("API-KEY") == "API_KEY"


def test_field_label_to_env_name_strips_non_ascii():
    # "clé api" must not survive as a Unicode-aware name like "CLÉ_API" —
    # non-ASCII letters are dropped rather than uppercased and kept.
    name = op._field_label_to_env_name("clé api")
    assert name == "CL_API"
    assert name.isascii()


def test_is_valid_env_name_rejects_unicode_letters():
    # Python's isalpha()/isalnum() accept "É" — the fixed regex must not.
    assert op._is_valid_env_name("CLÉ_API") is False
    assert op._is_valid_env_name("API_KEY") is True
    assert op._is_valid_env_name("1KEY") is False
    assert op._is_valid_env_name("") is False


# ---------------------------------------------------------------------------
# Cache key includes token + field_mapping identity (P1 finding)
# ---------------------------------------------------------------------------


def test_cache_key_includes_token_identity(monkeypatch):
    """Rotating the service account token must not reuse another identity's cache."""
    calls = []

    async def _fake_fetch(*, token, vault_name, item_title, field_mapping):
        calls.append(token)
        return {"KEY": f"secret-for-{token}"}, []

    monkeypatch.setattr(op, "_fetch_secrets_async", _fake_fetch)
    monkeypatch.setattr(op, "_ensure_sdk", lambda: None)

    secrets_a, _ = op.fetch_onepassword_secrets(
        token="token-a", vault_name="v", item_title="i", use_cache=True
    )
    secrets_b, _ = op.fetch_onepassword_secrets(
        token="token-b", vault_name="v", item_title="i", use_cache=True
    )

    assert secrets_a == {"KEY": "secret-for-token-a"}
    assert secrets_b == {"KEY": "secret-for-token-b"}
    assert calls == ["token-a", "token-b"]  # both fetched — no cross-identity cache hit


def test_cache_key_includes_field_mapping(monkeypatch):
    calls = []

    async def _fake_fetch(*, token, vault_name, item_title, field_mapping):
        calls.append(dict(field_mapping))
        return {"X": "v"}, []

    monkeypatch.setattr(op, "_fetch_secrets_async", _fake_fetch)
    monkeypatch.setattr(op, "_ensure_sdk", lambda: None)

    op.fetch_onepassword_secrets(
        token="t", vault_name="v", item_title="i",
        field_mapping={"a": "A"}, use_cache=True,
    )
    op.fetch_onepassword_secrets(
        token="t", vault_name="v", item_title="i",
        field_mapping={"a": "B"}, use_cache=True,
    )

    assert len(calls) == 2  # different mapping => cache miss, both fetched


def test_cache_hit_same_identity(monkeypatch):
    calls = []

    async def _fake_fetch(*, token, vault_name, item_title, field_mapping):
        calls.append(1)
        return {"X": "v"}, []

    monkeypatch.setattr(op, "_fetch_secrets_async", _fake_fetch)
    monkeypatch.setattr(op, "_ensure_sdk", lambda: None)

    op.fetch_onepassword_secrets(token="t", vault_name="v", item_title="i", use_cache=True)
    op.fetch_onepassword_secrets(token="t", vault_name="v", item_title="i", use_cache=True)

    assert len(calls) == 1  # identical identity => cache hit on the second call


# ---------------------------------------------------------------------------
# Ambiguous vault / item matches are rejected, not silently resolved (P1)
# ---------------------------------------------------------------------------


class _FakeVault:
    def __init__(self, id_, title):
        self.id = id_
        self.title = title


class _FakeItemOverview:
    def __init__(self, id_, title):
        self.id = id_
        self.title = title


class _FakeField:
    def __init__(self, title, value):
        self.title = title
        self.value = value


class _FakeItem:
    def __init__(self, fields):
        self.fields = fields


def _install_fake_sdk(monkeypatch, *, vaults, items_by_vault, items_by_id):
    """Inject a fake ``onepassword.client`` module returning the given fixtures."""

    class _FakeVaults:
        async def list_all(self):
            return vaults

    class _FakeItems:
        async def list_all(self, vault_id):
            return items_by_vault.get(vault_id, [])

        async def get(self, vault_id, item_id):
            return items_by_id[item_id]

    class _FakeClient:
        vaults = _FakeVaults()
        items = _FakeItems()

        @classmethod
        async def authenticate(cls, **kwargs):
            return cls()

    fake_client_module = mock.MagicMock()
    fake_client_module.Client = _FakeClient
    fake_onepassword_module = mock.MagicMock()
    fake_onepassword_module.client = fake_client_module

    monkeypatch.setitem(sys.modules, "onepassword", fake_onepassword_module)
    monkeypatch.setitem(sys.modules, "onepassword.client", fake_client_module)


def test_ambiguous_vault_name_rejected(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Shared"), _FakeVault("v2", "Shared")],
        items_by_vault={},
        items_by_id={},
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        asyncio.run(
            op._fetch_secrets_async(
                token="t", vault_name="Shared", item_title="Hermes", field_mapping={}
            )
        )


def test_ambiguous_item_title_rejected(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Vault1")],
        items_by_vault={"v1": [
            _FakeItemOverview("i1", "Hermes"),
            _FakeItemOverview("i2", "Hermes"),
        ]},
        items_by_id={},
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        asyncio.run(
            op._fetch_secrets_async(
                token="t", vault_name="Vault1", item_title="Hermes", field_mapping={}
            )
        )


def test_unambiguous_item_still_resolves(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Vault1")],
        items_by_vault={"v1": [_FakeItemOverview("i1", "Hermes")]},
        items_by_id={"i1": _FakeItem([_FakeField("API KEY", "sekret")])},
    )
    secrets, warnings = asyncio.run(
        op._fetch_secrets_async(
            token="t", vault_name="Vault1", item_title="Hermes", field_mapping={}
        )
    )
    assert secrets == {"API_KEY": "sekret"}
    assert warnings == []


# ---------------------------------------------------------------------------
# apply_onepassword_secrets: refresh, removal, and never-touch-others (P1)
# ---------------------------------------------------------------------------


def _base_config(**overrides):
    cfg = {
        "service_account_token_env": "OP_TOKEN",
        "vault": "",
        "item": "Hermes",
        "auto_install": False,
    }
    cfg.update(overrides)
    return cfg


def test_apply_refreshes_previously_managed_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OP_TOKEN", "tok")
    monkeypatch.setenv("SOME_KEY", "old-value")
    monkeypatch.setattr(op, "_check_sdk_available", lambda: True)
    monkeypatch.setattr(
        op, "fetch_onepassword_secrets",
        lambda **kw: ({"SOME_KEY": "new-value"}, []),
    )

    applied, removed = op.apply_onepassword_secrets(
        _base_config(override_existing=False), tmp_path, previously_managed={"SOME_KEY"}
    )

    assert applied == {"SOME_KEY": "***"}
    assert removed == []
    import os
    assert os.environ["SOME_KEY"] == "new-value"


def test_apply_does_not_touch_unmanaged_existing_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OP_TOKEN", "tok")
    monkeypatch.setenv("SOME_KEY", "operator-set-value")
    monkeypatch.setattr(op, "_check_sdk_available", lambda: True)
    monkeypatch.setattr(
        op, "fetch_onepassword_secrets",
        lambda **kw: ({"SOME_KEY": "op-value"}, []),
    )

    applied, removed = op.apply_onepassword_secrets(
        _base_config(override_existing=False), tmp_path, previously_managed=set()
    )

    assert applied == {}
    assert removed == []
    import os
    assert os.environ["SOME_KEY"] == "operator-set-value"


def test_apply_removes_key_that_disappeared_from_item(monkeypatch, tmp_path):
    monkeypatch.setenv("OP_TOKEN", "tok")
    monkeypatch.setenv("GONE_KEY", "stale-value")
    monkeypatch.setattr(op, "_check_sdk_available", lambda: True)
    monkeypatch.setattr(
        op, "fetch_onepassword_secrets",
        lambda **kw: ({}, []),  # field was deleted/renamed in 1Password
    )

    applied, removed = op.apply_onepassword_secrets(
        _base_config(override_existing=False), tmp_path, previously_managed={"GONE_KEY"}
    )

    assert applied == {}
    assert removed == ["GONE_KEY"]
    import os
    assert "GONE_KEY" not in os.environ


def test_apply_never_removes_unmanaged_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OP_TOKEN", "tok")
    monkeypatch.setenv("UNRELATED_KEY", "keep-me")
    monkeypatch.setattr(op, "_check_sdk_available", lambda: True)
    monkeypatch.setattr(
        op, "fetch_onepassword_secrets",
        lambda **kw: ({}, []),
    )

    applied, removed = op.apply_onepassword_secrets(
        _base_config(override_existing=False), tmp_path, previously_managed=set()
    )

    assert removed == []
    import os
    assert os.environ["UNRELATED_KEY"] == "keep-me"
