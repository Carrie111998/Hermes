"""Hermes-managed SSH target discovery for the gateway control plane.

Targets are loaded only from ``$HERMES_HOME/ssh/targets.yaml``. Hermes does
not implicitly import the user's OpenSSH config: making a host available to an
agent is an explicit configuration decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import display_hermes_home, get_hermes_home


@dataclass(frozen=True)
class SshTarget:
    """A redaction-safe view of one configured SSH target."""

    alias: str
    host: str | None = None
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    cwd: str | None = None
    source: str = "hermes"
    errors: tuple[str, ...] = ()


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_port(value: Any) -> tuple[int | None, str | None]:
    if value in (None, ""):
        return None, None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None, "port must be an integer"
    if not 1 <= port <= 65535:
        return None, "port must be between 1 and 65535"
    return port, None


def _coerce_target(
    alias: str,
    data: Any,
    *,
    source: str = "hermes",
) -> SshTarget | None:
    clean_alias = str(alias or "").strip()
    if not clean_alias or not isinstance(data, dict):
        return None

    errors: list[str] = []
    if any(char.isspace() for char in clean_alias):
        errors.append("alias must not contain whitespace")
    if clean_alias.lower() == "local":
        errors.append("alias `local` is reserved")

    port, port_error = _coerce_port(data.get("port"))
    if port_error:
        errors.append(port_error)

    return SshTarget(
        alias=clean_alias,
        host=_clean_optional(data.get("host") or data.get("hostname")),
        user=_clean_optional(data.get("user")),
        port=port,
        identity_file=_clean_optional(
            data.get("identity_file")
            or data.get("identityfile")
            or data.get("key")
        ),
        cwd=_clean_optional(data.get("cwd") or data.get("remote_cwd")),
        source=source,
        errors=tuple(errors),
    )


def _targets_from_mapping(
    raw_targets: Any,
    *,
    source: str = "hermes",
) -> list[SshTarget]:
    targets: list[SshTarget] = []
    if isinstance(raw_targets, dict):
        entries = raw_targets.items()
    elif isinstance(raw_targets, list):
        entries = (
            (item.get("alias") or item.get("name") or "", item)
            for item in raw_targets
            if isinstance(item, dict)
        )
    else:
        return targets

    for alias, data in entries:
        target = _coerce_target(str(alias), data, source=source)
        if target is not None:
            targets.append(target)
    return targets


def parse_hermes_ssh_targets(
    config_text: str,
    *,
    source: str = "hermes",
) -> list[SshTarget]:
    """Parse the supported target registry YAML shapes."""

    try:
        import yaml

        data = yaml.safe_load(config_text) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    ssh_section = data.get("ssh")
    if isinstance(ssh_section, dict) and "targets" in ssh_section:
        return _targets_from_mapping(ssh_section.get("targets"), source=source)
    return _targets_from_mapping(data.get("targets"), source=source)


def default_ssh_targets_path() -> Path:
    """Return the profile-scoped SSH target registry path."""

    return get_hermes_home() / "ssh" / "targets.yaml"


def load_ssh_targets(
    config_path: str | Path | None = None,
) -> list[SshTarget]:
    """Load targets from the Hermes registry, never ``~/.ssh/config``."""

    path = (
        Path(config_path).expanduser()
        if config_path is not None
        else default_ssh_targets_path()
    )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_hermes_ssh_targets(text)


def find_ssh_target(
    targets: Iterable[SshTarget],
    alias: str,
) -> SshTarget | None:
    """Return the exact configured alias, if present."""

    wanted = str(alias or "").strip()
    if not wanted:
        return None
    return next((target for target in targets if target.alias == wanted), None)


def validate_ssh_target_for_runtime(target: SshTarget) -> str | None:
    """Return a user-facing error when a target cannot safely be selected."""

    errors = list(target.errors)
    if not target.host:
        errors.append("missing host")
    if not target.user:
        errors.append("missing user")
    if not errors:
        return None
    return (
        f"SSH target `{target.alias}` is invalid: {', '.join(errors)}. "
        "No backend change was made."
    )


def render_ssh_targets(
    targets: Iterable[SshTarget],
    *,
    current_alias: str = "local",
) -> str:
    """Render local plus configured SSH targets without exposing key paths."""

    target_list = list(targets)
    lines = ["SSH backends:"]
    local_mark = " (current)" if current_alias == "local" else ""
    lines.append(f"- `local` — local execution{local_mark}")

    for target in target_list:
        marks: list[str] = []
        if target.alias == current_alias:
            marks.append("current")
        if validate_ssh_target_for_runtime(target):
            marks.append("invalid")
        mark_text = f" ({', '.join(marks)})" if marks else ""
        details: list[str] = []
        if target.host:
            details.append(f"host={target.host}")
        if target.user:
            details.append(f"user={target.user}")
        if target.port:
            details.append(f"port={target.port}")
        if target.cwd:
            details.append(f"cwd={target.cwd}")
        if target.identity_file:
            details.append("identity=[REDACTED_PATH]")
        suffix = f" — {'; '.join(details)}" if details else ""
        lines.append(f"- `{target.alias}` — SSH{mark_text}{suffix}")

    if not target_list:
        lines.extend(
            [
                "",
                "No SSH targets configured.",
                f"Registry: {display_hermes_home()}/ssh/targets.yaml",
            ]
        )
    return "\n".join(lines)
