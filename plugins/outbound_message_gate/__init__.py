"""Outbound URL reachability and success-claim receipt policy.

The plugin is intentionally target-scoped.  It observes completed tool calls by
(session_id, turn_id), then evaluates text at the adapter boundary immediately
before a protected recipient can see it.  Prompt instructions are not part of
the enforcement path.
"""

from __future__ import annotations

import re
import ipaddress
import http.client
import socket
import ssl
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit


_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_RECEIPT_RE = re.compile(r"(?mi)^Receipt:\s*([^\n]+)\s*$")
_OUTPUT_RE = re.compile(r"(?mi)^Passing output:\s*([^\n]+)\s*$")
_DEFAULT_SUCCESS_TERMS = (
    "fixed", "working", "resolved", "live", "ready", "deployed", "verified",
    "done", "complete", "completed", "operational",
)
_REQUIRED_SUCCESS_PATTERNS = (
    re.compile(r"\b(?:the\s+)?(?:bug|defect|issue|problem)\s+no\s+longer\s+(?:occurs|happens|reproduces)\b", re.I),
    re.compile(r"\ball\s+(?:checks|tests|ratchets|journeys)\s+pass(?:ed)?\b", re.I),
)
_LIVE_BUILD_RE = re.compile(r"(?:\bBUILD_ID\s*=\s*\S+|\blive build\s+\S+)", re.IGNORECASE)
_PASS_RE = re.compile(r"(?:\bPASS\b|\bpassed\b|\bsuccess\b|\bexit_code\s*[=:]\s*0\b)", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>'\""
_LINKEDIN_MARKERS = ("authorize=PASS", "post=PASS", "public_url=PASS", "fetch=PASS")
_MAX_RECEIPTS = 256
SAFE_POLICY_FAILURE_NOTICE = (
    "DELIVERY BLOCKED\n\n"
    "The original message was withheld because outbound safety verification failed."
)


@dataclass(frozen=True)
class ToolReceipt:
    session_id: str
    turn_id: str
    tool_name: str
    check_id: str
    verifier_id: str
    journey_id: str
    exit_status: int
    build_id: str
    runtime_id: str
    timestamp: str
    output_digest: str
    public_url: str
    public_fetch_ok: bool


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
    allowed_verifiers: Mapping[str, Mapping[str, Any]] | None = None,
    **_: Any,
) -> None:
    if not session_id or not turn_id or not isinstance(result, Mapping):
        return
    payload = result.get("outbound_verifier_receipt")
    if not isinstance(payload, Mapping) or not isinstance(allowed_verifiers, Mapping):
        return
    verifier_id = str(payload.get("verifier_id") or "")
    verifier = allowed_verifiers.get(verifier_id)
    if not isinstance(verifier, Mapping):
        return
    check_id = str(payload.get("check_id") or "")
    journey_id = str(payload.get("journey_id") or "")
    if (
        str(tool_name) != str(verifier.get("tool_name") or "")
        or check_id != str(verifier.get("check_id") or "")
        or journey_id != str(verifier.get("journey_id") or "")
        or str(payload.get("session_id") or "") != str(session_id)
        or str(payload.get("turn_id") or "") != str(turn_id)
        or str(status or "").lower() not in {"success", "ok", "completed"}
    ):
        return
    digest = str(payload.get("output_digest") or "")
    try:
        exit_status = int(payload.get("exit_status", -1))
    except (TypeError, ValueError):
        return
    if exit_status != 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return
    public_fetch = payload.get("public_fetch")
    receipt = ToolReceipt(
        session_id=str(session_id),
        turn_id=str(turn_id),
        tool_name=str(tool_name),
        check_id=check_id,
        verifier_id=verifier_id,
        journey_id=journey_id,
        exit_status=0,
        build_id=str(payload.get("build_id") or ""),
        runtime_id=str(payload.get("runtime_id") or ""),
        timestamp=str(payload.get("timestamp") or ""),
        output_digest=digest,
        public_url=str(payload.get("public_url") or ""),
        public_fetch_ok=bool(isinstance(public_fetch, Mapping) and public_fetch.get("ok") is True),
    )
    if not receipt.build_id or not receipt.runtime_id or not receipt.timestamp:
        return
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


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _request_pinned(url: str, addresses: set[ipaddress._BaseAddress], timeout: float) -> dict[str, Any]:
    """GET one URL through a previously validated and now pinned IP address."""
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last_error: Exception | None = None
    for address in sorted(addresses, key=str):
        connection_cls = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        connection = connection_cls(str(parsed.hostname), port, str(address), timeout)
        try:
            connection.request(
                "GET",
                path,
                headers={"User-Agent": "Hermes-Outbound-Link-Gate/2.0", "Accept": "*/*"},
            )
            response = connection.getresponse()
            return {
                "status": int(response.status),
                "headers": {str(k).lower(): str(v) for k, v in response.getheaders()},
                "body": response.read(1024),
            }
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    raise OSError(str(last_error or "connection failed"))


