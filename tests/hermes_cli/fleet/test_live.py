from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.fleet.adapters.live_routes import (
    AntigravityAdapter,
    ClaudeCodeAdapter,
)
from hermes_cli.fleet.adapters.native_provider import NativeProviderAdapter
from hermes_cli.fleet.live import FleetQualificationDoctor
from hermes_cli.fleet.profiles import profile_map
from hermes_cli.fleet.types import TaskSpec
from hermes_cli.subcommands.fleet import _default_service


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def test_live_doctor_qualifies_exact_subscription_routes_from_receipts():
    profiles = profile_map()
    commands = []

    def run(argv):
        commands.append(tuple(argv))
        if argv[0] == "claude" and tuple(argv[1:3]) == ("auth", "status"):
            return 0, (
                '{"loggedIn":true,"authMethod":"claude.ai",'
                '"apiProvider":"firstParty","email":"never-record@example.com",'
                '"org":"never-record"}'
            ), ""
        if argv[0] == "claude":
            return 0, "2.1.217 (Claude Code)", ""
        if argv[0] == "agy" and argv[1] == "--version":
            return 0, "agy 1.2.3", ""
        if argv[0] == "agy" and argv[1] == "models":
            return 0, "gemini-3.1-pro-high\nGemini 3.6 Flash", ""
        raise AssertionError(argv)

    doctor = FleetQualificationDoctor(
        auth_status=lambda provider: {
            "logged_in": True,
            "auth_mode": "chatgpt" if provider == "openai-codex" else "oauth_device_code",
            "source": "pool:test",
        },
        which=lambda name: f"C:/tools/{name}.exe",
        command=run,
        environment={},
        now=lambda: NOW,
    )

    qualifications = doctor.qualify(profiles.values())

    assert qualifications["chatgpt_codex"].qualified
    assert qualifications["chatgpt_codex"].models == ("gpt-5.6-sol",)
    assert qualifications["chatgpt_codex"].efforts[-2:] == ("max", "ultra")
    assert qualifications["chatgpt_codex"].overage_disabled is True
    assert qualifications["grok"].models == ("grok-4.5",)
    assert qualifications["grok"].efforts[-2:] == ("max", "ultra")
    assert qualifications["claude_code"].models == ("claude-opus-4-8",)
    assert qualifications["claude_code"].efforts == (
        "low",
        "medium",
        "high",
        "max",
    )
    assert qualifications["claude_code"].fast_off_supported
    assert "never-record" not in qualifications["claude_code"].detail
    assert qualifications["antigravity"].models == ("gemini-3.1-pro-high",)
    assert qualifications["antigravity"].efforts == ("low", "medium", "high")
    assert qualifications["antigravity"].qualified
    assert ("claude", "auth", "status", "--json") in commands
    assert ("agy", "models") in commands
    assert not any(command[0] == "agy" and "auth" in command for command in commands)
    assert "provider-reported served-model" in qualifications["antigravity"].detail


def test_live_doctor_requires_exact_agy_model_list_qualification():
    def run(argv):
        if argv[1] == "--version":
            return 0, "agy 1.2.3", ""
        if argv[1] == "models":
            return 0, "gemini-3.6-flash", ""
        raise AssertionError(argv)

    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=run,
        environment={},
        now=lambda: NOW,
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))[
        "antigravity"
    ]

    assert not qualification.qualified
    assert qualification.detail == "required exact model absent from agy models"


def test_live_doctor_rejects_forbidden_api_key_without_exposing_value():
    doctor = FleetQualificationDoctor(
        auth_status=lambda _: {"logged_in": True, "auth_mode": "chatgpt", "source": "pool:test"},
        which=lambda name: f"C:/tools/{name}.exe",
        command=lambda argv: (0, "ok", ""),
        environment={"OPENAI_API_KEY": "do-not-expose-this"},
        now=lambda: NOW,
    )

    qualification = doctor.qualify((profile_map()["chatgpt_codex"],))["chatgpt_codex"]

    assert not qualification.qualified
    assert "OPENAI_API_KEY" in qualification.detail
    assert "do-not-expose-this" not in qualification.detail


