from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    _SAFE_OUTBOUND_POLICY_NOTICE,
    _outbound_gate_required_for_target,
    bind_outbound_receipt_context,
)
from hermes_cli import plugins as plugins_mod


class GateAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.sent = []
        self.edited = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id="m1")

    async def edit_message(
        self, chat_id, message_id, content, *, finalize=False, metadata=None
    ):
        self.edited.append((chat_id, message_id, content, finalize))
        return SendResult(success=True, message_id=message_id)


class AllEgressAdapter(GateAdapter):
    def __init__(self):
        super().__init__()
        self.egress = []
        self._platform_by_chat = {"paul": "slack"}

    async def send_for_platform(self, logical_platform, chat_id, content, metadata=None):
        self.egress.append(("relay", str(logical_platform), chat_id, content))
        return SendResult(success=True)

    async def send_draft(self, chat_id, draft_id, content, metadata=None):
        self.egress.append(("draft", chat_id, draft_id, content))
        return SendResult(success=True)

    async def send_native_task_card_progress(
        self, chat_id, tasks, *, title="Working", metadata=None, fallback_text=None
    ):
        self.egress.append(("card", chat_id, tasks, title, fallback_text))
        return SendResult(success=True)

    async def _post_interactive(self, chat_id, interactive_body, reply_to=None, metadata=None):
        self.egress.append(("interactive", chat_id, interactive_body))
        return SendResult(success=True)

    async def _send_media(
        self, chat_id, *, source, caption=None, metadata=None, **kwargs
    ):
        self.egress.append(("media", chat_id, source, caption))
        return SendResult(success=True)


@pytest.mark.asyncio
async def test_common_adapter_send_boundary_applies_rewrite(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **kwargs: [
            {"action": "rewrite", "content": "UNVERIFIED\n\noriginal", "reason": "test"}
        ] if hook_name == "pre_gateway_send" else [],
    )
    adapter = GateAdapter()
    result = await adapter.send(
        "paul",
        "original",
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "t1"},
    )
    assert result.success is True
    assert adapter.sent == [
        ("paul", "UNVERIFIED\n\noriginal", {"_hermes_session_id": "s1", "_hermes_turn_id": "t1"})
    ]


@pytest.mark.asyncio
async def test_common_adapter_send_preserves_positional_metadata_api(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **kwargs: (
            [{"action": "allow"}] if hook_name == "pre_gateway_send" else []
        ),
    )
    adapter = GateAdapter()
    metadata = {"_hermes_session_id": "s1", "_hermes_turn_id": "t1"}

    result = await adapter.send("paul", "original", "reply-1", metadata)

    assert result.success is True
    assert adapter.sent == [("paul", "original", metadata)]


@pytest.mark.asyncio
async def test_common_adapter_edit_boundary_applies_rewrite(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **kwargs: [
            {"action": "rewrite", "content": "UNVERIFIED edited", "reason": "test"}
        ] if hook_name == "pre_gateway_send" else [],
    )
    adapter = GateAdapter()
    result = await adapter.edit_message("paul", "m1", "fixed", finalize=True)
    assert result.success is True
    assert adapter.edited == [("paul", "m1", "UNVERIFIED edited", True)]


@pytest.mark.asyncio
async def test_final_policy_covers_relay_draft_card_interactive_and_media_egress(monkeypatch):
    captured = []

    def invoke(hook_name, **kwargs):
        captured.append((hook_name, kwargs))
        if hook_name == "final_gateway_send_policy":
            return [{"policy_id": "outbound-message-gate", "action": "rewrite", "content": "SAFE"}]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    adapter = AllEgressAdapter()

    await adapter.send_for_platform("SLACK", "paul", "fixed")
    await adapter.send_draft("paul", 7, "fixed")
    await adapter.send_native_task_card_progress(
        "paul", [{"title": "fixed"}], fallback_text="fixed"
    )
    await adapter._post_interactive("paul", {"body": {"text": "fixed"}})
    await adapter._send_media(
        "paul", source="https://dead.example/image.png", caption="fixed"
    )

    policy_calls = [payload for name, payload in captured if name == "final_gateway_send_policy"]
    assert [call["operation"] for call in policy_calls] == [
        "send_for_platform", "send_draft", "send_native_task_card_progress",
        "_post_interactive", "_send_media",
    ]
    assert policy_calls[0]["platform"] == "slack"
    assert policy_calls[0]["chat_id"] == "paul"
    assert all("fixed" in call["content"] or "dead.example" in call["content"] for call in policy_calls)
    assert all("fixed" not in repr(item) and "dead.example" not in repr(item) for item in adapter.egress)


@pytest.mark.asyncio
async def test_only_required_gate_can_decide_after_ordinary_transformations(monkeypatch):
    seen_by_gate = []

    def invoke(hook_name, **kwargs):
        if hook_name == "pre_gateway_send":
            return [{"action": "rewrite", "content": kwargs["content"] + " fixed"}]
        if hook_name == "final_gateway_send_policy":
            seen_by_gate.append(kwargs["content"])
            return [
                {"policy_id": "outbound-message-gate", "action": "rewrite", "content": "UNVERIFIED"},
                {"policy_id": "unrelated", "action": "rewrite", "content": "fixed again"},
            ]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    adapter = GateAdapter()
    await adapter.send("paul", "original")

    assert seen_by_gate == ["original fixed"]
    assert adapter.sent == [("paul", _SAFE_OUTBOUND_POLICY_NOTICE, None)]


