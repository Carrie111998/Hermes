from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_cli.kanban_runtime.admission import AdmissionPolicy, ToolDescriptor
from hermes_cli.kanban_runtime.capabilities import CapabilityManifest
from hermes_cli.kanban_runtime.context import (
    context_digest,
    read_context_file,
    write_context_file,
)
from hermes_cli.kanban_store.types import (
    ContractError,
    RunBinding,
    RunFence,
    RuntimeIdentity,
    WorkerContext,
)


class AdmissionTests(unittest.TestCase):
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
        instances = policy.initialize_catalog([
            ToolDescriptor("host_terminal", ("execute",), lambda: called.append("bad")),
            ToolDescriptor("workspace", ("read",), lambda: called.append("good") or object()),
        ])
        self.assertEqual(called, ["good"])
        self.assertEqual(set(instances), {"workspace"})

    def test_action_level_nested_denial(self):
        policy = AdmissionPolicy(self.manifest())
        with self.assertRaises(PermissionError):
            policy.require("workspace", "delete", path="nested")

    def test_context_is_immutable_and_digest_bound(self):
        context = WorkerContext(
            fence=RunFence("t", 1, 1, "x" * 64),
            binding=RunBinding("b", "d", "w", "p", True),
            capability_manifest_sha256="a" * 64,
            runtime_identity=RuntimeIdentity("provider", "model", "responses", "session"),
            workspace="/workspace",
            artifact_root="/workspace",
            broker_socket="/run/hermes-broker/broker.sock",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "context.json"
            digest = write_context_file(path, context)
            self.assertEqual(digest, context_digest(context))
            self.assertEqual(read_context_file(path, digest), context)
            path.chmod(0o600)
            with self.assertRaises(ContractError):
                read_context_file(path, digest)


if __name__ == "__main__":
    unittest.main()
