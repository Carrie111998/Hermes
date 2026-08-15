"""Discord command registry - pure logic for command sync parity.

Feature I1: command registry sync parity. No network, no side effects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


class CommandRegistryError(ValueError):
    """Raised when a command definition is invalid."""


@dataclass
class CommandDef:
    """A Discord application command definition.

    Attributes:
        name: Command name (surrounding whitespace is normalized away on use).
        command_type: Discord command type int (1 = chat input, etc.).
        integration_types: Discord integration type ints (0 = guild install);
            defaults to [0] when None/empty via normalize_command.
        guild_id: Optional guild scope; None means global.
    """

    name: str
    command_type: int
    integration_types: Optional[list[int]] = None
    guild_id: Optional[str] = None


def normalize_command(cmd: CommandDef) -> CommandDef:
    """Return a normalized copy of ``cmd``.

    - name is stripped; empty/whitespace-only names raise CommandRegistryError
    - command_type must be an int (bool excluded); otherwise CommandRegistryError
    - integration_types defaults to [0] when None/empty
    - integration_types are deduped and sorted ascending
    """
    if not isinstance(cmd.name, str):
        raise CommandRegistryError("command name must be a non-empty string")
    name = cmd.name.strip()
    if not name:
        raise CommandRegistryError("command name must be a non-empty string")

    if not isinstance(cmd.command_type, int) or isinstance(cmd.command_type, bool):
        raise CommandRegistryError("command_type must be an int")

    integration_types = sorted(set(cmd.integration_types or [0]))

    return CommandDef(
        name=name,
        command_type=cmd.command_type,
        integration_types=integration_types,
        guild_id=cmd.guild_id,
    )


def command_fingerprint(cmd: CommandDef) -> str:
    """Stable string key for a (normalized) command definition.

    Combines (command_type, name.lower(), sorted integration_types,
    guild_id or 'global'). Two commands with the same fingerprint are
    considered duplicates / unchanged for sync purposes. JSON encoding
    keeps the key unambiguous and deterministic.
    """
    norm = normalize_command(cmd)
    payload = [
        norm.command_type,
        norm.name.lower(),
        norm.integration_types,
        norm.guild_id or "global",
    ]
    return json.dumps(payload, separators=(",", ":"))


def duplicate_diagnostics(commands: list[CommandDef]) -> dict[str, list[str]]:
    """Map fingerprint -> [names] for fingerprints seen more than once.

    Empty dict when every command is unique. Detection operates on the
    normalized (type, name) identity via command_fingerprint.
    """
    seen: dict[str, list[str]] = {}
    for c in commands:
        fp = command_fingerprint(c)
        seen.setdefault(fp, []).append(c.name.strip())
    return {fp: names for fp, names in seen.items() if len(names) > 1}


def should_sync(current: CommandDef, deployed_fingerprint: Optional[str]) -> bool:
    """Whether ``current`` must be (re)synced given the deployed fingerprint.

    True when never deployed (deployed_fingerprint is None) or the current
    fingerprint differs (changed); False when identical (unchanged, skip sync).
    Raises CommandRegistryError on invalid name (empty/whitespace) or
    command_type not int.
    """
    current_fp = command_fingerprint(current)  # validates name/command_type
    if deployed_fingerprint is None:
        return True
    return current_fp != deployed_fingerprint
