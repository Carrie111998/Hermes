"""Outbound URL reachability and success-claim receipt policy.

The plugin is intentionally target-scoped.  It observes completed tool calls by
(session_id, turn_id), then evaluates text at the adapter boundary immediately
before a protected recipient can see it.  Prompt instructions are not part of
the enforcement path.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_RECEIPT_RE = re.compile(r"(?mi)^Receipt:\s*([^\n]+)\s*$")
_OUTPUT_RE = re.compile(r"(?mi)^Passing output:\s*([^\n]+)\s*$")
_DEFAULT_SUCCESS_TERMS = (
    "fixed", "working", "resolved", "live", "ready", "deployed", "verified"
)
_LIVE_BUILD_RE = re.compile(r"(?:\bBUILD_ID\s*=\s*\S+|\blive build\s+\S+)", re.IGNORECASE)
_PASS_RE = re.compile(r"(?:\bPASS\b|\bpassed\b|\bsuccess\b|\bexit_code\s*[=:]\s*0\b)", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>'\""
_LINKEDIN_MARKERS = ("authorize=PASS", "post=PASS", "public_url=PASS", "fetch=PASS")
_MAX_RECEIPTS = 256


@dataclass(frozen=True)
class ToolReceipt:
    session_id: str
    turn_id: str
    tool_name: str
    command: str
    output: str
    status: str


_receipts: list[ToolReceipt] = []
_lock = threading.RLock()


def clear_receipts_for_tests() -> None:
    with _lock:
        _receipts.clear()


def _stringify_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    return repr(result)


def record_tool_result(
    *,
    session_id: str = "",
    turn_id: str = "",
    tool_name: str = "",
    args: Mapping[str, Any] | None = None,
    result: Any = None,
    status: str = "",
    **_: Any,
) -> None:
    if not session_id or not turn_id:
        return
    command = ""
    if isinstance(args, Mapping):
        command = str(args.get("command") or args.get("code") or "")
    receipt = ToolReceipt(
        session_id=str(session_id),
        turn_id=str(turn_id),
        tool_name=str(tool_name),
        command=command,
        output=_stringify_result(result),
        status=str(status or ""),
    )
    with _lock:
        _receipts.append(receipt)
        if len(_receipts) > _MAX_RECEIPTS:
            del _receipts[: len(_receipts) - _MAX_RECEIPTS]


def _extract_urls(content: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_RE.finditer(content or ""):
        url = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        if url and url not in urls:
            urls.append(url)
    return urls


def fetch_url_live(url: str, timeout: float = 10.0) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"ok": False, "status": None, "final_url": "", "error": "unsupported or malformed URL"}
    request = Request(
        url,
        headers={"User-Agent": "Hermes-Outbound-Link-Gate/1.0", "Accept": "*/*"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=max(0.1, float(timeout))) as response:
            status = int(getattr(response, "status", response.getcode()))
            final_url = str(response.geturl())
            response.read(1024)
    except HTTPError as exc:
        status = int(exc.code)
        final_url = str(exc.geturl() or url)
        # OAuth callbacks and protected resources can be reachable while a bare
        # probe lacks state/auth.  Dead/not-gone and server failures still block.
        ok = status not in {404, 410} and status < 500
        return {
            "ok": ok,
            "status": status,
            "final_url": final_url,
            "error": "" if ok else f"HTTP {status}",
        }
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "status": None, "final_url": "", "error": str(exc)}
    ok = status not in {404, 410} and status < 500
    return {
        "ok": ok,
        "status": status,
        "final_url": final_url,
        "error": "" if ok else f"HTTP {status}",
    }


def _target_is_protected(platform: str, chat_id: str, settings: Mapping[str, Any]) -> bool:
    target = f"{str(platform).lower()}:{chat_id}"
    raw = settings.get("protected_targets", [])
    return isinstance(raw, list) and target in {str(item).lower() for item in raw}


def _contains_success_claim(content: str, settings: Mapping[str, Any]) -> bool:
    terms = settings.get("success_terms", list(_DEFAULT_SUCCESS_TERMS))
    if not isinstance(terms, list):
        terms = list(_DEFAULT_SUCCESS_TERMS)
    return any(
        re.search(rf"(?<![A-Za-z]){re.escape(str(term))}(?![A-Za-z])", content, re.IGNORECASE)
        for term in terms
        if str(term).strip()
    )


def _safe_dead_url_label(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return f"{parsed.netloc} {path}".strip()


def _receipt_for_turn(session_id: str, turn_id: str, output: str) -> ToolReceipt | None:
    if not session_id or not turn_id or not output:
        return None
    with _lock:
        candidates = tuple(_receipts)
    for receipt in reversed(candidates):
        if receipt.session_id != session_id or receipt.turn_id != turn_id:
            continue
        if receipt.status.lower() not in {"success", "ok", "completed"}:
            continue
        if output not in receipt.output:
            continue
        return receipt
    return None


def _unverified(original: str, reason: str, missing: str) -> dict[str, str]:
    return {
        "action": "rewrite",
        "reason": reason,
        "content": f"UNVERIFIED\n\nMissing: {missing}\n\n{original}",
    }


def gate_outbound_message(
    *,
    platform: str,
    chat_id: str,
    content: str,
    metadata: Mapping[str, Any] | None,
    settings: Mapping[str, Any],
    fetcher: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    if not _target_is_protected(platform, chat_id, settings):
        return {"action": "allow"}

    text = str(content or "")
    fetch = fetcher or (
        lambda url: fetch_url_live(url, float(settings.get("fetch_timeout_seconds", 10.0)))
    )
    failed: list[tuple[str, Mapping[str, Any]]] = []
    for url in _extract_urls(text):
        try:
            result = fetch(url)
        except Exception as exc:  # policy dependencies fail closed
            result = {"ok": False, "status": None, "error": str(exc), "final_url": ""}
        if not result.get("ok"):
            failed.append((url, result))
    if failed:
        lines = ["LINK CHECK BLOCKED", "", "The original message was not delivered because a URL failed its immediate live fetch:"]
        for url, result in failed:
            detail = result.get("error") or (
                f"HTTP {result.get('status')}" if result.get("status") is not None else "fetch failed"
            )
            lines.append(f"- {_safe_dead_url_label(url)}: {detail}")
        return {"action": "rewrite", "reason": "url_check_failed", "content": "\n".join(lines)}

    if not _contains_success_claim(text, settings):
        return {"action": "allow"}

    receipt_match = _RECEIPT_RE.search(text)
    output_match = _OUTPUT_RE.search(text)
    if not receipt_match or not output_match:
        return _unverified(
            text,
            "claim_receipt_missing",
            "a named same-turn ratchet or journey receipt and its passing output",
        )
    receipt_name = receipt_match.group(1).strip()
    passing_output = output_match.group(1).strip()
    if not re.search(r"(?:ratchet|journey)", receipt_name, re.IGNORECASE):
        return _unverified(text, "claim_receipt_missing", "a named ratchet or journey check")
    if not _LIVE_BUILD_RE.search(passing_output) or not _PASS_RE.search(passing_output):
        return _unverified(text, "claim_receipt_missing", "passing output naming the live BUILD_ID")

    meta = metadata if isinstance(metadata, Mapping) else {}
    receipt = _receipt_for_turn(
        str(meta.get("_hermes_session_id") or ""),
        str(meta.get("_hermes_turn_id") or ""),
        passing_output,
    )
    if receipt is None:
        return _unverified(
            text,
            "claim_receipt_missing",
            "passing output produced by a tool in this same session turn",
        )

    if re.search(r"linkedin", text, re.IGNORECASE):
        missing = [marker for marker in _LINKEDIN_MARKERS if marker not in passing_output]
        if receipt_name.lower() != "linkedin-public-post-journey" or missing:
            return _unverified(
                text,
                "linkedin_journey_incomplete",
                "LinkedIn receipt linkedin-public-post-journey with " + ", ".join(_LINKEDIN_MARKERS),
            )
    return {"action": "allow"}


def _build_settings(ctx) -> dict[str, Any]:
    return {
        "protected_targets": ctx.get_config("protected_targets", []),
        "success_terms": ctx.get_config("success_terms", list(_DEFAULT_SUCCESS_TERMS)),
        "fetch_timeout_seconds": ctx.get_config("fetch_timeout_seconds", 10.0),
    }


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", record_tool_result)

    def _pre_gateway_send(
        platform: str = "",
        chat_id: str = "",
        content: str = "",
        metadata: Mapping[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, str]:
        return gate_outbound_message(
            platform=platform,
            chat_id=chat_id,
            content=content,
            metadata=metadata,
            settings=_build_settings(ctx),
        )

    ctx.register_hook("pre_gateway_send", _pre_gateway_send)
