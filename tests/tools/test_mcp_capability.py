from __future__ import annotations

import copy
import math

import pytest

from tools.mcp_capability import (
    McpCapabilityError,
    mint_mcp_capability,
    verify_mcp_capability,
)


AUDIENCE = "com.hermes.mcp/portable/example-workflows/workflows"
ARGS = {"title": "Review roadmap", "lane": "today"}
SESSION = {
    "platform": "wearable",
    "profile": "test-profile",
    "chat_id": "device",
    "session_id": "agent:main:wearable:dm:device",
    "message_id": "wearable-turn-1-call",
    "tool_call_id": "tool-call-opaque-1",
}


def _mint():
    return mint_mcp_capability(
        audience=AUDIENCE,
        binding="example-workflows:workflows",
        package_digest="sha256:" + "a" * 64,
        workflow="workflow_create_task",
        arguments=ARGS,
        session=SESSION,
        now=100,
        ttl_seconds=10,
    )


def _verify(capability, **overrides):
    values = {
        "expected_audience": AUDIENCE,
        "expected_workflow": "workflow_create_task",
        "expected_arguments": ARGS,
        "now": 105,
    }
    values.update(overrides)
    return verify_mcp_capability(capability, **values)


def test_round_trip_binds_every_active_turn_identity_and_package_digest():
    claims = _verify(_mint())
    assert {key: claims[key] for key in SESSION} == SESSION
    assert claims["package_digest"] == "sha256:" + "a" * 64
    assert claims["audience"] == AUDIENCE
    assert claims["workflow"] == "workflow_create_task"


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("expected_audience", AUDIENCE + ".stolen"),
        ("expected_workflow", "workflow_set_timer"),
        ("expected_arguments", {"title": "Changed", "lane": "today"}),
        ("now", 110),
        ("now", 99),
    ],
)
def test_wrong_audience_workflow_arguments_or_lifetime_is_rejected(override, value):
    with pytest.raises(McpCapabilityError):
        _verify(_mint(), **{override: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("issued_at", True),
        ("expires_at", True),
        ("message_id", ""),
        ("tool_call_id", " call "),
        ("package_digest", "sha256:wrong"),
    ],
)
def test_tampered_claims_fail_signature_and_strict_shape(field, value):
    capability = _mint()
    capability["claims"][field] = value
    with pytest.raises(McpCapabilityError):
        _verify(capability)


def test_extra_claim_and_bad_signature_are_rejected():
    extra = _mint()
    extra["claims"]["extra"] = "no"
    with pytest.raises(McpCapabilityError):
        _verify(extra)
    bad_signature = _mint()
    bad_signature["signature"] = "0" * 64
    with pytest.raises(McpCapabilityError):
        _verify(bad_signature)


def test_nonfinite_model_arguments_are_never_signed():
    with pytest.raises(McpCapabilityError):
        mint_mcp_capability(
            audience=AUDIENCE,
            binding="example-workflows:workflows",
            package_digest="sha256:" + "a" * 64,
            workflow="workflow_create_task",
            arguments={"value": math.nan},
            session=SESSION,
            now=100,
        )


def test_oversized_model_arguments_are_never_signed():
    with pytest.raises(McpCapabilityError, match="capability bound"):
        mint_mcp_capability(
            audience=AUDIENCE,
            binding="example-workflows:workflows",
            package_digest="sha256:" + "a" * 64,
            workflow="workflow_create_task",
            arguments={"title": "x" * (64 * 1024)},
            session=SESSION,
            now=100,
        )


def test_call_time_package_replacement_revokes_discovery_grant(tmp_path, monkeypatch):
    from hermes_cli.agent_plugins import canonical_package_digest
    from hermes_cli import plugins as plugins_module
    from tools import mcp_tool

    package = tmp_path / "package"
    package.mkdir()
    (package / "plugin.json").write_text("{}", encoding="utf-8")
    (package / "server.py").write_text("# reviewed\n", encoding="utf-8")
    digest = canonical_package_digest(package)
    authority = {
        "audience": AUDIENCE,
        "binding": "example-workflows:workflows",
        "package_digest": digest,
        "package_root": str(package),
    }
    monkeypatch.setattr(
        plugins_module,
        "_trusted_portable_session_capability_bindings",
        lambda: {authority["binding"]: digest},
    )
    monkeypatch.setattr(mcp_tool, "_hermes_session_call_meta", lambda: SESSION)

    meta = mcp_tool._prepare_session_capability(
        "internal-server", "workflow_create_task", ARGS, authority
    )
    capability_value = meta["com.hermes/capability"]
    claims = verify_mcp_capability(
        capability_value,
        expected_audience=AUDIENCE,
        expected_workflow="workflow_create_task",
        expected_arguments=ARGS,
        now=capability_value["claims"]["issued_at"],
    )
    assert claims["package_digest"] == digest

    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "server.cpython-313.pyc").write_bytes(b"crafted-bytecode")
    with pytest.raises(PermissionError):
        mcp_tool._prepare_session_capability(
            "internal-server", "workflow_create_task", ARGS, authority
        )
    (cache / "server.cpython-313.pyc").unlink()
    cache.rmdir()

    (package / "server.py").write_text("# replaced after discovery\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        mcp_tool._prepare_session_capability(
            "internal-server", "workflow_create_task", ARGS, authority
        )
