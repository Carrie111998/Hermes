"""Safe discovery of active dashboard plugin pages.

This module mirrors the dashboard plugin source/activation rules but exposes only
plain page metadata. It never imports plugin code or accepts an origin from a
caller, so REST and both MCP servers can share the same extension catalog.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_SAFE_TAB_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._~-]*(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*$")
_SAFE_PAGE_ID = re.compile(r"[^a-z0-9._-]+")
_RESERVED_PREFIXES = (
    "/api",
    "/assets",
    "/ws",
    "/openapi.json",
    "/redoc",
)
_MARKDOWN_PUNCTUATION = frozenset("[]()<>`\\*_|!#")
_BUILTIN_DASHBOARD_PATHS = frozenset(
    {
        "/",
        "/sessions",
        "/chat",
        "/files",
        "/analytics",
        "/models",
        "/logs",
        "/cron",
        "/skills",
        "/plugins",
        "/config",
        "/env",
        "/system",
        "/channels",
        "/mcp",
        "/pairing",
        "/profiles",
        "/profiles/new",
        "/webhooks",
        "/docs",
    }
)


def safe_dashboard_plugin_tab_path(
    value: Any, *, allow_builtin: bool = False
) -> str | None:
    """Return a safe same-origin React route or ``None``."""
    if not isinstance(value, str) or value != value.strip():
        return None
    if value == "/":
        return value if allow_builtin else None
    if not _SAFE_TAB_PATH.fullmatch(value):
        return None
    lowered = value.casefold()
    if any(lowered == prefix or lowered.startswith(prefix + "/") for prefix in _RESERVED_PREFIXES):
        return None
    if not allow_builtin and lowered in _BUILTIN_DASHBOARD_PATHS:
        return None
    return value


def _safe_text(value: Any, fallback: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split()).strip()
    if not text or len(text) > maximum:
        return fallback
    if any(ord(character) < 32 or character in _MARKDOWN_PUNCTUATION for character in text):
        return fallback
    return text


def _plugin_roots() -> list[tuple[Path, str]]:
    from hermes_cli.config import get_process_hermes_home
    from hermes_cli.plugins import get_bundled_plugins_dir

    bundled = get_bundled_plugins_dir()
    roots = [
        (get_process_hermes_home() / "plugins", "user"),
        (bundled / "memory", "bundled"),
        (bundled, "bundled"),
    ]
    project_plugins_enabled = os.getenv("HERMES_ENABLE_PROJECT_PLUGINS", "").strip().casefold()
    if project_plugins_enabled in {"1", "true", "yes", "on"}:
        roots.append((Path.cwd() / ".hermes" / "plugins", "project"))
    return roots


def list_dashboard_plugin_pages() -> list[dict[str, str]]:
    """Return active, visible, non-overriding plugin tabs as safe page metadata."""
    from hermes_cli.config import cfg_get, load_config
    from hermes_cli.plugins_cmd import _get_disabled_set, _get_enabled_set

    config = load_config()
    hidden = set(cfg_get(config, "dashboard", "hidden_plugins", default=[]) or [])
    enabled = _get_enabled_set()
    disabled = _get_disabled_set()
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    pages: list[dict[str, str]] = []

    for root, source in _plugin_roots():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            manifest_path = child / "dashboard" / "manifest.json"
            if not child.is_dir() or not manifest_path.is_file():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            raw_name = data.get("name", child.name)
            if not isinstance(raw_name, str) or not raw_name or raw_name in seen_names:
                continue
            seen_names.add(raw_name)
            if raw_name in hidden or raw_name in disabled:
                continue
            if source == "user" and raw_name not in enabled:
                continue

            tab_value = data.get("tab")
            tab: dict[str, Any] = tab_value if isinstance(tab_value, dict) else {}
            if tab.get("hidden") or tab.get("override"):
                continue
            path = safe_dashboard_plugin_tab_path(tab.get("path", f"/{raw_name}"))
            if path is None or path in seen_paths:
                continue
            page_id_suffix = _SAFE_PAGE_ID.sub("-", raw_name.casefold()).strip("-._")
            if not page_id_suffix:
                continue
            page_id = f"plugin-{page_id_suffix}"
            if page_id in seen_ids:
                continue

            fallback_label = path.rsplit("/", 1)[-1].replace("-", " ").title()
            label = _safe_text(data.get("label"), fallback_label, maximum=80)
            description = _safe_text(
                data.get("description"),
                f"Open the {label} dashboard extension.",
                maximum=240,
            )
            pages.append(
                {
                    "id": page_id,
                    "label": label,
                    "path": path,
                    "group": "extensions",
                    "description": description,
                }
            )
            seen_ids.add(page_id)
            seen_paths.add(path)

    return pages
