"""Bot Mode cross-connection relay — connections ARE the peer set.

Every gateway connected to the user's Desktop (local, remote URL, SSH,
Hermes Cloud, docker) is a persistent line. This module is the gateway-side
half of the relay that rides those lines so agents on ANY connected gateway
can find and message agents on ANY other, with `message_agent` as the one
send path (Teknium ruling, Aug 2026 — the peers-vs-connections split was
itself the bug).

How the relay works:

- ``bot_relay/rosters/`` — one route snapshot per Desktop namespace, pushed
  over each connection's WebSocket (``bot_relay.roster.sync``).
  ``tools/bot_mode_probe.py`` folds it into the Bot Chat protocol section so
  every bot knows every reachable teammate, and ``message_agent`` resolves
  cross-connection targets against it.
- profile ``state.db`` — v2 envelopes, renewable courier leases, immutable
  terminal outcomes, and recipient receipts.  The Desktop claims only free
  capacity, renews slow turns, then ACKs or NACKs with a fenced token and
  generation.  Expired leases are reclaimable; committed target results are
  replayed without a second Bot Chat turn.
- ``bot_relay/outbox|claimed|replies`` — the explicit v1 rolling-upgrade lane
  and a compatibility result projection.  These files are not v2 authority.

The gateway never holds another connection's credentials; the Desktop owns
every socket and does all cross-connection I/O. The state machine is local;
this module performs no network I/O. Everything else here is plain file
plumbing on the gateway's own HERMES root — no network. The public helpers
never raise, with one deliberate exception: ``enqueue_envelope`` raises
``EnvelopeRefusedError`` when the target is definitively offline, so the
sender fails fast instead of queueing a DM nobody will drain (#93091).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

RELAY_DIR_NAME = "bot_relay"
ROSTER_FILE = "roster.json"
ROSTERS_DIR = "rosters"
OUTBOX_DIR = "outbox"
CLAIMED_DIR = "claimed"
REPLIES_DIR = "replies"
LOCKS_DIR = "locks"

# Fallback wait budget for a queued delivery turn when config is unreadable.
# The real knob is ``bot_mode.turn_wait_seconds`` in config.yaml.
TURN_WAIT_SECONDS_FALLBACK = 120

DELIVERY_STREAM = "bot_relay.outbox.v2"
DELIVERY_INBOX = "bot_relay.inbox.v2"

# A reply must arrive before the waiter gives up. Cross-connection turns can
# be slow (remote model, cold gateway) — generous, but bounded.
REPLY_WAIT_SECONDS = 1200

# Envelopes and replies older than this are stale artifacts (Desktop was
# closed, connection died) and are swept opportunistically.
STALE_AFTER_SECONDS = 6 * 3600

# Fallback envelope TTL when config is unreachable — mirrors the
# ``bot_mode.envelope_ttl_seconds`` default in hermes_cli/config_defaults.py.
# Envelopes older than the TTL are refused at drain time with a
# 'queued_expired' error reply instead of being delivered late.
DEFAULT_ENVELOPE_TTL_SECONDS = 900

# A roster older than this proves nothing about who is offline: the Desktop
# pushes roster.sync on connection-state changes, so only a recently-written
# roster is treated as an authoritative view for the fail-fast check.
ROSTER_FRESH_SECONDS = 600


class EnvelopeRefusedError(RuntimeError):
    """``enqueue_envelope`` refused to queue — nothing was written to disk.

    ``reason`` is a stable machine code; ``str(exc)`` is the human text.
    'runtime_offline' matches the #93091 item-1 failure-reason enum (plain
    literal here so the branches merge cleanly).
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# A short source lease is renewed while a 600s target turn is in flight.  If
# the renderer dies, another courier can recover promptly; the target's longer
# durable processing receipt prevents that recovery from running a second
# tool-capable turn.
LEASE_SECONDS = 180
DELIVERY_PROCESSING_SECONDS = 660
MAX_CLAIM_BATCH = 32
# Five-minute capped retries need at least 72 attempts to span the six-hour
# envelope deadline.  Keep a finite poison-message guard, but never let that
# guard defeat the advertised reconnect horizon under ordinary outages.
MAX_DELIVERY_ATTEMPTS = 128
MAX_ROSTER_ROWS = 500
MAX_ROSTER_BYTES = 512 * 1024
ROSTER_STALE_SECONDS = 3 * 60

_HANDLE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_OPAQUE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Desktop normally claims one envelope at a time.  Keep the next profile
# cursor across RPCs so a perpetually busy default profile cannot starve a
# named profile merely because state.db is first in the filesystem order.
# The lock also gives concurrent claim RPCs distinct starting positions.
_claim_cursor_lock = threading.Lock()
_claim_cursors: dict[tuple[str, str], int] = {}
_MAX_CLAIM_CURSOR_KEYS = 128


def _clean_label(value: Any, limit: int) -> str:
    """Collapse controls/newlines in Desktop-provided prompt text."""
    printable = "".join(ch if ch.isprintable() else " " for ch in str(value or ""))
    return " ".join(printable.split())[:limit]


def _normalize_opaque_id(value: Any, *, field: str, required: bool = True) -> str:
    cleaned = str(value or "").strip()
    if not cleaned and not required:
        return ""
    if not _OPAQUE_ID_RE.fullmatch(cleaned):
        raise ValueError(f"invalid {field}")
    return cleaned


def relay_root(root: Path | str) -> Path:
    return Path(root) / RELAY_DIR_NAME


def _profile_home(root: Path | str, profile: str) -> Path:
    base = Path(root)
    return base if profile == "default" else base / "profiles" / profile


def _profile_state_db(root: Path | str, profile: str) -> Path:
    return _profile_home(root, profile) / "state.db"


