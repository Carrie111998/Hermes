"""Computer Use capture-target identity and resolution policy.

This bounded owner serves both the tool response guard and cua-driver window
resolution while ``cua_backend.py`` is fractured under #79937. Legacy functions
and methods remain as compatibility delegates at their original surfaces.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

_CAPTURE_SCREEN_SENTINELS = frozenset({
    "screen", "desktop", "fullscreen", "full-screen", "all",
})

_CAPTURE_BROWSER_IDENTITY_TOKENS = frozenset({
    "brave", "chrome", "chromium", "edge", "firefox", "msedge",
    "opera", "safari", "vivaldi",
})

_CAPTURE_APP_ALIASES = {
    "chrome": "chrome",
    "google-chrome": "chrome",
    "google-chrome-stable": "chrome",
    "chrome-browser": "chrome",
    "com-google-chrome": "chrome",
    "firefox": "firefox",
    "mozilla-firefox": "firefox",
    "org-mozilla-firefox": "firefox",
    "edge": "edge",
    "msedge": "edge",
    "microsoft-edge": "edge",
    "com-microsoft-edgemac": "edge",
    "chromium": "chromium",
    "chromium-browser": "chromium",
}


def canonical_capture_app_name(value: Any) -> str:
    """Canonicalize exact app identities without accepting title substrings."""
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[\s_]+", "-", value.strip().casefold()).strip("-")
    if normalized.endswith(".app"):
        normalized = normalized[:-4]
    return _CAPTURE_APP_ALIASES.get(normalized, normalized)


def capture_app_matches(requested_app: Any, returned_app: Any) -> bool:
    """Accept app-name variants without trusting unrelated browser titles."""
    requested = canonical_capture_app_name(requested_app)
    if requested in _CAPTURE_SCREEN_SENTINELS:
        return True

    returned = canonical_capture_app_name(returned_app)
    if not requested or not returned:
        return False
    if requested == returned:
        return True

    requested_tokens = tuple(re.findall(r"[a-z0-9]+", requested))
    returned_tokens = tuple(re.findall(r"[a-z0-9]+", returned))
    requested_browsers = set(requested_tokens) & _CAPTURE_BROWSER_IDENTITY_TOKENS
    returned_browsers = set(returned_tokens) & _CAPTURE_BROWSER_IDENTITY_TOKENS
    if returned_browsers - requested_browsers:
        return False

    if not requested_tokens or len(requested_tokens) > len(returned_tokens):
        return False
    width = len(requested_tokens)
    return any(
        all(
            returned_token.startswith(requested_token)
            for requested_token, returned_token in zip(
                requested_tokens, returned_tokens[start:start + width]
            )
        )
        for start in range(len(returned_tokens) - width + 1)
    )


def match_windows_for_app(
    windows: list[dict[str, Any]],
    app: str,
    *,
    list_apps: Callable[[], list[dict[str, Any]]],
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Resolve ``app=`` through exact names before convenience substrings."""
    app_lower = app.strip().lower()
    if not app_lower:
        return []

    direct_exact = [
        window
        for window in windows
        if app_lower == str(window.get("app_name", "")).strip().lower()
    ]
    if direct_exact:
        return direct_exact

    log = logger or logging.getLogger(__name__)
    try:
        running_apps = list_apps()
    except Exception as exc:
        log.debug("computer_use list_apps fallback failed for %r: %s", app, exc)
        running_apps = []

    exact_pids: set[int] = set()
    partial_pids: set[int] = set()
    for raw_app in running_apps:
        if not isinstance(raw_app, dict) or raw_app.get("running") is False:
            continue
        raw_pid = raw_app.get("pid")
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, (int, str)):
            continue
        try:
            pid = int(raw_pid)
        except ValueError:
            continue
        if pid <= 0:
            continue

        aliases = {
            value.strip().lower()
            for key in ("bundle_id", "bundleId", "name", "app_name", "display_name")
            if isinstance((value := raw_app.get(key)), str) and value.strip()
        }
        if app_lower in aliases:
            exact_pids.add(pid)
        elif any(app_lower in alias for alias in aliases):
            partial_pids.add(pid)

    metadata_exact = [window for window in windows if window.get("pid") in exact_pids]
    if exact_pids:
        return metadata_exact

    direct_partial = [
        window
        for window in windows
        if app_lower in str(window.get("app_name", "")).lower()
    ]
    if direct_partial:
        return direct_partial

    metadata_partial = [
        window for window in windows if window.get("pid") in partial_pids
    ]
    if metadata_partial:
        return metadata_partial

    return [
        window
        for window in windows
        if not str(window.get("app_name", "")).strip()
        and app_lower in str(window.get("title", "")).lower()
    ]
