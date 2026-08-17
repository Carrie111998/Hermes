"""Regression tests for the hyper.charm.land random-402 fix class.

Real-world failure (2026-08-08, custom provider hyper.charm.land, 3-key pool):

1. The funded key hit a transient 429 "Please try again in a few minutes."
   The pool benched it for the full 1-hour default TTL because the fuzzy
   retry guidance in the message was not parsed — ``_extract_retry_delay_seconds``
   only understood structured formats (Fix 1).
2. Rotation walked into two genuinely-depleted keys (402 billing), the pool
   reached "no available entries", and the turn aborted as "Billing or
   credits exhausted" even though the funded key would recover in minutes
   (Fix 3).
3. While the pool was fully benched, runtime resolution silently fell
   through to the config singleton key — serving keys the pool had benched,
   burning requests against depleted accounts with no rotation and no error
   (Fix 2).

These tests pin all three behaviors, including the #31273 money-burn guard
(pure-billing exhaustion must NOT get fuzzy cooldowns or recovery waits).
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fix 1 — fuzzy retry-delay parsing + billing guard (agent/credential_pool.py)
# ---------------------------------------------------------------------------


class TestFuzzyRetryDelayParsing:
    """Provider throttle messages without structured reset times must yield a
    bounded cooldown instead of falling through to the 1-hour default."""

    def test_a_few_minutes_parses_to_180s(self):
        from agent.credential_pool import _extract_retry_delay_seconds

        assert (
            _extract_retry_delay_seconds("Please try again in a few minutes.")
            == 180.0
        )

    def test_explicit_numeric_minutes(self):
        from agent.credential_pool import _extract_retry_delay_seconds

        assert _extract_retry_delay_seconds("Please try again in 5 minutes.") == 300.0

    def test_numeric_hours_capped_at_10_minutes(self):
        """Fuzzy-derived guidance can never bench longer than the cap."""
        from agent.credential_pool import (
            FUZZY_RETRY_DELAY_CAP_SECONDS,
            _extract_retry_delay_seconds,
        )

        assert (
            _extract_retry_delay_seconds("try again in about 2 hours")
            == FUZZY_RETRY_DELAY_CAP_SECONDS
        )

    def test_short_fuzzy_variants(self):
        from agent.credential_pool import _extract_retry_delay_seconds

        assert _extract_retry_delay_seconds("try again in a moment") == 60.0
        assert _extract_retry_delay_seconds("please try again shortly") == 60.0

    def test_unrelated_messages_still_return_none(self):
        from agent.credential_pool import _extract_retry_delay_seconds

        assert _extract_retry_delay_seconds("You're out of credits.") is None
        assert _extract_retry_delay_seconds("") is None

    def test_structured_formats_unaffected(self):
        from agent.credential_pool import _extract_retry_delay_seconds

        assert _extract_retry_delay_seconds("quotaResetDelay: 45s") == 45.0
        assert _extract_retry_delay_seconds("retry after 30 seconds") == 30.0


class TestFuzzyCooldownEndToEnd:
    """Marking a credential exhausted with a fuzzy throttle message stores the
    parsed window as reset_at, survives persistence, and unlocks the entry."""

    def _pool(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "credential_pool": {
                        "openrouter": [
                            {
                                "id": "cred-1",
                                "label": "cred-1",
                                "auth_type": "api_key",
                                "priority": 0,
                                "source": "manual",
                                "access_token": "sk-test-1",
                                "base_url": "https://openrouter.ai/api/v1",
                            }
                        ]
                    },
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        from agent.credential_pool import load_pool

        return load_pool("openrouter"), hermes_home

    def test_fuzzy_429_stores_parsed_reset_and_recovers(self, tmp_path, monkeypatch):
        pool, hermes_home = self._pool(tmp_path, monkeypatch)
        before = time.time()
        pool.mark_exhausted_and_rotate(
            status_code=429,
            error_context={
                "reason": "rate_limit",
                "message": "Please try again in a few minutes.",
            },
            api_key_hint="sk-test-1",
            failure_reason="rate_limit",
        )
        entry = pool.entries()[0]
        assert entry.last_status == "exhausted"
        assert entry.last_error_reset_at is not None
        # ~180s window, not the 1-hour default
        assert 170 <= (entry.last_error_reset_at - before) <= 200

        # Simulate the window elapsing: rewind the persisted timestamps and
        # reload from disk (also proves the parsed window survives restart).
        store = json.loads((hermes_home / "auth.json").read_text())
        for e in store["credential_pool"]["openrouter"]:
            if e.get("last_status") == "exhausted":
                e["last_status_at"] = time.time() - 300
                e["last_error_reset_at"] = time.time() - 60
        (hermes_home / "auth.json").write_text(json.dumps(store))

        from agent.credential_pool import load_pool

        pool2 = load_pool("openrouter")
        recovered = pool2.select()
        assert recovered is not None
        assert recovered.id == "cred-1"
        assert recovered.last_status == "ok"

    def test_billing_failure_drops_fuzzy_window(self, tmp_path, monkeypatch):
        """The #31273 guard: a billing-classified failure must NOT inherit a
        fuzzy window parsed from its message — the entry keeps the full bench."""
        pool, _ = self._pool(tmp_path, monkeypatch)
        pool.mark_exhausted_and_rotate(
            status_code=402,
            error_context={
                "reason": "billing_error",
                "message": "You're out of credits. Try again in 5 minutes.",
            },
            api_key_hint="sk-test-1",
            failure_reason="billing",
        )
        entry = pool.entries()[0]
        assert entry.last_error_reset_at is None
        assert pool.has_available() is False

    def test_billing_failure_keeps_structured_reset(self, tmp_path, monkeypatch):
        """Provider-supplied structured reset_at is the provider's contract —
        the billing guard only drops message-PARSED windows."""
        pool, _ = self._pool(tmp_path, monkeypatch)
        reset = time.time() + 7200
        pool.mark_exhausted_and_rotate(
            status_code=402,
            error_context={
                "reason": "billing_error",
                "message": "You're out of credits.",
                "reset_at": reset,
            },
            api_key_hint="sk-test-1",
            failure_reason="billing",
        )
        entry = pool.entries()[0]
        assert entry.last_error_reset_at == pytest.approx(reset, abs=5)


class TestExtractApiErrorContextFuzzy:
    """The conversation-loop error-context extractor must populate reset_at
    from fuzzy throttle messages so the pool bench uses the provider window."""

    def _fake_error(self, message: str, error_type: str = "rate_limit_error"):
        err = Exception(f"Error code: 429 - {message}")
        err.body = {"error": {"message": message, "type": error_type, "code": None}}
        return err

    def test_fuzzy_message_populates_reset_at(self):
        from agent.agent_runtime_helpers import extract_api_error_context

        before = time.time()
        ctx = extract_api_error_context(
            self._fake_error("Please try again in a few minutes.")
        )
        assert ctx.get("message") == "Please try again in a few minutes."
        assert ctx.get("reset_at") is not None
        assert 170 <= (ctx["reset_at"] - before) <= 200

    def test_explicit_numeric_window(self):
        from agent.agent_runtime_helpers import extract_api_error_context

        before = time.time()
        ctx = extract_api_error_context(self._fake_error("Please try again in 5 minutes."))
        assert ctx.get("reset_at") == pytest.approx(before + 300, abs=5)

    def test_structured_field_wins_over_fuzzy(self):
        from agent.agent_runtime_helpers import extract_api_error_context

        reset = time.time() + 999
        err = self._fake_error("Please try again in a few minutes.")
        err.body["error"]["resets_at"] = reset
        ctx = extract_api_error_context(err)
        assert ctx.get("reset_at") == reset
        # Structured provider values are NOT tagged message-derived — the
        # billing guard must keep them.
        assert ctx.get("reset_at_source") is None

    def test_message_parsed_windows_are_tagged(self):
        from agent.agent_runtime_helpers import extract_api_error_context

        for message in (
            "Please try again in a few minutes.",
            "quotaResetDelay: 45s",
            "resets in 5 minutes",
            "retry after 30 seconds",
        ):
            ctx = extract_api_error_context(self._fake_error(message))
            assert ctx.get("reset_at_source") == "message_parsed", message


class TestBillingGuardIntegration:
    """End-to-end seam: extract_api_error_context -> mark_exhausted_and_rotate.

    The billing guard must drop message-derived windows on the RUNTIME path —
    not just when a hand-built dict reaches the pool directly (which is what
    let the seam ship green: the extractor pre-parses messages into reset_at,
    so the pool sees a populated field, not an empty one).
    """

    def _pool(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "credential_pool": {
                        "openrouter": [
                            {
                                "id": "cred-1",
                                "label": "cred-1",
                                "auth_type": "api_key",
                                "priority": 0,
                                "source": "manual",
                                "access_token": "sk-test-1",
                                "base_url": "https://openrouter.ai/api/v1",
                            }
                        ]
                    },
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        from agent.credential_pool import load_pool

        return load_pool("openrouter")

    def _billing_error(self, message: str):
        err = Exception(f"Error code: 402 - {message}")
        err.body = {"error": {"message": message, "type": "billing_error", "code": None}}
        return err

    def test_fuzzy_billing_body_gets_full_bench_not_fuzzy_window(
        self, tmp_path, monkeypatch
    ):
        """A genuinely-depleted account whose 402 body contains fuzzy retry
        phrasing must NOT re-enter rotation after the fuzzy window — that is
        one burned paid request per window, indefinitely (#31273 class)."""
        from agent.agent_runtime_helpers import extract_api_error_context

        pool = self._pool(tmp_path, monkeypatch)
        ctx = extract_api_error_context(
            self._billing_error("You're out of credits. Try again in 5 minutes.")
        )
        # The extractor tags the parsed window; the pool must drop it for
        # billing failures.
        assert ctx.get("reset_at_source") == "message_parsed"
        pool.mark_exhausted_and_rotate(
            status_code=402,
            error_context=ctx,
            api_key_hint="sk-test-1",
            failure_reason="billing",
        )
        entry = pool.entries()[0]
        assert entry.last_error_reset_at is None
        assert pool.has_available() is False

    def test_structured_billing_reset_survives_runtime_path(self, tmp_path, monkeypatch):
        """Provider-supplied structured resets_at is the provider's contract —
        it must survive the billing guard even through the extractor."""
        from agent.agent_runtime_helpers import extract_api_error_context

        pool = self._pool(tmp_path, monkeypatch)
        reset = time.time() + 7200
        err = self._billing_error("You're out of credits.")
        err.body["error"]["resets_at"] = reset
        ctx = extract_api_error_context(err)
        pool.mark_exhausted_and_rotate(
            status_code=402,
            error_context=ctx,
            api_key_hint="sk-test-1",
            failure_reason="billing",
        )
        entry = pool.entries()[0]
        assert entry.last_error_reset_at == pytest.approx(reset, abs=5)

    def test_fuzzy_transient_body_keeps_window_runtime_path(self, tmp_path, monkeypatch):
        """The guard is billing-only: a transient 429 with the same fuzzy
        message keeps its parsed window through the runtime path."""
        from agent.agent_runtime_helpers import extract_api_error_context

        pool = self._pool(tmp_path, monkeypatch)
        before = time.time()
        err = Exception("Error code: 429 - rate_limit_error")
        err.body = {
            "error": {
                "message": "Please try again in a few minutes.",
                "type": "rate_limit_error",
                "code": None,
            }
        }
        ctx = extract_api_error_context(err)
        pool.mark_exhausted_and_rotate(
            status_code=429,
            error_context=ctx,
            api_key_hint="sk-test-1",
            failure_reason="rate_limit",
        )
        entry = pool.entries()[0]
        assert entry.last_error_reset_at is not None
        assert 170 <= (entry.last_error_reset_at - before) <= 200


# ---------------------------------------------------------------------------
# Fix 2 — runtime resolution must never serve a billing-benched key
#         (hermes_cli/runtime_provider.py)
# ---------------------------------------------------------------------------


class TestPickCustomApiKeyBenchFilter:
    def _exhaustion(self, benched, pool_key="custom:hyper.test"):
        return {"pool_key": pool_key, "benched": benched, "next_available_at": None}

    def test_billing_benched_key_skipped(self):
        from hermes_cli.runtime_provider import _pick_custom_api_key

        key = _pick_custom_api_key(
            ["sk-benched", "sk-healthy"],
            self._exhaustion({"sk-benched": {"billing": True, "reset_at": time.time() + 3000}}),
            context="test",
        )
        assert key == "sk-healthy"

    def test_all_benched_raises_structured_exhaustion(self):
        from hermes_cli.auth import AuthError
        from hermes_cli.runtime_provider import _pick_custom_api_key

        with pytest.raises(AuthError) as ei:
            _pick_custom_api_key(
                ["sk-benched"],
                self._exhaustion({"sk-benched": {"billing": True, "reset_at": time.time() + 3000}}),
                context="test",
            )
        assert ei.value.code == "insufficient_credits"

    def test_transient_bench_is_served_with_warning(self):
        """A transiently-benched key is the only recovery vehicle when the
        pool has no alternative — it must still be served (the wait-and-retry
        path handles recovery)."""
        from hermes_cli.runtime_provider import _pick_custom_api_key

        key = _pick_custom_api_key(
            ["sk-throttled"],
            self._exhaustion({"sk-throttled": {"billing": False, "reset_at": time.time() + 120}}),
            context="test",
        )
        assert key == "sk-throttled"

    def test_no_pool_state_is_legacy_behavior(self):
        from hermes_cli.runtime_provider import _pick_custom_api_key

        assert _pick_custom_api_key(["sk-any"], None, context="test") == "sk-any"
        assert _pick_custom_api_key(["sk-any"], {}, context="test") == "sk-any"

    def test_untracked_key_served(self):
        """Keys the pool never saw (env-only, never seeded) are unaffected."""
        from hermes_cli.runtime_provider import _pick_custom_api_key

        key = _pick_custom_api_key(
            ["sk-env-only"],
            self._exhaustion({"sk-other": {"billing": True, "reset_at": time.time() + 60}}),
            context="test",
        )
        assert key == "sk-env-only"


class TestCustomPoolExhaustionInfo:
    """_try_resolve_from_custom_pool must report pool exhaustion so callers
    can refuse billing-benched fallthrough keys (the silent-bypass defect)."""

    def _setup_hermes_home(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "config.yaml").write_text(
            "custom_providers:\n"
            "  - name: HyperTest\n"
            "    base_url: https://hyper.test/v1\n"
        )
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "credential_pool": {
                        "custom:hypertest": [
                            {
                                "id": "cred-1",
                                "label": "manual-1",
                                "auth_type": "api_key",
                                "priority": 0,
                                "source": "manual",
                                "access_token": "sk-bench-1",
                                "base_url": "https://hyper.test/v1",
                                "last_status": "exhausted",
                                "last_status_at": time.time(),
                                "last_error_code": 402,
                                "failure_reason": "billing",
                            }
                        ]
                    },
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
            monkeypatch.delenv(var, raising=False)

    def test_exhausted_pool_reports_benched_keys(self, tmp_path, monkeypatch):
        self._setup_hermes_home(tmp_path, monkeypatch)
        from hermes_cli.auth import AuthError
        from hermes_cli.runtime_provider import (
            _pick_custom_api_key,
            _try_resolve_from_custom_pool,
        )

        info: dict = {}
        result = _try_resolve_from_custom_pool(
            "https://hyper.test/v1", "custom", exhaustion_info=info
        )
        assert result is None
        assert info.get("pool_key") == "custom:hypertest"
        benched = info.get("benched", {})
        assert "sk-bench-1" in benched
        assert benched["sk-bench-1"]["billing"] is True
        # reset_at must be a concrete future epoch, not a self-reference.
        reset_at = benched["sk-bench-1"]["reset_at"]
        assert isinstance(reset_at, float)
        assert reset_at > time.time()
        # The fallthrough config key is exactly the benched key → refuse it.
        with pytest.raises(AuthError) as ei:
            _pick_custom_api_key(["sk-bench-1"], info, context="custom_provider:HyperTest")
        assert ei.value.code == "insufficient_credits"

    def test_healthy_pool_unaffected(self, tmp_path, monkeypatch):
        self._setup_hermes_home(tmp_path, monkeypatch)
        hermes_home = tmp_path / "hermes"
        store = json.loads((hermes_home / "auth.json").read_text())
        entry = store["credential_pool"]["custom:hypertest"][0]
        for k in ("last_status", "last_status_at", "last_error_code", "failure_reason"):
            entry.pop(k, None)
        (hermes_home / "auth.json").write_text(json.dumps(store))

        from hermes_cli.runtime_provider import _try_resolve_from_custom_pool

        info: dict = {}
        result = _try_resolve_from_custom_pool(
            "https://hyper.test/v1", "custom", exhaustion_info=info
        )
        assert result is not None
        assert result["api_key"] == "sk-bench-1"
        assert info == {}  # never populated when the pool can serve


# ---------------------------------------------------------------------------
# Fix 3 — recoverable-exhaustion wait (pool assessment + recovery helper)
# ---------------------------------------------------------------------------


class TestConfigSeededBenchSurvivesReload:
    """Borrowed (config-seeded) entries persist without their secret and are
    re-hydrated from custom_providers.api_key on every load_pool(). The
    upsert must treat re-hydration as SAME secret (bench survives), not as a
    rotation (bench wiped) — otherwise a billing-benched config key is served
    again after every reload, defeating the cooldown filter for exactly the
    config-seeded pools it exists to protect. A genuinely changed api_key in
    config.yaml is a real rotation and MUST still clear the bench.
    """

    def _setup(self, tmp_path, monkeypatch, api_key: str, *, benched: bool):
        from agent.credential_persistence import _fingerprint_value

        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "config.yaml").write_text(
            "custom_providers:\n"
            "  - name: BenchTest\n"
            "    base_url: https://bench.test/v1\n"
            f"    api_key: {api_key}\n"
        )
        entry = {
            "id": "cfg-1",
            "label": "BenchTest",
            "auth_type": "api_key",
            "priority": 0,
            "source": "config:BenchTest",
            # Borrowed entries persist WITHOUT the secret.
            "base_url": "https://bench.test/v1",
        }
        if benched:
            entry.update(
                {
                    "last_status": "exhausted",
                    "last_status_at": time.time(),
                    "last_error_code": 402,
                    "failure_reason": "billing",
                    "last_error_message": "You're out of credits.",
                    "secret_fingerprint": _fingerprint_value(api_key),
                }
            )
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {"version": 1, "credential_pool": {"custom:benchtest": [entry]}}
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        return hermes_home

    def test_bench_survives_rehydration(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, "sk-cfg-1", benched=True)
        from agent.credential_pool import load_pool

        pool = load_pool("custom:benchtest")
        entries = pool.entries()
        assert len(entries) == 1
        # Re-hydrated from config, still benched — NOT treated as rotation.
        assert entries[0].runtime_api_key == "sk-cfg-1"
        assert entries[0].last_status == "exhausted"
        assert pool.has_available() is False
        assert pool.select() is None

    def test_real_key_rotation_clears_bench(self, tmp_path, monkeypatch):
        """Persisted bench + fingerprint for sk-cfg-1, but config now carries
        sk-cfg-2: the secret genuinely changed, so the stale bench clears."""
        hermes_home = self._setup(tmp_path, monkeypatch, "sk-cfg-1", benched=True)
        (hermes_home / "config.yaml").write_text(
            "custom_providers:\n"
            "  - name: BenchTest\n"
            "    base_url: https://bench.test/v1\n"
            "    api_key: sk-cfg-2\n"
        )
        from agent.credential_pool import load_pool

        pool = load_pool("custom:benchtest")
        entries = pool.entries()
        assert len(entries) == 1
        assert entries[0].runtime_api_key == "sk-cfg-2"
        assert entries[0].last_status is None
        assert pool.select() is not None

    def test_runtime_bench_filter_honors_surviving_bench(self, tmp_path, monkeypatch):
        """End-to-end with the resolution layer: exhausted pool + config
        fallthrough key is the benched key -> AuthError, even after a fresh
        load_pool() re-hydrated the borrowed entry."""
        self._setup(tmp_path, monkeypatch, "sk-cfg-1", benched=True)
        from hermes_cli.auth import AuthError
        from hermes_cli.runtime_provider import (
            _pick_custom_api_key,
            _try_resolve_from_custom_pool,
        )

        info: dict = {}
        result = _try_resolve_from_custom_pool(
            "https://bench.test/v1", "custom", exhaustion_info=info
        )
        assert result is None
        assert info.get("benched", {}).get("sk-cfg-1", {}).get("billing") is True
        with pytest.raises(AuthError) as ei:
            _pick_custom_api_key(["sk-cfg-1"], info, context="custom_provider:BenchTest")
        assert ei.value.code == "insufficient_credits"


