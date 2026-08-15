"""Action-level admission before initialization or dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence, TypeVar

from hermes_cli.kanban_store.types import ContractError

from .capabilities import CapabilityManifest

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    actions: tuple[str, ...]
    initializer: Callable[[], object]
    source: str = "core"


class AdmissionPolicy:
    def __init__(self, manifest: CapabilityManifest) -> None:
        self.manifest = manifest
        self._allowed = {
            (tool, action)
            for tool, actions in manifest.tool_actions.items()
            for action in actions
        }

    def allows(self, tool: str, action: str) -> bool:
        return (tool, action) in self._allowed

    def require(self, tool: str, action: str, *, path: str = "direct") -> None:
        if path not in {"direct", "nested", "deferred", "hook", "plugin", "mcp"}:
            raise ContractError("unknown dispatch path")
        if not self.allows(tool, action):
            raise PermissionError(f"strict worker denied {tool}.{action} via {path}")

    def admit_catalog(self, descriptors: Iterable[ToolDescriptor]) -> tuple[ToolDescriptor, ...]:
        """Filter descriptors without initializing a denied plugin or MCP."""

        admitted: list[ToolDescriptor] = []
        for descriptor in descriptors:
            allowed_actions = tuple(
                action for action in descriptor.actions if self.allows(descriptor.name, action)
            )
            if not allowed_actions:
                continue
            admitted.append(
                ToolDescriptor(
                    name=descriptor.name,
                    actions=allowed_actions,
                    initializer=descriptor.initializer,
                    source=descriptor.source,
                )
            )
        return tuple(admitted)

    def initialize_catalog(self, descriptors: Iterable[ToolDescriptor]) -> dict[str, object]:
        instances: dict[str, object] = {}
        for descriptor in self.admit_catalog(descriptors):
            if descriptor.name in instances:
                raise ContractError("duplicate admitted tool name")
            instances[descriptor.name] = descriptor.initializer()
        return instances

    def dispatch(
        self,
        handlers: Mapping[tuple[str, str], Callable[..., T]],
        *,
        tool: str,
        action: str,
        path: str,
        kwargs: Mapping[str, object],
    ) -> T:
        self.require(tool, action, path=path)
        handler = handlers.get((tool, action))
        if handler is None:
            raise ContractError("admitted action has no registered handler")
        return handler(**dict(kwargs))
