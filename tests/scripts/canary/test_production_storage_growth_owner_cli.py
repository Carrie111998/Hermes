from __future__ import annotations

import io
import subprocess
from pathlib import Path

from scripts.canary import full_canary_owner_launcher as owner
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_owner_cli as cli
from scripts.canary import production_storage_growth_contract as contract


RELEASE = "a" * 40


class _Stream:
    def __init__(self, payload: bytes = b"") -> None:
        self.buffer = io.BytesIO(payload)


def _frame(operation: str, document: dict) -> bytes:
    unsigned = {
        "schema": cli.FRAME_SCHEMA,
        "operation": operation,
        "document": document,
    }
    return protocol.canonical_json_bytes({
        **unsigned,
        "frame_sha256": protocol.sha256_json(unsigned),
    })


def _artifact_attestation() -> dict:
    artifacts = {
        name: {
            "release_relative": relative,
            "sha256": "1" * 64,
            "size": 1024,
        }
        for name, relative in contract.RUNTIME_ARTIFACT_RELATIVES.items()
    }
    unsigned = {
        "schema": contract.RUNTIME_ARTIFACT_ATTESTATION_SCHEMA,
        "release_revision": RELEASE,
        "owner_support_manifest_sha256": "2" * 64,
        "owner_support_source_tree_oid": "3" * 40,
        "artifacts": artifacts,
    }
    return {
        **unsigned,
        "attestation_sha256": protocol.sha256_json(unsigned),
    }


def test_fixed_cli_request_calls_only_owner_route_with_canonical_stdin(
    monkeypatch,
) -> None:
    calls = []
    plan = {"plan_sha256": "b" * 64}

    class Route:
        def request(self, **kwargs):
            calls.append(kwargs)
            return {"request_id": "c" * 64}

    monkeypatch.setattr(cli, "_build_route", lambda _release: (Route(), object()))
    monkeypatch.setattr(
        cli,
        "_revalidate_runtime",
        lambda _runtime, _release: calls.append("revalidated"),
    )
    output = _Stream()
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        _Stream(_frame(
            "request",
            {
                "growth_plan": plan,
                "authorization_nonce_sha256": "d" * 64,
            },
        )),
    )
    monkeypatch.setattr(cli.sys, "stdout", output)
    assert cli.main([
        "--release-sha",
        RELEASE,
        "request",
    ]) == 0
    response = protocol.decode_canonical_json(
        output.buffer.getvalue().removesuffix(b"\n")
    )
    assert response["result"]["request_id"] == "c" * 64
    assert calls == [
        {
            "growth_plan": plan,
            "authorization_nonce_sha256": "d" * 64,
        },
        "revalidated",
    ]
    assert response["caller_selected_paths_allowed"] is False
    assert response["caller_selected_commands_allowed"] is False
    assert response["caller_selected_targets_allowed"] is False


def test_fixed_cli_rejects_unknown_document_field_before_route(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_build_route",
        lambda _release: (_ for _ in ()).throw(AssertionError("route reached")),
    )
    output = _Stream()
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        _Stream(_frame("install-guest", {"path": "/tmp/attacker"})),
    )
    monkeypatch.setattr(cli.sys, "stdout", output)
    assert cli.main([
        "--release-sha",
        RELEASE,
        "install-guest",
    ]) == 2
    response = protocol.decode_canonical_json(
        output.buffer.getvalue().removesuffix(b"\n")
    )
    assert response["error_code"] == "production_storage_owner_cli_frame_invalid"


def test_install_owner_state_validates_sealed_runtime_before_privileged_mutation(
    monkeypatch,
) -> None:
    events: list[str] = []
    runtime = object()
    artifacts = _artifact_attestation()
    binding = cli.installer.build_owner_artifact_binding(RELEASE, artifacts)

    class Route:
        def attest_authority_key(self):
            events.append("authority")
            return {"attestation_sha256": "4" * 64}

    monkeypatch.setattr(
        cli,
        "_build_route",
        lambda _release: (events.append("route") or (Route(), runtime)),
    )
    monkeypatch.setattr(
        cli.owner,
        "observe_exact_production_storage_runtime_artifacts",
        lambda **_kwargs: (events.append("artifacts") or artifacts),
    )
    monkeypatch.setattr(
        cli,
        "_install_owner_state_privileged",
        lambda release, observed, authority: (
            events.append("privileged")
            if authority["attestation_sha256"] == "4" * 64
            else (_ for _ in ()).throw(AssertionError("wrong authority"))
        )
        or (
            events.append("privileged_result")
            or {
                "schema": cli.installer.OWNER_READINESS_SCHEMA,
                "release_sha": release,
                "sealed_artifact_binding_sha256": observed["binding_sha256"],
                "owner_support_manifest_sha256": observed[
                    "owner_support_manifest_sha256"
                ],
                "ready": True,
            }
        ),
    )
    monkeypatch.setattr(
        cli,
        "_revalidate_runtime",
        lambda observed, _release: (
            events.append("revalidate")
            if observed is runtime
            else (_ for _ in ()).throw(AssertionError("wrong runtime"))
        ),
    )
    output = _Stream()
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        _Stream(_frame("install-owner-state", {})),
    )
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main([
        "--release-sha",
        RELEASE,
        "install-owner-state",
    ]) == 0
    response = protocol.decode_canonical_json(
        output.buffer.getvalue().removesuffix(b"\n")
    )
    assert response["result"]["sealed_artifact_binding_sha256"] == binding[
        "binding_sha256"
    ]
    assert events == [
        "route", "artifacts", "authority", "privileged",
        "privileged_result", "revalidate",
    ]


