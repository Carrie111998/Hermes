"""Regression coverage for deferred Feishu SDK loading."""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, patch


def _feishu_adapter_module():
    """Import the adapter with the Windows app-data root available in CI."""
    with patch.dict(os.environ, {"LOCALAPPDATA": tempfile.gettempdir()}):
        from plugins.platforms.feishu import adapter

    return adapter


def test_configured_feishu_dependency_check_does_not_load_sdk():
    """Gateway configuration can validate Feishu without importing its SDK."""
    feishu_adapter = _feishu_adapter_module()

    with (
        patch.object(feishu_adapter, "FEISHU_AVAILABLE", False),
        patch("tools.lazy_deps.ensure", autospec=True) as ensure,
    ):
        assert feishu_adapter.check_feishu_requirements() is True
        assert feishu_adapter.FEISHU_AVAILABLE is False

    ensure.assert_called_once_with("platform.feishu", prompt=False)


def test_feishu_requirements_rebinds_webhook_modules_after_lazy_install():
    """Both Feishu transports must rebind after a lazy install."""
    feishu_adapter = _feishu_adapter_module()

    with (
        patch.object(feishu_adapter, "FEISHU_AVAILABLE", False),
        patch.object(feishu_adapter, "aiohttp", None),
        patch.object(feishu_adapter, "web", None),
        patch.object(feishu_adapter, "websockets", None),
        patch.object(feishu_adapter, "FEISHU_WEBHOOK_AVAILABLE", False),
        patch.object(feishu_adapter, "FEISHU_WEBSOCKET_AVAILABLE", False),
        patch("tools.lazy_deps.ensure", autospec=True) as ensure,
    ):
        assert feishu_adapter.check_feishu_requirements() is True
        rebound = (
            feishu_adapter.aiohttp,
            feishu_adapter.web,
            feishu_adapter.websockets,
            feishu_adapter.FEISHU_WEBHOOK_AVAILABLE,
            feishu_adapter.FEISHU_WEBSOCKET_AVAILABLE,
        )

    ensure.assert_called_once_with("platform.feishu", prompt=False)
    assert rebound[0] is not None
    assert rebound[1] is not None
    assert rebound[2] is not None
    assert rebound[3] is True
    assert rebound[4] is True


def test_feishu_passive_probe_does_not_trust_loaded_sdk_with_stale_metadata():
    feishu_adapter = _feishu_adapter_module()

    with (
        patch.object(feishu_adapter, "FEISHU_AVAILABLE", True),
        patch.object(feishu_adapter, "FEISHU_WEBHOOK_AVAILABLE", True),
        patch("tools.lazy_deps.is_available", return_value=False) as available,
    ):
        assert feishu_adapter.feishu_deps_present() is False

    available.assert_called_once_with("platform.feishu")


def test_feishu_passive_probe_requires_websocket_runtime():
    feishu_adapter = _feishu_adapter_module()

    with (
        patch.object(feishu_adapter, "FEISHU_AVAILABLE", True),
        patch.object(feishu_adapter, "FEISHU_WEBHOOK_AVAILABLE", True),
        patch.object(feishu_adapter, "FEISHU_WEBSOCKET_AVAILABLE", False),
        patch("tools.lazy_deps.is_available", return_value=True),
    ):
        assert feishu_adapter.feishu_deps_present() is False


def test_feishu_requirement_failure_preserves_loaded_transport_state():
    """A failed active repair must not break already-running adapters."""
    feishu_adapter = _feishu_adapter_module()

    with (
        patch.object(feishu_adapter, "FEISHU_AVAILABLE", True),
        patch.object(feishu_adapter, "aiohttp", object()),
        patch.object(feishu_adapter, "web", object()),
        patch.object(feishu_adapter, "websockets", object()),
        patch.object(feishu_adapter, "FEISHU_WEBHOOK_AVAILABLE", True),
        patch.object(feishu_adapter, "FEISHU_WEBSOCKET_AVAILABLE", True),
        patch.object(feishu_adapter, "_FEISHU_ACTIVE_CHECK_FAILED", False),
        patch("tools.lazy_deps.ensure", side_effect=RuntimeError("resolver failed")),
    ):
        assert feishu_adapter.check_feishu_requirements() is False
        assert feishu_adapter.FEISHU_AVAILABLE is True
        assert feishu_adapter.aiohttp is not None
        assert feishu_adapter.web is not None
        assert feishu_adapter.websockets is not None
        assert feishu_adapter.FEISHU_WEBHOOK_AVAILABLE is True
        assert feishu_adapter.FEISHU_WEBSOCKET_AVAILABLE is True
        assert feishu_adapter._FEISHU_ACTIVE_CHECK_FAILED is True
        assert feishu_adapter.feishu_deps_present() is False


def test_feishu_missing_lazy_helper_preserves_loaded_transport_state(monkeypatch):
    """A missing helper must not tear down already-running adapters."""
    import sys

    feishu_adapter = _feishu_adapter_module()
    with (
        patch.object(feishu_adapter, "FEISHU_AVAILABLE", True),
        patch.object(feishu_adapter, "aiohttp", object()),
        patch.object(feishu_adapter, "web", object()),
        patch.object(feishu_adapter, "websockets", object()),
        patch.object(feishu_adapter, "FEISHU_WEBHOOK_AVAILABLE", True),
        patch.object(feishu_adapter, "FEISHU_WEBSOCKET_AVAILABLE", True),
        patch.object(feishu_adapter, "_FEISHU_ACTIVE_CHECK_FAILED", False),
        patch.dict(sys.modules, {"tools.lazy_deps": None}),
    ):
        assert feishu_adapter.check_feishu_requirements() is False
        assert feishu_adapter.FEISHU_AVAILABLE is True
        assert feishu_adapter.aiohttp is not None
        assert feishu_adapter.web is not None
        assert feishu_adapter.websockets is not None
        assert feishu_adapter.FEISHU_WEBHOOK_AVAILABLE is True
        assert feishu_adapter.FEISHU_WEBSOCKET_AVAILABLE is True
        assert feishu_adapter._FEISHU_ACTIVE_CHECK_FAILED is True
        assert feishu_adapter.feishu_deps_present() is False


def test_feishu_connect_loads_sdk_on_worker_thread():
    """The first SDK import is deferred until a configured adapter connects."""
    from gateway.config import PlatformConfig
    feishu_adapter = _feishu_adapter_module()

    adapter = feishu_adapter.FeishuAdapter(
        PlatformConfig(
            extra={
                "app_id": "cli_test",
                "app_secret": "secret_test",
                "connection_mode": "websocket",
            }
        )
    )

    with (
        patch.object(feishu_adapter, "FEISHU_AVAILABLE", False),
        patch.object(feishu_adapter, "_load_lark_oapi", return_value=True) as load_sdk,
        patch.object(feishu_adapter.asyncio, "to_thread", new_callable=AsyncMock, return_value=True) as to_thread,
        patch.object(adapter, "_connect_with_retry", new_callable=AsyncMock),
        patch.object(feishu_adapter, "acquire_scoped_lock", return_value=(True, {})),
    ):
        assert asyncio.run(adapter.connect()) is True

    to_thread.assert_awaited_once_with(load_sdk)
