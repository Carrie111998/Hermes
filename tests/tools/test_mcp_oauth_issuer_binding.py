"""Refresh-token issuer binding for MCP OAuth (port of openai/codex#39615).

A stored refresh token must never be sent to a different authorization
server issuer than the one that granted it. The authorization server
discovered for an MCP server can change (protected-resource metadata edit,
server migration, DNS takeover) — without binding, the new issuer receives
a long-lived credential minted by the old one.

Covered here:
  * ``set_tokens`` stamps ``hermes_issuer`` when an issuer is bound.
  * ``get_tokens`` surfaces the recorded issuer without leaking the private
    field into the SDK's ``OAuthToken`` model.
  * ``enforce_refresh_token_issuer`` strips the refresh token (disk and
    in-memory) on issuer mismatch while keeping the access token.
  * Legacy token files without an issuer are stamped once (no forced
    re-login) so the next read is protected.
  * No metadata discovered yet -> no-op (SDK 401-branch will discover).
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from mcp.shared.auth import OAuthToken  # noqa: E402


def _storage(tmp_path, name="srv"):
    from tools.mcp_oauth import HermesTokenStorage

    return HermesTokenStorage(name, hermes_home=tmp_path)


def _token_file(tmp_path, name="srv"):
    return tmp_path / "mcp-tokens" / f"{name}.json"


def _write_token_file(tmp_path, payload, name="srv"):
    path = _token_file(tmp_path, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _context(storage, issuer=None, tokens=None):
    """Minimal stand-in for the SDK's OAuthContext."""
    meta = SimpleNamespace(issuer=issuer) if issuer is not None else None
    return SimpleNamespace(
        storage=storage,
        oauth_metadata=meta,
        current_tokens=tokens,
    )


class TestIssuerPersistence:
    def test_set_tokens_stamps_bound_issuer(self, tmp_path):
        storage = _storage(tmp_path)
        storage.bind_issuer("https://as.example.com")
        asyncio.run(
            storage.set_tokens(
                OAuthToken(
                    access_token="a",
                    token_type="Bearer",
                    expires_in=3600,
                    refresh_token="r",
                )
            )
        )
        on_disk = json.loads(_token_file(tmp_path).read_text())
        assert on_disk["hermes_issuer"] == "https://as.example.com"

    def test_set_tokens_without_bound_issuer_writes_no_field(self, tmp_path):
        storage = _storage(tmp_path)
        asyncio.run(
            storage.set_tokens(
                OAuthToken(access_token="a", token_type="Bearer", expires_in=3600)
            )
        )
        on_disk = json.loads(_token_file(tmp_path).read_text())
        assert "hermes_issuer" not in on_disk

    def test_get_tokens_surfaces_issuer_and_strips_private_field(self, tmp_path):
        _write_token_file(
            tmp_path,
            {
                "access_token": "a",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "r",
                "hermes_issuer": "https://as.example.com",
            },
        )
        storage = _storage(tmp_path)
        tokens = asyncio.run(storage.get_tokens())
        assert tokens is not None
        assert tokens.access_token == "a"
        assert storage.loaded_issuer == "https://as.example.com"
        # The private field must not leak into the SDK model.
        assert not hasattr(tokens, "hermes_issuer")


