"""Regression test for the Codex auth-path split-brain (2026-05-28).

`obs/oauth_llm.py::_get_oauth_token` read its credential_pool from a HARDCODED
``Path.home() / ".hermes" / "auth.json"`` (root), ignoring ``HERMES_HOME``.
Every other runtime reader (agent/credential_pool.py, agent/auxiliary_client.py)
and the CLI writer (`hermes auth add`) resolve the store via
``get_hermes_home()``, which is profile-scoped (``~/.hermes/profiles/main`` when
``HERMES_HOME`` is set). So a credential written by the CLI under the active
profile was invisible to this resolver, which fell back to a stale root token
=> 401 token_expired on every Codex LLM call from the LangGraph path
(critic / matcher).

The invariant: ``_get_oauth_token`` must read the credential_pool from the SAME
file the CLI writes — ``get_hermes_home()/auth.json`` — not a hardcoded root.

The conftest's ``_hermetic_environment`` fixture already points HERMES_HOME at a
per-test tempdir; its docstring names this exact bug class ("code using
``Path.home() / '.hermes'`` instead of ``get_hermes_home()`` is a bug to fix at
the callsite").
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import obs.oauth_llm as oauth_llm


def _valid_jwt(tag: str) -> str:
    """A syntactically valid, unexpired JWT whose `sub` encodes `tag`.

    Distinct tags produce distinct token strings so we can assert WHICH store
    the resolver read from.
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = {"exp": int(time.time()) + 3600, "sub": tag}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig-{tag}"


def _write_pool(auth_json: Path, token: str) -> None:
    auth_json.parent.mkdir(parents=True, exist_ok=True)
    auth_json.write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "t",
                            "source": "manual:device_code",
                            "access_token": token,
                            "last_refresh": "2026-05-28T22:00:00Z",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def test_get_oauth_token_reads_pool_from_hermes_home_not_hardcoded_root(
    tmp_path, monkeypatch
):
    # Reset the module-level token cache so a prior resolution can't leak in.
    monkeypatch.setattr(oauth_llm, "_CACHED_TOKEN", None)
    monkeypatch.setattr(oauth_llm, "_CACHED_AT", 0.0)

    # Step 1 source (~/.codex via CODEX_HOME) → empty dir, so it contributes
    # nothing and the test isolates the Hermes-store read.
    codex_home = tmp_path / "codex_empty"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    # The hardcoded-root location the BUG reads: Path.home()/.hermes/auth.json.
    # Point Path.home() at a fake home and plant a DISTINCT valid token there.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(oauth_llm.Path, "home", staticmethod(lambda: fake_home))
    root_token = _valid_jwt("ROOT-hardcoded")
    _write_pool(fake_home / ".hermes" / "auth.json", root_token)

    # The profile-scoped store the CLI actually writes: get_hermes_home()/auth.json.
    # The conftest fixture set HERMES_HOME to a per-test tempdir.
    from hermes_constants import get_hermes_home

    profile_token = _valid_jwt("PROFILE-hermes-home")
    _write_pool(get_hermes_home() / "auth.json", profile_token)

    # The resolver must return the token from get_hermes_home(), NOT the root.
    assert oauth_llm._get_oauth_token() == profile_token
