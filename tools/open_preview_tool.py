#!/usr/bin/env python3
"""Open a URL, dev server, or file in the Hermes desktop GUI's preview pane.

Lives in the ``desktop_ui`` toolset, which the GUI gateway enables only for a
session whose source is the desktop app — so the schema never reaches a CLI,
messaging, or cron agent, and it DOES reach a desktop client on a remote/cloud
backend. Emits ``preview.open`` through the shared ``desktop_ui`` bridge; the
renderer opens the pane beside the chat for the window that asked and never
steals focus for a background session.
"""

import json
import re

from tools import desktop_ui
from tools.registry import registry, tool_error


def _normalize_target(raw: str) -> str:
    """Coax a bare host/domain into a fetchable URL; leave paths + schemes alone.

    ``www.cnn.com`` → ``https://www.cnn.com``; ``localhost:3000`` →
    ``http://localhost:3000``. File paths and explicit schemes pass through for
    the renderer's preview normalizer to classify.
    """
    v = raw.strip().strip("`").strip()
    if not v or "://" in v or v.startswith(("/", "./", "../", "~", "file:")):
        return v
    if re.match(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?(/|$)", v, re.I):
        return "http://" + v
    if re.match(r"^[\w.-]+\.[a-z]{2,}(:\d+)?(/.*)?$", v, re.I):
        return "https://" + v
    return v


def open_preview_tool(url: str, label: str = "", new_tab: bool = False) -> str:
    """Ask the desktop GUI to show ``url`` in the preview pane beside the chat."""
    target = _normalize_target(url or "")
    if not target:
        return tool_error(
            "url is required — a web URL (https://…), a localhost dev server, or a "
            "file path to show in the preview pane."
        )

    label = (label or "").strip()
    try:
        ok = desktop_ui.emit(
            "preview.open", {"url": target, "label": label, "new_tab": bool(new_tab)}
        )
    except Exception as exc:
        return tool_error(f"Failed to open the preview pane: {exc}")
    if not ok:
        return tool_error("The preview pane is only available in the Hermes desktop app.")

    return json.dumps(
        {"success": True, "url": target, "label": label, "new_tab": bool(new_tab)},
        ensure_ascii=False,
    )


OPEN_PREVIEW_SCHEMA = {
    "name": "open_preview",
    "description": (
        "Open a page in the in-app browser beside this chat — YOUR browser in "
        "the Hermes desktop app, not just a viewer for the user. Reach for it "
        "whenever a web page would answer the question or finish the job: "
        "checking your own work on a dev server, reading documentation, "
        "verifying a fix rendered, following a link the user pasted. You do "
        "not need to be asked to open it; if you would benefit from looking at "
        "a page, open one. Of course also use it when the user asks to see "
        "something — \"open cnn.com\", \"preview localhost:3000\". "
        "Accepts a web URL (a bare domain like www.cnn.com is fine), a "
        "localhost dev-server URL, or a file path (HTML renders live; other "
        "files show their contents). "
        "You get your OWN browser tab: opening a page never replaces the tab "
        "the user is reading, and you keep the same tab as you work, so open "
        "once and then move around with drive_preview action='navigate'. "
        "Then read_preview reads the page (including its console errors) and "
        "drive_preview clicks, types and navigates in it. "
        "Prefer this over the browser_* tools whenever the user could "
        "reasonably want to watch, or the page is theirs (a local dev server, "
        "an app they are building): this pane is visible to them and costs no "
        "extra browser process. The browser_* tools are for bulk, headless or "
        "background automation that nobody needs to see. "
        "The pane opens for the current window only. To close the pane or a "
        "tab, use close_preview."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "What to preview: a web URL (https://… or a bare domain), a "
                    "localhost URL (localhost:3000), or a file path."
                ),
            },
            "label": {
                "type": "string",
                "description": "Optional tab label; defaults to the target's name.",
            },
            "new_tab": {
                "type": "boolean",
                "description": (
                    "Open in an ADDITIONAL browser tab instead of the one you are "
                    "already using. Only for when you need two pages side by side "
                    "— comparing them, or keeping a reference open while you work. "
                    "Leave it off otherwise: re-using your tab is what keeps the "
                    "user's tab strip readable."
                ),
            },
        },
        "required": ["url"],
    },
}


# Registration removed: consolidated into the `desktop_preview` tool (#95681);
# this module keeps its functions for preview_tool. `new_tab` is local and
# rides along there — a second tab is how one conversation holds two pages.
