"""Hermetic tests for the 1Password (`op` CLI) secret source.

We never invoke the real ``op`` binary: ``subprocess.run`` is mocked so the
suite stays fast and offline-safe.  A live resolve is exercised manually via
``hermes secrets onepassword sync`` outside of pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest


# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources import onepassword as op  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_caches():
    op._reset_cache_for_tests()
    yield
    op._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _clean_op_env(monkeypatch):
    """Start every test from a known 1Password auth state."""
    for key in list(os.environ):
        if key.startswith("OP_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("OP_ACCOUNT", raising=False)
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)
    yield


def _ok(value: str):
    return mock.Mock(returncode=0, stdout=value, stderr="")


def _err(code: int, stderr: str):
    return mock.Mock(returncode=code, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------


def test_validate_references_filters_bad_names_and_refs():
    refs = {
        "OPENAI_API_KEY": "op://Private/OpenAI/api key",
        "1BAD_NAME": "op://Private/x/y",          # bad env name
        "HAS SPACE": "op://Private/x/y",          # bad env name
        "NOT_A_REF": "https://example.com",        # not op://
        "WHITESPACE": "  op://Private/z/field  ",  # stripped + kept
    }
    valid, warnings = op._validate_references(refs)
    assert valid == {
        "OPENAI_API_KEY": "op://Private/OpenAI/api key",
        "WHITESPACE": "op://Private/z/field",
    }
    assert len(warnings) == 3


# ---------------------------------------------------------------------------
# fetch_onepassword_secrets
# ---------------------------------------------------------------------------


def test_fetch_happy_path(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    values = {
        "op://Private/OpenAI/api key": "sk-abc\n",
        "op://Private/Anthropic/credential": "sk-ant-xyz",
    }

    def fake_run(cmd, **kwargs):
        # argv list, never shell=True; reference passed after `--`.
        assert "--" in cmd
        ref = cmd[cmd.index("--") + 1]
        return _ok(values[ref])

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    secrets, warnings = op.fetch_onepassword_secrets(
        references={
            "OPENAI_API_KEY": "op://Private/OpenAI/api key",
            "ANTHROPIC_API_KEY": "op://Private/Anthropic/credential",
        },
        binary=fake_op,
        use_cache=False,
    )
    assert secrets == {"OPENAI_API_KEY": "sk-abc", "ANTHROPIC_API_KEY": "sk-ant-xyz"}
    assert warnings == []


def test_op_child_receives_headless_desktop_flag(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setenv("OP_LOAD_DESKTOP_APP_SETTINGS", "false")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs["env"])
        return _ok("resolved")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, binary=fake_op, use_cache=False
    )

    assert seen["OP_LOAD_DESKTOP_APP_SETTINGS"] == "false"


def test_four_hung_reads_share_one_global_deadline(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    release = threading.Event()

    def hung(*args, **kwargs):
        release.wait(0.5)
        raise op._OpReadError(op.ErrorKind.TIMEOUT, "op read timed out")

    monkeypatch.setattr(op, "_run_op_read", hung)
    monkeypatch.setattr(op, "_FIRST_ATTEMPT_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(op, "_TOTAL_FETCH_BUDGET_SECONDS", 0.10)
    monkeypatch.setattr(op, "_retry_jitter_seconds", lambda: 0.0)

    started = time.monotonic()
    try:
        secrets, warnings = op.fetch_onepassword_secrets(
            references={f"K{i}": f"op://V/I/F{i}" for i in range(4)},
            binary=fake_op,
            use_cache=False,
        )
    finally:
        release.set()

    assert time.monotonic() - started < 0.25
    assert secrets == {}
    assert len(warnings) == 4
    assert all("kind=timeout" in warning for warning in warnings)


def test_retry_only_timeout_and_network(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    first_kinds = {
        "op://V/I/timeout": op.ErrorKind.TIMEOUT,
        "op://V/I/network": op.ErrorKind.NETWORK,
        "op://V/I/auth": op.ErrorKind.AUTH_FAILED,
        "op://V/I/invalid": op.ErrorKind.REF_INVALID,
    }
    calls = {ref: 0 for ref in first_kinds}

    def fake_read(_binary, reference, **kwargs):
        calls[reference] += 1
        if calls[reference] == 1:
            raise op._OpReadError(first_kinds[reference], "redacted failure")
        return "recovered"

    monkeypatch.setattr(op, "_run_op_read", fake_read)
    monkeypatch.setattr(op, "_retry_jitter_seconds", lambda: 0.0)
    secrets, warnings = op.fetch_onepassword_secrets(
        references={
            "TIMEOUT": "op://V/I/timeout",
            "NETWORK": "op://V/I/network",
            "AUTH": "op://V/I/auth",
            "INVALID": "op://V/I/invalid",
        },
        binary=fake_op,
        use_cache=False,
    )

    assert secrets == {"TIMEOUT": "recovered", "NETWORK": "recovered"}
    assert calls == {
        "op://V/I/timeout": 2,
        "op://V/I/network": 2,
        "op://V/I/auth": 1,
        "op://V/I/invalid": 1,
    }
    assert {warning.split()[0] for warning in warnings} == {"AUTH", "INVALID"}






def test_fetch_read_failure_becomes_warning(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(
        op.subprocess, "run", lambda *a, **k: _err(1, "\x1b[31m[ERROR] not signed in\x1b[0m")
    )

    secrets, warnings = op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, binary=fake_op, use_cache=False
    )
    assert secrets == {}
    assert len(warnings) == 1
    # ANSI control sequences are fully scrubbed from the surfaced message.
    assert "\x1b" not in warnings[0]
    assert "[31m" not in warnings[0]
    assert "kind=auth_failed" in warnings[0]










# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_inprocess_cache_hit(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("v")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)
    for _ in range(2):
        op.fetch_onepassword_secrets(
            references={"K": "op://V/I/F"}, cache_ttl_seconds=60,
            binary=fake_op, home_path=tmp_path,
        )
    assert calls["n"] == 1  # second call served from L1 cache








def test_connect_credential_change_invalidates_cache(monkeypatch, tmp_path):
    """A different 1Password Connect identity must not reuse a cached value."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("v")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)

    monkeypatch.setenv("OP_CONNECT_HOST", "https://connect.example.com")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "tokenA")
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, cache_ttl_seconds=300,
        binary=fake_op, home_path=tmp_path,
    )
    # Rotate the Connect token → new identity.
    monkeypatch.setenv("OP_CONNECT_TOKEN", "tokenB")
    op._CACHE.clear()
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, cache_ttl_seconds=300,
        binary=fake_op, home_path=tmp_path,
    )
    assert calls["n"] == 2  # cache key changed → refetch


