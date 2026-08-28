"""Durable per-webhook-operation toolset authority.

Route toolsets are validated once, before dispatch, and persisted in the
operation ledger. Agent execution consumes only that exact durable grant.
Missing, malformed, or unreadable authority is an explicit deny-all result;
mutable route or platform configuration is never consulted as a fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_auth import WebhookLocalBypassReceipt
from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookEnvelope,
    WebhookRouteConfig,
)
from gateway.platforms.webhook_ledger import (
    AdmitDisposition,
    WebhookOperationLedger,
)
from gateway.run import GatewayRunner
from hermes_cli.tools_config import _get_platform_tools


class _Src:
    def __init__(self, chat_id: str):
        self.chat_id = chat_id


class _BrokenLedger:
    def lookup_session(self, _session_key: str):
        raise RuntimeError("injected ledger failure")


def _make_adapter(ledger, *, routes=None, runner=None) -> WebhookAdapter:
    adapter = object.__new__(WebhookAdapter)
    adapter._operation_ledger = ledger
    adapter._routes = routes or {}
    adapter.gateway_runner = runner
    return adapter


def _make_runner(adapter) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = lambda source: adapter
    return runner


def _prepared_authority(
    tmp_path: Path,
    grant_snapshot: dict,
    *,
    trace_id: str = "trace-1",
):
    ledger = WebhookOperationLedger(tmp_path / f"{trace_id}.db")
    body = json.dumps(
        {"event": "push", "trace": trace_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    route = WebhookRouteConfig.bind(
        "mon",
        {"provider": "generic", "profile": "default"},
        headers={},
        request_profile="default",
    )
    receipt = WebhookLocalBypassReceipt._issue(route, body, {})
    envelope = WebhookEnvelope.from_receipt(
        receipt,
        raw_body=body,
        media_type="application/json",
        trace_id=trace_id,
    )
    admitted = ledger.admit(envelope)
    assert admitted.disposition is AdmitDisposition.ACCEPTED
    assert admitted.authority is not None
    prepared = ledger.prepare(
        admitted.authority,
        event_snapshot={"v": 1, "kind": "agent", "prompt": "inspect"},
        target_snapshot={"v": 1, "kind": "log", "profile": "default"},
        grant_snapshot=grant_snapshot,
    )
    return ledger, prepared


BASE_CONFIG = {"platform_toolsets": {"webhook": ["web", "vision", "clarify"]}}


class TestWebhookAdapterDurableGrant:
    def test_returns_exact_persisted_grant(self, tmp_path: Path):
        ledger, authority = _prepared_authority(
            tmp_path,
            {"v": 1, "toolsets": ["terminal", "file"]},
        )
        adapter = _make_adapter(ledger)
        source = _Src(authority.session_key)

        assert adapter.resolved_toolsets_for_source(source) == ["terminal", "file"]
        assert adapter.toolsets_for_source(source) == ["terminal", "file"]

    def test_mutable_route_cannot_replace_persisted_grant(self, tmp_path: Path):
        ledger, authority = _prepared_authority(
            tmp_path,
            {"v": 1, "toolsets": ["web"]},
        )
        routes = {"mon": {"toolsets": ["terminal"]}}
        adapter = _make_adapter(ledger, routes=routes)
        source = _Src(authority.session_key)

        assert adapter.resolved_toolsets_for_source(source) == ["web"]
        routes["mon"]["toolsets"] = ["file", "terminal"]
        assert adapter.resolved_toolsets_for_source(source) == ["web"]

    def test_missing_authority_is_deny_all_even_when_route_is_wider(
        self,
        tmp_path: Path,
    ):
        ledger = WebhookOperationLedger(tmp_path / "state.db")
        adapter = _make_adapter(
            ledger,
            routes={"mon": {"toolsets": ["terminal", "file"]}},
        )

        assert adapter.resolved_toolsets_for_source(_Src("webhook:missing")) == []
        assert adapter.toolsets_for_source(_Src("webhook:missing")) == []

    @pytest.mark.parametrize(
        "grant_snapshot",
        [
            pytest.param({}, id="missing-version"),
            pytest.param({"v": 2, "toolsets": ["terminal"]}, id="wrong-version"),
            pytest.param({"v": 1}, id="missing-list"),
            pytest.param({"v": 1, "toolsets": "terminal"}, id="string-list"),
            pytest.param({"v": 1, "toolsets": ["terminal", ""]}, id="blank"),
            pytest.param({"v": 1, "toolsets": ["terminal", 7]}, id="non-string"),
        ],
    )
    def test_malformed_persisted_grant_is_deny_all(
        self,
        tmp_path: Path,
        grant_snapshot: dict,
    ):
        ledger, authority = _prepared_authority(
            tmp_path,
            grant_snapshot,
            trace_id=f"malformed-{abs(hash(repr(grant_snapshot)))}",
        )
        adapter = _make_adapter(ledger)

        assert adapter.resolved_toolsets_for_source(_Src(authority.session_key)) == []

    def test_ledger_failure_and_blank_source_are_deny_all(self):
        adapter = _make_adapter(_BrokenLedger())

        assert adapter.resolved_toolsets_for_source(_Src("webhook:mon:d")) == []
        assert adapter.resolved_toolsets_for_source(_Src("")) == []

    def test_base_adapter_has_no_exact_or_legacy_override(self):
        adapter = object.__new__(WebhookAdapter)
        source = _Src("webhook:mon:d")

        assert BasePlatformAdapter.resolved_toolsets_for_source(adapter, source) is None
        assert BasePlatformAdapter.toolsets_for_source(adapter, source) is None


class TestAdmissionTimeResolution:
    def test_declared_route_grant_is_normalized_and_validated_once(
        self,
        monkeypatch,
    ):
        runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False))
        adapter = _make_adapter(None, runner=runner)
        original_config = {
            "platform_toolsets": {"webhook": ["vision"]},
            "unrelated": {"preserved": True},
        }
        seen = []

        monkeypatch.setattr(
            "gateway.run._load_gateway_config",
            lambda: original_config,
        )

        def resolve(config, platform_key):
            seen.append((config, platform_key))
            return {"terminal", "web"}

        monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", resolve)

        resolved = adapter._resolve_admitted_toolsets(
            {"toolsets": [" terminal ", "web", "terminal"]},
            _Src("webhook:default:mon:generic:trace"),
        )

        assert resolved == ["terminal", "web"]
        assert len(seen) == 1
        resolved_config, platform_key = seen[0]
        assert platform_key == "webhook"
        assert resolved_config["platform_toolsets"]["webhook"] == [
            "terminal",
            "web",
        ]
        assert original_config == {
            "platform_toolsets": {"webhook": ["vision"]},
            "unrelated": {"preserved": True},
        }

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("terminal", id="not-list"),
            pytest.param(["terminal", ""], id="blank"),
            pytest.param(["terminal", 7], id="non-string"),
        ],
    )
    def test_invalid_declared_route_grant_fails_before_dispatch(self, raw):
        runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False))
        adapter = _make_adapter(None, runner=runner)

        with pytest.raises(WebhookContractError, match="toolset"):
            adapter._resolve_admitted_toolsets(
                {"toolsets": raw},
                _Src("webhook:default:mon:generic:trace"),
            )

    def test_empty_declared_grant_is_explicit_deny_all(self, monkeypatch):
        runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False))
        adapter = _make_adapter(None, runner=runner)

        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("empty grant must not broaden through live config")
            ),
        )

        assert (
            adapter._resolve_admitted_toolsets(
                {"toolsets": []},
                _Src("webhook:default:mon:generic:trace"),
            )
            == []
        )

    def test_missing_gateway_runner_is_deny_all(self):
        adapter = _make_adapter(None, runner=None)

        assert (
            adapter._resolve_admitted_toolsets(
                {"toolsets": ["terminal"]},
                _Src("webhook:default:mon:generic:trace"),
            )
            == []
        )


class TestGatewayConsumesExactGrant:
    def test_exact_grant_bypasses_mutable_resolution(self, tmp_path, monkeypatch):
        ledger, authority = _prepared_authority(
            tmp_path,
            {"v": 1, "toolsets": ["web", "terminal", "web"]},
        )
        adapter = _make_adapter(
            ledger,
            routes={"mon": {"toolsets": ["file"]}},
        )
        runner = _make_runner(adapter)
        config = {"platform_toolsets": {"webhook": ["vision"]}}

        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("durable grant must not be re-resolved")
            ),
        )

        resolved, has_override, failed = (
            GatewayRunner._resolve_toolset_authority_for_source(
                runner,
                config,
                _Src(authority.session_key),
                "webhook",
            )
        )

        assert resolved == ["web", "terminal"]
        assert has_override is True
        assert failed is False
        assert config == {"platform_toolsets": {"webhook": ["vision"]}}

    def test_exact_empty_grant_is_deny_all(self, tmp_path, monkeypatch):
        ledger, authority = _prepared_authority(
            tmp_path,
            {"v": 1, "toolsets": []},
        )
        adapter = _make_adapter(ledger)
        runner = _make_runner(adapter)

        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("deny-all must not be re-resolved")
            ),
        )

        resolved, has_override, failed = (
            GatewayRunner._resolve_toolset_authority_for_source(
                runner,
                BASE_CONFIG,
                _Src(authority.session_key),
                "webhook",
            )
        )

        assert resolved == []
        assert has_override is True
        assert failed is False

    def test_missing_durable_authority_does_not_fall_back_to_platform_config(
        self,
        tmp_path,
        monkeypatch,
    ):
        adapter = _make_adapter(
            WebhookOperationLedger(tmp_path / "state.db"),
            routes={"mon": {"toolsets": ["terminal"]}},
        )
        runner = _make_runner(adapter)

        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("missing authority must be deny-all")
            ),
        )

        resolved, has_override, failed = (
            GatewayRunner._resolve_toolset_authority_for_source(
                runner,
                BASE_CONFIG,
                _Src("webhook:missing"),
                "webhook",
            )
        )

        assert resolved == []
        assert has_override is True
        assert failed is False

    def test_unreadable_durable_authority_does_not_fall_back(self, monkeypatch):
        runner = _make_runner(_make_adapter(_BrokenLedger()))

        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("ledger failure must be deny-all")
            ),
        )

        resolved, has_override, failed = (
            GatewayRunner._resolve_toolset_authority_for_source(
                runner,
                BASE_CONFIG,
                _Src("webhook:mon:d"),
                "webhook",
            )
        )

        assert resolved == []
        assert has_override is True
        assert failed is False

    def test_missing_webhook_adapter_fails_closed(self):
        runner = _make_runner(None)

        resolved, has_override, failed = (
            GatewayRunner._resolve_toolset_authority_for_source(
                runner,
                BASE_CONFIG,
                _Src("webhook:mon:d"),
                "webhook",
            )
        )

        assert resolved == []
        assert has_override is True
        assert failed is True