def _status_is_allowed_exception(
    parsed, status: int, status_exceptions: tuple[Mapping[str, Any], ...]
) -> bool:
    for item in status_exceptions:
        if str(item.get("host") or "").lower() != str(parsed.hostname or "").lower():
            continue
        if str(item.get("path") or "") != (parsed.path or "/"):
            continue
        statuses = item.get("statuses")
        if isinstance(statuses, (list, tuple, set)) and status in {int(value) for value in statuses}:
            return True
    return False


def fetch_url_live(
    url: str,
    timeout: float = 10.0,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    requester: Callable[[str, set[ipaddress._BaseAddress], float], Mapping[str, Any]] = _request_pinned,
    status_exceptions: tuple[Mapping[str, Any], ...] = (),
    max_redirects: int = 3,
) -> dict[str, Any]:
    """Fetch a public HTTP(S) URL with per-hop validation and DNS pinning."""
    current = str(url or "")
    deadline = time.monotonic() + max(0.1, float(timeout))
    for hop in range(max_redirects + 1):
        parsed = urlsplit(current)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "status": None, "final_url": "", "error": "unsupported or malformed URL"}
        if parsed.username is not None or parsed.password is not None:
            return {"ok": False, "status": None, "final_url": "", "error": "credential-bearing URL is forbidden"}
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return {"ok": False, "status": None, "final_url": "", "error": "unsupported or malformed URL"}
        if port not in {80, 443}:
            return {"ok": False, "status": None, "final_url": "", "error": "unsafe destination port"}
        try:
            literal = ipaddress.ip_address(str(parsed.hostname))
            addresses = {literal}
        except ValueError:
            try:
                addresses = {
                    ipaddress.ip_address(item[4][0])
                    for item in resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
                }
            except (OSError, ValueError, TypeError):
                addresses = set()
        if not addresses or any(not address.is_global for address in addresses):
            return {"ok": False, "status": None, "final_url": "", "error": "destination is not public"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ok": False, "status": None, "final_url": "", "error": "fetch timeout"}
        try:
            response = requester(current, addresses, remaining)
            status = int(response.get("status"))
            headers = response.get("headers") or {}
        except (OSError, TimeoutError, ValueError, TypeError) as exc:
            return {"ok": False, "status": None, "final_url": "", "error": str(exc)}
        if status in {301, 302, 303, 307, 308}:
            location = headers.get("location") if isinstance(headers, Mapping) else None
            if not location:
                return {"ok": False, "status": status, "final_url": current, "error": "redirect missing location"}
            if hop >= max_redirects:
                return {"ok": False, "status": status, "final_url": current, "error": "too many redirects"}
            redirected = urljoin(current, str(location))
            if parsed.scheme == "https" and urlsplit(redirected).scheme != "https":
                return {"ok": False, "status": status, "final_url": current, "error": "HTTPS downgrade redirect"}
            current = redirected
            continue
        ok = 200 <= status < 300 or _status_is_allowed_exception(parsed, status, status_exceptions)
        return {"ok": ok, "status": status, "final_url": current, "error": "" if ok else f"HTTP {status}"}
    return {"ok": False, "status": None, "final_url": current, "error": "too many redirects"}


def normalize_target(platform: str, chat_id: str | None = None) -> str:
    """Canonical target identity: case-insensitive platform, opaque recipient id."""
    if chat_id is None:
        raw = str(platform or "").strip()
        if ":" not in raw:
            raise ValueError("protected target must be '<platform>:<chat_id>'")
        platform, chat_id = raw.split(":", 1)
    normalized_platform = str(platform or "").strip().lower()
    normalized_chat_id = str(chat_id or "").strip()
    if not normalized_platform or not normalized_chat_id:
        raise ValueError("protected target must include platform and chat id")
    return f"{normalized_platform}:{normalized_chat_id}"


def _target_is_protected(platform: str, chat_id: str, settings: Mapping[str, Any]) -> bool:
    raw = settings.get("protected_targets", [])
    if not isinstance(raw, list):
        raise ValueError("protected_targets must be a list")
    target = normalize_target(platform, chat_id)
    return target in {normalize_target(str(item)) for item in raw}


def _contains_success_claim(content: str, settings: Mapping[str, Any]) -> bool:
    # Normalize compatibility characters (full-width letters, ligatures) and
    # remove Markdown emphasis delimiters so ``com**plete**`` is inspected as
    # the visible word the recipient sees.
    visible = unicodedata.normalize("NFKC", str(content or ""))
    visible = re.sub(r"[`*_~]", "", visible)
    configured = settings.get("success_terms", [])
    additive = configured if isinstance(configured, list) else []
    terms = {str(term).strip() for term in (*_DEFAULT_SUCCESS_TERMS, *additive) if str(term).strip()}
    if any(
        re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", visible, re.IGNORECASE)
        for term in terms
    ):
        return True
    return any(pattern.search(visible) for pattern in _REQUIRED_SUCCESS_PATTERNS)


def _safe_dead_url_label(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return f"{parsed.netloc} {path}".strip()


def _receipt_for_turn(session_id: str, turn_id: str, check_id: str) -> ToolReceipt | None:
    if not session_id or not turn_id or not check_id:
        return None
    with _lock:
        candidates = tuple(_receipts)
    for receipt in reversed(candidates):
        if receipt.session_id != session_id or receipt.turn_id != turn_id:
            continue
        if receipt.check_id != check_id:
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
    raw_status_exceptions = settings.get("status_exceptions", [])
    if not isinstance(raw_status_exceptions, list) or any(
        not isinstance(item, Mapping) for item in raw_status_exceptions
    ):
        raise ValueError("status_exceptions must be a list of mappings")
    timeout_seconds = max(0.1, float(settings.get("fetch_timeout_seconds", 10.0)))
    deadline = time.monotonic() + timeout_seconds
    urls = _extract_urls(text)
    try:
        max_urls = int(settings.get("max_urls", 8))
    except (TypeError, ValueError):
        max_urls = 8
    if max_urls < 1 or len(urls) > max_urls:
        return {
            "action": "rewrite",
            "reason": "url_check_failed",
            "content": SAFE_POLICY_FAILURE_NOTICE,
        }
    fetch = fetcher
    failed: list[tuple[str, Mapping[str, Any]]] = []
    for url in urls:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failed.append((url, {"ok": False, "error": "total preflight timeout"}))
            break
        try:
            result = (
                fetch(url)
                if fetch is not None
                else fetch_url_live(
                    url,
                    remaining,
                    status_exceptions=tuple(raw_status_exceptions),
                )
            )
        except Exception as exc:  # policy dependencies fail closed
            result = {"ok": False, "status": None, "error": str(exc), "final_url": ""}
        if not result.get("ok"):
            failed.append((url, result))
    if failed:
        return {
            "action": "rewrite",
            "reason": "url_check_failed",
            "content": SAFE_POLICY_FAILURE_NOTICE,
        }

    if not _contains_success_claim(text, settings):
        return {"action": "allow"}

    meta = metadata if isinstance(metadata, Mapping) else {}
    workflow_id = str(meta.get("outbound_workflow_id") or "").strip().lower()
    linkedin_claim = workflow_id in {"linkedin", "li-publishing"} or bool(
        re.search(r"\b(?:linkedin|li\s+publishing|publishing\s+flow)\b", text, re.IGNORECASE)
    )
    check_id = "linkedin-public-post-journey" if linkedin_claim else str(
        meta.get("_outbound_claim_check_id") or ""
    )
    receipt = _receipt_for_turn(
        str(meta.get("_hermes_session_id") or ""),
        str(meta.get("_hermes_turn_id") or ""),
        check_id,
    )
    if receipt is None:
        return _unverified(
            text,
            "claim_receipt_missing",
            "a structured receipt from an allowlisted verifier in this same session turn",
        )

    if linkedin_claim:
        if (
            receipt.journey_id != "linkedin-public-post-journey"
            or not receipt.public_url
            or not receipt.public_fetch_ok
        ):
            return _unverified(
                text,
                "linkedin_journey_incomplete",
                "LinkedIn receipt linkedin-public-post-journey with a public URL and passing fresh public fetch",
            )
    return {"action": "allow"}


def _build_settings(ctx) -> dict[str, Any]:
    del ctx
    from hermes_cli.outbound_policy import outbound_policy_settings

    return outbound_policy_settings()


def register(ctx) -> None:
    def _record(**kwargs: Any) -> None:
        record_tool_result(
            **kwargs,
            allowed_verifiers=_build_settings(ctx).get("allowed_verifiers", {}),
        )

    ctx.register_hook("post_tool_call", _record)

    def _final_gateway_send_policy(
        platform: str = "",
        chat_id: str = "",
        content: str = "",
        metadata: Mapping[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, str]:
        decision = gate_outbound_message(
            platform=platform,
            chat_id=chat_id,
            content=content,
            metadata=metadata,
            settings=_build_settings(ctx),
        )
        return {"policy_id": "outbound-message-gate", **decision}

    setattr(_final_gateway_send_policy, "_hermes_policy_id", "outbound-message-gate")
    ctx.register_hook("final_gateway_send_policy", _final_gateway_send_policy)