def _seed_stale_cache(tmp_path, refs, *, age_seconds=100):
    key = (
        op._auth_fingerprint("OP_SERVICE_ACCOUNT_TOKEN"),
        "",
        str(tmp_path),
        op._refs_fingerprint(refs),
    )
    op._CACHE[key] = op.CachedFetch(
        secrets={name: f"cached-{name}" for name in refs},
        fetched_at=time.time() - age_seconds,
    )


@pytest.mark.parametrize("kind", [op.ErrorKind.TIMEOUT, op.ErrorKind.NETWORK])
def test_complete_stale_cache_allowed_for_transport_errors(monkeypatch, tmp_path, kind):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/I/A", "B": "op://V/I/B"}
    _seed_stale_cache(tmp_path, refs)

    def fail(*args, **kwargs):
        raise op._OpReadError(kind, "redacted failure")

    monkeypatch.setattr(op, "_run_op_read", fail)
    monkeypatch.setattr(op, "_retry_jitter_seconds", lambda: 0.0)
    secrets, warnings = op.fetch_onepassword_secrets(
        references=refs,
        binary=fake_op,
        cache_ttl_seconds=1,
        stale_if_error_seconds=900,
        home_path=tmp_path,
    )

    assert secrets == {"A": "cached-A", "B": "cached-B"}
    assert any("stale cache" in warning for warning in warnings)


@pytest.mark.parametrize(
    "kind",
    [
        op.ErrorKind.AUTH_FAILED,
        op.ErrorKind.AUTH_EXPIRED,
        op.ErrorKind.REF_INVALID,
        op.ErrorKind.EMPTY_VALUE,
    ],
)
def test_stale_cache_never_used_for_nontransport_errors(monkeypatch, tmp_path, kind):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/I/A"}
    _seed_stale_cache(tmp_path, refs)

    def fail(*args, **kwargs):
        raise op._OpReadError(kind, "redacted failure")

    monkeypatch.setattr(op, "_run_op_read", fail)
    secrets, warnings = op.fetch_onepassword_secrets(
        references=refs,
        binary=fake_op,
        cache_ttl_seconds=1,
        stale_if_error_seconds=900,
        home_path=tmp_path,
    )

    assert secrets == {}
    assert not any("stale cache" in warning for warning in warnings)


