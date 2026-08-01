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


def test_is_valid_env_name_rejects_trailing_newline():
    # re.match(r"...$", ...) matches just before a trailing "\n" even
    # though the "\n" itself was never consumed by the pattern — a YAML
    # literal-block field_mapping value commonly has exactly this shape.
    assert op._is_valid_env_name("OPENAI_API_KEY\n") is False
    assert op._is_valid_env_name("OPENAI_API_KEY\n\n") is False
    assert op._is_valid_env_name("OPENAI_API_KEY") is True


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


def test_rotated_token_evicts_old_cache_entry(monkeypatch):
    """A long-lived gateway that rotates its service account token must not
    keep the old token/entry reachable in _CACHE forever — that's an
    unbounded memory leak of prior bootstrap tokens and fetched secrets."""

    async def _fake_fetch(*, token, vault_name, item_title, field_mapping):
        return {"KEY": f"secret-for-{token}"}, []

    monkeypatch.setattr(op, "_fetch_secrets_async", _fake_fetch)
    monkeypatch.setattr(op, "_ensure_sdk", lambda: None)

    op.fetch_onepassword_secrets(token="token-a", vault_name="v", item_title="i", use_cache=True)
    assert len(op._CACHE) == 1

    op.fetch_onepassword_secrets(token="token-b", vault_name="v", item_title="i", use_cache=True)

    # Still exactly one entry for this (vault, item) slot — the old
    # token-a entry was evicted, not left to accumulate.
    assert len(op._CACHE) == 1
    remaining_key = next(iter(op._CACHE))
    assert remaining_key[2] == "token-b"


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
    with pytest.raises(op.VaultAmbiguousError, match="ambiguous"):
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
    with pytest.raises(op.ItemAmbiguousError, match="ambiguous"):
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


# ---------------------------------------------------------------------------
# Duplicate field labels are rejected, not silently collapsed (P1 finding)
# ---------------------------------------------------------------------------


def test_duplicate_field_labels_rejected(monkeypatch):
    """Two fields sharing the exact same label must both be dropped as a
    collision, not have the second silently overwrite the first in a
    label-keyed dict before collision detection ever runs."""
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Vault1")],
        items_by_vault={"v1": [_FakeItemOverview("i1", "Hermes")]},
        items_by_id={
            "i1": _FakeItem([
                _FakeField("API KEY", "first-value"),
                _FakeField("API KEY", "second-value"),
            ])
        },
    )
    secrets, warnings = asyncio.run(
        op._fetch_secrets_async(
            token="t", vault_name="Vault1", item_title="Hermes", field_mapping={}
        )
    )
    # Neither value should win — both are dropped as an ambiguous collision.
    assert "API_KEY" not in secrets
    assert secrets == {}


def test_duplicate_labels_do_not_shadow_a_third_distinct_field(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Vault1")],
        items_by_vault={"v1": [_FakeItemOverview("i1", "Hermes")]},
        items_by_id={
            "i1": _FakeItem([
                _FakeField("API KEY", "first-value"),
                _FakeField("API KEY", "second-value"),
                _FakeField("OTHER FIELD", "unaffected-value"),
            ])
        },
    )
    secrets, warnings = asyncio.run(
        op._fetch_secrets_async(
            token="t", vault_name="Vault1", item_title="Hermes", field_mapping={}
        )
    )
    assert "API_KEY" not in secrets
    assert secrets == {"OTHER_FIELD": "unaffected-value"}


# ---------------------------------------------------------------------------
# get_onepassword_status skips the live connection check when disabled (P2)
# ---------------------------------------------------------------------------


def test_status_skips_connection_check_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("OP_TOKEN", "tok")
    monkeypatch.setattr(op, "_check_sdk_available", lambda: True)

    def _boom(**kw):
        raise AssertionError("fetch_onepassword_secrets must not be called when disabled")

    monkeypatch.setattr(op, "fetch_onepassword_secrets", _boom)

    status = op.get_onepassword_status(
        {
            "enabled": False,
            "service_account_token_env": "OP_TOKEN",
            "item": "Hermes",
        },
        tmp_path,
    )

    assert status["connection_ok"] is None
    assert status["connection_error"] is None


def test_status_connection_error_is_exception_category_only(monkeypatch, tmp_path):
    """connection_error must be the exception's class name, never str(exc)
    — see the "Error categories" comment in onepassword.py. A raw message
    could embed vault titles/item ids returned by the 1Password Client."""

    def _boom(**kw):
        raise op.VaultAmbiguousError(
            "Vault name 'Top Secret Vault' is ambiguous: 3 vaults share this title."
        )

    monkeypatch.setenv("OP_TOKEN", "tok")
    monkeypatch.setattr(op, "_check_sdk_available", lambda: True)
    monkeypatch.setattr(op, "fetch_onepassword_secrets", _boom)

    status = op.get_onepassword_status(
        {
            "enabled": True,
            "service_account_token_env": "OP_TOKEN",
            "item": "Hermes",
        },
        tmp_path,
    )

    assert status["connection_ok"] is False
    assert status["connection_error"] == "VaultAmbiguousError"
    assert "Top Secret Vault" not in status["connection_error"]


