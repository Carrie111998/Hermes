"""External drain-control marker contract (dashboard → gateway).

Task 2.2 of the safe-shutdown plan (decisions.md Q-B, option A): the dashboard
has no way to call into a running gateway — there is no HTTP control channel
into the gateway process (guardrails: "there is NO external control channel
into a running gateway"). Restart/drain is driven only by the gateway reacting
to its own inputs: slash commands, process signals, and file markers it writes
itself (``.restart_notify.json``).

So the begin/cancel-drain dashboard endpoint communicates with the running
gateway the same way: it writes (or removes) a marker file, and a gateway
background watcher reacts to it. This module owns that marker contract so both
sides — the dashboard endpoint (writer) and the gateway watcher (reader) —
share one definition and can never disagree.

Contract (presence-based, mirroring ``.restart_notify.json``):

  * begin-drain  → write ``{HERMES_HOME}/.drain_request.json`` with
    ``{"action": "drain", "requested_at": <iso>, "principal": <str>,
    "epoch": <instantiation-epoch>, "suppress_notification": <bool>}``.
  * cancel-drain → remove the marker.
  * The gateway watcher treats **presence of a marker stamped with the current
    instantiation epoch** as "external drain active": flip
    ``gateway_state -> "draining"`` and stop accepting new turns. Absence (or a
    marker from a *prior* instantiation) means "not draining" (revert to
    ``running`` if we had flipped it).

Why the epoch (NS-570). ``HERMES_HOME`` is a **durable** store — on Hermes
Cloud it is a persistent Fly volume (``/opt/data``). A begin-drain marker
written there *survives a machine restart*. But the disruptive lifecycle
actions a drain protects (auto-update / image migrate / env edit / profile
change) all **restart the machine**, which is exactly the signal that the drain
is over. Without the epoch, a freshly-restarted gateway re-reads the orphaned
marker on boot and parks itself right back in ``draining`` forever (NS-570: an
auto-updated instance refused every turn for ~52 min). Stamping the marker with
an identity of *this* container/VM instantiation, and ignoring a marker whose
epoch doesn't match, makes "a deliberate restart clears the drain" true by
construction — while a marker written during the *current* instantiation (the
live drain) still matches, and an s6 respawn of just the gateway (PID 1 / init
unchanged) still honours an in-flight drain.

Reading the marker never raises: a malformed/half-written file reads as
"present but contentless", which the watcher still treats as drain-active
(fail-safe toward quiescing — a corrupt begin marker must not be ignored). The
epoch check is deliberately **lenient**: it ignores a marker only on a
*definite* epoch mismatch. A marker with no epoch (legacy/corrupt/contentless),
or an environment where the epoch cannot be computed (non-Linux, no ``/proc``),
both degrade to the original presence-only behaviour — never fail-closed.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no production helper.
    fcntl = None  # type: ignore[assignment]

from hermes_constants import get_hermes_home
from utils import atomic_json_write

_log = logging.getLogger(__name__)

_DRAIN_REQUEST_FILENAME = ".drain_request.json"
_DRAIN_REQUEST_LOCK_FILENAME = ".drain_request.lock"
_PROCESS_DRAIN_MUTATION_LOCK = threading.RLock()
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HELD_TRANSACTION_FIELD = "held_transaction_sha256"
_HELD_CAPABILITY_FIELD = "held_mutation_capability_sha256"
_MAX_MUTATION_MARKER_BYTES = 64 * 1024


@functools.lru_cache(maxsize=1)
def current_instantiation_epoch() -> str:
    """Identity of THIS container / VM instantiation.

    Stable for the life of the PID-1 init process — so an s6 respawn of just
    the gateway keeps the same epoch and an in-flight drain is honoured — but
    changes when the machine/container is recreated (a fresh PID 1 → a fresh
    epoch). Composed from two ``/proc`` facts:

      * the kernel **boot id** (``/proc/sys/kernel/random/boot_id``) — changes
        on a VM / microVM reboot (e.g. a Fly Firecracker machine restart);
      * **PID 1's start time** (field 22 of ``/proc/1/stat``) — changes on a
        plain ``docker restart`` (the host kernel, hence boot_id, is unchanged,
        but ``/init`` is a brand-new process).

    Together they discriminate every restart mode that matters:

      | event                          | boot_id | pid1 start | epoch  | marker |
      |--------------------------------|---------|------------|--------|--------|
      | Fly microVM reboot (auto-upd.) | changes | changes    | NEW    | reject |
      | plain ``docker restart``       | same    | changes    | NEW    | reject |
      | s6 respawn of the gateway only | same    | same       | SAME   | honour |
      | host ``hermes gateway restart``| same    | same(init) | SAME   | honour |

    The last row is intentional: a host install has no durable-volume drain
    bug, and honouring a drain across a deliberate process restart is the
    intended reversible behaviour (D4a) — PID 1 there is the long-lived init
    (systemd/launchd), so the epoch is stable.

    Returns ``""`` when neither identity source is readable (non-Linux, no
    ``/proc``). An empty epoch disables the staleness check downstream,
    degrading to the released presence-only behaviour — never fail-closed.
    Memoised: the epoch is constant for the life of the process.
    """
    boot_id = ""
    try:
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        pass

    pid1_start = ""
    try:
        # /proc/1/stat: "<pid> (<comm>) <state> ... <starttime@field22> ...".
        # comm can contain spaces and parens, so split on the LAST ')' and
        # index into the whitespace-delimited tail. starttime is field 22
        # (1-indexed); after the comm the tail starts at field 3, so it is the
        # tail's index 19.
        stat = Path("/proc/1/stat").read_text(encoding="utf-8")
        tail = stat.rsplit(")", 1)[1].split()
        pid1_start = tail[19]
    except (OSError, IndexError):
        pass

    if not boot_id and not pid1_start:
        return ""
    return f"{boot_id}:{pid1_start}"


def drain_request_path(home: Optional[Path] = None) -> Path:
    """Absolute path to the drain-request marker, respecting HERMES_HOME."""
    base = home if home is not None else get_hermes_home()
    return Path(base) / _DRAIN_REQUEST_FILENAME


def drain_request_lock_path(home: Optional[Path] = None) -> Path:
    """Shared lock used by every drain-marker mutator."""
    base = home if home is not None else get_hermes_home()
    return Path(base) / _DRAIN_REQUEST_LOCK_FILENAME


@contextmanager
def drain_request_mutation_lock(
    *,
    home: Optional[Path] = None,
) -> Iterator[None]:
    """Serialize marker replacement/removal across gateway processes.

    The production release helper takes this same advisory lock before it
    publishes or clears its receipt-bound marker.  A stable descriptor/path
    identity check prevents a replaced lock pathname from splitting writers
    across different lock inodes.
    """
    path = drain_request_lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _PROCESS_DRAIN_MUTATION_LOCK:
        descriptor = os.open(path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
            reachable = path.stat(follow_symlinks=False)
            expected_uid = (
                os.geteuid()
                if hasattr(os, "geteuid")
                else opened.st_uid
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(reachable.st_mode)
                or opened.st_dev != reachable.st_dev
                or opened.st_ino != reachable.st_ino
                or opened.st_uid != expected_uid
                or (
                    os.name != "nt"
                    and stat.S_IMODE(opened.st_mode) != 0o600
                )
            ):
                raise OSError("unsafe drain-request lock identity")
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                reachable = path.stat(follow_symlinks=False)
                if (
                    opened.st_dev != reachable.st_dev
                    or opened.st_ino != reachable.st_ino
                ):
                    raise OSError("drain-request lock changed while acquiring")
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _capability_sha256(capability: str) -> str:
    if (
        not isinstance(capability, str)
        or len(capability.encode("utf-8")) < 32
        or len(capability.encode("utf-8")) > 1024
    ):
        raise ValueError("invalid drain-marker mutation capability")
    return hashlib.sha256(capability.encode("utf-8")).hexdigest()


def _read_marker_for_mutation(path: Path) -> Optional[dict[str, Any]]:
    """Strict read used only by mutators; unlike the watcher it fails closed."""
    if not os.path.lexists(path):
        return None
    descriptor: Optional[int] = None
    try:
        before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = _MAX_MUTATION_MARKER_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        reachable = path.lstat()
    except OSError as exc:
        raise PermissionError("drain marker cannot be verified for mutation") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_gid,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    expected_uid = (
        os.geteuid()
        if hasattr(os, "geteuid")
        else opened.st_uid
    )
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != expected_uid
        or (
            os.name != "nt"
            and stat.S_IMODE(opened.st_mode) != 0o600
        )
        or len(raw) != opened.st_size
        or not 0 < len(raw) <= _MAX_MUTATION_MARKER_BYTES
        or identity(before) != identity(opened)
        or identity(before) != identity(after)
        or identity(before) != identity(reachable)
    ):
        raise PermissionError("drain marker cannot be verified for mutation")
    listxattr = getattr(os, "listxattr", None)
    if listxattr is not None:
        try:
            attributes = listxattr(path, follow_symlinks=False)
        except OSError as exc:
            raise PermissionError(
                "drain marker metadata cannot be verified for mutation"
            ) from exc
        if attributes:
            raise PermissionError(
                "drain marker metadata cannot be verified for mutation"
            )
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise PermissionError("malformed drain marker cannot be mutated") from exc
    if not isinstance(value, dict) or not value:
        raise PermissionError("malformed drain marker cannot be mutated")
    return value


def _held_marker_binding(body: Optional[dict[str, Any]]) -> Optional[tuple[str, str]]:
    """Return the held transaction/capability digests, failing closed.

    A transaction-held marker intentionally cannot be replaced or removed by
    ordinary dashboard/gateway callers while a release is between activation
    and final-health.  The marker contains only digests; the root-only release
    manifest retains the capability preimage.
    """
    if body is None:
        return None
    if not body:
        raise PermissionError("malformed drain marker cannot be mutated")
    transaction = body.get(_HELD_TRANSACTION_FIELD)
    capability = body.get(_HELD_CAPABILITY_FIELD)
    if transaction is None and capability is None:
        return None
    if (
        not isinstance(transaction, str)
        or _SHA256.fullmatch(transaction) is None
        or not isinstance(capability, str)
        or _SHA256.fullmatch(capability) is None
    ):
        raise PermissionError("malformed held drain marker cannot be mutated")
    return transaction, capability


def _authorize_marker_mutation(
    body: Optional[dict[str, Any]],
    mutation_capability: Optional[str],
) -> Optional[tuple[str, str]]:
    binding = _held_marker_binding(body)
    if binding is None:
        return None
    _transaction, expected = binding
    if mutation_capability is None or not hmac.compare_digest(
        _capability_sha256(mutation_capability),
        expected,
    ):
        raise PermissionError("transaction-held drain marker")
    return binding


def write_drain_request(
    *,
    principal: str = "drain-control",
    suppress_notification: bool = False,
    home: Optional[Path] = None,
    mutation_capability: Optional[str] = None,
    hold_transaction_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Write the begin-drain marker. Returns the payload written.

    Atomic write so the gateway watcher never reads a half-written file.
    Idempotent: re-writing while a drain is already in progress just refreshes
    ``requested_at`` (harmless — the watcher keys off presence, not content).

    Stamps the marker with :func:`current_instantiation_epoch` so a marker that
    later survives a machine restart on the durable HERMES_HOME volume can be
    recognised as stale and ignored (NS-570).

    ``suppress_notification`` is a generic "be quiet on the shutdown that ends
    this drain" flag. When the drain culminates in a process exit (e.g. NAS
    recreates the machine for an auto-update image migration), the gateway's
    shutdown path reads it via :func:`drain_notification_suppressed` and skips
    the *home-channel* "gateway shutting down" broadcast — the operator-flavoured
    ping that would otherwise fire on every routine auto-update, potentially
    dozens of times a day. It NEVER suppresses the per-active-session interrupt
    ping. The gateway stays agnostic about *why* the drain is quiet; the policy
    of which drain causes set the flag lives entirely in the caller (NAS). The
    field defaults False so legacy/operator drains behave exactly as before.
    """
    with drain_request_mutation_lock(home=home):
        existing = _read_marker_for_mutation(drain_request_path(home))
        held = _authorize_marker_mutation(existing, mutation_capability)
        if hold_transaction_sha256 is not None:
            if (
                not isinstance(hold_transaction_sha256, str)
                or _SHA256.fullmatch(hold_transaction_sha256) is None
                or mutation_capability is None
            ):
                raise ValueError("held drain marker requires exact transaction binding")
            if held is not None and held[0] != hold_transaction_sha256:
                raise PermissionError("held drain marker belongs to another transaction")
            held = (
                hold_transaction_sha256,
                _capability_sha256(mutation_capability),
            )
        payload = {
            "action": "drain",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "principal": principal,
            "epoch": current_instantiation_epoch(),
            "suppress_notification": bool(suppress_notification),
        }
        if held is not None:
            payload[_HELD_TRANSACTION_FIELD] = held[0]
            payload[_HELD_CAPABILITY_FIELD] = held[1]
        atomic_json_write(drain_request_path(home), payload)
    return payload


