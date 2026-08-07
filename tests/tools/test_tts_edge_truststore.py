"""Windows truststore behavior for the Edge TTS lazy import path."""

import builtins
import sys
from types import ModuleType
from unittest.mock import patch

from tools import tts_tool


def _module(name: str) -> ModuleType:
    return ModuleType(name)


def test_windows_installs_and_injects_truststore_before_edge_tts_import():
    events: list[str] = []
    edge_tts = _module("edge_tts")
    truststore = _module("truststore")
    truststore.inject_into_ssl = lambda: events.append("inject")
    real_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        if name in {"truststore", "edge_tts"}:
            events.append(f"import:{name}")
        return real_import(name, *args, **kwargs)

    def record_ensure(feature: str, *, prompt: bool):
        events.append(f"ensure:{feature}")

    with (
        patch.object(tts_tool, "_is_windows", return_value=True),
        patch("tools.lazy_deps.ensure", side_effect=record_ensure),
        patch.dict(sys.modules, {"truststore": truststore, "edge_tts": edge_tts}),
        patch("builtins.__import__", side_effect=tracking_import),
    ):
        result = tts_tool._import_edge_tts()

    assert result is edge_tts
    assert events == [
        "ensure:tts.edge.windows",
        "import:truststore",
        "inject",
        "import:edge_tts",
    ]


def test_non_windows_keeps_edge_only_lazy_feature():
    edge_tts = _module("edge_tts")

    with (
        patch.object(tts_tool, "_is_windows", return_value=False),
        patch("tools.lazy_deps.ensure") as ensure,
        patch.dict(sys.modules, {"edge_tts": edge_tts}),
    ):
        result = tts_tool._import_edge_tts()

    assert result is edge_tts
    ensure.assert_called_once_with("tts.edge", prompt=False)


def test_missing_truststore_falls_back_to_edge_tts():
    edge_tts = _module("edge_tts")

    with (
        patch.object(tts_tool, "_is_windows", return_value=True),
        patch("tools.lazy_deps.ensure"),
        patch.object(tts_tool.logger, "debug") as debug,
        patch.dict(sys.modules, {"truststore": None, "edge_tts": edge_tts}),
    ):
        result = tts_tool._import_edge_tts()

    assert result is edge_tts
    debug.assert_called_once_with(
        "truststore is unavailable; Edge TTS will use certifi"
    )


def test_failing_truststore_injection_warns_and_falls_back(caplog):
    edge_tts = _module("edge_tts")
    truststore = _module("truststore")

    def fail_injection():
        raise RuntimeError("injection failed")

    truststore.inject_into_ssl = fail_injection

    with (
        patch.object(tts_tool, "_is_windows", return_value=True),
        patch("tools.lazy_deps.ensure"),
        patch.dict(sys.modules, {"truststore": truststore, "edge_tts": edge_tts}),
    ):
        result = tts_tool._import_edge_tts()

    assert result is edge_tts
    assert "Could not inject Windows certificate store" in caplog.text
    assert "injection failed" in caplog.text