def test_status_checks_connection_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("OP_TOKEN", "tok")
    monkeypatch.setattr(op, "_check_sdk_available", lambda: True)
    monkeypatch.setattr(op, "fetch_onepassword_secrets", lambda **kw: ({"X": "v"}, []))

    status = op.get_onepassword_status(
        {
            "enabled": True,
            "service_account_token_env": "OP_TOKEN",
            "item": "Hermes",
        },
        tmp_path,
    )

    assert status["connection_ok"] is True


# ---------------------------------------------------------------------------
# Editor/pager env vars are blocked (P1 finding — code execution via
# `hermes config edit`'s $EDITOR/$VISUAL exec)
# ---------------------------------------------------------------------------


def test_editor_and_visual_are_blocked(monkeypatch):
    assert "EDITOR" in op._DANGEROUS_ENV_VARS
    assert "VISUAL" in op._DANGEROUS_ENV_VARS
    assert "PAGER" in op._DANGEROUS_ENV_VARS


def test_editor_field_is_skipped_not_injected(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Vault1")],
        items_by_vault={"v1": [_FakeItemOverview("i1", "Hermes")]},
        items_by_id={
            "i1": _FakeItem([
                _FakeField("Editor", "/bin/rm"),
                _FakeField("API KEY", "sekret"),
            ])
        },
    )
    secrets, warnings = asyncio.run(
        op._fetch_secrets_async(
            token="t", vault_name="Vault1", item_title="Hermes", field_mapping={}
        )
    )
    assert "EDITOR" not in secrets
    assert secrets == {"API_KEY": "sekret"}


# ---------------------------------------------------------------------------
# status() surfaces skipped-field warnings, not just connection_ok (P2)
# ---------------------------------------------------------------------------


def test_status_surfaces_field_warnings(monkeypatch, tmp_path):
    monkeypatch.setenv("OP_TOKEN", "tok")
    monkeypatch.setattr(op, "_check_sdk_available", lambda: True)
    monkeypatch.setattr(
        op, "fetch_onepassword_secrets",
        lambda **kw: ({"GOOD_KEY": "v"}, ["Skipping field 'bad label': ..."]),
    )

    status = op.get_onepassword_status(
        {
            "enabled": True,
            "service_account_token_env": "OP_TOKEN",
            "item": "Hermes",
        },
        tmp_path,
    )

    assert status["connection_ok"] is True
    assert status["field_warnings"] == ["Skipping field 'bad label': ..."]


# ---------------------------------------------------------------------------
# Embedded null bytes are stripped, not left to crash os.environ[k]=v (P2)
# ---------------------------------------------------------------------------


def test_null_byte_in_field_value_is_stripped(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Vault1")],
        items_by_vault={"v1": [_FakeItemOverview("i1", "Hermes")]},
        items_by_id={
            "i1": _FakeItem([_FakeField("API KEY", "sek\x00ret")]),
        },
    )
    secrets, warnings = asyncio.run(
        op._fetch_secrets_async(
            token="t", vault_name="Vault1", item_title="Hermes", field_mapping={}
        )
    )
    assert secrets == {"API_KEY": "sekret"}
    assert "\x00" not in secrets["API_KEY"]


def test_field_value_that_is_only_null_bytes_is_skipped(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Vault1")],
        items_by_vault={"v1": [_FakeItemOverview("i1", "Hermes")]},
        items_by_id={
            "i1": _FakeItem([
                _FakeField("EMPTY", "\x00\x00"),
                _FakeField("API KEY", "sekret"),
            ]),
        },
    )
    secrets, warnings = asyncio.run(
        op._fetch_secrets_async(
            token="t", vault_name="Vault1", item_title="Hermes", field_mapping={}
        )
    )
    assert "EMPTY" not in secrets
    assert secrets == {"API_KEY": "sekret"}


# ---------------------------------------------------------------------------
# Vault/item ambiguity errors no longer leak other vaults'/items' titles
# or ids from the 1Password account (CodeQL clear-text-logging finding)
# ---------------------------------------------------------------------------


def test_vault_not_found_error_omits_other_vault_titles(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Top Secret Executive Vault")],
        items_by_vault={},
        items_by_id={},
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            op._fetch_secrets_async(
                token="t", vault_name="Nope", item_title="Hermes", field_mapping={}
            )
        )
    assert "Top Secret Executive Vault" not in str(excinfo.value)


def test_ambiguous_vault_error_omits_vault_ids(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("vault-id-1", "Shared"), _FakeVault("vault-id-2", "Shared")],
        items_by_vault={},
        items_by_id={},
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            op._fetch_secrets_async(
                token="t", vault_name="Shared", item_title="Hermes", field_mapping={}
            )
        )
    assert "vault-id-1" not in str(excinfo.value)
    assert "vault-id-2" not in str(excinfo.value)


def test_ambiguous_item_error_omits_item_ids(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        vaults=[_FakeVault("v1", "Vault1")],
        items_by_vault={"v1": [
            _FakeItemOverview("item-id-1", "Hermes"),
            _FakeItemOverview("item-id-2", "Hermes"),
        ]},
        items_by_id={},
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            op._fetch_secrets_async(
                token="t", vault_name="Vault1", item_title="Hermes", field_mapping={}
            )
        )
    assert "item-id-1" not in str(excinfo.value)
    assert "item-id-2" not in str(excinfo.value)