class TestEnforcement:
    def _stored(self, tmp_path, issuer="https://old.example.com"):
        payload = {
            "access_token": "a",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "r",
        }
        if issuer is not None:
            payload["hermes_issuer"] = issuer
        _write_token_file(tmp_path, payload)
        storage = _storage(tmp_path)
        tokens = asyncio.run(storage.get_tokens())
        assert tokens is not None
        return storage, tokens

    def test_issuer_mismatch_strips_refresh_token(self, tmp_path):
        from tools.mcp_oauth import enforce_refresh_token_issuer

        storage, tokens = self._stored(tmp_path)
        ctx = _context(storage, issuer="https://evil.example.com", tokens=tokens)
        enforce_refresh_token_issuer(ctx)

        # In-memory: refresh token gone, access token kept.
        assert ctx.current_tokens.refresh_token is None
        assert ctx.current_tokens.access_token == "a"
        # On disk: refresh token gone too.
        on_disk = json.loads(_token_file(tmp_path).read_text())
        assert "refresh_token" not in on_disk
        assert on_disk["access_token"] == "a"

    def test_matching_issuer_keeps_refresh_token(self, tmp_path):
        from tools.mcp_oauth import enforce_refresh_token_issuer

        storage, tokens = self._stored(tmp_path, issuer="https://as.example.com")
        ctx = _context(storage, issuer="https://as.example.com", tokens=tokens)
        enforce_refresh_token_issuer(ctx)
        assert ctx.current_tokens.refresh_token == "r"
        on_disk = json.loads(_token_file(tmp_path).read_text())
        assert on_disk["refresh_token"] == "r"

    def test_trailing_slash_issuers_match(self, tmp_path):
        from tools.mcp_oauth import enforce_refresh_token_issuer

        storage, tokens = self._stored(tmp_path, issuer="https://as.example.com/")
        ctx = _context(storage, issuer="https://as.example.com", tokens=tokens)
        enforce_refresh_token_issuer(ctx)
        assert ctx.current_tokens.refresh_token == "r"

    def test_legacy_file_without_issuer_is_stamped_not_rejected(self, tmp_path):
        from tools.mcp_oauth import enforce_refresh_token_issuer

        storage, tokens = self._stored(tmp_path, issuer=None)
        ctx = _context(storage, issuer="https://as.example.com", tokens=tokens)
        enforce_refresh_token_issuer(ctx)

        # Refresh token survives (no forced re-login for existing installs)...
        assert ctx.current_tokens.refresh_token == "r"
        # ...and the file is now stamped so the NEXT read is protected.
        on_disk = json.loads(_token_file(tmp_path).read_text())
        assert on_disk["hermes_issuer"] == "https://as.example.com"
        assert storage.loaded_issuer == "https://as.example.com"

    def test_no_metadata_is_a_noop(self, tmp_path):
        from tools.mcp_oauth import enforce_refresh_token_issuer

        storage, tokens = self._stored(tmp_path)
        ctx = _context(storage, issuer=None, tokens=tokens)
        enforce_refresh_token_issuer(ctx)
        assert ctx.current_tokens.refresh_token == "r"
        on_disk = json.loads(_token_file(tmp_path).read_text())
        assert on_disk["hermes_issuer"] == "https://old.example.com"

    def test_no_refresh_token_is_a_noop(self, tmp_path):
        from tools.mcp_oauth import enforce_refresh_token_issuer

        _write_token_file(
            tmp_path,
            {"access_token": "a", "token_type": "Bearer", "expires_in": 3600},
        )
        storage = _storage(tmp_path)
        tokens = asyncio.run(storage.get_tokens())
        ctx = _context(storage, issuer="https://as.example.com", tokens=tokens)
        from tools.mcp_oauth import enforce_refresh_token_issuer as enforce

        enforce(ctx)  # must not raise or rewrite anything meaningful
        assert ctx.current_tokens.access_token == "a"

    def test_foreign_storage_is_ignored(self, tmp_path):
        from tools.mcp_oauth import enforce_refresh_token_issuer

        ctx = _context(object(), issuer="https://as.example.com", tokens=None)
        ctx.storage = object()  # not a HermesTokenStorage
        enforce_refresh_token_issuer(ctx)  # no exception


class TestBindFromContext:
    def test_bind_issuer_from_context_records_issuer(self, tmp_path):
        from tools.mcp_oauth import bind_issuer_from_context

        storage = _storage(tmp_path)
        ctx = _context(storage, issuer="https://as.example.com")
        bind_issuer_from_context(ctx)
        asyncio.run(
            storage.set_tokens(
                OAuthToken(
                    access_token="a",
                    token_type="Bearer",
                    expires_in=3600,
                    refresh_token="r",
                )
            )
        )
        on_disk = json.loads(_token_file(tmp_path).read_text())
        assert on_disk["hermes_issuer"] == "https://as.example.com"

    def test_bind_without_metadata_is_noop(self, tmp_path):
        from tools.mcp_oauth import bind_issuer_from_context

        storage = _storage(tmp_path)
        ctx = _context(storage, issuer=None)
        bind_issuer_from_context(ctx)
        asyncio.run(
            storage.set_tokens(
                OAuthToken(access_token="a", token_type="Bearer", expires_in=3600)
            )
        )
        on_disk = json.loads(_token_file(tmp_path).read_text())
        assert "hermes_issuer" not in on_disk
