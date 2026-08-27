"""Outbound URL reachability and success-claim receipt policy.

The plugin is intentionally target-scoped.  It observes completed tool calls by
(session_id, turn_id), then evaluates text at the adapter boundary immediately
before a protected recipient can see it.  Prompt instructions are not part of
the enforcement path.
"""

from __future__ import annotations

import re
import hashlib
import ipaddress
import http.client
import json
import os
import queue
import secrets
import socket
import ssl
import stat
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
    command_id: str
    exit_status: int
    build_id: str
    runtime_id: str
    timestamp: str
    timestamp_epoch: float
    output_digest: str
    public_url: str



_receipts: list[ToolReceipt] = []
_lock = threading.RLock()
_MAX_RECEIPT_AGE_SECONDS = 300.0


_RUNTIME_GENERATION = secrets.token_urlsafe(32)


def current_runtime_id() -> str:
    """Host-produced identity for the exact process accepting the receipt."""
    return f"pid:{os.getpid()}:generation:{_RUNTIME_GENERATION}"


_GATE_BUILD_MANIFEST_VERSION = 1
# Review-controlled security manifest. Dynamic inventory below is only a
# fail-closed ratchet: it may reject a new source, never silently add it to the
# build identity.
GATE_BUILD_SOURCE_PATHS = (
    "cron/scheduler.py",
    "cron/scheduler_provider.py",
    "gateway/delivery.py",
    "gateway/platform_registry.py",
    "gateway/platforms/__init__.py",
    "gateway/platforms/_http_client_limits.py",
    "gateway/platforms/api_server.py",
    "gateway/platforms/base.py",
    "gateway/platforms/bluebubbles.py",
    "gateway/platforms/helpers.py",
    "gateway/platforms/media_cache.py",
    "gateway/platforms/msgraph_webhook.py",
    "gateway/platforms/qqbot/__init__.py",
    "gateway/platforms/qqbot/adapter.py",
    "gateway/platforms/qqbot/chunked_upload.py",
    "gateway/platforms/qqbot/constants.py",
    "gateway/platforms/qqbot/crypto.py",
    "gateway/platforms/qqbot/keyboards.py",
    "gateway/platforms/qqbot/onboard.py",
    "gateway/platforms/qqbot/utils.py",
    "gateway/platforms/signal.py",
    "gateway/platforms/signal_format.py",
    "gateway/platforms/signal_rate_limit.py",
    "gateway/platforms/webhook.py",
    "gateway/platforms/webhook_filters.py",
    "gateway/platforms/weixin.py",
    "gateway/platforms/whatsapp_cloud.py",
    "gateway/platforms/whatsapp_common.py",
    "gateway/platforms/yuanbao.py",
    "gateway/platforms/yuanbao_media.py",
    "gateway/platforms/yuanbao_proto.py",
    "gateway/platforms/yuanbao_sticker.py",
    "gateway/relay/adapter.py",
    "gateway/run.py",
    "hermes_cli/lifecycle.py",
    "hermes_cli/outbound_policy.py",
    "hermes_cli/plugins.py",
    "plugins/outbound_message_gate/__init__.py",
    "plugins/platforms/a2a/adapter.py",
    "plugins/platforms/buzz/adapter.py",
    "plugins/platforms/dingtalk/adapter.py",
    "plugins/platforms/discord/adapter.py",
    "plugins/platforms/email/adapter.py",
    "plugins/platforms/feishu/adapter.py",
    "plugins/platforms/google_chat/adapter.py",
    "plugins/platforms/homeassistant/adapter.py",
    "plugins/platforms/irc/adapter.py",
    "plugins/platforms/line/adapter.py",
    "plugins/platforms/matrix/adapter.py",
    "plugins/platforms/mattermost/adapter.py",
    "plugins/platforms/ntfy/adapter.py",
    "plugins/platforms/photon/adapter.py",
    "plugins/platforms/raft/adapter.py",
    "plugins/platforms/simplex/adapter.py",
    "plugins/platforms/slack/adapter.py",
    "plugins/platforms/sms/adapter.py",
    "plugins/platforms/teams/adapter.py",
    "plugins/platforms/telegram/adapter.py",
    "plugins/platforms/wecom/adapter.py",
    "plugins/platforms/whatsapp/adapter.py",
    "tools/send_message_tool.py",
)


