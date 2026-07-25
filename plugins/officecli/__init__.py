"""OfficeCLI plugin — registers a small set of safe Office tools for Hermes.

Capabilities are intentionally capped to:
create / view / add / get / close on a single workbook path per turn.

Heavier workflows (template rendering, watch servers, batch automation)
are left for later if needed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


def _office_bin() -> str | None:
    candidates = [
        os.environ.get("OFFICE_BIN"),
        shutil.which("officecli"),
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "OfficeCLI", "officecli"),
        "/usr/local/bin/officecli",
        os.path.expanduser("~/.local/bin/officecli"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _run(cmd: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=180, stdin=subprocess.DEVNULL)
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(exc)}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "code": -2, "stdout": "", "stderr": f"timeout after {exc.timeout}s"}


def _officecli_tool(name, schema, handler):
    return name, schema, handler, "📁"


def officecli_create(args: dict[str, Any], **_: Any) -> str:
    bin_ = _office_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "officecli binary not found"}, ensure_ascii=False)
    path = str(args.get("path", "")).strip()
    ftype = str(args.get("type", "")).strip().lower()
    if not path or ftype not in {"docx", "xlsx", "pptx"}:
        return json.dumps({"ok": False, "error": "path and type=docx|xlsx|pptx required"}, ensure_ascii=False)
    res = _run([bin_, "create", path, "--type", ftype])
    return json.dumps({"ok": res["ok"], "code": res["code"], "path": path, "type": ftype, "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def officecli_view(args: dict[str, Any], **_: Any) -> str:
    bin_ = _office_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "officecli binary not found"}, ensure_ascii=False)
    path = str(args.get("path", "")).strip()
    mode = str(args.get("mode", "html")).strip().lower()
    if not path:
        return json.dumps({"ok": False, "error": "path is required"}, ensure_ascii=False)
    res = _run([bin_, "view", path, mode])
    return json.dumps({"ok": res["ok"], "code": res["code"], "path": path, "mode": mode, "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def officecli_add(args: dict[str, Any], **_: Any) -> str:
    bin_ = _office_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "officecli binary not found"}, ensure_ascii=False)
    path = str(args.get("path", "")).strip()
    target = str(args.get("target", "/")).strip()
    kind = str(args.get("kind", "")).strip().lower()
    props = args.get("props", {}) or {}
    if not path or not kind:
        return json.dumps({"ok": False, "error": "path and kind are required"}, ensure_ascii=False)
    cmd = [bin_, "add", path, target, "--type", kind]
    for k, v in props.items():
        cmd += ["--prop", f"{k}={v}"]
    res = _run(cmd)
    return json.dumps({"ok": res["ok"], "code": res["code"], "path": path, "target": target, "kind": kind, "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def officecli_get(args: dict[str, Any], **_: Any) -> str:
    bin_ = _office_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "officecli binary not found"}, ensure_ascii=False)
    path = str(args.get("path", "")).strip()
    target = str(args.get("target", "/")).strip()
    if not path:
        return json.dumps({"ok": False, "error": "path is required"}, ensure_ascii=False)
    cmd = [bin_, "get", path, target, "--json"]
    res = _run(cmd)
    return json.dumps({"ok": res["ok"], "code": res["code"], "path": path, "target": target, "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def officecli_close(args: dict[str, Any], **_: Any) -> str:
    bin_ = _office_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "officecli binary not found"}, ensure_ascii=False)
    path = str(args.get("path", "")).strip()
    if not path:
        return json.dumps({"ok": False, "error": "path is required"}, ensure_ascii=False)
    res = _run([bin_, "close", path])
    return json.dumps({"ok": res["ok"], "code": res["code"], "path": path, "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


_TOOLS = [
    _officecli_tool(
        "officecli_create",
        {
            "name": "officecli_create",
            "description": "Create a blank Office document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Output file path."},
                    "type": {"type": "string", "enum": ["docx", "xlsx", "pptx"], "description": "Document type."},
                },
                "required": ["path", "type"],
            },
        },
        officecli_create,
    ),
    _officecli_tool(
        "officecli_view",
        {
            "name": "officecli_view",
            "description": "Render an Office document to html/screenshot/outline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Document path."},
                    "mode": {"type": "string", "enum": ["html", "screenshot", "outline"], "description": "Output mode.", "default": "html"},
                },
                "required": ["path"],
            },
        },
        officecli_view,
    ),
    _officecli_tool(
        "officecli_add",
        {
            "name": "officecli_add",
            "description": "Add content to an Office document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Document path."},
                    "target": {"type": "string", "description": "XPath-like target.", "default": "/"},
                    "kind": {"type": "string", "description": "Element kind, e.g. slide, shape, table."},
                    "props": {"type": "object", "description": "Key/value properties for the new element.", "default": {}},
                },
                "required": ["path", "kind"],
            },
        },
        officecli_add,
    ),
    _officecli_tool(
        "officecli_get",
        {
            "name": "officecli_get",
            "description": "Read document elements by target path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Document path."},
                    "target": {"type": "string", "description": "XPath-like target.", "default": "/"},
                },
                "required": ["path"],
            },
        },
        officecli_get,
    ),
    _officecli_tool(
        "officecli_close",
        {
            "name": "officecli_close",
            "description": "Close a document session when done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Document path."}
                },
                "required": ["path"],
            },
        },
        officecli_close,
    ),
]


def register(ctx) -> None:
    for _name, _schema, _handler, _emoji in _TOOLS:
        try:
            ctx.register_tool(
                name=_name,
                toolset="officecli",
                schema=_schema,
                handler=_handler,
                emoji=_emoji,
            )
        except TypeError:
            ctx.register_tool(_name, _schema, _handler)
