"""Skillven registry publish provider for ``hermes skills publish --to skillven``.

Footprint Ladder rung-2 (CLI capability, no new core tool). Orchestration lives
in ``do_skillven_publish``; the bundle builder and HTTP client are kept in this
standalone module per the task's "small private helper or separate module"
requirement so ``hermes_cli/skills_hub.py`` only gets a thin dispatch branch.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx
import yaml

DEFAULT_REGISTRY = "https://api.skillven.com"
TOKEN_ENV_VAR = "SKILLVEN_PUBLISH_TOKEN"

# Mirrors Skillven's documented publish-API bundle contract exactly.
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_FILE_COUNT = 64
MAX_PATH_DEPTH = 8
MAX_PATH_LEN = 255

REQUIRED_METADATA_FIELDS = (
    "name",
    "version",
    "description",
    "license",
    "compatible_agents",
    "required_tools",
    "permissions",
    "risk_level",
)
_METADATA_STRING_FIELDS = ("name", "version", "description", "license", "risk_level")
_METADATA_LIST_FIELDS = ("compatible_agents", "required_tools", "permissions")
_RISK_LEVELS = {"low", "medium", "high"}


class SkillvenPublishError(Exception):
    """Any Skillven publish failure. Message text is always safe to print — never token/body dumps."""


@dataclass
class SkillvenBundle:
    metadata: dict
    files: list  # list[tuple[str, bytes]] — (posix-relative path, content)


def _validate_relative_path(root: Path, rel: Path) -> Path:
    """Reject absolute paths, ``..`` escapes, NUL bytes, and symlinks under ``root``."""
    raw = str(rel)
    if "\x00" in raw:
        raise SkillvenPublishError(f"Rejected path containing a NUL byte: {raw!r}")
    if rel.is_absolute():
        raise SkillvenPublishError(f"Rejected absolute path in bundle: {rel}")
    if ".." in rel.parts:
        raise SkillvenPublishError(f"Rejected path escaping the skill directory: {rel}")

    full = root / rel
    if full.is_symlink():
        raise SkillvenPublishError(f"Rejected symlink in bundle: {rel}")

    resolved_root = root.resolve()
    resolved = full.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise SkillvenPublishError(
            f"Rejected path escaping the skill directory: {rel}"
        ) from None
    return resolved


def _collect_bundle_files(root: Path) -> list:
    """Return sorted relative Paths of allowed regular files under ``root``."""
    root = root.resolve()
    out = []
    for full in sorted(root.rglob("*")):
        if full.is_dir():
            continue
        rel = full.relative_to(root)
        _validate_relative_path(root, rel)
        if not full.is_file():
            raise SkillvenPublishError(f"Rejected non-regular file in bundle: {rel}")
        out.append(rel)
    return out


def _load_metadata(root: Path) -> dict:
    meta_path = root / "metadata.yaml"
    if not meta_path.exists():
        raise SkillvenPublishError(
            "metadata.yaml is required for --to skillven but was not found."
        )
    try:
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SkillvenPublishError(f"metadata.yaml is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise SkillvenPublishError("metadata.yaml must contain a YAML mapping.")

    missing = [f for f in REQUIRED_METADATA_FIELDS if f not in data]
    if missing:
        raise SkillvenPublishError(
            "metadata.yaml is missing required field(s): " + ", ".join(missing)
        )
    for field in _METADATA_STRING_FIELDS:
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise SkillvenPublishError(
                f"metadata.yaml field '{field}' must be a non-empty string."
            )
    for field in _METADATA_LIST_FIELDS:
        if not isinstance(data.get(field), list):
            raise SkillvenPublishError(f"metadata.yaml field '{field}' must be a list.")
    if data["risk_level"] not in _RISK_LEVELS:
        raise SkillvenPublishError(
            f"metadata.yaml field 'risk_level' must be one of: {', '.join(sorted(_RISK_LEVELS))}."
        )
    return data


def _check_skill_md_frontmatter_free(skill_md_text: str) -> None:
    text = skill_md_text.lstrip("﻿")
    if re.match(r"^\s*---\s*\n", text):
        raise SkillvenPublishError(
            "SKILL.md for --to skillven must not contain YAML frontmatter; "
            "Skillven renders frontmatter from metadata.yaml at publish time."
        )


def _check_bundle_limits(files: list) -> None:
    if len(files) > MAX_BUNDLE_FILE_COUNT:
        raise SkillvenPublishError(
            f"Too many files in bundle (> {MAX_BUNDLE_FILE_COUNT}): {len(files)}"
        )
    total = 0
    for rel, content in files:
        if len(rel.encode("utf-8")) > MAX_PATH_LEN:
            raise SkillvenPublishError(f"Path too long (> {MAX_PATH_LEN} bytes): {rel}")
        if len(rel.split("/")) > MAX_PATH_DEPTH:
            raise SkillvenPublishError(
                f"Path too deep (> {MAX_PATH_DEPTH} segments): {rel}"
            )
        if len(content) > MAX_FILE_BYTES:
            raise SkillvenPublishError(
                f"File too large (> {MAX_FILE_BYTES} bytes): {rel}"
            )
        total += len(content)
    if total > MAX_BUNDLE_BYTES:
        raise SkillvenPublishError(
            f"Bundle too large (> {MAX_BUNDLE_BYTES} bytes): {total} bytes"
        )


def build_skillven_payload(root) -> dict:
    """Build the exact JSON-able payload sent to both the preview and publish endpoints."""
    root = Path(root)
    skill_md_path = root / "SKILL.md"
    if not skill_md_path.exists():
        raise SkillvenPublishError("SKILL.md is required but was not found.")
    _check_skill_md_frontmatter_free(skill_md_path.read_text(encoding="utf-8"))

    metadata = _load_metadata(root)

    rel_paths = _collect_bundle_files(root)
    if Path("SKILL.md") not in rel_paths or Path("metadata.yaml") not in rel_paths:
        raise SkillvenPublishError(
            "Bundle must include both SKILL.md and metadata.yaml."
        )

    files = [(rel.as_posix(), (root / rel).read_bytes()) for rel in rel_paths]
    _check_bundle_limits(files)

    payload_files = [
        {"path": rel, "content_base64": base64.b64encode(content).decode("ascii")}
        for rel, content in files
    ]
    return {"metadata": metadata, "files": payload_files}


def get_publish_token() -> str:
    from agent.secret_scope import get_secret

    token = get_secret(TOKEN_ENV_VAR)
    if not token:
        raise SkillvenPublishError(
            f"{TOKEN_ENV_VAR} is not set. Add it to your .env (never commit it), then retry: "
            "hermes skills publish <path> --to skillven"
        )
    return token


def _confirm(prompt: str) -> bool:
    try:
        resp = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return resp in {"y", "yes"}


class SkillvenClient:
    """Thin Skillven HTTP wrapper. ``transport`` is injectable for tests (httpx.MockTransport)."""

    def __init__(
        self, registry: str, token: str, *, transport=None, timeout: float = 30.0
    ):
        self._client = httpx.Client(
            base_url=registry.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def preview(self, payload: dict) -> dict:
        return self._request("POST", "/v1/skills/preview", payload)

    def publish(self, payload: dict) -> dict:
        return self._request("POST", "/v1/skills", payload)

    def get_version(self, name: str, version: str) -> dict:
        return self._request("GET", f"/v1/skills/{name}/versions/{version}", None)

    def _request(self, method: str, path: str, payload: Optional[dict]) -> dict:
        try:
            resp = self._client.request(method, path, json=payload)
        except httpx.TimeoutException:
            raise SkillvenPublishError(
                "Skillven request timed out. Please retry."
            ) from None
        except httpx.HTTPError:
            raise SkillvenPublishError(
                "Network error contacting the Skillven registry."
            ) from None

        if resp.status_code == 401:
            raise SkillvenPublishError(
                "Skillven rejected the token as invalid (401). Check SKILLVEN_PUBLISH_TOKEN."
            )
        if resp.status_code == 403:
            raise SkillvenPublishError(
                "Skillven denied the request (403): you may not own this skill name."
            )
        if resp.status_code == 409:
            raise SkillvenPublishError(
                "Skillven reports this name/version already exists (409 duplicate version)."
            )
        if resp.status_code >= 400:
            code, message = self._safe_error_fields(resp)
            raise SkillvenPublishError(
                f"Skillven returned {resp.status_code} ({code}): {message}"
            )

        try:
            return resp.json()
        except ValueError:
            raise SkillvenPublishError(
                "Skillven returned an invalid (non-JSON) response."
            ) from None

    @staticmethod
    def _safe_error_fields(resp: httpx.Response):
        try:
            data = resp.json()
        except ValueError:
            return ("unknown", "no further details")
        if not isinstance(data, dict):
            return ("unknown", "no further details")
        code = data.get("code") or data.get("error") or "unknown"
        message = data.get("message") or "no further details"
        return (str(code)[:200], str(message)[:500])


def do_skillven_publish(
    path,
    *,
    registry: str,
    scan_result,
    console,
    confirm: Optional[Callable[[str], bool]] = None,
    client_factory: Optional[Callable[[str, str], SkillvenClient]] = None,
) -> None:
    """Preview -> confirm -> publish -> read-back. Caller must have already run
    ``scan_skill(path, source="self")``; a dangerous verdict is re-checked here
    defensively so this function never reaches the network without it."""
    if scan_result is None or getattr(scan_result, "verdict", None) == "dangerous":
        console.print("[bold red]Cannot publish a skill with DANGEROUS verdict.[/]\n")
        return

    registry = (registry or DEFAULT_REGISTRY).rstrip("/")
    confirm = confirm or _confirm
    client_factory = client_factory or SkillvenClient

    try:
        payload = build_skillven_payload(path)
        token = get_publish_token()
    except SkillvenPublishError as exc:
        console.print(f"[bold red]Error:[/] {exc}\n")
        return

    metadata = payload["metadata"]
    name, version = metadata["name"], metadata["version"]

    client = client_factory(registry, token)
    try:
        try:
            preview = client.preview(payload)
        except SkillvenPublishError as exc:
            console.print(f"[bold red]Preview failed:[/] {exc}\n")
            return

        warnings = preview.get("warnings") or []
        if not preview.get("preview") or not preview.get("installable") or warnings:
            console.print(
                "[bold red]Skillven preview did not pass; aborting before any real publish.[/]"
            )
            for w in warnings[:20]:
                console.print(f"  [yellow]- {w}[/]")
            return

        console.print(f"[bold]Skillven preview OK[/] for '{name}' v{version}")
        console.print(f"  files: {len(payload['files'])}")
        console.print(
            f"  license: {metadata.get('license')}  risk_level: {metadata.get('risk_level')}"
        )

        if not confirm(f"Publish '{name}' v{version} to Skillven ({registry})?"):
            console.print("[yellow]Aborted — no real publish sent.[/]\n")
            return

        try:
            published = client.publish(payload)
        except SkillvenPublishError as exc:
            console.print(f"[bold red]Publish failed:[/] {exc}\n")
            return

        pub_name = published.get("name", name)
        pub_version = published.get("version", version)
        console.print("[bold green]Published to Skillven[/]")
        console.print(f"  name: {pub_name}")
        console.print(f"  version: {pub_version}")
        console.print(f"  installable: {published.get('installable')}")
        console.print(
            f"  source_skill_md_sha256: {published.get('source_skill_md_sha256')}"
        )
        console.print(f"  files: {len(published.get('files', payload['files']))}")

        try:
            confirmed = client.get_version(pub_name, pub_version)
            console.print(
                f"[bold]Read-back confirmed:[/] version={confirmed.get('version')} "
                f"installable={confirmed.get('installable')}\n"
            )
        except SkillvenPublishError as exc:
            console.print(
                f"[yellow]Warning:[/] could not read back published version: {exc}\n"
            )
    finally:
        client.close()
