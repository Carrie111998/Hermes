from __future__ import annotations

import socket
import struct
import unittest

from hermes_cli.kanban_runtime.admission import AdmissionPolicy, ToolDescriptor
from hermes_cli.kanban_runtime.broker import BrokerSession, SessionState, recv_frame, send_frame
from hermes_cli.kanban_runtime.host import StrictWorkerHost
from hermes_cli.kanban_runtime.capabilities import CapabilityManifest
from hermes_cli.kanban_runtime.oci import StrictOciConfig, build_create_command, validate_effective_inspect
from hermes_cli.kanban_store.claims import issue_claim
from hermes_cli.kanban_store.types import (
    ContractError,
    RunBinding,
    RunFence,
    RuntimeIdentity,
    TrustedIntentPolicy,
    WorkerContext,
)

from .helpers import TempRoot, add_task, database


def identity():
    return RuntimeIdentity(
        provider="openai-codex",
        model="gpt-5.6-sol",
        api_mode="responses",
        session_id="strict-session-1",
    )


class RuntimeIdentityTests(unittest.TestCase):
    def test_runtime_identity_source_is_host_reserved(self):
        from hermes_cli.kanban_store.types import ContractError, RuntimeIdentity

        with self.assertRaises(ContractError):
            RuntimeIdentity(
                provider="p", model="m", api_mode="responses",
                session_id="s", source="worker_supplied",
            )


