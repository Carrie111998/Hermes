"""Canonical dashboard page metadata and safe deep-link construction.

This module is intentionally dependency-free so it can be reused by the web API,
the Hermes MCP server, and lightweight tooling. Generated links never contain
session tokens or other credentials; dashboard authentication remains in the
browser's existing header/cookie channel.
"""

from __future__ import annotations

import ipaddress
import os
import string
from typing import Final
from urllib.parse import unquote, urlsplit, urlunsplit

DashboardPage = dict[str, str]

DEFAULT_DASHBOARD_BASE_URL: Final = "http://127.0.0.1:9119"

_SAFE_BASE_PATH_CHARACTERS: Final = frozenset(
    string.ascii_letters + string.digits + "-._~/"
)
_SAFE_IPV6_SCOPE_CHARACTERS: Final = frozenset(
    string.ascii_letters + string.digits + "-._"
)


def _is_safe_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    if "%" in hostname:
        address, scope = hostname.split("%", 1)
        if not scope or any(
            character not in _SAFE_IPV6_SCOPE_CHARACTERS for character in scope
        ):
            return False
        try:
            return isinstance(ipaddress.ip_address(address), ipaddress.IPv6Address)
        except ValueError:
            return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_hostname.split(".")
    return all(
        label
        and not label.startswith("-")
        and not label.endswith("-")
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )

_DASHBOARD_PAGES: Final[tuple[DashboardPage, ...]] = (
    {
        "id": "sessions",
        "label": "Sessions",
        "path": "/sessions",
        "group": "workspace",
        "description": "Browse, search, resume, export, and manage conversations.",
    },
    {
        "id": "chat",
        "label": "Chat",
        "path": "/chat",
        "group": "workspace",
        "description": "Open the live Hermes conversation workspace.",
    },
    {
        "id": "analytics",
        "label": "Analytics",
        "path": "/analytics",
        "group": "workspace",
        "description": "Review usage, token, provider, and session analytics.",
    },
    {
        "id": "files",
        "label": "Files",
        "path": "/files",
        "group": "workspace",
        "description": "Browse files available to Hermes.",
    },
    {
        "id": "cron",
        "label": "Scheduled jobs",
        "path": "/cron",
        "group": "automations",
        "description": "Create and manage scheduled jobs and automations.",
    },
    {
        "id": "webhooks",
        "label": "Webhooks",
        "path": "/webhooks",
        "group": "automations",
        "description": "Configure inbound webhook automations.",
    },
    {
        "id": "channels",
        "label": "Channels",
        "path": "/channels",
        "group": "integrations",
        "description": "Connect and manage messaging channels.",
    },
    {
        "id": "mcp",
        "label": "MCP servers",
        "path": "/mcp",
        "group": "integrations",
        "description": "Connect, test, and manage Model Context Protocol servers.",
    },
    {
        "id": "pairing",
        "label": "Pairing",
        "path": "/pairing",
        "group": "integrations",
        "description": "Review and manage pending channel pairing requests.",
    },
    {
        "id": "models",
        "label": "Models",
        "path": "/models",
        "group": "manage",
        "description": "Configure model providers, defaults, and fallbacks.",
    },
    {
        "id": "skills",
        "label": "Skills",
        "path": "/skills",
        "group": "manage",
        "description": "Browse and manage reusable Hermes skills.",
    },
    {
        "id": "plugins",
        "label": "Plugins",
        "path": "/plugins",
        "group": "manage",
        "description": "Install, enable, and manage Hermes plugins.",
    },
    {
        "id": "profiles",
        "label": "Profiles",
        "path": "/profiles",
        "group": "manage",
        "description": "Create and manage isolated Hermes profiles.",
    },
    {
        "id": "config",
        "label": "Configuration",
        "path": "/config",
        "group": "manage",
        "description": "Edit supported Hermes configuration settings.",
    },
    {
        "id": "env",
        "label": "API keys",
        "path": "/env",
        "group": "manage",
        "description": "Manage environment-backed provider credentials.",
    },
    {
        "id": "logs",
        "label": "Logs",
        "path": "/logs",
        "group": "manage",
        "description": "Inspect local Hermes logs and diagnostics.",
    },
    {
        "id": "system",
        "label": "System",
        "path": "/system",
        "group": "manage",
        "description": "Review runtime and host system status.",
    },
    {
        "id": "docs",
        "label": "Documentation",
        "path": "/docs",
        "group": "manage",
        "description": "Open Hermes documentation and reference links.",
    },
)

_PAGE_BY_ID: Final = {page["id"]: page for page in _DASHBOARD_PAGES}


def list_dashboard_pages(query: str | None = None) -> list[DashboardPage]:
    """Return safe public page metadata, optionally filtered by free text."""
    needle = (query or "").strip().casefold()
    pages = _DASHBOARD_PAGES
    if needle:
        pages = tuple(
            page
            for page in pages
            if needle
            in " ".join(
                (page["id"], page["label"], page["description"], page["group"])
            ).casefold()
        )
    return [dict(page) for page in pages]


def build_dashboard_link(
    page_id: str,
    base_url: str = DEFAULT_DASHBOARD_BASE_URL,
) -> DashboardPage:
    """Build a credential-free URL for one canonical dashboard page.

    ``base_url`` may include a reverse-proxy path prefix, but it must be a plain
    HTTP(S) origin/path with no credentials, query, or fragment. Unknown page IDs
    are rejected instead of being interpolated into a path.
    """
    page = _PAGE_BY_ID.get(page_id.strip().casefold())
    if page is None:
        raise KeyError(page_id)

    parsed = urlsplit(base_url.strip())
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(
            "base_url must use a valid HTTP(S) host and port"
        ) from exc

    decoded_path = unquote(parsed.path)
    path_segments = decoded_path.split("/")
    unsafe_path = (
        "\\" in decoded_path
        or "//" in decoded_path
        or any(segment in {".", ".."} for segment in path_segments)
        or any(ord(character) < 32 for character in decoded_path)
        or any(character not in _SAFE_BASE_PATH_CHARACTERS for character in decoded_path)
    )
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not _is_safe_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or unsafe_path
    ):
        raise ValueError("base_url must be a credential-free HTTP(S) origin/path")

    prefix = parsed.path.rstrip("/")
    route_path = page["path"]
    url = urlunsplit((parsed.scheme, parsed.netloc, f"{prefix}{route_path}", "", ""))
    result = dict(page)
    result["url"] = url
    result["markdown"] = f"[Open {page['label']}]({url})"
    return result


def build_configured_dashboard_link(page_id: str) -> DashboardPage:
    """Build a link from operator-controlled process configuration.

    MCP callers cannot choose the origin. Reverse-proxy deployments may set
    ``HERMES_DASHBOARD_URL`` in the MCP server environment; local deployments
    use the loopback dashboard by default.
    """
    base_url = os.environ.get("HERMES_DASHBOARD_URL", DEFAULT_DASHBOARD_BASE_URL)
    return build_dashboard_link(page_id, base_url)