def test_build_plan_has_no_caller_runtime_hash_surface(monkeypatch) -> None:
    calls = []
    runtime = object()
    source = {"observation_sha256": "5" * 64}

    class Route:
        def collect_source_preflight(self):
            calls.append("collect")
            return source

    monkeypatch.setattr(
        cli,
        "_build_route",
        lambda _release: (Route(), runtime),
    )
    monkeypatch.setattr(
        cli.owner,
        "build_exact_production_storage_growth_plan",
        lambda **kwargs: calls.append(kwargs) or {"plan_sha256": "4" * 64},
    )
    monkeypatch.setattr(cli, "_revalidate_runtime", lambda *_args: None)
    output = _Stream()
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        _Stream(_frame("build-plan", {})),
    )
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main([
        "--release-sha",
        RELEASE,
        "build-plan",
    ]) == 0
    assert calls == ["collect", {
        "release_sha": RELEASE,
        "source_preflight": source,
        "trusted_runtime": runtime,
        "now_unix": calls[1]["now_unix"],
    }]
    assert not {
        "executor_binary_sha256",
        "mutation_wrapper_sha256",
        "read_only_collector_sha256",
        "remote_transport_sha256",
    }.intersection(calls[1])


def test_build_plan_rejects_caller_supplied_source_before_route(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_build_route",
        lambda _release: (_ for _ in ()).throw(AssertionError("route reached")),
    )
    output = _Stream()
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        _Stream(_frame(
            "build-plan",
            {"source_preflight": {"observation_sha256": "5" * 64}},
        )),
    )
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main([
        "--release-sha",
        RELEASE,
        "build-plan",
    ]) == 2
    response = protocol.decode_canonical_json(
        output.buffer.getvalue().removesuffix(b"\n")
    )
    assert response["error_code"] == "production_storage_owner_cli_frame_invalid"


def test_privileged_owner_install_uses_one_fixed_isolated_installer_command() -> None:
    binding = cli.installer.build_owner_artifact_binding(
        RELEASE,
        _artifact_attestation(),
    )
    authority = {"attestation_sha256": "4" * 64}
    captured = {}
    readiness = {
        "schema": cli.installer.OWNER_READINESS_SCHEMA,
        "release_sha": RELEASE,
        "sealed_artifact_binding_sha256": binding["binding_sha256"],
        "owner_support_manifest_sha256": binding[
            "owner_support_manifest_sha256"
        ],
        "ready": True,
    }

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            cli.installer.canonical_json_bytes(readiness) + b"\n",
            b"",
        )

    assert cli._install_owner_state_privileged(
        RELEASE,
        binding,
        authority,
        runner=runner,
    ) == readiness
    assert captured["command"] == (
        "/usr/bin/sudo",
        "--non-interactive",
        "--",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        str(cli.Path(cli.installer.__file__).resolve(strict=True)),
        "install-owner-state",
        "--release-sha",
        RELEASE,
    )
    assert captured["input"] == cli.installer.canonical_json_bytes({
        "sealed_artifact_binding": binding,
        "authority_key_attestation": authority,
    })


def test_ambient_launcher_invokes_only_exact_sealed_owner_cli(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "owner-support" / "source"
    entrypoint = (
        source_root
        / "scripts/canary/production_storage_growth_owner_cli.py"
    )
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("# sealed owner cli\n", encoding="utf-8")
    events: list[str] = []

    class Runtime:
        def trusted_owner_support_paths(self):
            events.append("support")
            return source_root, tmp_path / "owner-support" / "site"

        def trusted_command_prefix(self):
            events.append("prefix")
            return ("/fixed/trusted/python3",)

    runtime = Runtime()
    monkeypatch.setattr(
        owner,
        "require_local_launcher_provenance",
        lambda release: (
            events.append("launcher") or "1" * 64
            if release == RELEASE
            else (_ for _ in ()).throw(AssertionError("wrong release"))
        ),
    )
    monkeypatch.setattr(
        owner,
        "require_trusted_owner_runtime",
        lambda release: (
            events.append("runtime") or runtime
            if release == RELEASE
            else (_ for _ in ()).throw(AssertionError("wrong release"))
        ),
    )
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        unsigned = {
            "schema": "muncho-production-storage-growth-owner-cli-response.v1",
            "operation": "build-plan",
            "release_sha": RELEASE,
            "result": {"plan_sha256": "2" * 64},
            "caller_selected_paths_allowed": False,
            "caller_selected_commands_allowed": False,
            "caller_selected_targets_allowed": False,
        }
        response = {
            **unsigned,
            "response_sha256": protocol.sha256_json(unsigned),
        }
        return subprocess.CompletedProcess(
            command,
            0,
            protocol.canonical_json_bytes(response) + b"\n",
            b"",
        )

    response = owner.invoke_exact_production_storage_growth_owner_cli(
        release_sha=RELEASE,
        operation="build-plan",
        document={},
        runner=runner,
    )

    assert response["result"]["plan_sha256"] == "2" * 64
    assert captured["command"] == (
        "/fixed/trusted/python3",
        *owner._GCLOUD_PYTHON_ISOLATION_ARGS,
        str(entrypoint),
        "--release-sha",
        RELEASE,
        "build-plan",
    )
    frame = protocol.decode_canonical_json(captured["input"])
    assert frame["operation"] == "build-plan"
    assert frame["document"] == {}
    assert frame["frame_sha256"] == protocol.sha256_json({
        "schema": frame["schema"],
        "operation": frame["operation"],
        "document": frame["document"],
    })
    assert captured["env"] == {
        "HOME": owner._canonical_owner_home(),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": owner._FIXED_OWNER_PATH,
    }
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert events == [
        "launcher",
        "runtime",
        "support",
        "prefix",
        "prefix",
        "launcher",
    ]
