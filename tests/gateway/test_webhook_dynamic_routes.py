"""Tests for webhook adapter dynamic route loading."""

import json
import os
from pathlib import Path

import pytest

import gateway.platforms.webhook_intake as webhook_intake_module
from gateway.config import PlatformConfig
from gateway.platforms.webhook import (
    WebhookAdapter,
    _DYNAMIC_ROUTES_FILENAME,
    _INSECURE_NO_AUTH,
)
from gateway.platforms.webhook_filters import BoundedRegularFileSnapshot
from gateway.platforms.webhook_ledger import WebhookLedgerError


def _make_adapter(routes=None, extra=None):
    _extra = extra or {}
    if routes:
        _extra["routes"] = routes
    _extra.setdefault("secret", "test-global-secret")
    config = PlatformConfig(enabled=True, extra=_extra)
    return WebhookAdapter(config)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


class TestDynamicRouteLoading:
    def test_no_dynamic_file(self):
        adapter = _make_adapter(
            routes={"static": {"secret": "s", "provider": "generic"}}
        )
        adapter._reload_dynamic_routes()
        assert "static" in adapter._routes
        assert len(adapter._dynamic_routes) == 0

    def test_loads_dynamic_routes(self, tmp_path):
        subs = {
            "my-hook": {
                "secret": "dynamic-secret",
                "provider": "generic",
                "prompt": "test",
                "events": [],
            }
        }
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(json.dumps(subs))

        adapter = _make_adapter(
            routes={"static": {"secret": "s", "provider": "generic"}}
        )
        adapter._reload_dynamic_routes()
        assert "my-hook" in adapter._routes
        assert "static" in adapter._routes

    def test_invalid_unicode_event_is_rejected_without_transient_retry(
        self,
        tmp_path,
    ):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({
                "invalid-event": {
                    "secret": "dynamic-secret",
                    "provider": "github",
                    "events": ["\udcff"],
                }
            }),
            encoding="utf-8",
        )
        adapter = _make_adapter()

        adapter._reload_dynamic_routes()

        assert "invalid-event" not in adapter._routes
        assert adapter._dynamic_routes_file_identity is not None
        assert adapter._dynamic_routes_content_sha256 is not None
        assert adapter._dynamic_routes_rejected_file_identity is None
        assert adapter._dynamic_routes_rejected_content_sha256 is None
        assert adapter._dynamic_routes_transient_file_identity is None
        assert adapter._dynamic_routes_retry_after == 0.0


