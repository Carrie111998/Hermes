"""Shared immutable carriers and validation helpers for webhook shards."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.platforms.base import (
    MessageEvent,
)
from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookEnvelope,
)
from gateway.platforms.webhook_filters import (
    WebhookPreparedScript,
)
from gateway.platforms.webhook_ledger import (
    DEFAULT_RECOVERY_BATCH_SIZE,
    DEFAULT_MAX_STORAGE_BYTES,
    MAXIMUM_MAX_RECORDS,
    MAXIMUM_MAX_STORAGE_BYTES,
    OperationAuthority,
    WebhookLedgerError,
    WebhookOperationLedger,
)
from gateway.response_filters import is_autonomous_silence_response

logger = logging.getLogger(__name__)


def _is_webhook_silence_response(content: Any) -> bool:
    """Whether an agent response means "deliberately say nothing".

    Webhook routes are autonomous background lanes: a subscription prompt tells
    the agent to answer with ``[SILENT]`` when a tick produced nothing worth a
    human's attention (a duplicate inbound, a stand-down because a sibling lane
    already replied, a routine close).  Nobody is waiting on the other end, so
    there is no reader for whom a "nothing happened" message is useful.

    The reason this is the loose autonomous rule rather than the live gateway's
    is what the two lanes optimise for.  In an interactive chat, swallowing a
    real answer because it happens to open with a marker is much worse than
    showing a stray marker, so ``is_intentional_silence_response`` demands the
    response be EXACTLY a marker.  A webhook run has the opposite payoff: the
    cost of a leaked non-story is a pointless notification on every tick, and
    models reliably add a sentence explaining why they stayed quiet — which
    under the strict rule flips the whole thing back to "deliver".  That is not
    a hypothetical: it is why a Helper support lane kept messaging its owner to
    report that it had nothing to report.

    So use the shared autonomous-lane matcher (also used by cron), which treats
    a marker on its own first or last line as silence while still delivering
    prose that merely mentions one mid-sentence.  Sharing the function keeps
    the two autonomous lanes from drifting apart, and keeps the interactive
    path untouched.
    """
    return is_autonomous_silence_response(content)


# Sentinel returned by _resolve_request_profile when a /p/<profile>/ prefix
# names a profile this gateway does not serve (→ 404). Distinct from None
# (no prefix / multiplexing off → handle as the default profile).
_PROFILE_REJECTED = object()

# Default bind host. ``None`` tells aiohttp/asyncio's ``create_server`` to bind
# BOTH address families (IPv4 + IPv6) — the portable dual-stack default.
#
# Why not "0.0.0.0" (the old default) or "::"?
#   - "0.0.0.0" binds IPv4 ONLY. On IPv6-only private networks — notably Fly.io
#     6PN, where an agent's ``<app>.internal`` name resolves to an ``fdaa:…``
#     IPv6 address — an IPv4-only listener is unreachable. That is exactly why
#     hosted-agent webhook routes were publicly unreachable: the edge router
#     reverse-proxies to ``<app>.internal:8644`` over 6PN (IPv6) but the adapter
#     was listening on 0.0.0.0 (v4 only) → connection refused.
#   - "::" is NOT a safe fix: on hosts where the kernel sets IPV6_V6ONLY=1
#     (verified on Fly machines), binding "::" yields an IPv6-ONLY socket, which
#     then breaks the IPv4 loopback health check (``curl 127.0.0.1:8644/health``)
#     and the AF_INET port-conflict probe in connect().
#   - ``None`` asks the event loop to create a listening socket per resolved
#     family, so both 127.0.0.1 (v4) and the 6PN fdaa (v6) are served regardless
#     of the bindv6only sysctl. Users can still pin a specific host via
#     ``platforms.webhook.extra.host``.
DEFAULT_HOST = None
DEFAULT_PORT = 8644
_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"
_DYNAMIC_ROUTES_FILENAME = "webhook_subscriptions.json"
_MAX_DYNAMIC_ROUTES_FILE_BYTES = 4 * 1024 * 1024
_DYNAMIC_ROUTES_CONTENT_RECHECK_SECONDS = 1.0
_MAX_BODY_BYTES_LIMIT = 1024 * 1024
_MAX_RENDERED_PROMPT_BYTES = 512 * 1024
_MAX_DURABLE_EVENT_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_DURABLE_AUTHORITY_SNAPSHOT_BYTES = 64 * 1024
_PROFILE_AUTHORITY_INCARNATION_FILENAME = ".webhook-profile-incarnation"
_PROFILE_AUTHORITY_INCARNATION_BYTES = 32
_MAX_CONCURRENT_AUTHORITY_PROOFS = 4
_MAX_CONCURRENT_ROUTE_WORKERS = 4
_RATE_WINDOW_SECONDS = 60.0
_MAX_RATE_LIMIT_PER_MINUTE = 10_000
_MAX_SCRIPT_TIMEOUT_SECONDS = 300
_IDEMPOTENCY_DEFAULT_MAX_ENTRIES = 4096
_IDEMPOTENCY_MAX_ENTRIES_LIMIT = MAXIMUM_MAX_RECORDS
_IDEMPOTENCY_DEFAULT_MAX_STORAGE_BYTES = DEFAULT_MAX_STORAGE_BYTES
_IDEMPOTENCY_MAX_STORAGE_BYTES_LIMIT = MAXIMUM_MAX_STORAGE_BYTES
_RAW_PAYLOAD_DEFAULT_CAP_BYTES = 4_000
_RAW_PAYLOAD_MIN_CAP_BYTES = 64
_RAW_PAYLOAD_MAX_CAP_BYTES = 1_000_000


class WebhookConfigurationError(ValueError):
    """A deterministic webhook setting that cannot improve on retry."""


# ``disconnect()`` normally fences every operation owned by its exact ledger
# instance before returning.  A SQLite/I/O failure at that seam is special:
# another adapter in the same process has the same PID, so ordinary dead-owner
# recovery correctly refuses to steal the first adapter's rows.  Keep the exact
# failed owner ID in a process-local quarantine, scoped by the canonical DB
# path, so a replacement can retry that one retirement transaction first.
#
# The registry is deliberately bounded.  Overflow is represented by a
# fail-closed sentinel rather than evicting an owner ID and accidentally
# reopening intake while an unretired same-process owner still exists.
_MAX_RETIREMENT_QUARANTINE_DATABASES = 128
_MAX_RETIREMENT_QUARANTINE_OWNERS_PER_DATABASE = 128
_RECOVERY_CONCURRENCY_LIMIT = DEFAULT_RECOVERY_BATCH_SIZE
_RECOVERY_PAGE_BUDGET = 4
_route_worker_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_ROUTE_WORKERS)


@dataclass
class _RetirementQuarantine:
    owners: dict[str, None] = field(default_factory=dict)
    saturated: bool = False


_retirement_quarantine_lock = threading.Lock()
_retirement_quarantines: dict[str, _RetirementQuarantine] = {}
_retirement_quarantine_registry_saturated = False


def _reset_retirement_quarantines_after_fork() -> None:
    """Never inherit same-process owner assertions into a child process."""

    global _retirement_quarantine_lock
    global _retirement_quarantines
    global _retirement_quarantine_registry_saturated
    global _route_worker_slots

    # Replacing the lock also avoids inheriting a lock held by a vanished
    # parent thread at the fork boundary.
    _retirement_quarantine_lock = threading.Lock()
    _retirement_quarantines = {}
    _retirement_quarantine_registry_saturated = False
    # No executor worker survives fork. Reset the process-global filter/script
    # budget so inherited acquisitions cannot permanently fence the child.
    _route_worker_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_ROUTE_WORKERS)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_retirement_quarantines_after_fork)


def _retirement_quarantine_key(ledger: WebhookOperationLedger) -> str:
    """Return the physical durable-ledger identity used for quarantine."""

    try:
        return os.path.normcase(str(ledger.db_path.resolve(strict=False)))
    except (AttributeError, OSError, RuntimeError) as exc:
        raise WebhookLedgerError(
            "webhook retirement ledger path cannot be resolved"
        ) from exc


def _quarantine_failed_retirement(ledger: WebhookOperationLedger) -> bool:
    """Record one exact failed owner, returning false on bounded overflow."""

    global _retirement_quarantine_registry_saturated

    try:
        key = _retirement_quarantine_key(ledger)
    except Exception:
        # Losing the path identity means no replacement can safely determine
        # which durable store contains this owner.  Preserve safety with the
        # bounded process-wide fail-closed sentinel.
        with _retirement_quarantine_lock:
            _retirement_quarantine_registry_saturated = True
        raise
    owner = ledger.instance_id
    with _retirement_quarantine_lock:
        quarantine = _retirement_quarantines.get(key)
        if quarantine is None:
            if len(_retirement_quarantines) >= _MAX_RETIREMENT_QUARANTINE_DATABASES:
                _retirement_quarantine_registry_saturated = True
                return False
            quarantine = _RetirementQuarantine()
            _retirement_quarantines[key] = quarantine
        if owner in quarantine.owners:
            return True
        if len(quarantine.owners) >= _MAX_RETIREMENT_QUARANTINE_OWNERS_PER_DATABASE:
            quarantine.saturated = True
            return False
        quarantine.owners[owner] = None
        return True


def _quarantined_retirement_owners(
    ledger: WebhookOperationLedger,
) -> tuple[str, ...]:
    """Snapshot exact prior owners for this ledger, or fail closed."""

    key = _retirement_quarantine_key(ledger)
    with _retirement_quarantine_lock:
        if _retirement_quarantine_registry_saturated:
            raise WebhookLedgerError(
                "webhook retirement quarantine registry is saturated; "
                "process restart is required"
            )
        quarantine = _retirement_quarantines.get(key)
        if quarantine is None:
            return ()
        if quarantine.saturated:
            raise WebhookLedgerError(
                "webhook retirement quarantine for this ledger is saturated; "
                "process restart is required"
            )
        return tuple(quarantine.owners)


def _clear_quarantined_retirement_owner(
    ledger: WebhookOperationLedger,
    owner: str,
) -> None:
    """Clear only one successfully retired owner marker."""

    key = _retirement_quarantine_key(ledger)
    with _retirement_quarantine_lock:
        quarantine = _retirement_quarantines.get(key)
        if quarantine is None:
            return
        quarantine.owners.pop(owner, None)
        if not quarantine.owners and not quarantine.saturated:
            _retirement_quarantines.pop(key, None)


_PROMPT_TOKEN_RE = re.compile(
    r"\{(?P<token>__raw__(?::(?P<raw_cap>[^{}]*))?|[a-zA-Z0-9_.]+)\}"
)
# Hostnames/IP literals that only serve connections originating on the same
# machine. Anything else is treated as a public bind for safety-rail purposes.
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ip6-localhost",
    "ip6-loopback",
})


def _is_loopback_host(host: Optional[str]) -> bool:
    """True when `host` binds only to the local machine.

    Covers IPv4 loopback, the standard `localhost` alias, IPv6 loopback in
    both bracketed and bare form, and the common Debian-style aliases. Any
    falsy value (empty string, None) is conservatively treated as non-loopback
    because an unset host usually means the platform-default public bind.
    """
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def check_webhook_requirements() -> bool:
    """Check if webhook adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


