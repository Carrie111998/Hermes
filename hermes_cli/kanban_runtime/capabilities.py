"""Versioned strict-worker capability manifest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from hermes_cli.kanban_store.canonical import canonical_json_bytes, sha256_hex
from hermes_cli.kanban_store.types import ContractError


FORBIDDEN_CAPABILITIES = frozenset(
    {
        "network.generic",
        "host.terminal",
        "host.filesystem",
        "host.home",
        "kanban.database",
        "docker.socket",
        "github.credential",
        "gateway.credential",
        "provider.credential",
        "plugin.dynamic",
        "mcp.dynamic",
        "publication.direct",
    }
)

BROKER_METHODS = frozenset(
    {
        "inference.request",
        "event.append",
        "intent.draft",
        "artifact.declare",
        "heartbeat",
        "finalize",
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    schema_version: int
    broker_methods: tuple[str, ...]
    tool_actions: Mapping[str, tuple[str, ...]]
    workspace_prefixes: tuple[str, ...]
    inference_profiles: tuple[str, ...]
    limits: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported capability manifest schema")
        methods = set(self.broker_methods)
        if not methods <= BROKER_METHODS:
            raise ContractError("manifest requests an unsupported broker method")
        flattened = {f"{tool}.{action}" for tool, actions in self.tool_actions.items() for action in actions}
        if flattened & FORBIDDEN_CAPABILITIES:
            raise ContractError("manifest grants a forbidden capability")
        if any(not path.startswith("/") for path in self.workspace_prefixes):
            raise ContractError("workspace prefixes must be container-absolute")
        if any(int(value) < 0 for value in self.limits.values()):
            raise ContractError("capability limits must be non-negative")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "hermes.kanban.capability-manifest.v1",
            "schema_version": self.schema_version,
            "broker_methods": sorted(self.broker_methods),
            "tool_actions": {
                name: sorted(actions) for name, actions in sorted(self.tool_actions.items())
            },
            "workspace_prefixes": sorted(self.workspace_prefixes),
            "inference_profiles": sorted(self.inference_profiles),
            "limits": dict(sorted(self.limits.items())),
        }

    @property
    def bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return sha256_hex(self.bytes)