class TestDynamicRouteSecretValidation:
    """Empty/missing secrets must be rejected during hot-reload.

    Regression for HMAC bypass: prior to the fix, an agent-induced
    dynamic route with `"secret": ""` would be merged into self._routes
    by _reload_dynamic_routes(), then _handle_webhook's
    `if secret and secret != _INSECURE_NO_AUTH` would skip signature
    validation because empty string is falsy. Unauthenticated POSTs
    would then execute the webhook prompt.
    """

    def test_empty_secret_rejected(self, tmp_path):
        # Explicit empty-string secret must NOT fall back to the global
        # secret, and the route must be skipped entirely.
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({
                "evil": {
                    "secret": "",
                    "provider": "generic",
                    "prompt": "rm -rf",
                }
            })
        )
        adapter = _make_adapter()  # has global secret
        adapter._reload_dynamic_routes()
        assert "evil" not in adapter._routes
        assert "evil" not in adapter._dynamic_routes

    def test_missing_secret_no_global_rejected(self, tmp_path):
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({"orphan": {"provider": "generic", "prompt": "test"}})
        )
        # No global secret configured
        adapter = _make_adapter(extra={"secret": ""})
        adapter._reload_dynamic_routes()
        assert "orphan" not in adapter._routes
        assert "orphan" not in adapter._dynamic_routes

    def test_missing_secret_inherits_global(self, tmp_path):
        # No per-route secret but a global one is set → route is kept,
        # the global secret protects it. Preserves existing fallback.
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({"valid": {"provider": "generic", "prompt": "ok"}})
        )
        adapter = _make_adapter()  # global secret set
        adapter._reload_dynamic_routes()
        assert "valid" in adapter._routes

    def test_dynamic_route_cannot_reuse_static_route_secret(self, tmp_path):
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({
                "dynamic": {
                    "secret": "shared-secret",
                    "provider": "generic",
                }
            })
        )
        adapter = _make_adapter(
            routes={
                "static": {
                    "secret": "shared-secret",
                    "provider": "github",
                }
            }
        )

        adapter._reload_dynamic_routes()

        assert "dynamic" not in adapter._routes

    def test_later_dynamic_route_cannot_reuse_sibling_secret(self, tmp_path):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({
                "first": {
                    "secret": "dynamic-shared-secret",
                    "provider": "generic",
                }
            })
        )
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert set(adapter._dynamic_routes) == {"first"}

        subscriptions.write_text(
            json.dumps({
                "first": {
                    "secret": "dynamic-shared-secret",
                    "provider": "generic",
                },
                "second": {
                    "secret": "dynamic-shared-secret",
                    "provider": "github",
                },
            })
        )
        adapter._reload_dynamic_routes()

        assert set(adapter._dynamic_routes) == {"first"}

    def test_conflicting_reordered_snapshot_keeps_last_known_good(self, tmp_path):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({
                "low": {
                    "secret": "captured-shared-secret",
                    "provider": "generic",
                }
            })
        )
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert set(adapter._dynamic_routes) == {"low"}

        subscriptions.write_text(
            json.dumps({
                "high": {
                    "secret": "captured-shared-secret",
                    "provider": "github",
                    "toolsets": ["terminal"],
                },
                "low": {
                    "secret": "captured-shared-secret",
                    "provider": "generic",
                },
            })
        )
        adapter._reload_dynamic_routes()

        assert set(adapter._dynamic_routes) == {"low"}
        assert "high" not in adapter._routes

    @pytest.mark.parametrize("mtime_delta_ns", [0, -2_000_000_000])
    def test_content_change_loads_with_equal_or_older_mtime(
        self,
        tmp_path,
        mtime_delta_ns,
    ):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({"hook": {"secret": "secret-one", "provider": "generic"}})
        )
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        original = subscriptions.stat()

        subscriptions.write_text(
            json.dumps({"hook": {"secret": "secret-two", "provider": "generic"}})
        )
        os.utime(
            subscriptions,
            ns=(original.st_atime_ns, original.st_mtime_ns + mtime_delta_ns),
        )
        adapter._reload_dynamic_routes()

        assert adapter._routes["hook"]["secret"] == "secret-two"

    def test_unchanged_file_is_not_rehashed_within_integrity_interval(
        self,
        tmp_path,
        monkeypatch,
    ):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({"hook": {"secret": "stable-secret", "provider": "generic"}})
        )
        adapter = _make_adapter()
        real_snapshot = webhook_intake_module.read_bounded_regular_file_snapshot
        reads = 0

        def tracked_snapshot(path, *, max_bytes):
            nonlocal reads
            if path == subscriptions:
                reads += 1
            return real_snapshot(path, max_bytes=max_bytes)

        monkeypatch.setattr(
            webhook_intake_module,
            "read_bounded_regular_file_snapshot",
            tracked_snapshot,
        )
        adapter._reload_dynamic_routes()
        adapter._reload_dynamic_routes()

        assert reads == 1

    def test_preserved_metadata_still_rehashes_changed_content(
        self,
        tmp_path,
        monkeypatch,
    ):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        first = json.dumps({
            "hook": {"secret": "secret-one", "provider": "generic"}
        }).encode()
        second = json.dumps({
            "hook": {"secret": "secret-two", "provider": "generic"}
        }).encode()
        assert len(first) == len(second)
        subscriptions.write_bytes(first)
        adapter = _make_adapter()
        clock = [100.0]
        monkeypatch.setattr(
            webhook_intake_module.time,
            "monotonic",
            lambda: clock[0],
        )
        adapter._reload_dynamic_routes()
        original_stat = subscriptions.stat()

        subscriptions.write_bytes(second)
        real_snapshot = webhook_intake_module.read_bounded_regular_file_snapshot
        real_stat = Path.stat

        def preserved_metadata(path, *args, **kwargs):
            if path == subscriptions:
                return original_stat
            return real_stat(path, *args, **kwargs)

        def preserved_snapshot(path, *, max_bytes):
            snapshot = real_snapshot(path, max_bytes=max_bytes)
            if path == subscriptions:
                return BoundedRegularFileSnapshot(snapshot.content, original_stat)
            return snapshot

        monkeypatch.setattr(Path, "stat", preserved_metadata)
        monkeypatch.setattr(
            webhook_intake_module,
            "read_bounded_regular_file_snapshot",
            preserved_snapshot,
        )

        adapter._reload_dynamic_routes()
        assert adapter._routes["hook"]["secret"] == "secret-one"

        clock[0] += (
            webhook_intake_module._DYNAMIC_ROUTES_CONTENT_RECHECK_SECONDS + 0.001
        )
        adapter._reload_dynamic_routes()

        assert adapter._routes["hook"]["secret"] == "secret-two"

    def test_malformed_replacement_withdraws_previous_dynamic_routes(
        self,
        tmp_path,
    ):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({"hook": {"secret": "revoked-secret", "provider": "generic"}})
        )
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert "hook" in adapter._routes
        profile = str(adapter._authenticated_route_bundles["hook"].authority[0])
        assert adapter._record_rate_limit_hit("hook", 1.0, profile=profile)
        assert (profile, "hook") in adapter._rate_counts

        subscriptions.write_text('{"hook":')
        adapter._reload_dynamic_routes()

        assert adapter._dynamic_routes == {}
        assert "hook" not in adapter._routes
        assert (profile, "hook") not in adapter._rate_counts

    def test_route_name_churn_does_not_retain_rate_limit_buckets(self, tmp_path):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        adapter = _make_adapter()

        for index in range(24):
            route_name = f"rotated-{index}"
            subscriptions.write_text(
                json.dumps({
                    route_name: {
                        "secret": f"route-secret-{index}",
                        "provider": "generic",
                    }
                })
            )
            adapter._reload_dynamic_routes()
            bundle = adapter._authenticated_route_bundles[route_name]
            profile = str(bundle.authority[0])
            assert adapter._record_rate_limit_hit(
                route_name,
                float(index),
                profile=profile,
            )
            assert set(adapter._rate_counts) == {(profile, route_name)}

        assert len(adapter._rate_counts) == 1

    def test_deep_dynamic_json_is_a_deterministic_rejected_snapshot(self, tmp_path):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            '{"hook":{"secret":"deep-secret","provider":"generic","filters":'
            + '{"not":' * 1_200
            + '{"field":"x","equals":1}'
            + "}" * 1_200
            + "}}",
            encoding="utf-8",
        )
        adapter = _make_adapter()

        adapter._reload_dynamic_routes()

        assert adapter._dynamic_routes == {}
        assert "hook" not in adapter._routes
        assert adapter._dynamic_routes_rejected_file_identity is not None

    def test_same_key_policy_change_withdraws_old_route(self, tmp_path):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({
                "hook": {
                    "secret": "policy-secret",
                    "provider": "generic",
                    "enabled": True,
                }
            })
        )
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert "hook" in adapter._routes
        profile = str(adapter._authenticated_route_bundles["hook"].authority[0])
        assert adapter._record_rate_limit_hit("hook", 1.0, profile=profile)

        subscriptions.write_text(
            json.dumps({
                "hook": {
                    "secret": "policy-secret",
                    "provider": "generic",
                    "enabled": False,
                }
            })
        )
        adapter._reload_dynamic_routes()

        assert adapter._dynamic_routes == {}
        assert "hook" not in adapter._routes
        assert (profile, "hook") not in adapter._rate_counts

    def test_file_deletion_revokes_dynamic_route_even_if_static_bind_fails(
        self,
        tmp_path,
        monkeypatch,
    ):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({"hook": {"secret": "deleted-secret", "provider": "generic"}})
        )
        adapter = _make_adapter(
            routes={"static": {"secret": "static-secret", "provider": "generic"}}
        )
        adapter._reload_dynamic_routes()
        assert "hook" in adapter._routes
        profile = str(adapter._authenticated_route_bundles["hook"].authority[0])
        assert adapter._record_rate_limit_hit("hook", 1.0, profile=profile)
        assert adapter._record_rate_limit_hit("static", 1.0, profile=profile)

        subscriptions.unlink()

        def fail_static_bind(_routes):
            raise WebhookLedgerError("root authority store unavailable")

        monkeypatch.setattr(
            adapter,
            "_bind_route_authentication_authorities",
            fail_static_bind,
        )
        adapter._reload_dynamic_routes()

        assert adapter._dynamic_routes == {}
        assert set(adapter._routes) == {"static"}
        assert adapter._dynamic_routes_file_present is False
        assert (profile, "hook") not in adapter._rate_counts
        assert (profile, "static") in adapter._rate_counts

    def test_transient_bind_failure_withdraws_changed_dynamic_authority(
        self,
        tmp_path,
        monkeypatch,
    ):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({"hook": {"secret": "old-secret", "provider": "generic"}})
        )
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert adapter._routes["hook"]["secret"] == "old-secret"

        subscriptions.write_text(
            json.dumps({"hook": {"secret": "new-secret", "provider": "generic"}})
        )

        def fail_bind(_routes):
            raise OSError("temporary authority store failure")

        monkeypatch.setattr(
            adapter,
            "_bind_route_authentication_authorities",
            fail_bind,
        )
        adapter._reload_dynamic_routes()

        assert adapter._dynamic_routes == {}
        assert "hook" not in adapter._routes
        assert adapter._dynamic_routes_transient_file_identity is not None

    def test_stat_race_after_exists_withdraws_old_dynamic_authority(
        self,
        tmp_path,
        monkeypatch,
    ):
        subscriptions = tmp_path / _DYNAMIC_ROUTES_FILENAME
        subscriptions.write_text(
            json.dumps({"hook": {"secret": "old-secret", "provider": "generic"}})
        )
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert "hook" in adapter._routes

        real_stat = Path.stat
        real_exists = Path.exists

        def raced_exists(path):
            if path == subscriptions:
                return True
            return real_exists(path)

        def raced_stat(path, *args, **kwargs):
            if path == subscriptions:
                raise FileNotFoundError("removed after exists")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", raced_exists)
        monkeypatch.setattr(Path, "stat", raced_stat)
        adapter._reload_dynamic_routes()

        assert adapter._dynamic_routes == {}
        assert "hook" not in adapter._routes

    @pytest.mark.parametrize("name", [" route", "route ", "Route", "r" * 129])
    def test_dynamic_route_name_must_be_canonical(self, tmp_path, name):
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({
                name: {
                    "secret": f"secret-{len(name)}-{name[:1]}",
                    "provider": "generic",
                }
            })
        )
        adapter = _make_adapter()

        adapter._reload_dynamic_routes()

        assert name not in adapter._routes
