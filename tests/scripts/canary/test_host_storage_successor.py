from __future__ import annotations

from scripts.canary import host_storage_successor as successor


def test_successor_is_exact_steady_state_without_mutation_authority() -> None:
    value = successor.canonical_payload()

    assert value == {
        "schema": "muncho-isolated-canary-host-storage-successor.v1",
        "project": "adventico-ai-platform",
        "zone": "europe-west3-a",
        "vm_name": "muncho-canary-v2-01",
        "disk_name": "muncho-canary-v2-01",
        "target_size_gb": 100,
        "audit": {
            "method": "v1.compute.disks.resize",
            "principal": "lomliev@adventico.com",
            "resource_name": (
                "projects/adventico-ai-platform/zones/europe-west3-a/"
                "disks/muncho-canary-v2-01"
            ),
            "requested_at": "2026-08-03T22:38:45.472891Z",
            "completed_at": "2026-08-03T22:38:50.119667Z",
            "requested_size_gb": 100,
        },
        "mutation_authority": False,
        "resize_authority": False,
        "delete_authority": False,
        "shrink_authority": False,
        "steady_state_only": True,
    }
    assert len(successor.canonical_sha256()) == 64