@dataclass(frozen=True)
class _SourceSnapshot:
    relative_path: str
    resolved_path: str
    source_bytes: bytes
    digest: str


def _gate_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _security_source_inventory(root: Path) -> set[str]:
    found: set[str] = set()
    platform_root = root / "gateway" / "platforms"
    if platform_root.is_dir():
        found.update(str(path.relative_to(root)) for path in platform_root.rglob("*.py"))
    plugin_root = root / "plugins" / "platforms"
    if plugin_root.is_dir():
        found.update(
            str(path.relative_to(root)) for path in plugin_root.rglob("*.py")
            if path.name == "adapter.py" or "transport" in path.name.lower()
        )
    cron_root = root / "cron"
    if cron_root.is_dir():
        found.update(
            str(path.relative_to(root)) for path in cron_root.rglob("*.py")
            if "deliver" in path.name.lower() or "scheduler" in path.name.lower()
        )
    for relative in (
        "gateway/delivery.py", "gateway/platform_registry.py", "gateway/relay/adapter.py",
        "gateway/run.py", "hermes_cli/lifecycle.py", "hermes_cli/outbound_policy.py",
        "hermes_cli/plugins.py", "plugins/outbound_message_gate/__init__.py",
        "tools/send_message_tool.py",
    ):
        if (root / relative).exists():
            found.add(relative)
    return found


def _validate_gate_build_manifest(source_paths: tuple[str, ...], *, root: Path) -> None:
    if tuple(sorted(set(source_paths))) != tuple(source_paths):
        raise RuntimeError("gate build manifest must be unique and sorted")
    actual = _security_source_inventory(root)
    declared = set(source_paths)
    missing = sorted(actual - declared)
    stale = sorted(declared - actual)
    if missing or stale:
        details = []
        if missing:
            details.append("unreviewed=" + ",".join(missing))
        if stale:
            details.append("missing=" + ",".join(stale))
        raise RuntimeError("gate build manifest mismatch: " + "; ".join(details))


def _read_security_source_bytes(root: Path, relative_text: str) -> bytes:
    root = root.resolve(strict=True)
    path = root / relative_text
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise RuntimeError(f"security source symlink forbidden: {relative_text}")
        cursor = cursor.parent
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing security source: {relative_text}") from exc
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"security source is not a regular file: {relative_text}")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"security source escapes repository: {relative_text}") from exc
    return path.read_bytes()


def _capture_source_snapshots(
    *, root: Path, source_paths: tuple[str, ...],
) -> dict[str, _SourceSnapshot]:
    snapshots: dict[str, _SourceSnapshot] = {}
    for relative in source_paths:
        data = _read_security_source_bytes(root, relative)
        snapshots[relative] = _SourceSnapshot(
            relative_path=relative,
            resolved_path=str((root / relative).resolve(strict=True)),
            source_bytes=data,
            digest=hashlib.sha256(data).hexdigest(),
        )
    return snapshots


def _module_name_for_source(relative: str) -> str:
    module = relative[:-3].replace("/", ".")
    return module[:-9] if module.endswith(".__init__") else module