@pytest.mark.parametrize(
    "lane_id",
    ["chatgpt_codex", "claude_code", "grok", "antigravity"],
)
def test_default_service_qualifies_and_executes_each_live_lane(
    tmp_path, lane_id
):
    bridge = tmp_path / "usage-weekly.json"
    labels = {
        "chatgpt_codex": "ChatGPT Pro · Codex",
        "claude_code": "Claude Max 20x",
        "grok": "SuperGrok",
        "antigravity": "Google AI · Antigravity",
    }
    bridge.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-24T00:00:00Z",
                "source": "controlled-test",
                "plans": [
                    {
                        "label": labels[lane_id],
                        "agents": [],
                        "weekly_pct_used": 10,
                        "resets": "weekly",
                        "checked_at": "2026-07-24T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def command(argv):
        if argv[0] == "claude" and argv[1] == "--version":
            return 0, "2.1.217 (Claude Code)", ""
        if argv[0] == "claude":
            return 0, '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}', ""
        if argv[0] == "agy" and argv[1] == "--version":
            return 0, "agy 1.2.3", ""
        if argv[0] == "agy" and argv[1] == "models":
            return 0, "gemini-3.1-pro-high", ""
        raise AssertionError(argv)

    doctor = FleetQualificationDoctor(
        auth_status=lambda provider: {
            "logged_in": True,
            "auth_mode": "chatgpt" if provider == "openai-codex" else "oauth_device_code",
            "source": "pool:test",
        },
        which=lambda _: sys.executable,
        command=command,
        environment={},
        now=lambda: NOW,
    )

    def native(**kwargs):
        return {
            "ok": True,
            "provider_id": kwargs["provider_id"],
            "model_id": kwargs["model"],
            "auth_kind": "oauth_subscription",
            "output": "native complete",
        }

    process_calls = []

    def process(argv, **kwargs):
        process_calls.append(argv)
        if "--output-format" in argv:
            model = argv[argv.index("--model") + 1]
            stdout = json.dumps(
                {"result": "claude complete", "modelUsage": {model: {}}}
            )
        else:
            stdout = "antigravity complete"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    adapters = {
        "chatgpt_codex": NativeProviderAdapter(native),
        "claude_code": ClaudeCodeAdapter(sys.executable, run_process=process),
        "grok": NativeProviderAdapter(native),
        "antigravity": AntigravityAdapter(sys.executable, run_process=process),
    }
    config = {
        "fleet": {
            "enabled": True,
            "bridge_usage_file": str(bridge),
            "lanes": {
                lane: {"enabled": lane == lane_id}
                for lane in profile_map()
            },
        }
    }
    service = _default_service(
        config_data=config,
        doctor=doctor,
        adapters=adapters,
        store_path=tmp_path / "state.db",
        now=lambda: NOW,
    )
    result = service.run(
        TaskSpec(
            task_id=f"task-{lane_id}",
            cwd=tmp_path,
            required_capabilities=frozenset({"workspace_write", "shell"}),
            reservation_pct=Decimal("5"),
        ),
        prompt="bounded test task",
    )

    assert result.ok
    assert result.pin is not None
    assert result.pin.lane_id == lane_id
    if lane_id == "antigravity":
        assert process_calls == [
            [
                str(Path(sys.executable).resolve()),
                "-p",
                "--model",
                "gemini-3.1-pro-high",
                "--effort",
                "medium",
                "--print-timeout",
                "1800s",
            ]
        ]
        route_proof = result.adapter_result.metadata["route_proof"]
        assert route_proof["requested_model_id"] == "gemini-3.1-pro-high"
        assert route_proof["model_qualification"] == "agy models"
        assert route_proof["served_model_id"] is None