def clear_drain_request(
    *,
    home: Optional[Path] = None,
    mutation_capability: Optional[str] = None,
) -> bool:
    """Remove the drain marker (cancel-drain). Returns True if one existed.

    Best-effort: a missing file is not an error (cancel is idempotent).
    """
    path = drain_request_path(home)
    with drain_request_mutation_lock(home=home):
        _authorize_marker_mutation(
            _read_marker_for_mutation(path),
            mutation_capability,
        )
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            _log.warning("drain-control: failed to remove %s: %s", path, e)
            return False


def _marker_epoch_is_stale(body: dict[str, Any]) -> bool:
    """True iff ``body``'s epoch is a *definite* mismatch with this process.

    Lenient by design — returns False (i.e. "not stale, honour it") whenever it
    can't be sure:
      * the current epoch can't be computed ("" fallback, no /proc), OR
      * the marker carries no epoch (legacy marker, or a corrupt/contentless
        ``{}`` body).
    Only a marker whose epoch is present AND differs from the current
    instantiation epoch is considered stale. This preserves the
    fail-safe-toward-quiescing contract for malformed markers.
    """
    current = current_instantiation_epoch()
    if not current:
        return False
    marker_epoch = body.get("epoch")
    if not marker_epoch:
        return False
    return marker_epoch != current