class TestLegacyFingerprintUpgrade:
    """auth.json written before the fingerprint field existed has borrowed
    entries with no in-memory secret AND no persisted secret_fingerprint.
    The first load_pool() must NOT treat that as a rotation and wipe the
    bench — the legacy-upgrade path must keep the bench and carry the
    fingerprint forward so subsequent loads compare correctly."""

    def test_bench_survives_first_load_without_fingerprint(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "config.yaml").write_text(
            "custom_providers:\n"
            "  - name: BenchTest\n"
            "    base_url: https://bench.test/v1\n"
            "    api_key: sk-legacy-1\n"
        )
        # Legacy entry: benched, but NO secret_fingerprint field.
        entry = {
            "id": "cfg-1",
            "label": "BenchTest",
            "auth_type": "api_key",
            "priority": 0,
            "source": "config:BenchTest",
            "base_url": "https://bench.test/v1",
            "last_status": "exhausted",
            "last_status_at": time.time(),
            "last_error_code": 402,
            "failure_reason": "billing",
            "last_error_message": "You're out of credits.",
        }
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {"version": 1, "credential_pool": {"custom:benchtest": [entry]}}
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
            monkeypatch.delenv(var, raising=False)

        from agent.credential_pool import load_pool

        pool = load_pool("custom:benchtest")
        entries = pool.entries()
        assert len(entries) == 1
        # Legacy upgrade: bench survives even without a persisted fingerprint.
        assert entries[0].last_status == "exhausted"
        assert pool.has_available() is False
        # Fingerprint is carried forward onto the entry so the next load
        # compares correctly.
        from agent.credential_persistence import _fingerprint_value

        assert entries[0].extra.get("secret_fingerprint") == _fingerprint_value("sk-legacy-1")


