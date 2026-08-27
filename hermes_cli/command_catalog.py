"""Versioned command catalog contract shared by Hermes surfaces.

This module is deliberately transport-free.  It converts the legacy
``COMMAND_REGISTRY`` into a stable, JSON-serializable semantic catalog with
canonical command identities and a deterministic revision fingerprint.

Dynamic contributors (plugins, skills, bundles, quick commands, client-local
commands) are layered by callers through ``build_command_catalog``.  Duplicate
IDs, names, or aliases fail closed unless a future explicit override authority
is added here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from hermes_cli.commands import COMMAND_REGISTRY, CommandDef

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CommandSpec:
    schema_version: int
    command_id: str
    name: str
    aliases: tuple[str, ...]
    description_fallback: str
    category: str
    args_hint: str
    subcommands: tuple[str, ...]
    execution_owner: str
    handler_id: str
    origin: str
    availability: Mapping[str, Any]
    visibility: Mapping[str, bool]
    busy_policy: str
    live_session_requirement: str
    authorization_policy: str
    confirmation_policy: str
    mutation_scope: str
    idempotency_policy: str
    retry_policy: str
    result_capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "name": self.name,
            "aliases": list(self.aliases),
            "description_fallback": self.description_fallback,
            "category": self.category,
            "argument_schema": {
                "kind": "legacy",
                "hint": self.args_hint,
                "subcommands": list(self.subcommands),
            },
            "execution_owner": self.execution_owner,
            "handler_id": self.handler_id,
            "origin": self.origin,
            "availability": dict(self.availability),
            "visibility": dict(self.visibility),
            "busy_policy": self.busy_policy,
            "live_session_requirement": self.live_session_requirement,
            "authorization_policy": self.authorization_policy,
            "confirmation_policy": self.confirmation_policy,
            "mutation_scope": self.mutation_scope,
            "idempotency_policy": self.idempotency_policy,
            "retry_policy": self.retry_policy,
            "result_capabilities": list(self.result_capabilities),
        }


@dataclass(frozen=True)
class CommandCatalog:
    schema_version: int
    revision: str
    commands: tuple[CommandSpec, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "commands": [command.to_dict() for command in self.commands],
        }


def _command_id(name: str) -> str:
    return "command." + name.replace("_", "-")


def _legacy_spec(command: CommandDef) -> CommandSpec:
    if command.cli_only:
        surfaces = ["cli", "desktop", "tui"]
    elif command.gateway_only:
        surfaces = ["gateway", "discord", "telegram", "slack", "matrix"]
    else:
        surfaces = [
            "cli",
            "desktop",
            "tui",
            "gateway",
            "discord",
            "telegram",
            "slack",
            "matrix",
        ]

    return CommandSpec(
        schema_version=SCHEMA_VERSION,
        command_id=_command_id(command.name),
        name=command.name,
        aliases=tuple(command.aliases),
        description_fallback=command.description,
        category=command.category,
        args_hint=command.args_hint,
        subcommands=tuple(command.subcommands),
        execution_owner="server",
        handler_id=command.execute or command.name,
        origin="core",
        availability={
            "surfaces": surfaces,
            "gateway_config_gate": command.gateway_config_gate,
        },
        visibility={
            "help": True,
            "completion": True,
            "native_menu": not command.cli_only,
            "hidden": False,
        },
        busy_policy=command.busy_policy,
        live_session_requirement="session",
        authorization_policy="existing",
        confirmation_policy="existing",
        mutation_scope="existing",
        idempotency_policy="existing",
        retry_policy="existing",
        result_capabilities=("text",),
    )


def _normalize_external_spec(value: Mapping[str, Any], origin: str) -> CommandSpec:
    name = str(value.get("name") or "").lstrip("/").strip()
    if not name:
        raise ValueError(f"{origin} command contribution missing name")
    command_id = str(value.get("command_id") or f"{origin}.{name}")
    aliases = tuple(str(item).lstrip("/") for item in value.get("aliases", ()) or ())
    subcommands = tuple(str(item) for item in value.get("subcommands", ()) or ())
    availability = value.get("availability") or {}
    visibility = value.get("visibility") or {}
    if not isinstance(availability, Mapping) or not isinstance(visibility, Mapping):
        raise ValueError(f"{command_id}: availability/visibility must be mappings")
    return CommandSpec(
        schema_version=SCHEMA_VERSION,
        command_id=command_id,
        name=name,
        aliases=aliases,
        description_fallback=str(value.get("description_fallback") or value.get("description") or ""),
        category=str(value.get("category") or "Extensions"),
        args_hint=str(value.get("args_hint") or ""),
        subcommands=subcommands,
        execution_owner=str(value.get("execution_owner") or origin),
        handler_id=str(value.get("handler_id") or command_id),
        origin=origin,
        availability=dict(availability),
        visibility={
            "help": bool(visibility.get("help", True)),
            "completion": bool(visibility.get("completion", True)),
            "native_menu": bool(visibility.get("native_menu", True)),
            "hidden": bool(visibility.get("hidden", False)),
        },
        busy_policy=str(value.get("busy_policy") or "reject"),
        live_session_requirement=str(value.get("live_session_requirement") or "session"),
        authorization_policy=str(value.get("authorization_policy") or "existing"),
        confirmation_policy=str(value.get("confirmation_policy") or "existing"),
        mutation_scope=str(value.get("mutation_scope") or "existing"),
        idempotency_policy=str(value.get("idempotency_policy") or "existing"),
        retry_policy=str(value.get("retry_policy") or "existing"),
        result_capabilities=tuple(str(item) for item in value.get("result_capabilities", ("text",)) or ()),
    )


def validate_command_specs(specs: Sequence[CommandSpec]) -> None:
    ids: dict[str, str] = {}
    tokens: dict[str, str] = {}
    for spec in specs:
        previous = ids.setdefault(spec.command_id, spec.origin)
        if previous != spec.origin:
            raise ValueError(f"duplicate command_id {spec.command_id!r}: {previous} vs {spec.origin}")
        if spec.busy_policy not in {"dispatch", "reject", "interrupt_then_dispatch"}:
            raise ValueError(f"{spec.command_id}: invalid busy_policy {spec.busy_policy!r}")
        for token in (spec.name, *spec.aliases):
            key = token.lower()
            owner = tokens.setdefault(key, spec.command_id)
            if owner != spec.command_id:
                raise ValueError(f"command token {token!r} collides: {owner} vs {spec.command_id}")


def _fingerprint(specs: Sequence[CommandSpec]) -> str:
    payload = json.dumps(
        [spec.to_dict() for spec in specs],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_command_catalog(
    *,
    contributions: Iterable[tuple[str, Mapping[str, Any]]] = (),
) -> CommandCatalog:
    specs = [_legacy_spec(command) for command in COMMAND_REGISTRY]
    specs.extend(_normalize_external_spec(value, origin) for origin, value in contributions)
    specs.sort(key=lambda item: (item.category.casefold(), item.name.casefold(), item.command_id))
    validate_command_specs(specs)
    return CommandCatalog(
        schema_version=SCHEMA_VERSION,
        revision=_fingerprint(specs),
        commands=tuple(specs),
    )


def resolve_catalog_command(catalog: CommandCatalog, token: str) -> CommandSpec | None:
    normalized = str(token or "").lstrip("/").casefold()
    if not normalized:
        return None
    for spec in catalog.commands:
        if spec.name.casefold() == normalized:
            return spec
        if any(alias.casefold() == normalized for alias in spec.aliases):
            return spec
    return None