def drain_requested(*, home: Optional[Path] = None) -> bool:
    """True iff a begin-drain marker for THIS instantiation is present.

    A marker whose ``epoch`` does not match the current instantiation epoch is
    treated as absent: it survived a container/VM restart (HERMES_HOME is a
    durable Fly volume on Hermes Cloud) and the lifecycle action that triggered
    the drain has already completed — honouring it would wedge the
    freshly-restarted gateway in ``draining`` (NS-570). The staleness check is
    lenient (see :func:`_marker_epoch_is_stale`): a legacy/corrupt marker with
    no epoch, or an environment without ``/proc``, still reads as drain-active.
    """
    body = read_drain_request(home=home)
    if body is None:
        return False
    if _marker_epoch_is_stale(body):
        return False
    return True


def drain_notification_suppressed(*, home: Optional[Path] = None) -> bool:
    """True iff an ACTIVE drain marker asks to suppress the shutdown broadcast.

    "Active" means exactly what :func:`drain_requested` means — a marker present
    AND stamped with the current instantiation epoch. A stale (other-epoch)
    marker that survived a machine restart on the durable HERMES_HOME volume is
    ignored here just as it is for drain state (NS-570): we must never let an
    orphaned marker's flag silence a *fresh* gateway's legitimate shutdown
    broadcast.

    Only honours the flag when it is explicitly truthy in the marker body. A
    legacy marker without the field, a corrupt/contentless ``{}`` body, or an
    absent marker all read as "not suppressed" (False) — fail toward the louder,
    more-visible behaviour, consistent with :func:`read_drain_request`'s
    never-raise contract. The gateway's shutdown path uses this to skip ONLY the
    home-channel broadcast; the per-active-session interrupt ping is unaffected.
    """
    body = read_drain_request(home=home)
    if body is None:
        return False
    if _marker_epoch_is_stale(body):
        return False
    return bool(body.get("suppress_notification"))


