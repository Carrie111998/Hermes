"""Tests for the orchestrator stale-cache fallback (SecretSource.stale_secrets).

Covers: kind gating (NETWORK/TIMEOUT/BINARY_MISSING rescue, AUTH_FAILED and
INTERNAL never rescue), the no-cache path, hook exceptions being contained,
the 1Password ``stale_secrets`` implementation (disk-cache round-trip, auth
fingerprint mismatch, cache opt-out), and the all-references-failed
truthfulness fix in ``OnePasswordSource.fetch``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources.base import (  # noqa: E402
    ErrorKind,
    FetchResult,
    SecretSource,
    reset_source_environment,
    set_source_environment,
)
from agent.secret_sources import onepassword as op_mod  # noqa: E402
from agent.secret_sources import registry as reg  # noqa: E402
from agent.secret_sources._cache import CachedFetch  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    """Each test starts with an empty registry and no builtin auto-load."""
    reg._reset_registry_for_tests()
    monkeypatch.setattr(reg, "_ensure_builtin_sources", lambda: None)
    yield
    reg._reset_registry_for_tests()


def _failing_source(
    name="flaky",
    error_kind=ErrorKind.TIMEOUT,
    stale=None,
    stale_raises=False,
):
    """A source whose fetch always fails, with a configurable stale hook."""

    class _Src(SecretSource):
        def fetch(self, cfg, home_path):
            res = FetchResult()
            res.error = "backend unreachable"
            res.error_kind = error_kind
            return res

        def stale_secrets(self, cfg, home_path):
            if stale_raises:
                raise RuntimeError("boom")
            return stale

    _Src.name = name
    _Src.label = name.title()
    _Src.shape = "mapped"
    return _Src()


class TestOrchestratorStaleFallback:
    @pytest.mark.parametrize("kind", sorted(reg._STALE_FALLBACK_KINDS))
    def test_retryable_kinds_rescue_from_stale(self, tmp_path, kind):
        src = _failing_source(error_kind=kind, stale=({"API_TOKEN": "v1"}, 7200.0))
        reg.register_source(src)
        env: dict = {}
        report = reg.apply_all({"flaky": {"enabled": True}}, tmp_path, environ=env)

        assert env["API_TOKEN"] == "v1"
        sr = report.sources[0]
        assert sr.result.ok
        assert sr.applied == ["API_TOKEN"]
        assert any("serving last cached secrets" in w for w in sr.result.warnings)
        assert any("age 2.0h" in w for w in sr.result.warnings)
        assert report.provenance["API_TOKEN"].source == "flaky"

    @pytest.mark.parametrize(
        "kind",
        [ErrorKind.AUTH_FAILED, ErrorKind.AUTH_EXPIRED, ErrorKind.INTERNAL,
         ErrorKind.NOT_CONFIGURED, ErrorKind.REF_INVALID, ErrorKind.EMPTY_VALUE],
    )
    def test_non_retryable_kinds_never_rescue(self, tmp_path, kind):
        # The stale hook HAS data — policy alone must refuse it.
        src = _failing_source(error_kind=kind, stale=({"API_TOKEN": "v1"}, 60.0))
        reg.register_source(src)
        env: dict = {}
        report = reg.apply_all({"flaky": {"enabled": True}}, tmp_path, environ=env)

        assert "API_TOKEN" not in env
        sr = report.sources[0]
        assert not sr.result.ok
        assert sr.result.error_kind == kind

    def test_no_stale_data_keeps_the_error(self, tmp_path):
        src = _failing_source(error_kind=ErrorKind.TIMEOUT, stale=None)
        reg.register_source(src)
        env: dict = {}
        report = reg.apply_all({"flaky": {"enabled": True}}, tmp_path, environ=env)

        assert env == {}
        sr = report.sources[0]
        assert not sr.result.ok
        assert sr.result.error_kind == ErrorKind.TIMEOUT

    def test_hook_exception_is_contained(self, tmp_path):
        src = _failing_source(error_kind=ErrorKind.TIMEOUT, stale_raises=True)
        reg.register_source(src)
        env: dict = {}
        report = reg.apply_all({"flaky": {"enabled": True}}, tmp_path, environ=env)

        assert env == {}
        assert not report.sources[0].result.ok

    def test_wall_clock_timeout_takes_the_stale_path(self, tmp_path):
        """A source that blows the registry budget (never returns) rescues."""

        class _Hang(SecretSource):
            def fetch(self, cfg, home_path):
                time.sleep(5)
                return FetchResult()

            def fetch_timeout_seconds(self, cfg):
                return 0.05

            def stale_secrets(self, cfg, home_path):
                return {"SLOW_TOKEN": "cached"}, 60.0

        _Hang.name = "hang"
        _Hang.label = "Hang"
        _Hang.shape = "mapped"
        reg.register_source(_Hang())
        env: dict = {}
        reg.apply_all({"hang": {"enabled": True}}, tmp_path, environ=env)

        assert env["SLOW_TOKEN"] == "cached"


class TestOnePasswordStaleSecrets:
    CFG = {
        "enabled": True,
        "env": {"MY_TOKEN": "op://vault/item/field"},
        "cache_ttl_seconds": 300,
    }

    def _seed_disk_cache(self, home, cfg, environ, *, fetched_at):
        """Write a disk-cache entry with the same key fetch() would use."""
        token = set_source_environment(environ)
        try:
            valid, _ = op_mod._validate_references(cfg["env"])
            key = (
                op_mod._auth_fingerprint("OP_SERVICE_ACCOUNT_TOKEN"),
                "",
                str(home),
                op_mod._refs_fingerprint(valid),
            )
        finally:
            reset_source_environment(token)
        entry = CachedFetch(secrets={"MY_TOKEN": "cached-value"},
                            fetched_at=fetched_at)
        # ttl only gates freshness on read; any positive value writes.
        op_mod._DISK_CACHE.write(key, entry, 300, home)

    def test_returns_stale_entry_with_age(self, tmp_path):
        environ = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"}
        # Entry far older than the 300 s TTL: fresh read misses, stale hits.
        self._seed_disk_cache(tmp_path, self.CFG, environ,
                              fetched_at=time.time() - 86400)
        token = set_source_environment(environ)
        try:
            got = op_mod.OnePasswordSource().stale_secrets(self.CFG, tmp_path)
        finally:
            reset_source_environment(token)

        assert got is not None
        secrets, age = got
        assert secrets == {"MY_TOKEN": "cached-value"}
        assert age > 80000

    def test_different_auth_identity_never_matches(self, tmp_path):
        self._seed_disk_cache(tmp_path, self.CFG,
                              {"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"},
                              fetched_at=time.time() - 3600)
        token = set_source_environment({"OP_SERVICE_ACCOUNT_TOKEN": "OTHER"})
        try:
            got = op_mod.OnePasswordSource().stale_secrets(self.CFG, tmp_path)
        finally:
            reset_source_environment(token)
        assert got is None

    def test_cache_opt_out_disables_stale(self, tmp_path):
        environ = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"}
        self._seed_disk_cache(tmp_path, self.CFG, environ,
                              fetched_at=time.time() - 3600)
        cfg = dict(self.CFG, cache_ttl_seconds=0)
        token = set_source_environment(environ)
        try:
            got = op_mod.OnePasswordSource().stale_secrets(cfg, tmp_path)
        finally:
            reset_source_environment(token)
        assert got is None

    def test_missing_cache_returns_none(self, tmp_path):
        token = set_source_environment({"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"})
        try:
            got = op_mod.OnePasswordSource().stale_secrets(self.CFG, tmp_path)
        finally:
            reset_source_environment(token)
        assert got is None


class TestAllReferencesFailedIsAnError:
    def test_total_failure_reports_classified_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(op_mod, "find_op", lambda *_: Path("/usr/bin/true"))

        def _always_fail(op, ref, **kwargs):
            raise RuntimeError(f"op read failed for {ref!r}: connection refused")

        monkeypatch.setattr(op_mod, "_run_op_read", _always_fail)
        op_mod._reset_cache_for_tests(tmp_path)
        token = set_source_environment({"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"})
        try:
            result = op_mod.OnePasswordSource().fetch(self._cfg(), tmp_path)
        finally:
            reset_source_environment(token)

        assert not result.ok
        assert result.error_kind == ErrorKind.NETWORK
        assert "all 2 op:// reference(s) failed" in result.error

    def test_partial_failure_stays_ok_with_warnings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(op_mod, "find_op", lambda *_: Path("/usr/bin/true"))

        def _one_fails(op, ref, **kwargs):
            if "beta" in ref:
                raise RuntimeError(f"op read failed for {ref!r}: connection refused")
            return "value-a"

        monkeypatch.setattr(op_mod, "_run_op_read", _one_fails)
        op_mod._reset_cache_for_tests(tmp_path)
        token = set_source_environment({"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"})
        try:
            result = op_mod.OnePasswordSource().fetch(self._cfg(), tmp_path)
        finally:
            reset_source_environment(token)

        assert result.ok
        assert result.secrets == {"VAR_A": "value-a"}
        assert any("op read failed" in w for w in result.warnings)

    @staticmethod
    def _cfg():
        return {
            "enabled": True,
            "env": {
                "VAR_A": "op://vault/alpha/field",
                "VAR_B": "op://vault/beta/field",
            },
            # Cache off so the fetch path actually runs the (mocked) reads.
            "cache_ttl_seconds": 0,
        }
