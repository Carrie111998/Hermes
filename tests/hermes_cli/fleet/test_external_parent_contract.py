import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from hermes_cli.fleet import inspection
from hermes_cli.fleet.inspection import build_fleet_service, serialize_evaluation
from hermes_cli.fleet.live import FleetQualificationDoctor
from hermes_cli.fleet.policy import evaluate_lane
from hermes_cli.fleet.profiles import profile_map
from hermes_cli.fleet.types import (
    AdapterKind,
    CapacityRead,
    LaneInputs,
    OverageState,
    ReasonCode,
    RoutePurpose,
    TaskSpec,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
DISPLAY_MODEL_LABEL = "Gemini 3.1 Pro (High)"
_LIVE_RECEIPT_MATCH = (
    "Starting new conversation\n"
    "Created conversation 11111111-1111-1111-1111-111111111111\n"
    "authMethod=consumer\n"
    "daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n"
    "I0724 11:02:16.509256 40296 model_config_manager.go:272] "
    f'Propagating selected model override to backend: label="{DISPLAY_MODEL_LABEL}"\n'
)


def _probe_process(log_text: str = _LIVE_RECEIPT_MATCH):
    def process(argv, **_kwargs):
        log_path = Path(argv[argv.index("--log-file") + 1])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(log_text, encoding="utf-8")
        return type("Completed", (), {"returncode": 0, "stdout": "pong", "stderr": ""})()

    return process


def _doctor(tmp_path, *, overage_state: str | None = None) -> FleetQualificationDoctor:
    def command(argv):
        if argv[1] == "--version":
            return 0, "agy 1.1.6", ""
        if argv[1] == "models":
            return 0, "gemini-3.1-pro-high", ""
        raise AssertionError(argv)

    kwargs = {}
    if overage_state is not None:
        kwargs["billing_status"] = lambda _lane: {
            "overage_state": overage_state
        }
    return FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=command,
        run_process=_probe_process(),
        environment={},
        now=lambda: NOW,
        proof_cache_dir=tmp_path / "fleet" / "evidence" / "agy",
        **kwargs,
    )


def test_agy_qualification_exposes_implemented_parent_session_contract(tmp_path):
    qualification = _doctor(tmp_path).qualify((profile_map()["antigravity"],))[
        "antigravity"
    ]

    assert qualification.qualified
    assert qualification.parent_session_proven is True
    assert qualification.overage_state is OverageState.OFF
    assert qualification.overage_disabled is True
    assert "--conversation" in qualification.detail
    assert "persistent external parent driver" in qualification.detail
    assert "served-model receipt" in qualification.detail


def test_explicit_antigravity_overage_on_still_blocks_the_safe_default(tmp_path):
    qualification = _doctor(tmp_path, overage_state="on").qualify(
        (profile_map()["antigravity"],)
    )["antigravity"]

    assert qualification.overage_state is OverageState.ON
    assert qualification.overage_disabled is False


def test_antigravity_parent_admission_accepts_proven_external_driver(tmp_path):
    profile = profile_map()["antigravity"]
    qualification = _doctor(tmp_path).qualify((profile,))["antigravity"]
    evaluation = evaluate_lane(
        LaneInputs(
            profile=profile,
            enabled=True,
            adapter_found=True,
            qualification=qualification,
            capacity=CapacityRead(None, ReasonCode.CAPACITY_MISSING),
            max_concurrency=1,
            reserve_floor_pct=Decimal("0"),
        ),
        TaskSpec(
            task_id="external-parent-contract",
            cwd=Path("."),
            required_capabilities=frozenset({"workspace_write", "shell"}),
        ),
        now=NOW,
        purpose=RoutePurpose.DESKTOP_PARENT,
    )

    assert ReasonCode.PARENT_SESSION_UNSUPPORTED not in evaluation.reasons
    assert ReasonCode.PARENT_SESSION_UNPROVEN not in evaluation.reasons


def test_antigravity_is_external_for_workers_and_parent_sessions():
    profile = profile_map()["antigravity"]

    assert profile.adapter_kind is AdapterKind.EXTERNAL_CLI
    assert profile.supports_task_worker is True
    assert profile.supports_parent_session is True


def test_kimi_remains_disabled_for_workers_and_parent_sessions():
    profile = profile_map()["kimi"]

    assert profile.adapter_kind is AdapterKind.EXTERNAL_CLI
    assert profile.implemented is False
    assert profile.supports_task_worker is False
    assert profile.supports_parent_session is False


def test_live_service_uses_explicit_bridge_overage_for_antigravity_parent_catalog(
    tmp_path,
    monkeypatch,
):
    bridge = tmp_path / "usage.json"
    bridge.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "captured_at": NOW.isoformat(),
                "lanes": {
                    "antigravity": {
                        "used_pct": "10.000",
                        "remaining_pct": "90.000",
                        "confidence": "high",
                        "overage_disabled": True,
                        "comparability_group": "antigravity-weekly",
                        "quota_window_id": "2026-W30",
                        "measurement_kind": "measured",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class CapturingDoctor(FleetQualificationDoctor):
        def __init__(self, **kwargs):
            super().__init__(
                auth_status=lambda _provider: {"logged_in": False},
                claude_oauth_status=lambda: {"logged_in": False},
                which=lambda name: (
                    "C:/tools/agy.exe" if name == "agy" else None
                ),
                command=lambda argv: (
                    (0, "agy 1.1.6", "")
                    if argv[1] == "--version"
                    else (0, "gemini-3.1-pro-high", "")
                ),
                run_process=_probe_process(),
                environment={},
                billing_status=kwargs.get("billing_status"),
                now=lambda: NOW,
                proof_cache_dir=tmp_path / "fleet" / "evidence" / "agy",
            )

    monkeypatch.setattr(
        inspection,
        "FleetQualificationDoctor",
        CapturingDoctor,
    )
    service = build_fleet_service(
        config_data={
            "fleet": {
                "enabled": True,
                "parent_desktop_enabled": True,
                "bridge_usage_file": str(bridge),
                "lanes": {
                    "chatgpt_codex": {"enabled": False},
                    "claude_code": {"enabled": False},
                    "grok": {"enabled": False},
                    "antigravity": {"enabled": True},
                    "kimi": {"enabled": False},
                },
            }
        },
        adapters={"antigravity": object()},
        store_path=tmp_path / "fleet" / "state.db",
        now=lambda: NOW,
    )

    evaluation = next(
        item
        for item in service.inspect_parent(
            TaskSpec(
                task_id="antigravity-catalog",
                cwd=tmp_path,
                required_capabilities=frozenset(
                    {"workspace_write", "shell"}
                ),
            )
        )
        if item.lane_id == "antigravity"
    )
    catalog = serialize_evaluation(
        evaluation,
        purpose=RoutePurpose.DESKTOP_PARENT,
    )

    assert evaluation.eligible is True
    assert catalog["adapter_kind"] == "external_cli"
    assert catalog["provider_id"] == "antigravity-subscription"
    assert catalog["model_id"] == "gemini-3.1-pro-high"
    assert catalog["model_label"] == "Gemini 3.1 Pro (High)"
    assert catalog["supports_parent_session"] is True