@pytest.mark.asyncio
async def test_streaming_edit_boundary_carries_same_turn_metadata(monkeypatch):
    captured = {}

    def invoke(hook_name, **kwargs):
        if hook_name == "pre_gateway_send":
            captured.update(kwargs)
        return [{"action": "allow"}] if hook_name == "pre_gateway_send" else []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    adapter = GateAdapter()
    metadata = {
        "_hermes_session_id": "session-1",
        "_hermes_turn_id": "turn-1",
        "_interim_send": True,
    }
    result = await adapter.edit_message(
        "paul", "m1", "interim", finalize=False, metadata=metadata
    )

    assert result.success is True
    assert captured["operation"] == "edit_message"
    assert captured["metadata"] == metadata


@pytest.mark.asyncio
async def test_common_adapter_boundary_fails_closed_when_hook_invocation_raises(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    adapter = GateAdapter()

    result = await adapter.send("paul", "fixed")

    assert result.success is True
    assert adapter.sent == [("paul", _SAFE_OUTBOUND_POLICY_NOTICE, None)]


@pytest.mark.asyncio
async def test_common_adapter_boundary_blocks_when_required_gate_is_missing(monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.base._outbound_gate_required_for_target",
        lambda _platform, _chat_id: True,
    )
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: False)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    adapter = GateAdapter()

    result = await adapter.send("paul", "ordinary message")

    assert result.success is True
    assert adapter.sent == [("paul", _SAFE_OUTBOUND_POLICY_NOTICE, None)]


@pytest.mark.asyncio
async def test_common_adapter_boundary_blocks_on_plugin_failure(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **kwargs: [
            {"action": "block", "reason": "gate unavailable"}
        ] if hook_name == "pre_gateway_send" else [],
    )
    adapter = GateAdapter()
    result = await adapter.send("paul", "fixed")
    assert result.success is True
    assert adapter.sent == [("paul", _SAFE_OUTBOUND_POLICY_NOTICE, None)]


def test_pre_gateway_send_is_a_fail_closed_plugin_hook():
    assert "pre_gateway_send" in plugins_mod.VALID_HOOKS
    assert "final_gateway_send_policy" in plugins_mod.VALID_HOOKS
    assert "final_gateway_send_policy" in plugins_mod._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS


def test_canonical_policy_config_reads_legacy_alias_and_preserves_recipient_case(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "plugins": {
                "entries": {
                    "outbound_message_gate": {
                        "settings": {"protected_targets": ["slack:C123"]}
                    }
                }
            }
        },
    )
    assert _outbound_gate_required_for_target("SLACK", "C123") is True
    assert _outbound_gate_required_for_target("SLACK", "c123") is False


def test_unreadable_required_policy_config_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: (_ for _ in ()).throw(ValueError("malformed")),
    )
    with pytest.raises(RuntimeError, match="outbound policy config unavailable"):
        _outbound_gate_required_for_target("slack", "C123")


def test_protected_targets_make_bundled_gate_mandatory_without_plugins_enabled():
    config = {
        "plugins": {
            "entries": {
                "outbound-message-gate": {
                    "settings": {"protected_targets": ["telegram:paul"]}
                }
            }
        }
    }
    assert plugins_mod._outbound_gate_required_at_startup(config) is True


def test_plugin_discovery_atomically_loads_and_validates_required_gate(monkeypatch):
    manager = plugins_mod.PluginManager()
    manifest = plugins_mod.PluginManifest(
        name="outbound-message-gate", source="bundled", key="outbound-message-gate"
    )
    monkeypatch.setattr(manager, "_collect_directory_manifests", lambda: [manifest])
    monkeypatch.setattr(manager, "_scan_entry_points", lambda: [])
    monkeypatch.setattr(plugins_mod, "_get_disabled_plugins", lambda: set())
    monkeypatch.setattr(plugins_mod, "_get_enabled_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "plugins": {
                "entries": {
                    "outbound-message-gate": {
                        "settings": {"protected_targets": ["telegram:paul"]}
                    }
                }
            }
        },
    )
    monkeypatch.setattr(manager, "_warn_python_dependencies", lambda _manifest: None)
    monkeypatch.setattr(manager, "_validate_plugin_config_schema", lambda _manifest: None)
    loaded = []

    def load(required_manifest):
        loaded.append(required_manifest.name)
        callback = lambda **_kwargs: {"action": "allow"}
        setattr(callback, "_hermes_policy_id", "outbound-message-gate")
        manager._hooks.setdefault("final_gateway_send_policy", []).append(callback)

    monkeypatch.setattr(manager, "_load_plugin", load)
    manager._discover_and_load_inner()

    assert loaded == ["outbound-message-gate"]


def test_live_safe_preflight_harness_never_has_a_delivery_transport():
    from scripts.outbound_gate_preflight import run_preflight

    report = run_preflight()
    assert report["transport"] == "in-memory-only"
    assert report["dead_link"]["action"] == "rewrite"
    assert "dead.example" not in report["dead_link"]["content"]
    assert report["naked_fixed_claim"]["content"].startswith("UNVERIFIED")
    assert report["pass"] is True


def test_live_safe_preflight_script_runs_from_its_documented_direct_path():
    repo = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(repo / "scripts" / "outbound_gate_preflight.py")],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["transport"] == "in-memory-only"
    assert report["pass"] is True


def test_receipt_context_is_bound_to_every_turn_metadata_mapping():
    status = {}
    progress = {"thread_id": "topic-1"}
    bind_outbound_receipt_context(
        status,
        progress,
        session_id="session-1",
        turn_id="turn-1",
    )
    assert status == {
        "_hermes_session_id": "session-1",
        "_hermes_turn_id": "turn-1",
    }
    assert progress == {
        "thread_id": "topic-1",
        "_hermes_session_id": "session-1",
        "_hermes_turn_id": "turn-1",
    }