def _profile_state_dbs(root: Path | str) -> list[Path]:
    """Every profile-owned state.db a gateway relay may courier from."""
    base = Path(root)
    paths = [base / "state.db"]
    profiles = base / "profiles"
    try:
        paths.extend(
            child / "state.db"
            for child in sorted(profiles.iterdir())
            if child.is_dir()
        )
    except OSError:
        pass
    return paths


def _ensure_dirs(root: Path | str) -> Path:
    base = relay_root(root)
    for sub in (ROSTERS_DIR, OUTBOX_DIR, CLAIMED_DIR, REPLIES_DIR):
        (base / sub).mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
        for sub in (ROSTERS_DIR, OUTBOX_DIR, CLAIMED_DIR, REPLIES_DIR):
            (base / sub).chmod(0o700)
    except OSError:
        # Windows and some network filesystems do not implement POSIX modes.
        pass
    return base


# ── remote roster ────────────────────────────────────────────────────────────


def _normalize_roster_row(row: Any) -> Optional[dict]:
    """Validated, minimal roster row or None.

    Rows come from the Desktop over RPC — treat as untrusted input. A row
    names an agent on another connection: profile name, taggable handle,
    the connection id/label of the gateway that owns it, and optional
    friendly title/description for the protocol section.
    """
    if not isinstance(row, dict):
        return None
    profile = str(row.get("profile") or "").strip()
    handle = str(row.get("handle") or "").strip().lstrip("@")
    connection_id = str(row.get("connection_id") or "").strip()
    if not profile or not connection_id:
        return None
    if not handle:
        handle = "hermes" if profile == "default" else profile
    if not _HANDLE_RE.fullmatch(handle) or not _HANDLE_RE.fullmatch(profile):
        return None
    try:
        # Connection ids are short opaque route tokens; keep the stricter
        # handle charset (64-char cap) that also excludes shell metacharacters.
        connection_id = _normalize_opaque_id(connection_id, field="connection id")
        if not _HANDLE_RE.fullmatch(connection_id):
            return None
        namespace = _normalize_opaque_id(
            row.get("courier_namespace_id"),
            field="courier namespace",
            required=False,
        )
        install_id = _normalize_opaque_id(
            row.get("target_install_id") or row.get("install_id"),
            field="target install id",
            required=False,
        )
    except ValueError:
        return None
    out = {
        "profile": profile,
        "handle": handle,
        "connection_id": connection_id,
        "courier_namespace_id": namespace,
        "target_install_id": install_id,
        "connection_label": _clean_label(row.get("connection_label"), 80),
        "title": _clean_label(row.get("title"), 120),
        "description": _clean_label(row.get("description"), 160),
    }
    # Optional explicit liveness flag (additive — the Desktop may push it).
    # Preserved only when it is a real bool so absent stays distinguishable
    # from false: absent == liveness unknown == fail-open on enqueue.
    if isinstance(row.get("online"), bool):
        out["online"] = row["online"]
    return out