def read_drain_request(*, home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the marker payload, or ``None`` if absent.

    A present-but-unparseable marker returns ``{}`` (truthy-presence preserved
    via :func:`drain_requested`; callers that need the body get an empty dict
    rather than an exception). Never raises.
    """
    path = drain_request_path(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        _log.warning("drain-control: failed to read %s: %s", path, e)
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def active_drain_observation(
    *,
    home: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Return a digest-bound observation of one stable active marker.

    This is used only for the gateway's persisted drain acknowledgment.  It
    does not change the legacy fail-safe presence semantics of
    :func:`drain_requested`: malformed markers still engage drain, but cannot
    produce an exact acknowledgment for an offline release transaction.
    """

    path = drain_request_path(home)
    try:
        first = path.read_bytes()
        second = path.read_bytes()
    except OSError:
        return None
    if (
        not first
        or first != second
        or len(first) > _MAX_MUTATION_MARKER_BYTES
    ):
        return None
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate drain marker key")
            result[key] = value
        return result

    try:
        body = json.loads(first, object_pairs_hook=pairs)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(body, dict)
        or _marker_epoch_is_stale(body)
        or not isinstance(body.get("epoch"), str)
        or not body["epoch"]
    ):
        return None
    transaction = body.get(_HELD_TRANSACTION_FIELD)
    capability = body.get(_HELD_CAPABILITY_FIELD)
    if (
        not isinstance(transaction, str)
        or _SHA256.fullmatch(transaction) is None
        or not isinstance(capability, str)
        or _SHA256.fullmatch(capability) is None
    ):
        return None
    return {
        "marker_sha256": hashlib.sha256(first).hexdigest(),
        "transaction_sha256": transaction,
        "mutation_capability_sha256": capability,
        "epoch": body["epoch"],
    }