class RuntimeTests(unittest.TestCase):
    def manifest(self):
        return CapabilityManifest(
            1,
            ("heartbeat", "finalize"),
            {"workspace": ("read", "write")},
            ("/workspace",),
            ("worker",),
            {"max_frame_bytes": 1000},
        )

    def test_denied_plugin_is_never_initialized(self):
        called = []
        policy = AdmissionPolicy(self.manifest())
        catalog = [
            ToolDescriptor("host_terminal", ("execute",), lambda: called.append("bad")),
            ToolDescriptor("workspace", ("read",), lambda: called.append("good") or object()),
        ]
        instances = policy.initialize_catalog(catalog)
        self.assertEqual(called, ["good"])
        self.assertEqual(set(instances), {"workspace"})

    def test_action_level_nested_denial(self):
        policy = AdmissionPolicy(self.manifest())
        with self.assertRaises(PermissionError):
            policy.require("workspace", "delete_root", path="nested")

    def test_broker_rejects_unknown_fields_and_stale_generation(self):
        fence = RunFence("t", 1, 1, "x" * 64)
        session = BrokerSession(
            state=SessionState(fence, identity()),
            manifest=self.manifest(),
            fence_validator=lambda _f: None,
            handlers={"heartbeat": lambda _p, _f: {"ok": True}, "finalize": lambda _p, _f: {}},
        )
        request = {
            "schema": "hermes.kanban.broker.v1",
            "request_id": "r",
            "seq": 1,
            "task_id": "t",
            "run_id": 1,
            "claim_generation": 2,
            "method": "heartbeat",
            "params": {"ttl_seconds": 60},
        }
        self.assertFalse(session.handle(request)["ok"])
        request["claim_generation"] = 1
        request["extra"] = True
        self.assertFalse(session.handle(request)["ok"])

    def test_broker_finalization_closes_session(self):
        fence = RunFence("t", 1, 1, "x" * 64)
        session = BrokerSession(
            state=SessionState(fence, identity()),
            manifest=self.manifest(),
            fence_validator=lambda _f: None,
            handlers={"heartbeat": lambda _p, _f: {}, "finalize": lambda _p, _f: {"done": True}},
        )
        base = {
            "schema": "hermes.kanban.broker.v1",
            "request_id": "r",
            "seq": 1,
            "task_id": "t",
            "run_id": 1,
            "claim_generation": 1,
            "method": "finalize",
            "params": {"outcome": "completed", "summary": "done"},
        }
        self.assertTrue(session.handle(base)["ok"])
        base["request_id"] = "r2"
        base["seq"] = 2
        base["method"] = "heartbeat"
        self.assertFalse(session.handle(base)["ok"])

    def test_method_param_unknown_field_is_rejected(self):
        fence = RunFence("t", 1, 1, "x" * 64)
        session = BrokerSession(
            state=SessionState(fence, identity()),
            manifest=self.manifest(),
            fence_validator=lambda _f: None,
            handlers={"heartbeat": lambda _p, _f: {}},
        )
        response = session.handle({
            "schema": "hermes.kanban.broker.v1",
            "request_id": "r",
            "seq": 1,
            "task_id": "t",
            "run_id": 1,
            "claim_generation": 1,
            "method": "heartbeat",
            "params": {"ttl_seconds": 60, "terminal": "escape"},
        })
        self.assertFalse(response["ok"])

    def test_strict_host_stamps_trusted_runtime_identity(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(
                conn, task_id="task-1", profile="worker", ttl_seconds=60
            )
            manifest = CapabilityManifest(
                1,
                ("finalize",),
                {},
                ("/workspace",),
                ("worker",),
                {"max_param_bytes": 4096},
            )
            context = WorkerContext(
                fence=claim.fence,
                binding=RunBinding("board", "database", "worker-1", "worker"),
                capability_manifest_sha256=manifest.sha256,
                runtime_identity=identity(),
                workspace="/workspace",
                artifact_root="/workspace",
                broker_socket="/run/hermes-broker/broker.sock",
            )
            host = StrictWorkerHost(
                conn=conn,
                context=context,
                manifest=manifest,
                workspace=root,
                artifact_blob_root=root / "blobs",
                policy_resolver=lambda _draft: TrustedIntentPolicy(
                    False, "none", "none-v1", {}
                ),
                inference_handler=lambda _params, _identity: {},
            )
            response = host.session().handle({
                "schema": "hermes.kanban.broker.v1",
                "request_id": "final",
                "seq": 1,
                "task_id": "task-1",
                "run_id": claim.fence.run_id,
                "claim_generation": claim.fence.claim_generation,
                "method": "finalize",
                "params": {"outcome": "completed", "summary": "done"},
            })
            self.assertTrue(response["ok"], response)
            row = conn.execute(
                "SELECT runtime_provider,runtime_model,runtime_api_mode,"
                "runtime_session_id,runtime_identity_source,metadata FROM task_runs"
            ).fetchone()
            self.assertEqual(
                tuple(row[:5]),
                (
                    "openai-codex", "gpt-5.6-sol", "responses",
                    "strict-session-1", "controller_resolved_provider_route",
                ),
            )
            self.assertIn('"runtime_identity"', row[5])

    def test_framing_roundtrip(self):
        left, right = socket.socketpair()
        try:
            send_frame(left, {"x": 1})
            self.assertEqual(recv_frame(right), {"x": 1})
        finally:
            left.close(); right.close()

    def test_oci_request_contains_hard_boundary(self):
        config = StrictOciConfig(
            runtime="docker",
            image="registry/image@sha256:" + "a" * 64,
            container_name="worker",
            workspace_host="/tmp/work",
            context_host="/tmp/context",
            broker_dir_host="/tmp/broker",
            command=("python", "-m", "worker"),
        )
        command = build_create_command(config)
        joined = " ".join(command)
        for required in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges:true"):
            self.assertIn(required, joined)

    def test_effective_oci_drift_is_rejected(self):
        config = StrictOciConfig(
            runtime="docker",
            image="registry/image@sha256:" + "a" * 64,
            container_name="worker",
            workspace_host="/tmp/work",
            context_host="/tmp/context",
            broker_dir_host="/tmp/broker",
            command=("worker",),
        )
        inspect = {
            "HostConfig": {"NetworkMode": "bridge", "ReadonlyRootfs": True, "Privileged": False, "PidMode": "", "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"]},
            "Config": {"User": "65532:65532", "Env": [], "Image": config.image},
            "Mounts": [],
            "State": {},
        }
        with self.assertRaises(ContractError):
            validate_effective_inspect(config, inspect)


if __name__ == "__main__":
    unittest.main()