def _reject_nonfinite_json(constant: str) -> None:
    raise ValueError(f"non-finite JSON number {constant}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _snapshot_route_config(route: Any) -> dict[str, Any]:
    """Detach one JSON-only execution policy from hot-reloadable config."""

    if not isinstance(route, dict):
        raise WebhookContractError("webhook route config must be an object")
    try:
        encoded = json.dumps(
            route,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        snapshot = json.loads(encoded, parse_constant=_reject_nonfinite_json)
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise WebhookContractError(
            "webhook route execution policy must use JSON values"
        ) from exc
    if not isinstance(snapshot, dict):  # pragma: no cover - guarded above
        raise WebhookContractError("webhook route config must be an object")
    return snapshot


def _plain_json_snapshot(value: Any) -> Any:
    """Detach frozen JSON containers without accepting arbitrary objects."""

    if isinstance(value, Mapping):
        return {str(key): _plain_json_snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_snapshot(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise WebhookContractError("durable webhook carrier contains a non-JSON value")


@dataclass
class WebhookMessageEvent(MessageEvent):
    """Agent event carrying durable execution authority across async seams."""

    webhook_authority: OperationAuthority = field(kw_only=True)
    webhook_envelope: Optional[WebhookEnvelope] = field(default=None, kw_only=True)


@dataclass(frozen=True)
class PreparedTargetTemplate:
    """Structurally validated delivery choice plus snapshotted home fallback."""

    kind: str
    profile: str
    platform: Optional[str] = None
    home_chat_id: Optional[str] = None
    home_thread_id: Optional[str] = None
    home_scope_id: Optional[str] = None
    slack_static_chat_id: Optional[str] = None
    slack_static_scope_id: Optional[str] = None
    slack_scope_locked: bool = False


@dataclass(frozen=True)
class PreparedSkillInvocation:
    """Exact skill scaffold captured before an authenticated request runs."""

    command: str
    prefix: str
    suffix: str
    source_sha256: str
    inject_prompt: bool = True

    def render(self, prompt: str) -> str:
        if not self.inject_prompt:
            return self.prefix
        return f"{self.prefix}{prompt}{self.suffix}"


@dataclass(frozen=True)
class AuthenticatedRouteAuthority:
    """One immutable publication consumed as a unit by request handling."""

    authority: tuple[Any, ...]
    secret: str
    route_config: dict[str, Any]
    filter_route_config: dict[str, Any]
    effective_toolsets: tuple[str, ...]
    prepared_script: Optional[WebhookPreparedScript]
    prepared_skill: Optional[PreparedSkillInvocation]
    prepared_target: PreparedTargetTemplate
    profile_generation: str


class WebhookTargetDeliveryDisposition(str, Enum):
    """Typed result of consuming one durably staged target carrier."""

    CONFIRMED = "confirmed"
    SUPPRESSED = "suppressed"
    CACHED = "cached"
    PRE_EFFECT_FAILED = "pre_effect_failed"
    IN_PROGRESS = "in_progress"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class WebhookTargetDeliveryResult:
    disposition: WebhookTargetDeliveryDisposition
    message_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.disposition in {
            WebhookTargetDeliveryDisposition.CONFIRMED,
            WebhookTargetDeliveryDisposition.SUPPRESSED,
            WebhookTargetDeliveryDisposition.CACHED,
        }


def _bounded_positive_int(
    value: Any,
    *,
    default: int,
    maximum: int,
    minimum: int = 1,
) -> int:
    """Parse an integer setting and keep it inside a safe positive range."""

    if isinstance(value, bool):
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
    return min(max(parsed, minimum), maximum)


def _strict_bounded_int(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
    unit: Optional[str] = None,
) -> int:
    """Parse a safety authority without silently widening invalid input."""

    if isinstance(value, bool):
        raise WebhookConfigurationError(f"{label} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        parsed = int(value.strip())
    else:
        raise WebhookConfigurationError(f"{label} must be an integer")
    if parsed < minimum or parsed > maximum:
        suffix = f" {unit}" if unit else ""
        raise WebhookConfigurationError(
            f"{label} must be between {minimum} and {maximum}{suffix}"
        )
    return parsed


def _authentication_key_fingerprints(
    secret: str,
    signature_mode: str,
) -> frozenset[bytes]:
    """Return log-safe equality keys for every reusable secret material.

    Svix and Standard Webhooks decode ``whsec_`` values before verification.
    Both the configured text and the decoded verifier key are authority: a
    caller who knows either representation can derive the other.  Comparing
    both closes collisions with raw-key modes in both directions.
    """

    try:
        raw_key = secret.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WebhookContractError("webhook secret is not valid Unicode text") from exc
    effective_key = raw_key
    if signature_mode in {"svix", "standard_webhooks"} and secret.startswith("whsec_"):
        try:
            effective_key = base64.b64decode(
                secret.removeprefix("whsec_"),
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise WebhookContractError(
                "webhook whsec_ secret is not valid base64"
            ) from exc
        if not effective_key:
            raise WebhookContractError(
                "webhook whsec_ secret must decode to a non-empty key"
            )
    fingerprints: set[bytes] = set()
    for material in {raw_key, effective_key}:
        fingerprints.add(hashlib.sha256(material).digest())
        # HMAC-SHA256 first hashes keys longer than its 64-byte block, then
        # zero-pads shorter keys. Consequently b"key" and b"key\0" (and a
        # long key versus its 32-byte SHA-256 digest) are verifier-equivalent.
        normalized = (
            hashlib.sha256(material).digest()
            if len(material) > hashlib.sha256().block_size
            else material
        )
        hmac_key_block = normalized.ljust(hashlib.sha256().block_size, b"\0")
        fingerprints.add(hashlib.sha256(hmac_key_block).digest())
    return frozenset(fingerprints)


def _route_policy_sha256(
    route: Mapping[str, Any],
    authority_profile: str,
    effective_toolsets: tuple[str, ...],
    script_sha256: Optional[str],
    profile_generation: str,
    filter_authority: tuple[tuple[str, str], ...],
    skill_sha256: Optional[str],
    target_authority: Mapping[str, Any],
) -> str:
    """Digest the complete non-secret execution policy for key continuity."""

    if any(not isinstance(key, str) for key in route):
        raise WebhookContractError("webhook route policy keys must be strings")
    route_policy = {
        key: value for key, value in route.items() if key not in {"secret", "profile"}
    }
    # URL routing keeps its canonical ``default``/explicit-profile shape, but
    # execution policy continuity belongs to the physical authority domain.
    # This makes named nonmultiplex A (omitted profile) equivalent to
    # multiplex A (explicit profile=A) without widening either URL route.
    route_policy["profile"] = authority_profile
    policy = {
        "route": route_policy,
        "effective_toolsets": list(effective_toolsets),
        "script_sha256": script_sha256,
        "profile_generation": profile_generation,
        "filter_authority": [list(item) for item in filter_authority],
        "skill_sha256": skill_sha256,
        "target_authority": dict(target_authority),
    }
    try:
        canonical = json.dumps(
            policy,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise WebhookContractError(
            "webhook route policy must be finite JSON data"
        ) from exc
    return hashlib.sha256(canonical).hexdigest()


def _canonical_snapshot_size(value: Mapping[str, Any]) -> int:
    """Return the exact UTF-8 size used by durable canonical JSON."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise WebhookContractError(
            "webhook execution snapshot is not canonical JSON"
        ) from exc
    return len(encoded)


def _read_profile_incarnation(path: Path) -> str:
    """Read one bounded, regular, owner-only physical-profile token."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WebhookContractError(
            "profile authority incarnation token is unavailable"
        ) from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise WebhookContractError(
                "profile authority incarnation token is not a regular file"
            )
        if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise WebhookContractError(
                "profile authority incarnation token must have mode 0600"
            )
        expected_size = _PROFILE_AUTHORITY_INCARNATION_BYTES * 2 + 1
        raw = os.read(fd, expected_size + 1)
        if len(raw) != expected_size:
            raise WebhookContractError(
                "profile authority incarnation token has invalid length"
            )
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise WebhookContractError(
            "profile authority incarnation token is not ASCII"
        ) from exc
    if not re.fullmatch(
        rf"[0-9a-f]{{{_PROFILE_AUTHORITY_INCARNATION_BYTES * 2}}}\n",
        token,
    ):
        raise WebhookContractError(
            "profile authority incarnation token is not canonical"
        )
    return token[:-1]


def _profile_incarnation_token(profile_home: Path) -> str:
    """Atomically create or load the durable random profile incarnation."""

    token_path = profile_home / _PROFILE_AUTHORITY_INCARNATION_FILENAME
    try:
        return _read_profile_incarnation(token_path)
    except WebhookContractError as exc:
        # Creation is authorized only by a true missing-path result. A
        # malformed, symlinked, or unreadable final token is existing authority
        # corruption and must fail closed rather than being replaced.
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
    token = secrets.token_hex(_PROFILE_AUTHORITY_INCARNATION_BYTES)
    payload = f"{token}\n".encode("ascii")
    temporary_path = profile_home / (
        f".{_PROFILE_AUTHORITY_INCARNATION_FILENAME}.{secrets.token_hex(16)}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: Optional[int] = None
    try:
        try:
            fd = os.open(temporary_path, flags, 0o600)
        except OSError as exc:
            raise WebhookContractError(
                "profile authority incarnation token cannot be created"
            ) from exc
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("short profile incarnation token write")
                offset += written
            os.fsync(fd)
        except OSError as exc:
            raise WebhookContractError(
                "profile authority incarnation token cannot be persisted"
            ) from exc
        finally:
            open_fd = fd
            fd = None
            os.close(open_fd)

        published = False
        try:
            os.link(
                temporary_path,
                token_path,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError:
            return _read_profile_incarnation(token_path)
        except OSError as exc:
            raise WebhookContractError(
                "profile authority incarnation token cannot be published"
            ) from exc
        if published and os.name == "posix" and hasattr(os, "O_DIRECTORY"):
            directory_flags = os.O_RDONLY | os.O_DIRECTORY
            try:
                directory_fd = os.open(profile_home, directory_flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                raise WebhookContractError(
                    "profile authority incarnation directory cannot be persisted"
                ) from exc
        return token
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                logger.warning(
                    "[webhook] Could not close temporary profile incarnation %s",
                    temporary_path,
                )
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "[webhook] Could not remove temporary profile incarnation %s",
                temporary_path,
            )