def _assert_loaded_module_identity(
    module_name: str, module: Any, snapshot: _SourceSnapshot,
) -> None:
    module_file = getattr(module, "__file__", None)
    if not module_file or Path(module_file).is_symlink():
        raise RuntimeError(f"loaded module path mismatch: {module_name}")
    if str(Path(module_file).resolve(strict=False)) != snapshot.resolved_path:
        raise RuntimeError(f"loaded module path mismatch: {module_name}")
    spec = getattr(module, "__spec__", None)
    origin = getattr(spec, "origin", None)
    if not origin or str(Path(origin).resolve(strict=False)) != snapshot.resolved_path:
        raise RuntimeError(f"loaded module loader origin mismatch: {module_name}")
    loader = getattr(module, "__loader__", None)
    get_data = getattr(loader, "get_data", None)
    if not callable(get_data):
        raise RuntimeError(f"loaded module loader identity unavailable: {module_name}")
    try:
        loaded_bytes = get_data(module_file)
    except Exception as exc:
        raise RuntimeError(f"loaded module bytes unavailable: {module_name}") from exc
    if loaded_bytes != snapshot.source_bytes:
        raise RuntimeError(f"loaded module bytes mismatch: {module_name}")


def _assert_source_snapshots(
    snapshots: Mapping[str, _SourceSnapshot], *, root: Path,
    check_loaded_modules: bool = True,
) -> None:
    for relative, snapshot in snapshots.items():
        current = _read_security_source_bytes(root, relative)
        if current != snapshot.source_bytes:
            raise RuntimeError(f"security source disk drift: {relative}")
        if check_loaded_modules:
            module_name = _module_name_for_source(relative)
            module = sys.modules.get(module_name)
            if module is not None:
                _assert_loaded_module_identity(module_name, module, snapshot)


_GATE_ROOT = _gate_repo_root()
_validate_gate_build_manifest(GATE_BUILD_SOURCE_PATHS, root=_GATE_ROOT)
_STARTUP_SOURCE_SNAPSHOTS = _capture_source_snapshots(
    root=_GATE_ROOT, source_paths=GATE_BUILD_SOURCE_PATHS,
)


def _assert_runtime_build_identity() -> None:
    _validate_gate_build_manifest(GATE_BUILD_SOURCE_PATHS, root=_GATE_ROOT)
    _assert_source_snapshots(_STARTUP_SOURCE_SNAPSHOTS, root=_GATE_ROOT)


def _gate_build_digest(*, source_paths: tuple[str, ...] | None = None) -> str:
    """Digest startup-captured bytes, never mutable post-import disk alone."""
    paths = GATE_BUILD_SOURCE_PATHS if source_paths is None else tuple(source_paths)
    if tuple(sorted(set(paths))) != paths:
        raise ValueError("gate build source paths must be unique and sorted")
    digest = hashlib.sha256()
    digest.update(f"manifest-v{_GATE_BUILD_MANIFEST_VERSION}".encode())
    for relative_text in paths:
        snapshot = _STARTUP_SOURCE_SNAPSHOTS.get(relative_text)
        if snapshot is None:
            raise FileNotFoundError(f"missing startup gate source: {relative_text}")
        relative = relative_text.encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = snapshot.source_bytes
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


_ACTIVE_BUILD_ID = f"gate-build:sha256:{_gate_build_digest()}"
_GENERIC_VERIFIER_TOOLS = frozenset({
    "terminal", "shell", "bash", "sh", "execute_code", "python", "computer_use",
})