def write_remote_roster(
    root: Path | str, rows: Any, *, courier_namespace_id: str = ""
) -> int:
    """Atomically persist one Desktop namespace's remote roster.

    Connection ids are Desktop-local route coordinates.  A namespaced
    snapshot prevents a second Desktop from overwriting those coordinates or
    claiming events it cannot route.  Tokenless v1 callers retain the legacy
    single-snapshot lane for rolling upgrades.
    """
    base = _ensure_dirs(root)
    namespace = _normalize_opaque_id(
        courier_namespace_id, field="courier namespace", required=False
    )
    cleaned: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in (rows if isinstance(rows, list) else [])[:MAX_ROSTER_ROWS]:
        if isinstance(row, dict):
            row = {**row, "courier_namespace_id": namespace}
        norm = _normalize_roster_row(row)
        if not norm:
            continue
        key = (norm["connection_id"], norm["profile"])
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(norm)
    cleaned.sort(key=lambda r: (r["connection_id"], r["profile"]))
    payload = {
        "updated_at": int(time.time()),
        "courier_namespace_id": namespace,
        "agents": cleaned,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_ROSTER_BYTES:
        raise ValueError("roster payload too large")
    target = base / ROSTER_FILE
    if namespace:
        key = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        target = base / ROSTERS_DIR / f"{key}.json"
    fd, tmp = tempfile.mkstemp(dir=str(base), prefix=".roster-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(cleaned)


def read_remote_roster(root: Path | str) -> list[dict]:
    """Union of fresh namespaced snapshots plus the v1 legacy snapshot."""
    base = relay_root(root)
    paths = [base / ROSTER_FILE]
    try:
        paths.extend(sorted((base / ROSTERS_DIR).glob("*.json")))
    except OSError:
        pass
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    now = time.time()
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            updated_at = float(data.get("updated_at") or 0) if isinstance(data, dict) else 0
            namespace = str(data.get("courier_namespace_id") or "") if isinstance(data, dict) else ""
            if now - updated_at > ROSTER_STALE_SECONDS:
                continue
            agents = data.get("agents") if isinstance(data, dict) else None
            if not isinstance(agents, list):
                continue
            for row in agents[:MAX_ROSTER_ROWS]:
                if isinstance(row, dict) and namespace:
                    row = {**row, "courier_namespace_id": namespace}
                norm = _normalize_roster_row(row)
                if not norm:
                    continue
                key = (
                    norm["courier_namespace_id"],
                    norm["connection_id"],
                    norm["profile"],
                )
                if key not in seen:
                    seen.add(key)
                    out.append(norm)
        except FileNotFoundError:
            continue
        except Exception:
            logger.debug("bot_relay roster read failed: %s", path, exc_info=True)
    # A pre-v2 Desktop leaves ``roster.json`` behind when it upgrades.  Once
    # the same Desktop-local route/profile appears in a namespace-fenced
    # snapshot, prefer that addressable v2 row over the tokenless duplicate.
    # Otherwise a harmless rolling upgrade makes every common ``local``
    # target ambiguous forever (the legacy snapshot has no namespace by which
    # a courier could safely distinguish it).
    namespaced_routes = {
        (row["connection_id"].lower(), row["profile"].lower())
        for row in out
        if row["courier_namespace_id"]
    }
    return [
        row
        for row in out
        if row["courier_namespace_id"]
        or (row["connection_id"].lower(), row["profile"].lower())
        not in namespaced_routes
    ]


def resolve_remote_target(raw_target: str, roster: list[dict]) -> Any:
    """Resolve ``raw_target`` against the remote roster.

    Accepted forms:
    - bare handle/profile (``moxie``) — must be unique across connections;
    - ``<handle>@<connection-id>`` / ``<profile>@<connection-id>`` — exact.
    - ``<handle>@<connection-id>~<courier-namespace>`` — globally exact
      when multiple Desktops use the same connection id (notably ``local``).

    Returns the matched row, the string ``"ambiguous"`` when a bare form
    matches agents on several connections, or None for no match.
    """
    want = str(raw_target or "").strip().lstrip("@")
    if not want:
        return None
    conn: Optional[str] = None
    namespace: Optional[str] = None
    if "@" in want:
        want, _, conn = want.partition("@")
        want = want.strip()
        conn = conn.strip()
        if "~" in conn:
            conn, _, namespace = conn.rpartition("~")
            conn = conn.strip()
            namespace = namespace.strip()
        if not want or not conn:
            return None
        if "~" in str(namespace or "") or (namespace is not None and not namespace):
            return None
    matches = []
    for row in roster:
        if want.lower() not in (row["handle"].lower(), row["profile"].lower()):
            continue
        if conn and row["connection_id"].lower() != conn.lower():
            continue
        if namespace is not None and row["courier_namespace_id"].lower() != namespace.lower():
            continue
        matches.append(row)
    if not matches:
        return None
    if len(matches) > 1:
        return "ambiguous"
    return matches[0]


def remote_target_forms(roster: list[dict]) -> list[str]:
    """Shortest unambiguous target string for every roster row.

    Connection ids are scoped to one Desktop, so ``handle@local`` is still
    ambiguous when two Desktop namespaces are connected.  The ``~namespace``
    suffix is emitted only when it is needed; ``~`` cannot occur in a
    validated route coordinate, making the form reversible without escaping.
    """

    def count_matches(name: str, connection: str = "", namespace: str = "") -> int:
        return sum(
            1
            for candidate in roster
            if name.lower()
            in (candidate["handle"].lower(), candidate["profile"].lower())
            and (
                not connection
                or candidate["connection_id"].lower() == connection.lower()
            )
            and (
                not namespace
                or candidate["courier_namespace_id"].lower() == namespace.lower()
            )
        )

    forms: list[str] = []
    for row in roster:
        names = [row["handle"]]
        if row["profile"].lower() != row["handle"].lower():
            names.append(row["profile"])

        chosen = ""
        for name in names:
            if count_matches(name) == 1:
                chosen = name
                break
        if not chosen:
            for name in names:
                if count_matches(name, row["connection_id"]) == 1:
                    chosen = f"{name}@{row['connection_id']}"
                    break
        if not chosen and row["courier_namespace_id"]:
            for name in names:
                if (
                    count_matches(
                        name,
                        row["connection_id"],
                        row["courier_namespace_id"],
                    )
                    == 1
                ):
                    chosen = (
                        f"{name}@{row['connection_id']}"
                        f"~{row['courier_namespace_id']}"
                    )
                    break
        # A normalized namespaced row is unique by namespace/route/profile.
        # The fallback is defensive for malformed caller-supplied lists; the
        # resolver will still reject it as ambiguous rather than misroute.
        forms.append(chosen or f"{row['profile']}@{row['connection_id']}")
    return forms


# ── outbox / replies ─────────────────────────────────────────────────────────


def _envelope_ttl_seconds() -> int:
    """Configured drain TTL (``bot_mode.envelope_ttl_seconds``), lazily read.

    tools/ must not pull heavy CLI config at import time, so the read happens
    per-drain and falls back to ``DEFAULT_ENVELOPE_TTL_SECONDS`` when config
    is unavailable (tests, stripped installs). ``0`` (or negative) disables
    drain-time expiry.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        val = (cfg.get("bot_mode") or {}).get("envelope_ttl_seconds")
        if val is not None:
            return int(val)
    except Exception:
        logger.debug("bot_relay TTL config read failed", exc_info=True)
    return DEFAULT_ENVELOPE_TTL_SECONDS


def _target_liveness(root: Path | str, target: dict) -> Optional[bool]:
    """Tri-state liveness for ``target``: True / False / None (unknown).

    Roster rows carry no heartbeat today, so 'definitively offline' is keyed
    off the two signals roster.json actually gives us:

    - an explicit ``online: false`` on the target's row (additive field,
      honored when the Desktop starts pushing it);
    - the target's (connection_id, profile) being ABSENT from a *fresh*
      roster — the Desktop re-pushes the whole roster on connection-state
      changes, so a recently-synced roster that dropped the target means its
      connection is gone.

    A missing, unreadable, or stale (older than ``ROSTER_FRESH_SECONDS``)
    roster proves nothing → None, and callers fail open. Never raises.
    """
    try:
        roster_path = relay_root(root) / ROSTER_FILE
        try:
            age = time.time() - roster_path.stat().st_mtime
        except OSError:
            return None  # no roster ever synced — unknown
        if age > ROSTER_FRESH_SECONDS:
            return None  # stale view — unknown
        roster = read_remote_roster(root)
        if not roster:
            return None  # empty/corrupt roster — treat as unknown, fail open
        key = (str(target.get("connection_id") or ""), str(target.get("profile") or ""))
        for row in roster:
            if (row["connection_id"], row["profile"]) == key:
                online = row.get("online")
                if online is False:
                    return False
                return True if online is True else None
        return False  # fresh roster no longer lists the target — offline
    except Exception:
        logger.debug("bot_relay liveness check failed", exc_info=True)
        return None


def enqueue_envelope(
    root: Path | str,
    *,
    target: dict,
    message: str,
    sender_profile: str,
    sender_handle: str,
    body: str | None = None,
    idempotency_key: str = "",
) -> dict:
    """Durably queue a cross-connection DM. Returns its immutable envelope.

    Namespaced v2 routes use the profile's shared ``state.db`` delivery
    substrate.  A tokenless v1 roster stays on the legacy JSON lane so an old
    Desktop—which cannot ACK a lease—never causes delayed duplicate claims.

    Raises ``EnvelopeRefusedError`` (reason ``'runtime_offline'``) instead of
    queueing when the target is definitively offline per ``_target_liveness``
    (#93091). Unknown liveness enqueues as before (fail-open).
    """
    if _target_liveness(root, target) is False:
        label = (
            f"@{target.get('handle') or target.get('profile') or '?'} on "
            f"{target.get('connection_label') or target.get('connection_id') or '?'}"
        )
        # 'runtime_offline' matches the #93091 item-1 reason enum.
        raise EnvelopeRefusedError(
            "runtime_offline",
            f"{label} is offline right now — the message was NOT queued. "
            "Try again once that machine reconnects to the Desktop.",
        )
    base = _ensure_dirs(root)
    namespace = str(target.get("courier_namespace_id") or "")
    created_at = int(time.time())
    expires_at = created_at + STALE_AFTER_SECONDS
    event_id = uuid.uuid4().hex
    if idempotency_key:
        material = f"bot-relay-v2\0{sender_profile}\0{idempotency_key}"
        event_id = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    envelope = {
        "schema_version": 2 if namespace else 1,
        "id": event_id,
        "created_at": created_at,
        "expires_at": expires_at,
        "from_profile": sender_profile,
        "from_handle": sender_handle,
        "courier_namespace_id": namespace,
        "target_connection": target["connection_id"],
        "target_install_id": str(target.get("target_install_id") or ""),
        "target_profile": target["profile"],
        "target_handle": target["handle"],
        "message": message,
        "body": str(body if body is not None else message),
    }
    if namespace:
        from gateway.durable_events import enqueue

        durable_payload = {
            key: value
            for key, value in envelope.items()
            if key not in {"created_at", "expires_at"}
        }
        stored = enqueue(
            _profile_state_db(root, sender_profile),
            stream=DELIVERY_STREAM,
            event_id=event_id,
            payload=durable_payload,
            route_namespace=namespace,
            expires_at=float(expires_at),
        )
        payload = stored.get("payload") if isinstance(stored, dict) else None
        if isinstance(payload, dict):
            return {
                **payload,
                "created_at": int(stored.get("created_at") or created_at),
                "expires_at": int(stored.get("expires_at") or expires_at),
            }
        return envelope

    # Explicit rolling-upgrade lane.  Old Desktops call outbox.drain and
    # never ACK, so these files deliberately retain the v1 one-shot contract.
    path = base / OUTBOX_DIR / f"{envelope['id']}.json"
    fd, tmp = tempfile.mkstemp(dir=str(base / OUTBOX_DIR), prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return envelope


def claim_pending_envelopes(root: Path | str) -> list[dict]:
    """Legacy v1 one-shot drain.  V2 namespaced events never enter this lane.

    Renames outbox → claimed/ so a second drain can't double-deliver, and
    sweeps stale claimed/reply artifacts opportunistically.  Envelopes older
    than ``bot_mode.envelope_ttl_seconds`` are NOT delivered: each gets an
    error reply (reason ``'queued_expired'``) so the sender's waiter resolves,
    and its outbox file is removed (#93091 item 2).
    """
    base = _ensure_dirs(root)
    _sweep_stale(base)
    ttl = _envelope_ttl_seconds()
    now = time.time()
    out: list[dict] = []
    outbox = base / OUTBOX_DIR
    for path in sorted(outbox.glob("*.json")):
        if ttl > 0:
            expired = False
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
                created = float(env.get("created_at") or path.stat().st_mtime)
                if now - created > ttl:
                    expired = True
                    handle = str(env.get("target_handle") or "?")
                    conn = str(env.get("target_connection") or "?")
                    # 'queued_expired' matches the #93091 item-1 reason enum.
                    # Write the projection directly: the envelope is still in
                    # the outbox (never claimed), so the write-once
                    # ``write_reply`` gate would reject it.
                    _write_reply_projection(
                        root,
                        str(env.get("id") or ""),
                        error=(
                            f"queued message to @{handle} on {conn} expired after "
                            f"{ttl}s waiting for the Desktop to drain it — it was "
                            "NOT delivered. Resend once the Desktop reconnects."
                        ),
                        reason="queued_expired",
                    )
            except (OSError, ValueError):
                # Unreadable envelope or invalid id: if it already counted as
                # expired, still remove it below; otherwise let the normal
                # claim attempt below deal with it.
                pass
            if expired:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
        claimed = base / CLAIMED_DIR / path.name
        try:
            os.replace(path, claimed)  # atomic claim
            out.append(json.loads(claimed.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _bounded_lease_seconds(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = LEASE_SECONDS
    return max(30, min(seconds, LEASE_SECONDS))


def claim_leased_envelopes(
    root: Path | str,
    *,
    courier_namespace_id: str,
    courier_id: str,
    limit: int = MAX_CLAIM_BATCH,
    lease_seconds: int = LEASE_SECONDS,
    now: float | None = None,
) -> list[dict]:
    """Claim a bounded v2 batch with renewable, fenced leases.

    Claims are restricted to the Desktop namespace that minted the target
    route coordinates.  Profile databases are visited round-robin so one
    busy sender cannot monopolize all courier capacity.
    """
    from gateway.durable_events import claim, nack

    namespace = _normalize_opaque_id(
        courier_namespace_id, field="courier namespace"
    )
    owner = _normalize_opaque_id(courier_id, field="courier id")
    try:
        remaining = max(1, min(int(limit), MAX_CLAIM_BATCH))
    except (TypeError, ValueError):
        remaining = MAX_CLAIM_BATCH
    lease_for = _bounded_lease_seconds(lease_seconds)
    dbs = _profile_state_dbs(root)
    if dbs:
        cursor_key = (str(Path(root).resolve()), namespace)
        with _claim_cursor_lock:
            start = _claim_cursors.get(cursor_key, 0) % len(dbs)
            _claim_cursors[cursor_key] = (start + 1) % len(dbs)
            if len(_claim_cursors) > _MAX_CLAIM_CURSOR_KEYS:
                # A gateway has only a handful of roots/namespaces.  Bound
                # defensive process memory if an untrusted caller churns IDs.
                _claim_cursors.pop(next(iter(_claim_cursors)), None)
        dbs = dbs[start:] + dbs[:start]
    out: list[dict] = []
    # One event per profile per pass gives deterministic bounded fairness.
    while remaining and dbs:
        progressed = False
        for db_path in dbs:
            if remaining <= 0:
                break
            rows = claim(
                db_path,
                stream=DELIVERY_STREAM,
                route_namespace=namespace,
                owner=owner,
                limit=1,
                lease_seconds=lease_for,
                now=now,
            )
            if not rows:
                continue
            progressed = True
            remaining -= 1
            for row in rows:
                payload = row.get("payload") if isinstance(row, dict) else None
                if not isinstance(payload, dict):
                    nack(
                        db_path,
                        stream=DELIVERY_STREAM,
                        event_id=str(row.get("event_id") or "") if isinstance(row, dict) else "",
                        owner=owner,
                        lease_token=str(row.get("lease_token") or "") if isinstance(row, dict) else "",
                        generation=int(
                            row.get("generation") or row.get("lease_generation") or 0
                        )
                        if isinstance(row, dict)
                        else 0,
                        error="claimed payload must be an object",
                        retryable=False,
                        retry_after_seconds=0,
                        max_attempts=MAX_DELIVERY_ATTEMPTS,
                        now=now,
                    )
                    remaining += 1
                    continue
                if int(row.get("attempts") or 0) > MAX_DELIVERY_ATTEMPTS:
                    nack(
                        db_path,
                        stream=DELIVERY_STREAM,
                        event_id=str(row.get("event_id") or payload.get("id") or ""),
                        owner=owner,
                        lease_token=str(row.get("lease_token") or ""),
                        generation=int(row.get("generation") or 0),
                        error="delivery attempts exhausted",
                        retryable=False,
                        retry_after_seconds=0,
                        max_attempts=MAX_DELIVERY_ATTEMPTS,
                        now=now,
                    )
                    remaining += 1
                    continue
                out.append(
                    {
                        **payload,
                        "id": row.get("event_id") or payload.get("id"),
                        "created_at": row.get("created_at"),
                        "expires_at": row.get("expires_at"),
                        "lease_owner": owner,
                        "lease_token": row.get("lease_token"),
                        "lease_generation": row.get("lease_generation") or row.get("generation"),
                        "lease_expires_at": row.get("lease_expires_at"),
                        "attempt": row.get("attempts"),
                    }
                )
        if not progressed:
            break
    return out


def _with_leased_event(root: Path | str, operation):
    """Try a fenced operation across profile ledgers without leaking shape."""
    from gateway.durable_events import LeaseMismatch

    for db_path in _profile_state_dbs(root):
        try:
            return operation(db_path)
        except LeaseMismatch:
            continue
    raise LeaseMismatch()


def renew_envelope_lease(
    root: Path | str,
    *,
    envelope_id: str,
    courier_id: str,
    lease_token: str,
    lease_generation: int,
    lease_seconds: int = LEASE_SECONDS,
    now: float | None = None,
) -> dict:
    """Extend a live lease; stale owner/token/generation all fail opaquely."""
    from gateway.durable_events import renew

    _validate_event_id(envelope_id)
    owner = _normalize_opaque_id(courier_id, field="courier id")
    return _with_leased_event(
        root,
        lambda db_path: renew(
            db_path,
            stream=DELIVERY_STREAM,
            event_id=envelope_id,
            owner=owner,
            lease_token=lease_token,
            generation=int(lease_generation),
            lease_seconds=_bounded_lease_seconds(lease_seconds),
            now=now,
        ),
    )


def outcome_digest(envelope_id: str, reply: str, error: str) -> str:
    material = f"{envelope_id}\0{reply}\0{error}"
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def ack_envelope(
    root: Path | str,
    *,
    envelope_id: str,
    courier_id: str,
    lease_token: str,
    lease_generation: int,
    reply: str = "",
    error: str = "",
    claimed_outcome_digest: str = "",
    now: float | None = None,
) -> dict:
    """Atomically settle a v2 event and fence duplicate/conflicting ACKs."""
    from gateway.durable_events import ack, json_digest

    _validate_event_id(envelope_id)
    owner = _normalize_opaque_id(courier_id, field="courier id")
    reply = str(reply or "")
    error = str(error or "")
    if len(reply) > 200_000 or len(error) > 2_000:
        raise ValueError("relay outcome too large")
    digest = outcome_digest(envelope_id, reply, error)
    if claimed_outcome_digest and claimed_outcome_digest != digest:
        raise ValueError("outcome digest mismatch")
    outcome = {
        "status": "failed" if error else "completed",
        "reply": reply,
        "error": error,
    }
    result = _with_leased_event(
        root,
        lambda db_path: ack(
            db_path,
            stream=DELIVERY_STREAM,
            event_id=envelope_id,
            owner=owner,
            lease_token=lease_token,
            generation=int(lease_generation),
            outcome=outcome,
            outcome_digest=json_digest(outcome),
            now=now,
        ),
    )
    # Compatibility projection only; state.db is the delivery authority and
    # the waiter reads it directly.  A projection failure cannot undo ACK.
    try:
        _write_reply_projection(root, envelope_id, reply=reply, error=error)
    except Exception:
        logger.debug("bot_relay reply projection failed", exc_info=True)
    return result


def nack_envelope(
    root: Path | str,
    *,
    envelope_id: str,
    courier_id: str,
    lease_token: str,
    lease_generation: int,
    error: str,
    retryable: bool,
    retry_after_seconds: float = 5,
    now: float | None = None,
) -> dict:
    """Release for bounded retry or atomically terminalize a failed event."""
    from gateway.durable_events import nack

    _validate_event_id(envelope_id)
    owner = _normalize_opaque_id(courier_id, field="courier id")
    if not isinstance(retryable, bool):
        raise ValueError("retryable must be boolean")
    detail = _clean_label(error, 2_000) or "delivery failed"
    result = _with_leased_event(
        root,
        lambda db_path: nack(
            db_path,
            stream=DELIVERY_STREAM,
            event_id=envelope_id,
            owner=owner,
            lease_token=lease_token,
            generation=int(lease_generation),
            error=detail,
            retryable=retryable,
            retry_after_seconds=max(0.0, min(float(retry_after_seconds), 300.0)),
            max_attempts=MAX_DELIVERY_ATTEMPTS,
            now=now,
        ),
    )
    if result.get("state") in {"failed", "expired"}:
        try:
            _write_reply_projection(root, envelope_id, error=result.get("error") or detail)
        except Exception:
            logger.debug("bot_relay failure projection failed", exc_info=True)
    return result


def _validate_event_id(envelope_id: str) -> str:
    safe = str(envelope_id or "").strip()
    if not _EVENT_ID_RE.fullmatch(safe):
        raise ValueError(f"invalid envelope id: {envelope_id!r}")
    return safe


def _write_json_once(path: Path, payload: dict) -> Path:
    """Publish a fully-fsynced immutable JSON file without overwrite races."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("reply", "") == payload.get("reply", "") and existing.get(
            "error", ""
        ) == payload.get("error", ""):
            return path
        raise ValueError("conflicting terminal reply")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rep-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("reply", "") != payload.get("reply", "") or existing.get(
                "error", ""
            ) != payload.get("error", ""):
                raise ValueError("conflicting terminal reply")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _write_reply_projection(
    root: Path | str,
    envelope_id: str,
    *,
    reply: str = "",
    error: str = "",
    reason: str = "",
) -> Path:
    base = _ensure_dirs(root)
    safe = _validate_event_id(envelope_id)
    code = str(reason or "")
    if not code and error:
        from tools.bot_failure_reasons import classify_agent_error

        code = classify_agent_error(str(error))
    return _write_json_once(
        base / REPLIES_DIR / f"{safe}.json",
        {
            "id": safe,
            "at": int(time.time()),
            "reply": str(reply or ""),
            "error": str(error or ""),
            "reason": code,
        },
    )


def write_reply(
    root: Path | str, envelope_id: str, *, reply: str = "", error: str = "", reason: str = ""
) -> Path:
    """Settle a legacy claimed event with a write-once reply projection.

    ``reason`` is an optional typed failure code (see
    ``tools.bot_failure_reasons``, e.g. 'queued_expired'); when omitted and
    ``error`` is non-empty it is classified from the error text. The waiter
    only surfaces the human ``error``.
    """
    base = _ensure_dirs(root)
    safe = _validate_event_id(envelope_id)
    path = base / REPLIES_DIR / f"{safe}.json"
    if path.exists():
        return _write_reply_projection(
            root, safe, reply=reply, error=error, reason=reason
        )
    claimed = base / CLAIMED_DIR / f"{safe}.json"
    if not claimed.is_file():
        raise ValueError("unknown or unclaimed envelope id")
    result = _write_reply_projection(
        root, safe, reply=reply, error=error, reason=reason
    )
    try:
        claimed.unlink()
    except OSError:
        pass
    return result


def _sweep_stale(base: Path, *, now: float | None = None) -> int:
    cutoff = (time.time() if now is None else now) - STALE_AFTER_SECONDS
    removed = 0
    for sub in (CLAIMED_DIR, REPLIES_DIR, OUTBOX_DIR):
        try:
            for path in (base / sub).glob("*.json"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            continue
    return removed


def cleanup_bot_relay_artifacts(max_age_hours: float | None = None) -> int:
    """Sweep stale relay artifacts (envelopes/replies hold DM plaintext).

    ``_sweep_stale`` otherwise runs only when the Desktop drains the outbox
    (``claim_pending_envelopes``) — if the Desktop never reconnects, queued
    plaintext envelopes would sit on disk forever. Same contract as the
    ``cleanup_*_cache`` helpers so the gateway housekeeping loop can call it
    hourly. ``max_age_hours`` is accepted for signature compatibility but the
    relay's own ``STALE_AFTER_SECONDS`` governs staleness.
    """
    del max_age_hours  # relay staleness is governed by STALE_AFTER_SECONDS
    try:
        home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        root = home.parent.parent if home.parent.name == "profiles" else home
        base = relay_root(root)
        removed = _sweep_stale(base) if base.is_dir() else 0
        try:
            from gateway.durable_events import cleanup

            now = time.time()
            for db_path in _profile_state_dbs(root):
                if not db_path.is_file():
                    continue
                event_result = cleanup(
                    db_path,
                    stream=DELIVERY_STREAM,
                    retention_seconds=STALE_AFTER_SECONDS,
                    now=now,
                )
                inbox_result = cleanup(
                    db_path,
                    inbox=DELIVERY_INBOX,
                    retention_seconds=STALE_AFTER_SECONDS,
                    now=now,
                )
                if isinstance(event_result, dict):
                    removed += int(event_result.get("events_deleted") or 0)
                if isinstance(inbox_result, dict):
                    removed += int(inbox_result.get("inbox_deleted") or 0)
        except Exception:
            logger.debug("bot_relay durable event sweep failed", exc_info=True)
        return removed
    except Exception:
        logger.debug("bot_relay artifact sweep failed", exc_info=True)
        return 0


# ── waiter (runs on the sender gateway via terminal background process) ─────


def waiter_command(root: Path | str, envelope: dict) -> str:
    """Shell command that blocks until the reply file appears, then prints it.

    Spawned with ``terminal_tool(background=True, notify_on_complete=True)``
    so its stdout — the teammate's reply — arrives as the same completion
    notification local DMs use. Stdlib-only; runs under the sender gateway's
    interpreter.
    """
    reply_path = str(relay_root(root) / REPLIES_DIR / f"{envelope['id']}.json")
    label = f"@{envelope['target_handle']} on {envelope['target_connection']}"
    state_db = ""
    if int(envelope.get("schema_version") or 1) >= 2:
        state_db = str(_profile_state_db(root, str(envelope.get("from_profile") or "default")))
    # User/route data is passed as argv, never interpolated into executable
    # Python source.  The former f-string construction made a quoted
    # connection id a code-injection primitive even though the outer shell
    # command was correctly shlex-quoted.
    code = (
        "import json,os,sqlite3,sys,time\n"
        "p,label,wait_s,db,event_id = sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4],sys.argv[5]\n"
        "conn = None\n"
        "if db:\n"
        "    try:\n"
        "        conn = sqlite3.connect(db, timeout=2)\n"
        "    except Exception:\n"
        "        pass\n"
        "deadline = time.time() + wait_s\n"

        "while time.time() < deadline:\n"
        "    if os.path.exists(p):\n"
        "        d = json.load(open(p, encoding='utf-8'))\n"
        "        if d.get('error'):\n"
        # The typed reason code (#93091) rides ahead of the free text so the
        # sending agent can branch on it (auth vs rate limit vs offline)
        # without parsing provider prose.
        "            code = str(d.get('reason') or '').strip()\n"
        "            tag = ' [reason: ' + code + ']' if code else ''\n"
        "            print('Delivery to ' + label + ' failed' + tag + ': ' + d['error'])\n"
        "            sys.exit(1)\n"
        "        print('Reply from ' + label + ':')\n"
        "        print(d.get('reply') or '(empty reply)')\n"
        "        sys.exit(0)\n"
        "    if conn is not None:\n"
        "        try:\n"
        "            row = conn.execute('SELECT state,outcome_json FROM durable_events WHERE stream=? AND event_id=?', ('bot_relay.outbox.v2',event_id)).fetchone()\n"
        "            state = str(row[0] if row else '')\n"
        "            if state in {'acked','completed','failed','dead_lettered','expired','cancelled','indeterminate'}:\n"
        "                outcome = json.loads(row[1]) if row and row[1] else {}\n"
        "                error = str(outcome.get('error') or '')\n"
        "                if error or state not in {'acked','completed'}:\n"
        "                    print('Delivery to ' + label + ' failed: ' + (error or state))\n"
        "                    sys.exit(1)\n"
        "                print('Reply from ' + label + ':')\n"
        "                print(str(outcome.get('reply') or '(empty reply)'))\n"
        "                sys.exit(0)\n"
        "        except Exception:\n"
        "            pass\n"
        "    time.sleep(2)\n"
        "print('No reply from ' + label + ' within ' + str(wait_s) + "
        "'s. The message may still be delivered when the Desktop reconnects; "
        "do not resend blindly.')\n"
        "sys.exit(1)\n"
    )
    return shlex.join(
        [
            sys.executable or "python3",
            "-c",
            code,
            reply_path,
            label,
            str(REPLY_WAIT_SECONDS),
            state_db,
            str(envelope["id"]),
        ]
    )


# ── delivery command (used by the deliver RPC on the TARGET gateway) ────────


def _hermes_cli() -> str:
    """Resolve the hermes CLI beside this gateway's own interpreter.

    The deliver RPC runs on the target gateway, whose process is the venv
    python — its bin/Scripts directory holds the matching ``hermes``
    entrypoint. A bare ``"hermes"`` relies on PATH, which is exactly what
    service contexts (systemd units, desktop launchers, non-login SSH
    shells) do not provide, so delivery died with ENOENT there (#93590).
    When no sibling exists (e.g. running from a source tree without an
    installed script), a ``shutil.which`` lookup runs next — it honors
    whatever PATH the process does have — before falling back to the bare
    name, preserving today's behavior for interactive shells.
    """
    exe = Path(sys.executable or "")
    sibling = exe.parent / ("hermes.exe" if sys.platform == "win32" else "hermes")
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("hermes")
    if found:
        return found
    return "hermes"


def local_delivery_command(profile: str, query_file: str) -> list[str]:
    """argv that delivers a DM into ``profile``'s Bot Chat on THIS gateway."""
    return [
        _hermes_cli(),
        "-p",
        profile,
        "chat",
        "--in",
        "~",
        "-c",
        "Bot Chat",
        "--create-if-missing",
        "-Q",
        "--query-file",
        query_file,
    ]


# ── per-profile turn lock (#93091) ───────────────────────────────────────────
#
# Two deliveries into the SAME target profile must never run their Bot Chat
# turns concurrently: deliveries spawn separate ``hermes`` subprocesses, so
# an in-memory mutex is useless — the lock is a per-profile lockfile under
# ``<root>/bot_relay/locks/`` held with ``fcntl.flock`` for exactly the turn
# execution window. flock is released by the kernel when the holder's fd
# closes (including process death), so a crashed turn can never wedge the
# profile. A queued delivery waits up to ``bot_mode.turn_wait_seconds`` and
# then fails with a structured 'target_busy' refusal instead of blocking
# forever.


class TurnBusyError(RuntimeError):
    """A delivery turn is already running for the target profile.

    ``reason`` is 'target_busy' — extends the #93091 item-1 structured
    refusal enum. ``waited_seconds`` is roughly how long the caller queued
    behind the current turn before giving up.
    """

    reason = "target_busy"

    def __init__(self, profile: str, waited_seconds: float):
        self.profile = profile
        self.waited_seconds = waited_seconds
        super().__init__(
            f"target_busy: another delivery turn is already running for "
            f"profile '{profile}' — queued behind it for ~{int(round(waited_seconds))}s "
            "without it finishing. The message was NOT delivered; retry shortly."
        )


def turn_wait_seconds() -> float:
    """Wait budget for a queued delivery turn (config, lazily read)."""
    try:
        from hermes_cli.config import cfg_get, load_config

        val = cfg_get(load_config(), "bot_mode", "turn_wait_seconds", default=None)
        if val is not None:
            return max(0.0, float(val))
    except Exception:
        logger.debug("bot_mode.turn_wait_seconds read failed", exc_info=True)
    return float(TURN_WAIT_SECONDS_FALLBACK)


def turn_lock_path(root: Path | str, profile: str) -> Path:
    """Per-profile lockfile path (short — safe on macOS temp roots)."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(profile or ""))[:64] or "_"
    return relay_root(root) / LOCKS_DIR / f"{safe}.lock"


@contextlib.contextmanager
def acquire_turn_lock(
    root: Path | str, profile: str, timeout_seconds: float | None = None
) -> Iterator[Path]:
    """Hold ``profile``'s cross-process turn lock for the ``with`` body.

    Non-blocking flock probe + short-sleep retry loop up to the budget
    (``bot_mode.turn_wait_seconds`` unless ``timeout_seconds`` is given).
    No ordering guarantee among waiters — whichever probe lands first after
    release wins — but every waiter is bounded by the budget, so no
    deadlock. Raises :class:`TurnBusyError` when the budget is exhausted.
    On platforms without ``fcntl`` (Windows) the lock degrades to a no-op —
    those installs never had this race path in production.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows
        logger.debug("bot turn lock disabled: fcntl unavailable on this platform")
        yield turn_lock_path(root, profile)
        return

    budget = turn_wait_seconds() if timeout_seconds is None else max(0.0, float(timeout_seconds))
    path = turn_lock_path(root, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        start = time.monotonic()
        deadline = start + budget
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                now = time.monotonic()
                if now >= deadline:
                    raise TurnBusyError(profile, now - start)
                time.sleep(min(0.1, max(0.005, deadline - now)))
        try:
            yield path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover — kernel releases on close anyway
                pass
    finally:
        os.close(fd)
def begin_recipient_delivery(
    root: Path | str,
    *,
    event_id: str,
    target_profile: str,
    body: str,
    from_profile: str,
    from_handle: str,
    source_install_id: str = "",
    target_install_id: str = "",
    courier_namespace_id: str = "",
    now: float | None = None,
) -> dict:
    """Admit a v2 delivery exactly once up to an honest indeterminate edge.

    The inbox record is durable before the tool-capable Bot Chat turn starts.
    A committed result is replayed.  If the target process dies after
    admission but before committing a result, an expired processing record is
    terminalized as ``indeterminate`` and is never blindly re-executed.
    """
    from gateway.durable_events import begin_inbox

    safe_id = _validate_event_id(event_id)
    if not _HANDLE_RE.fullmatch(target_profile):
        raise ValueError("invalid target profile")
    if not _HANDLE_RE.fullmatch(from_profile) or not _HANDLE_RE.fullmatch(from_handle):
        raise ValueError("invalid sender identity")
    source_install = _normalize_opaque_id(
        source_install_id, field="source install id", required=False
    )
    target_install = _normalize_opaque_id(
        target_install_id, field="target install id", required=False
    )
    namespace = _normalize_opaque_id(
        courier_namespace_id, field="courier namespace", required=False
    )
    raw_body = str(body or "").strip()
    if not raw_body:
        raise ValueError("message body required")
    identity = {
        "from_profile": from_profile,
        "from_handle": from_handle,
        "source_install_id": source_install,
        "target_install_id": target_install,
        "target_profile": target_profile,
        "courier_namespace_id": namespace,
    }
    material = json.dumps(
        {"identity": identity, "body": raw_body},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    admission = begin_inbox(
        _profile_state_db(root, target_profile),
        inbox=DELIVERY_INBOX,
        event_id=safe_id,
        identity=identity,
        payload_hash=payload_hash,
        lane=target_profile,
        processing_seconds=DELIVERY_PROCESSING_SECONDS,
        now=now,
    )
    normalized = {
        **admission,
        "event_id": safe_id,
        "payload_hash": payload_hash,
        "message": f"Message from 🤖 {from_handle} (@{from_handle}): {raw_body}",
    }
    if admission.get("token") and not normalized.get("execution_token"):
        normalized["execution_token"] = admission["token"]
    return normalized


def finish_recipient_delivery(
    root: Path | str,
    *,
    event_id: str,
    target_profile: str,
    execution_token: str,
    status: str,
    reply: str = "",
    error: str = "",
    now: float | None = None,
) -> dict:
    """Commit the immutable recipient result before the RPC returns."""
    from gateway.durable_events import finish_inbox

    safe_id = _validate_event_id(event_id)
    if status == "indeterminate":
        terminal = "indeterminate"
    else:
        terminal = "succeeded" if status == "completed" and not error else "failed"
    return finish_inbox(
        _profile_state_db(root, target_profile),
        inbox=DELIVERY_INBOX,
        event_id=safe_id,
        execution_token=execution_token,
        status=terminal,
        result={
            "status": (
                "completed"
                if terminal == "succeeded"
                else "indeterminate"
                if terminal == "indeterminate"
                else "failed"
            ),
            "event_id": safe_id,
            "reply": str(reply or "")[:200_000],
            "error": str(error or "")[:2_000],
        },
        now=now,
    )