class TestRecoverableWaitSeconds:
    def _entry(self, error_code, *, age_seconds, failure_reason=None, reset_at=None):
        entry = {
            "id": f"cred-{error_code}-{age_seconds}",
            "label": f"cred-{error_code}",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": f"sk-{error_code}-{int(age_seconds)}",
            "base_url": "https://openrouter.ai/api/v1",
            "last_status": "exhausted",
            "last_status_at": time.time() - age_seconds,
            "last_error_code": error_code,
        }
        if failure_reason is not None:
            entry["failure_reason"] = failure_reason
        if reset_at is not None:
            entry["last_error_reset_at"] = reset_at
        return entry

    def _pool(self, tmp_path, monkeypatch, entries):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "auth.json").write_text(
            json.dumps({"version": 1, "credential_pool": {"openrouter": entries}})
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        from agent.credential_pool import load_pool

        return load_pool("openrouter")

    def test_fuzzy_throttle_recovers_within_bound(self, tmp_path, monkeypatch):
        """hyper.charm.land class: a 429 benched with a parsed 180s window
        yields a bounded, TRANSIENT wait assessment."""
        pool = self._pool(
            tmp_path, monkeypatch,
            [self._entry(429, age_seconds=30, reset_at=time.time() + 120)],
        )
        assessment = pool.recoverable_wait_seconds(max_wait=600)
        assert assessment is not None
        wait, transient = assessment
        assert 100 <= wait <= 130
        assert transient is True

    def test_hour_long_bench_exceeds_bound(self, tmp_path, monkeypatch):
        """Default-TTL benches (no provider window) stay beyond the wait bound —
        recovery must not hold a turn for an hour. Two entries so the
        sole-credential 60s cap does not apply."""
        pool = self._pool(
            tmp_path,
            monkeypatch,
            [
                self._entry(429, age_seconds=30),
                {**self._entry(429, age_seconds=30), "id": "cred-x",
                 "access_token": "sk-x"},
            ],
        )
        assert pool.recoverable_wait_seconds(max_wait=600) is None

    def test_billing_earliest_entry_not_transient(self, tmp_path, monkeypatch):
        """The #31273 guard: waiting on a billing-exhausted account is refused."""
        pool = self._pool(
            tmp_path, monkeypatch,
            [self._entry(402, age_seconds=30, failure_reason="billing",
                         reset_at=time.time() + 120)],
        )
        assessment = pool.recoverable_wait_seconds(max_wait=600)
        assert assessment is not None
        _wait, transient = assessment
        assert transient is False

    def test_transient_earliest_wins_over_billing_sibling(self, tmp_path, monkeypatch):
        """Mixed pool: the transient key recovers first and is served first,
        so waiting is justified even while a billing sibling stays benched."""
        pool = self._pool(
            tmp_path,
            monkeypatch,
            [
                self._entry(429, age_seconds=30, reset_at=time.time() + 100),
                self._entry(402, age_seconds=30, failure_reason="billing",
                            reset_at=time.time() + 300),
            ],
        )
        assessment = pool.recoverable_wait_seconds(max_wait=600)
        assert assessment is not None
        wait, transient = assessment
        assert 80 <= wait <= 110
        assert transient is True

    def test_available_pool_returns_none(self, tmp_path, monkeypatch):
        pool = self._pool(tmp_path, monkeypatch, [])
        assert pool.recoverable_wait_seconds(max_wait=600) is None

    def test_same_reset_at_transient_wins_tiebreak(self, tmp_path, monkeypatch):
        """Two entries with identical reset_at: a transient 429 and a billing
        402. The transient entry must win the tiebreak (candidates[0]) so the
        wait is offered. If the billing entry won, recoverable_wait_seconds
        would see billing=True for the earliest candidate and refuse to wait —
        stranding a recoverable transient key behind a depleted one at the
        same epoch.
        """
        reset = time.time() + 120
        pool = self._pool(
            tmp_path,
            monkeypatch,
            [
                self._entry(402, age_seconds=30, failure_reason="billing",
                            reset_at=reset),
                self._entry(429, age_seconds=30, reset_at=reset),
            ],
        )
        assessment = pool.recoverable_wait_seconds(max_wait=600)
        assert assessment is not None
        _wait, transient = assessment
        assert transient is True

    def test_402_classified_rate_limit_is_not_billing(self, tmp_path, monkeypatch):
        """A 402 that the error classifier resolved to ``rate_limit`` (transient
        usage-limit, e.g. "usage limit, try again in 5 minutes") must NOT be
        treated as billing. The _classify_402 disambiguation exists precisely
        for this case; _entry_is_billing_benched must honor the classifier
        override so the entry gets a transient bench and can trigger a wait.
        """
        reset = time.time() + 120
        pool = self._pool(
            tmp_path,
            monkeypatch,
            [
                self._entry(402, age_seconds=30, failure_reason="rate_limit",
                            reset_at=reset),
            ],
        )
        assessment = pool.recoverable_wait_seconds(max_wait=600)
        assert assessment is not None
        _wait, transient = assessment
        assert transient is True


class TestWaitForPoolRecovery:
    def _agent(self):
        agent = MagicMock()
        agent._interrupt_requested = False
        agent.log_prefix = ""
        return agent

    def _pool_with_assessment(self, assessment):
        class _Pool:
            def recoverable_wait_seconds(self, *, max_wait=600.0):
                return assessment

        return _Pool()

    def test_transient_wait_completes_and_reports_true(self):
        from agent.agent_runtime_helpers import _wait_for_pool_recovery

        start = time.time()
        assert _wait_for_pool_recovery(self._agent(), self._pool_with_assessment((0.3, True))) is True
        assert time.time() - start >= 0.3

    def test_billing_assessment_refused_immediately(self, monkeypatch):
        from agent.agent_runtime_helpers import _wait_for_pool_recovery

        sleep_calls = []
        import time as time_mod

        monkeypatch.setattr(time_mod, "sleep", lambda s: sleep_calls.append(s))
        assert _wait_for_pool_recovery(self._agent(), self._pool_with_assessment((0.3, False))) is False
        # Event-based: the billing refusal must not enter the sleep loop at
        # all (wall-clock bounds here would be flake-prone on slow runners).
        assert sleep_calls == []

    def test_none_assessment_refused(self):
        from agent.agent_runtime_helpers import _wait_for_pool_recovery

        assert _wait_for_pool_recovery(self._agent(), self._pool_with_assessment(None)) is False

    def test_interrupted_wait_returns_false(self):
        from agent.agent_runtime_helpers import _wait_for_pool_recovery

        agent = self._agent()
        agent._interrupt_requested = True
        assert _wait_for_pool_recovery(agent, self._pool_with_assessment((60.0, True))) is False

    def test_lightweight_pool_adapter_without_assessment_api(self):
        """Pool adapters lacking recoverable_wait_seconds degrade to no-wait
        instead of raising (compat contract from credential_pool docstring)."""
        from agent.agent_runtime_helpers import _wait_for_pool_recovery

        class _LegacyPool:
            def select(self):
                return None

        assert _wait_for_pool_recovery(self._agent(), _LegacyPool()) is False


class TestModelSwitchAuthErrorPropagation:
    """model_switch.py must propagate AuthError from resolve_runtime_provider
    instead of swallowing it via a broad except-Exception that falls back to
    the raw config key — which would serve a key the pool has benched for
    billing (#40960 / #31273)."""

    def test_autherror_not_swallowed_by_model_switch(self, tmp_path, monkeypatch):
        """When resolve_runtime_provider raises AuthError (e.g. all keys
        billing-benched), switch_model must NOT swallow it and silently fall
        back to the raw config key — it must propagate the error so the
        caller can route to the fallback chain / billing UX."""
        from hermes_cli.auth import AuthError
        from hermes_cli import runtime_provider as rp
        from hermes_cli.providers import ProviderDef
        import pytest as _pytest

        def _raise_auth_error(**kwargs):
            raise AuthError(
                "All credentials exhausted (billing).",
                provider="custom",
                code="insufficient_credits",
            )

        # Patch at the source module — model_switch imports it locally.
        monkeypatch.setattr(rp, "resolve_runtime_provider", _raise_auth_error)
        # Provide a valid ProviderDef so switch_model reaches the resolution
        # path that calls resolve_runtime_provider.
        monkeypatch.setattr(
            "hermes_cli.model_switch.resolve_provider_full",
            lambda *a, **k: ProviderDef(
                id="mycustom", name="mycustom", transport="openai_chat",
                api_key_env_vars=("MY_API_KEY",), base_url="https://test.example/v1",
                is_aggregator=False, auth_type="api_key", source="user-config",
            ),
        )

        from hermes_cli.model_switch import switch_model
        # AuthError must propagate — NOT be swallowed into a raw-key fallback.
        with _pytest.raises(AuthError) as ei:
            switch_model(
                raw_input="test-model",
                current_provider="mycustom",
                current_model="old-model",
                current_base_url="https://test.example/v1",
                current_api_key="sk-current",
                explicit_provider="mycustom",
                user_providers={"mycustom": {"api": "https://test.example/v1", "api_key": "sk-test"}},
            )
        assert ei.value.code == "insufficient_credits"
