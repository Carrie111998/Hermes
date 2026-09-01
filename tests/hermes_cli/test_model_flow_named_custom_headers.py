"""Regression coverage for the model-probe half of #88463.

The focused present/absent cases originate from liuhao1024's #88474.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: m\n", encoding="utf-8"
    )


def _provider_info(**extra):
    info = {
        "name": "bifrost",
        "base_url": "http://bifrost.local/v1",
        "api_key": "k",
        "model": "",
    }
    info.update(extra)
    return info


def test_named_custom_probe_carries_extra_headers():
    captured = {}

    def _fake_fetch(api_key, base_url, **kwargs):
        captured.update(kwargs)
        return ["m1", "m2"]

    with (
        patch("hermes_cli.models.fetch_api_models", _fake_fetch),
        patch("hermes_cli.curses_ui.curses_radiolist", return_value=-1),
    ):
        from hermes_cli.model_setup_flows import _model_flow_named_custom

        _model_flow_named_custom(
            None,
            _provider_info(
                extra_headers={"x-bf-vk": "vk-1", "x-tenant": "acme"}
            ),
        )

    assert captured.get("headers") == {
        "x-bf-vk": "vk-1",
        "x-tenant": "acme",
    }


def test_named_custom_probe_without_extra_headers_omits_headers_kwarg():
    captured = {}

    def _fake_fetch(api_key, base_url, **kwargs):
        captured.update(kwargs)
        return ["m1"]

    with (
        patch("hermes_cli.models.fetch_api_models", _fake_fetch),
        patch("hermes_cli.curses_ui.curses_radiolist", return_value=-1),
    ):
        from hermes_cli.model_setup_flows import _model_flow_named_custom

        _model_flow_named_custom(None, _provider_info())

    assert "headers" not in captured