def test_permission_failure_is_classified_as_nonretryable_auth_failure():
    assert (
        op._classify_op_error("permission denied for this vault")
        == op.ErrorKind.AUTH_FAILED
    )


def test_cache_ttl_zero_still_disables_all_cache_persistence(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("resolved"))

    op.fetch_onepassword_secrets(
        references={"A": "op://V/I/A"},
        binary=fake_op,
        cache_ttl_seconds=0,
        stale_if_error_seconds=900,
        home_path=tmp_path,
    )

    assert op._CACHE == {}
    assert not op._disk_cache_path(tmp_path).exists()


def test_incomplete_or_too_old_stale_cache_is_rejected(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/I/A", "B": "op://V/I/B"}
    _seed_stale_cache(tmp_path, refs, age_seconds=901)

    def fail(*args, **kwargs):
        raise op._OpReadError(op.ErrorKind.NETWORK, "redacted failure")

    monkeypatch.setattr(op, "_run_op_read", fail)
    monkeypatch.setattr(op, "_retry_jitter_seconds", lambda: 0.0)
    secrets, warnings = op.fetch_onepassword_secrets(
        references=refs,
        binary=fake_op,
        cache_ttl_seconds=1,
        stale_if_error_seconds=900,
        home_path=tmp_path,
    )

    assert secrets == {}
    assert not any("stale cache" in warning for warning in warnings)






# ---------------------------------------------------------------------------
# find_op
# ---------------------------------------------------------------------------


def test_find_op_pinned_path_not_on_path(tmp_path, monkeypatch):
    pinned = tmp_path / "op"
    pinned.write_text("")
    pinned.chmod(0o755)
    # PATH lookup must NOT be consulted when a binary_path is pinned.
    monkeypatch.setattr(op.shutil, "which", lambda name: "/usr/bin/op")
    assert op.find_op(str(pinned)) == pinned




# ---------------------------------------------------------------------------
# apply_onepassword_secrets
# ---------------------------------------------------------------------------


def test_apply_disabled_returns_empty():
    result = op.apply_onepassword_secrets(enabled=False, env={"K": "op://V/I/F"})
    assert result.ok
    assert not result.applied


def test_apply_missing_binary_sets_error(monkeypatch):
    monkeypatch.setattr(op, "find_op", lambda binary_path="": None)
    result = op.apply_onepassword_secrets(
        enabled=True, env={"K": "op://V/I/F"}
    )
    assert not result.ok
    assert "op CLI" in result.error


def test_apply_sets_env(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("resolved-val"))
    monkeypatch.delenv("MY_OP_KEY", raising=False)

    result = op.apply_onepassword_secrets(
        enabled=True, env={"MY_OP_KEY": "op://V/I/F"}, cache_ttl_seconds=0,
    )
    assert result.ok
    assert result.applied == ["MY_OP_KEY"]
    assert os.environ["MY_OP_KEY"] == "resolved-val"


def test_apply_skips_before_fetch_when_not_overriding(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("MY_OP_KEY", "from-env")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("from-1password")

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.apply_onepassword_secrets(
        enabled=True, env={"MY_OP_KEY": "op://V/I/F"},
        override_existing=False, cache_ttl_seconds=0,
    )
    assert "MY_OP_KEY" in result.skipped
    assert os.environ["MY_OP_KEY"] == "from-env"
    assert calls["n"] == 0  # never even called op for a value we'd discard


def test_apply_never_overrides_token_var(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "original")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("malicious")

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.apply_onepassword_secrets(
        enabled=True,
        env={"OP_SERVICE_ACCOUNT_TOKEN": "op://V/I/F"},
        override_existing=True, cache_ttl_seconds=0,
    )
    assert "OP_SERVICE_ACCOUNT_TOKEN" in result.skipped
    assert os.environ["OP_SERVICE_ACCOUNT_TOKEN"] == "original"
    assert calls["n"] == 0