def current_build_id() -> str:
    """Identity of the policy bytes loaded into this process generation."""
    return _ACTIVE_BUILD_ID


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
    try:
        _assert_runtime_build_identity()
    except Exception:
        return
    invoked_at = time.time()
    if not session_id or not turn_id or not isinstance(result, Mapping):
        return
    payload = result.get("outbound_verifier_receipt")
    if not isinstance(payload, Mapping) or not isinstance(allowed_verifiers, Mapping):
        return
    verifier_id = str(payload.get("verifier_id") or "")
    verifier = allowed_verifiers.get(verifier_id)
    if not isinstance(verifier, Mapping):
        return
    expected_args = verifier.get("args")
    configured_tool = str(verifier.get("tool_name") or "")
    tool_leaf = re.split(r"[.:/]", configured_tool.strip().lower())[-1].replace("-", "_")
    if (
        verifier.get("dedicated_verifier") is not True
        or tool_leaf in _GENERIC_VERIFIER_TOOLS
        or "verifier" not in configured_tool.strip().lower()
        or not isinstance(expected_args, Mapping)
        or not isinstance(args, Mapping)
        or dict(args) != dict(expected_args)
    ):
        return
    check_id = str(payload.get("check_id") or "")
    journey_id = str(payload.get("journey_id") or "")
    command_id = str(payload.get("command_id") or "")
    expected_output = verifier.get("passing_output")
    if (
        str(tool_name) != configured_tool
        or check_id != str(verifier.get("check_id") or "")
        or journey_id != str(verifier.get("journey_id") or "")
        or command_id != str(verifier.get("command_id") or "")
        or not command_id
        or not isinstance(expected_output, Mapping)
        or str(payload.get("session_id") or "") != str(session_id)
        or str(payload.get("turn_id") or "") != str(turn_id)
        or str(status or "").lower() not in {"success", "ok", "completed"}
    ):
        return
    output = payload.get("output")
    try:
        exit_status = int(payload.get("exit_status", -1))
    except (TypeError, ValueError):
        return
    if not isinstance(output, Mapping):
        return
    if dict(output) != dict(expected_output):
        return
    try:
        serialized_output = json.dumps(
            output, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return
    recomputed_digest = hashlib.sha256(serialized_output).hexdigest()
    if exit_status != 0:
        return
    timestamp_epoch = invoked_at
    timestamp = datetime.fromtimestamp(timestamp_epoch, timezone.utc).isoformat()
    receipt = ToolReceipt(
        session_id=str(session_id),
        turn_id=str(turn_id),
        tool_name=str(tool_name),
        check_id=check_id,
        verifier_id=verifier_id,
        journey_id=journey_id,
        command_id=command_id,
        exit_status=0,
        build_id=current_build_id(),
        runtime_id=current_runtime_id(),
        timestamp=timestamp,
        timestamp_epoch=timestamp_epoch,
        output_digest=recomputed_digest,
        public_url=str(payload.get("public_url") or ""),
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


_MAX_PINNED_ADDRESS_ATTEMPTS = 2
_RESOLVER_MAX_WORKERS = 2
_RESOLVER_MAX_QUEUE = 4


class _BoundedResolverPool:
    """Fixed daemon-worker pool for uncancellable blocking DNS calls.

    Admission covers running plus queued work.  A caller timing out marks its
    queued task cancelled; a worker skips it without invoking the resolver.
    Running C-library lookups remain uncancellable, but their number can never
    exceed ``max_workers`` and daemon workers cannot hold process exit hostage.
    """

    def __init__(
        self, *, max_workers: int, max_queue: int, name: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_workers < 1 or max_queue < 0:
            raise ValueError("resolver pool bounds must be non-negative with at least one worker")
        self.capacity = max_workers + max_queue
        self._clock = clock
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self.capacity)
        self._admission = threading.BoundedSemaphore(self.capacity)
        self._lock = threading.Lock()
        self._outstanding = 0
        self._stopped = threading.Event()
        self.worker_threads = tuple(
            threading.Thread(
                target=self._worker, daemon=True, name=f"{name}-{index + 1}"
            )
            for index in range(max_workers)
        )
        for worker in self.worker_threads:
            worker.start()

    @property
    def outstanding(self) -> int:
        with self._lock:
            return self._outstanding

    def _worker(self) -> None:
        while not self._stopped.is_set():
            task = self._queue.get()
            if task is None:
                return
            call, result_queue, cancelled = task
            try:
                if cancelled.is_set():
                    continue
                try:
                    result = (True, call())
                except BaseException as exc:
                    result = (False, exc)
                try:
                    result_queue.put(result, block=False)
                except queue.Full:
                    pass
            finally:
                with self._lock:
                    self._outstanding -= 1
                self._admission.release()

    def call(self, call: Callable[[], Any], deadline: float) -> Any:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("fetch timeout")
        if self._stopped.is_set() or not self._admission.acquire(blocking=False):
            raise RuntimeError("resolver overloaded")
        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        cancelled = threading.Event()
        with self._lock:
            self._outstanding += 1
        try:
            self._queue.put_nowait((call, result_queue, cancelled))
        except queue.Full:
            with self._lock:
                self._outstanding -= 1
            self._admission.release()
            raise RuntimeError("resolver overloaded")
        remaining = deadline - self._clock()
        if remaining <= 0:
            cancelled.set()
            raise TimeoutError("fetch timeout")
        try:
            ok, value = result_queue.get(timeout=remaining)
        except queue.Empty as exc:
            cancelled.set()
            raise TimeoutError("fetch timeout") from exc
        if not ok:
            raise value
        return value

    def shutdown(self, *, wait: bool = False) -> None:
        self._stopped.set()
        for _worker in self.worker_threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break
        if wait:
            for worker in self.worker_threads:
                worker.join(timeout=0.1)


_RESOLVER_POOL = _BoundedResolverPool(
    max_workers=_RESOLVER_MAX_WORKERS,
    max_queue=_RESOLVER_MAX_QUEUE,
    name="outbound-gate-dns",
)


def _call_before_deadline(call: Callable[[], Any], deadline: float) -> Any:
    """Run blocking DNS within the process-wide bounded resolver pool."""
    return _RESOLVER_POOL.call(call, deadline)


def _request_pinned(url: str, addresses: set[ipaddress._BaseAddress], timeout: float) -> dict[str, Any]:
    """GET one URL through a previously validated and now pinned IP address."""
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last_error: Exception | None = None
    deadline = time.monotonic() + max(0.0, timeout)
    for address in sorted(addresses, key=str)[:_MAX_PINNED_ADDRESS_ATTEMPTS]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("fetch timeout")
        connection_cls = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        connection = connection_cls(str(parsed.hostname), port, str(address), remaining)
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
            if deadline - time.monotonic() <= 0:
                return {"ok": False, "status": None, "final_url": "", "error": "fetch timeout"}
            try:
                resolved = _call_before_deadline(
                    lambda: resolver(parsed.hostname, port, type=socket.SOCK_STREAM),
                    deadline,
                )
                addresses = {
                    ipaddress.ip_address(item[4][0])
                    for item in resolved
                }
            except RuntimeError as exc:
                return {
                    "ok": False, "status": None, "final_url": "",
                    "error": str(exc) or "resolver overloaded",
                }
            except (OSError, TimeoutError, ValueError, TypeError):
                addresses = set()
        if deadline - time.monotonic() <= 0:
            return {"ok": False, "status": None, "final_url": "", "error": "fetch timeout"}
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
    """Compatibility alias for the one core-owned target normalizer."""
    from hermes_cli.outbound_policy import normalize_outbound_target

    return normalize_outbound_target(platform, chat_id)


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
        if time.time() - receipt.timestamp_epoch > _MAX_RECEIPT_AGE_SECONDS:
            continue
        if receipt.build_id != current_build_id() or receipt.runtime_id != current_runtime_id():
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
    try:
        _assert_runtime_build_identity()
    except Exception:
        return {
            "action": "rewrite",
            "reason": "runtime_build_identity_invalid",
            "content": SAFE_POLICY_FAILURE_NOTICE,
        }

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
        # Fail closed until authorization, controlled-post, and public-fetch
        # events are independently registered by three dedicated invocations
        # and bound to one canonical LinkedIn journey/public URL. A single
        # result dictionary is intentionally never sufficient.
        return _unverified(
            text,
            "linkedin_journey_incomplete",
            "independent same-turn LinkedIn authorization, controlled post, and SSRF-safe public-fetch verifier events",
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
        return decision

    ctx.register_final_gateway_send_policy(_final_gateway_send_policy)
