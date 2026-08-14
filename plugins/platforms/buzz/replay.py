"""Governed, one-shot replay of an already stored Buzz event.

This module deliberately does not start a Buzz subscription.  It fetches one
Nostr event by exact id, validates the same identity and intake gates used by
the Buzz adapter, then invokes ``BuzzAdapter._handle_event`` with only the
startup high-water entry removed from a private state copy.  A profile-local
SQLite ledger makes the operation one-shot across processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import re
import sqlite3
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


EVENT_ID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
CLAIMED = "CLAIMED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
_FETCH_TIMEOUT_SECONDS = 20.0


class ReplayError(RuntimeError):
    """A fail-closed replay error with a non-secret operator code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _utc_now() -> float:
    return time.time()


def _safe_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def replay_db_path(home: Path) -> Path:
    runtime = home / "runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        runtime.chmod(0o700)
    except OSError:
        pass
    return runtime / "buzz-replay.db"


class ReplayLedger:
    """Durable one-shot state keyed by profile and exact event id."""

    def __init__(self, path: Path, *, profile: str):
        self.path = Path(path)
        self.profile = profile
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._conn = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buzz_replay (
                profile TEXT NOT NULL,
                event_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('CLAIMED','COMPLETED','FAILED')),
                receipt_json TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(profile, event_id)
            )
            """
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _row(self, event_id: str) -> sqlite3.Row | None:
        self._conn.row_factory = sqlite3.Row
        return self._conn.execute(
            "SELECT profile,event_id,status,receipt_json,claimed_at,updated_at "
            "FROM buzz_replay WHERE profile=? AND event_id=?",
            (self.profile, event_id),
        ).fetchone()

    def get(self, event_id: str) -> dict[str, Any] | None:
        row = self._row(event_id)
        if row is None:
            return None
        try:
            receipt = json.loads(row["receipt_json"])
        except (TypeError, ValueError):
            receipt = {"receipt_corrupt": True}
        return {
            "profile": row["profile"],
            "event_id": row["event_id"],
            "status": row["status"],
            "receipt": receipt,
            "claimed_at": row["claimed_at"],
            "updated_at": row["updated_at"],
        }

    def claim(self, event_id: str, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        """Atomically claim an unseen event, or fail closed on any prior row."""
        now = _utc_now()
        payload = dict(receipt or {})
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(event_id)
            if row is not None:
                self._conn.execute("COMMIT")
                status = str(row["status"])
                return {
                    "claimed": False,
                    "status": status,
                    "reason": "already_claimed" if status == CLAIMED else "already_terminal",
                }
            self._conn.execute(
                "INSERT INTO buzz_replay(profile,event_id,status,receipt_json,claimed_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (self.profile, event_id, CLAIMED, _safe_json(payload), now, now),
            )
            self._conn.execute("COMMIT")
            return {"claimed": True, "status": CLAIMED}
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise

    def update_claimed(self, event_id: str, receipt: dict[str, Any]) -> bool:
        now = _utc_now()
        cursor = self._conn.execute(
            "UPDATE buzz_replay SET receipt_json=?,updated_at=? "
            "WHERE profile=? AND event_id=? AND status=?",
            (_safe_json(receipt), now, self.profile, event_id, CLAIMED),
        )
        return cursor.rowcount == 1

    def complete(self, event_id: str, receipt: dict[str, Any]) -> bool:
        return self._transition(event_id, COMPLETED, receipt)

    def fail(self, event_id: str, receipt: dict[str, Any]) -> bool:
        return self._transition(event_id, FAILED, receipt)

    def _transition(self, event_id: str, status: str, receipt: dict[str, Any]) -> bool:
        if status not in {COMPLETED, FAILED}:
            raise ValueError(f"invalid terminal status: {status}")
        cursor = self._conn.execute(
            "UPDATE buzz_replay SET status=?,receipt_json=?,updated_at=? "
            "WHERE profile=? AND event_id=? AND status=?",
            (status, _safe_json(receipt), _utc_now(), self.profile, event_id, CLAIMED),
        )
        return cursor.rowcount == 1

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReplayLedger":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


@contextmanager
def profile_replay_lock(home: Path) -> Iterator[None]:
    """Serialize one-shot operators within a profile."""
    from gateway.status import _release_file_lock, _try_acquire_file_lock

    path = home / "runtime" / "buzz-replay.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+", encoding="utf-8")
    locked = False
    try:
        locked = _try_acquire_file_lock(handle)
        if not locked:
            raise ReplayError("replay_lock_conflict")
        yield
    finally:
        if locked:
            _release_file_lock(handle)
        handle.close()


@contextmanager
def exclusive_replay_lock(home: Path) -> Iterator[None]:
    """Hold the authoritative profile gateway lock for the whole replay."""
    from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock

    acquired = False
    try:
        if not acquire_gateway_runtime_lock():
            raise ReplayError("gateway_running")
        acquired = True
        with profile_replay_lock(home):
            yield
    finally:
        if acquired:
            release_gateway_runtime_lock()


def replay_state(state: dict[str, Any], event_id: str) -> dict[str, Any]:
    """Copy adapter state while bypassing only this event's seen marker."""
    seen = state.get("seen")
    if not isinstance(seen, dict):
        raise ReplayError("invalid_channel_state")
    if event_id not in seen:
        raise ReplayError("event_not_in_startup_seen_set")
    copied = dict(state)
    copied["seen"] = OrderedDict((key, value) for key, value in seen.items() if key != event_id)
    return copied


def _tag_values(event: dict[str, Any], name: str) -> list[str]:
    tags = event.get("tags")
    if not isinstance(tags, list):
        return []
    return [
        str(tag[1]).lower()
        for tag in tags
        if isinstance(tag, (list, tuple)) and len(tag) > 1 and tag[0] == name and tag[1]
    ]


def _parent_tag(event: dict[str, Any]) -> tuple[str, bool]:
    """Return the single governed reply parent and whether its shape is safe."""
    tags = event.get("tags")
    if not isinstance(tags, list):
        return "", False
    parents = [
        tag
        for tag in tags
        if isinstance(tag, (list, tuple))
        and len(tag) >= 4
        and tag[0] == "e"
    ]
    if len(parents) != 1:
        return "", False
    parent = str(parents[0][1] or "").lower()
    marker = str(parents[0][3] or "").lower()
    return parent, bool(EVENT_ID_RE.fullmatch(parent) and marker == "reply")


def validate_event(
    event: dict[str, Any],
    requested_event_id: str,
    *,
    adapter: Any,
    watched_channels: set[str],
    expected_parent_event_id: str | None = None,
) -> dict[str, Any]:
    """Validate immutable Nostr/event and all Buzz intake gates."""
    from .nostr_auth import event_id as calculate_event_id, schnorr_verify

    raw_id = str(event.get("id") or "").lower()
    pubkey = str(event.get("pubkey") or "").lower()
    kind = event.get("kind")
    content = event.get("content")
    channels = _tag_values(event, "h")
    recipients = _tag_values(event, "p")
    parent_event_id, parent_tag_valid = _parent_tag(event)
    expected_parent = str(expected_parent_event_id or "").lower()
    parent_expected_match = (
        not expected_parent_event_id
        or bool(EVENT_ID_RE.fullmatch(expected_parent) and parent_event_id == expected_parent)
    )
    self_pubkey = str(getattr(adapter, "_self_pubkey", "") or "").lower()
    allowed = {
        str(value).lower()
        for value in (getattr(adapter, "_allowed_pubkeys", set()) or set())
        if value
    }
    event_id_match = bool(EVENT_ID_RE.fullmatch(raw_id)) and raw_id == requested_event_id.lower()
    calculated_id_match = event_id_match and raw_id == calculate_event_id(event)
    signature_valid = False
    signature = event.get("sig")
    if calculated_id_match and isinstance(signature, str):
        signature_valid = schnorr_verify(bytes.fromhex(raw_id), pubkey, signature)
    kind_valid = kind == 9
    channel = channels[0] if len(channels) == 1 else ""
    channel_valid = bool(channel and channel in watched_channels)
    recipient_mention = bool(self_pubkey and self_pubkey in recipients)
    author_allowed = bool(pubkey and allowed and pubkey in allowed)
    content_valid = isinstance(content, str) and bool(content.strip())
    mention_gate = bool(content_valid and getattr(adapter, "_is_mentioned")(content))
    profile_identity = bool(self_pubkey and recipient_mention)
    result = {
        "event_id_match": event_id_match,
        "calculated_id_match": calculated_id_match,
        "signature_valid": signature_valid,
        "kind": kind,
        "kind_valid": kind_valid,
        "channel": channel,
        "channel_valid": channel_valid,
        "parent_event_id": parent_event_id,
        "parent_tag_valid": parent_tag_valid,
        "parent_expected_match": parent_expected_match,
        "recipient_mention": recipient_mention,
        "author_allowed": author_allowed,
        "content_valid": content_valid,
        "mention_gate": mention_gate,
        "profile_identity": profile_identity,
    }
    if not all(
        (
            event_id_match,
            calculated_id_match,
            signature_valid,
            kind_valid,
            channel_valid,
            parent_tag_valid,
            parent_expected_match,
            recipient_mention,
            author_allowed,
            content_valid,
            mention_gate,
            profile_identity,
        )
    ):
        raise ReplayError("validation_failed", _safe_json(result))
    return result


async def fetch_event(adapter: Any, requested_event_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch one raw signed event by id through the configured relay only."""
    if not EVENT_ID_RE.fullmatch(requested_event_id or ""):
        raise ReplayError("invalid_event_id")
    if not getattr(adapter, "relay_url", ""):
        raise ReplayError("relay_not_configured")
    if not getattr(adapter, "_private_key", ""):
        raise ReplayError("identity_not_configured")
    try:
        import websockets
    except ImportError as exc:
        raise ReplayError("websocket_dependency_missing") from exc

    subscription = "hermes-replay-" + uuid.uuid4().hex[:16]
    found: dict[str, dict[str, Any]] = {}
    started = _utc_now()
    try:
        async with websockets.connect(adapter._websocket_url()) as websocket:
            await adapter._authenticate_websocket(websocket)
            await websocket.send(
                json.dumps(["REQ", subscription, {"ids": [requested_event_id.lower()]}])
            )
            while True:
                raw = await asyncio.wait_for(websocket.recv(), timeout=_FETCH_TIMEOUT_SECONDS)
                message = json.loads(raw)
                if not isinstance(message, list) or not message:
                    continue
                if message[0] == "EVENT" and len(message) >= 3 and isinstance(message[2], dict):
                    event = message[2]
                    event_id_value = str(event.get("id") or "").lower()
                    if event_id_value == requested_event_id.lower():
                        found[event_id_value] = event
                elif message[0] == "EOSE":
                    break
                elif message[0] in {"NOTICE", "CLOSED"}:
                    detail = str(message[-1] if len(message) > 1 else "relay rejected request")
                    raise ReplayError("relay_fetch_failed", detail[:160])
            with contextlib.suppress(Exception):
                await websocket.send(json.dumps(["CLOSE", subscription]))
    except ReplayError:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ReplayError("relay_fetch_timeout") from exc
    except Exception as exc:
        raise ReplayError("relay_fetch_failed", type(exc).__name__) from exc
    if len(found) != 1:
        raise ReplayError("event_not_found" if not found else "ambiguous_event_fetch")
    return next(iter(found.values())), {
        "relay": str(adapter.relay_url),
        "found_count": len(found),
        "fetched_at": _utc_now(),
        "duration_ms": round((_utc_now() - started) * 1000, 1),
    }


async def _prepare_adapter(profile: str) -> tuple[Any, Any, Any, str]:
    """Build the real GatewayRunner + BuzzAdapter without connecting Buzz."""
    from gateway.config import Platform, load_gateway_config
    from gateway.run import GatewayRunner
    from hermes_cli.config import get_hermes_home
    from plugins.platforms.buzz.adapter import BuzzAdapter, _resolve_private_key

    config = load_gateway_config()
    platform = Platform("buzz")
    platform_config = config.platforms.get(platform)
    if platform_config is None or not platform_config.enabled:
        raise ReplayError("buzz_not_enabled")
    runner = GatewayRunner(config)
    adapter = BuzzAdapter(platform_config)
    adapter.gateway_runner = runner
    if profile == "default":
        runner.adapters[platform] = adapter
        adapter.set_message_handler(runner._primary_message_handler())
        adapter.set_fatal_error_handler(runner._handle_adapter_fatal_error)
        adapter.set_session_store(runner.session_store)
        adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
        set_reaction_handler = getattr(adapter, "set_reaction_handler", None)
        if callable(set_reaction_handler):
            set_reaction_handler(runner._handle_reaction_event)
        adapter.set_topic_recovery_fn(runner._recover_telegram_topic_thread_id)
        adapter.set_authorization_check(runner._make_adapter_auth_check(platform))
        adapter._busy_text_mode = runner._busy_text_mode
    else:
        runner._profile_adapters.setdefault(profile, {})[platform] = adapter
        runner._configure_profile_adapter(adapter, profile, platform)
    runner.delivery_router.adapters = runner.adapters
    runner._running = True
    adapter._running = True
    adapter._private_key = _resolve_private_key(adapter._extra)
    if not adapter._private_key:
        raise ReplayError("identity_not_configured")

    from .nostr_auth import public_key_hex
    from plugins.platforms.buzz.adapter import hex_to_npub, _parse_json_list

    derived_pubkey = public_key_hex(adapter._private_key).lower()
    code, output, _error = await adapter._run_cli(["users", "get"])
    profiles = _parse_json_list(output) if code == 0 else []
    if not profiles or str(profiles[0].get("pubkey") or "").lower() != derived_pubkey:
        raise ReplayError("profile_identity_mismatch")
    adapter._self_pubkey = derived_pubkey
    adapter._self_npub = hex_to_npub(derived_pubkey) or ""
    adapter._display_name = str(profiles[0].get("display_name") or "").strip()
    if not adapter._display_name:
        raise ReplayError("profile_display_name_missing")
    return runner, adapter, platform, str(get_hermes_home())


async def _watched_channels(adapter: Any) -> set[str]:
    from plugins.platforms.buzz.adapter import _parse_json_list

    configured = {str(value).strip() for value in (adapter.channels or []) if str(value).strip()}
    if configured:
        return configured
    code, output, _error = await adapter._run_cli(["channels", "list"])
    if code != 0:
        raise ReplayError("channel_list_failed")
    listed = _parse_json_list(output)
    channels = {str(item.get("channel_id")) for item in listed if item.get("channel_id")}
    adapter._channel_names.update(
        {
            str(item.get("channel_id")): str(item.get("name") or item.get("channel_id"))
            for item in listed
            if item.get("channel_id")
        }
    )
    return channels


async def _close_runner(runner: Any) -> None:
    wait_for_workers = getattr(runner, "wait_for_all_session_quiescence", None)
    if callable(wait_for_workers):
        await wait_for_workers()
    runner._running = False
    seen: set[int] = set()
    for owner in (getattr(getattr(runner, "session_store", None), "_db", None),
                  getattr(getattr(runner, "_session_db", None), "_db", None)):
        if owner is None or id(owner) in seen:
            continue
        seen.add(id(owner))
        close = getattr(owner, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


async def _wait_for_session_quiescence(adapter: Any, session_key: str) -> None:
    runner = getattr(adapter, "gateway_runner", None)
    wait_for_session = getattr(runner, "wait_for_session_quiescence", None)
    if callable(wait_for_session):
        await wait_for_session(session_key)


async def dispatch_exact_event(
    adapter: Any,
    channel: str,
    event: dict[str, Any],
    event_id: str,
    startup_state: dict[str, Any],
    *,
    wait_timeout: float = 900.0,
) -> dict[str, Any]:
    """Run one event through the adapter and observe its one session task."""
    from gateway.platforms.base import ProcessingOutcome

    replayed_state = replay_state(startup_state, event_id)
    before = set(getattr(adapter, "_session_tasks", {}).keys())
    processing_outcomes: list[Any] = []
    missing = object()
    original_hook = getattr(adapter, "on_processing_complete", missing)

    async def _observe_processing_complete(processing_event: Any, outcome: Any) -> None:
        if isinstance(processing_event, dict):
            processing_event_id = processing_event.get("id") or processing_event.get("message_id")
        else:
            processing_event_id = getattr(processing_event, "message_id", None)
        if str(processing_event_id or "").lower() != event_id.lower():
            return
        processing_outcomes.append(outcome)
        if original_hook is missing:
            return
        result = original_hook(processing_event, outcome)
        if inspect.isawaitable(result):
            await result

    try:
        setattr(adapter, "on_processing_complete", _observe_processing_complete)
    except Exception:
        return {
            "dispatch": {
                "handler": "BuzzAdapter._handle_event",
                "accepted": False,
                "new_session_dispatches": 0,
            },
            "session": {},
            "processing": {
                "outcomes": [],
                "explicit_success": False,
            },
            "outcome": {
                "status": CLAIMED,
                "code": "processing_observer_unavailable",
            },
        }

    try:
        dispatch_error: BaseException | None = None
        try:
            await adapter._handle_event(channel, replayed_state, event)
            await asyncio.sleep(0)
        except BaseException as exc:
            dispatch_error = exc
        after_map = getattr(adapter, "_session_tasks", {})
        new_keys = [key for key in after_map if key not in before]
        result: dict[str, Any] = {
            "dispatch": {
                "handler": "BuzzAdapter._handle_event",
                "accepted": bool(new_keys),
                "new_session_dispatches": len(new_keys),
            },
            "session": {},
            "processing": {
                "outcomes": [],
                "explicit_success": False,
            },
            "outcome": {},
        }
        if dispatch_error is not None:
            result["dispatch"]["error_type"] = type(dispatch_error).__name__

        def _processing_result() -> tuple[list[str], Any | None]:
            names = [
                str(getattr(outcome, "value", outcome)).lower()
                for outcome in processing_outcomes
            ]
            result["processing"] = {
                "outcomes": names,
                "explicit_success": (
                    len(processing_outcomes) == 1
                    and (
                        processing_outcomes[0] is ProcessingOutcome.SUCCESS
                        or getattr(processing_outcomes[0], "value", None) == "success"
                    )
                ),
            }
            return names, processing_outcomes[-1] if processing_outcomes else None

        if len(new_keys) != 1:
            _names, processing_outcome = _processing_result()
            result["session"] = {
                "status": "CLAIMED",
                "task_observed": bool(processing_outcomes),
            }
            if dispatch_error is not None:
                result["outcome"] = {"status": CLAIMED, "code": "dispatch_exception"}
            elif len(processing_outcomes) != 1:
                result["outcome"] = {
                    "status": CLAIMED,
                    "code": (
                        "processing_outcome_ambiguous"
                        if processing_outcomes
                        else "session_dispatch_not_unique"
                    ),
                }
            elif processing_outcome is ProcessingOutcome.FAILURE or getattr(
                processing_outcome, "value", None
            ) == "failure":
                result["session"]["status"] = "FAILED"
                result["outcome"] = {"status": FAILED, "code": "processing_failed"}
            else:
                result["outcome"] = {
                    "status": CLAIMED,
                    "code": "session_dispatch_not_unique",
                }
            return result

        session_key = new_keys[0]
        result["session"] = {
            "status": "SPAWNED",
            "session_key_hash": hashlib.sha256(str(session_key).encode()).hexdigest()[:16],
            "task_observed": True,
        }
        task = after_map[session_key]
        try:
            # The authoritative gateway lock cannot be released while a
            # replay session or its executor worker is still running. Let
            # wait_for cancel the async task, then await the production worker
            # registry before returning the CLAIMED receipt.
            await asyncio.wait_for(task, timeout=wait_timeout)
        except asyncio.TimeoutError:
            await _wait_for_session_quiescence(adapter, session_key)
            result["session"]["status"] = "CANCELLED"
            result["session"]["quiesced"] = True
            _processing_result()
            result["outcome"] = {"status": CLAIMED, "code": "session_timeout"}
            return result
        except BaseException as exc:
            await _wait_for_session_quiescence(adapter, session_key)
            result["session"]["status"] = "CRASH_AMBIGUOUS"
            result["dispatch"]["error_type"] = type(exc).__name__
            _processing_result()
            result["outcome"] = {"status": CLAIMED, "code": "session_crash_ambiguous"}
            return result

        _names, processing_outcome = _processing_result()
        if dispatch_error is not None:
            result["session"]["status"] = "CLAIMED"
            result["outcome"] = {"status": CLAIMED, "code": "dispatch_exception"}
        elif processing_outcome is None:
            result["session"]["status"] = "CLAIMED"
            result["outcome"] = {"status": CLAIMED, "code": "processing_outcome_missing"}
        elif len(processing_outcomes) != 1:
            result["session"]["status"] = "CLAIMED"
            result["outcome"] = {"status": CLAIMED, "code": "processing_outcome_ambiguous"}
        elif processing_outcome is ProcessingOutcome.SUCCESS or getattr(
            processing_outcome, "value", None
        ) == "success":
            result["session"]["status"] = "COMPLETED"
            result["outcome"] = {"status": COMPLETED}
        elif processing_outcome is ProcessingOutcome.FAILURE or getattr(
            processing_outcome, "value", None
        ) == "failure":
            result["session"]["status"] = "FAILED"
            result["outcome"] = {"status": FAILED, "code": "processing_failed"}
        else:
            result["session"]["status"] = "CLAIMED"
            result["outcome"] = {"status": CLAIMED, "code": "processing_outcome_unknown"}
        return result
    finally:
        if original_hook is missing:
            with contextlib.suppress(AttributeError):
                delattr(adapter, "on_processing_complete")
        else:
            with contextlib.suppress(Exception):
                setattr(adapter, "on_processing_complete", original_hook)


async def run_replay(
    profile: str,
    requested_event_id: str,
    *,
    expected_parent_event_id: str | None = None,
    wait_timeout: float = 900.0,
) -> dict[str, Any]:
    """Execute exactly one governed replay and return a non-secret receipt."""
    from hermes_cli.config import get_hermes_home

    home = Path(get_hermes_home())
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "profile": profile,
        "event_id": requested_event_id.lower(),
        "expected_parent_event_id": (
            str(expected_parent_event_id).lower() if expected_parent_event_id else None
        ),
        "runtime_lock": {
            "authoritative": True,
            "acquired": False,
        },
        "fetch": {},
        "validation": {},
        "claim": {},
        "dispatch": {},
        "session": {},
        "outcome": {},
    }

    try:
        with exclusive_replay_lock(home):
            receipt["runtime_lock"]["acquired"] = True
            ledger_path = replay_db_path(home)
            with ReplayLedger(ledger_path, profile=profile) as ledger:
                prior = ledger.get(requested_event_id.lower())
                if prior is not None:
                    receipt["claim"] = {
                        "claimed": False,
                        "status": prior["status"],
                        "reason": (
                            "already_claimed"
                            if prior["status"] == CLAIMED
                            else "already_terminal"
                        ),
                    }
                    receipt["outcome"] = {
                        "status": FAILED,
                        "code": "replay_already_recorded",
                    }
                    return receipt

                runner = None
                try:
                    runner, adapter, _platform, _home = await _prepare_adapter(profile)
                    watched = await _watched_channels(adapter)
                    event, fetch_receipt = await fetch_event(adapter, requested_event_id)
                    receipt["fetch"] = fetch_receipt
                    receipt["validation"] = validate_event(
                        event,
                        requested_event_id,
                        adapter=adapter,
                        watched_channels=watched,
                        expected_parent_event_id=expected_parent_event_id,
                    )
                    channel = receipt["validation"]["channel"]
                    created_at = int(event.get("created_at") or 0)
                    startup_state = {
                        "chat_type": "group",
                        "last_ts": created_at,
                        "seen": OrderedDict([(requested_event_id.lower(), None)]),
                    }
                    receipt["claim"] = ledger.claim(requested_event_id.lower(), receipt)
                    if not receipt["claim"].get("claimed"):
                        receipt["outcome"] = {
                            "status": FAILED,
                            "code": "replay_already_recorded",
                        }
                        return receipt

                    adapter._channel_state[channel] = startup_state
                    dispatch_receipt = await dispatch_exact_event(
                        adapter,
                        channel,
                        event,
                        requested_event_id.lower(),
                        startup_state,
                        wait_timeout=wait_timeout,
                    )
                    receipt["dispatch"] = dispatch_receipt["dispatch"]
                    receipt["session"] = dispatch_receipt["session"]
                    receipt["outcome"] = dispatch_receipt["outcome"]
                    if dispatch_receipt.get("processing"):
                        receipt["processing"] = dispatch_receipt["processing"]
                    status = receipt["outcome"].get("status")
                    if status == COMPLETED and not dispatch_receipt.get(
                        "processing", {}
                    ).get("explicit_success", False):
                        status = CLAIMED
                        receipt["outcome"] = {
                            "status": CLAIMED,
                            "code": "processing_success_unproven",
                        }
                    if status == FAILED:
                        receipt["claim"]["status"] = FAILED
                        ledger.fail(requested_event_id.lower(), receipt)
                        return receipt
                    if status == CLAIMED:
                        ledger.update_claimed(requested_event_id.lower(), receipt)
                        return receipt
                    if status != COMPLETED:
                        receipt["claim"]["status"] = CLAIMED
                        receipt["outcome"] = {
                            "status": CLAIMED,
                            "code": "processing_outcome_unknown",
                        }
                        ledger.update_claimed(requested_event_id.lower(), receipt)
                        return receipt
                    if not ledger.complete(requested_event_id.lower(), receipt):
                        receipt["claim"]["status"] = CLAIMED
                        receipt["outcome"] = {
                            "status": CLAIMED,
                            "code": "claim_transition_failed",
                        }
                        ledger.update_claimed(requested_event_id.lower(), receipt)
                    return receipt
                except ReplayError as exc:
                    claimed = bool(receipt["claim"].get("claimed"))
                    status = CLAIMED if claimed else FAILED
                    receipt["outcome"] = {
                        "status": status,
                        "code": exc.code,
                        "retryable": not claimed,
                    }
                    if claimed:
                        receipt["claim"]["status"] = CLAIMED
                        ledger.update_claimed(requested_event_id.lower(), receipt)
                    else:
                        receipt["claim"] = {
                            "claimed": False,
                            "status": "UNCLAIMED",
                            "reason": exc.code,
                            "retryable": True,
                        }
                    return receipt
                except Exception as exc:
                    claimed = bool(receipt["claim"].get("claimed"))
                    status = CLAIMED if claimed else FAILED
                    receipt["outcome"] = {
                        "status": status,
                        "code": "replay_internal_error",
                        "error_type": type(exc).__name__,
                        "retryable": not claimed,
                    }
                    if claimed:
                        ledger.update_claimed(requested_event_id.lower(), receipt)
                    else:
                        receipt["claim"] = {
                            "claimed": False,
                            "status": "UNCLAIMED",
                            "reason": "replay_internal_error",
                            "retryable": True,
                        }
                    return receipt
                finally:
                    if runner is not None:
                        await _close_runner(runner)
    except ReplayError as exc:
        receipt["outcome"] = {
            "status": FAILED,
            "code": exc.code,
            "retryable": True,
        }
        receipt["claim"] = {
            "claimed": False,
            "status": "UNCLAIMED",
            "reason": exc.code,
            "retryable": True,
        }
    except Exception as exc:
        receipt["outcome"] = {
            "status": FAILED,
            "code": "replay_internal_error",
            "error_type": type(exc).__name__,
            "retryable": True,
        }
        receipt["claim"] = {
            "claimed": False,
            "status": "UNCLAIMED",
            "reason": "replay_internal_error",
            "retryable": True,
        }
    return receipt


def run_replay_cli(
    profile: str,
    requested_event_id: str,
    *,
    expected_parent_event_id: str | None = None,
    wait_timeout: float = 900.0,
) -> int:
    try:
        receipt = asyncio.run(
            run_replay(
                profile,
                requested_event_id,
                expected_parent_event_id=expected_parent_event_id,
                wait_timeout=wait_timeout,
            )
        )
    except ReplayError as exc:
        receipt = {
            "schema_version": 1,
            "profile": profile,
            "event_id": requested_event_id.lower(),
            "expected_parent_event_id": (
                str(expected_parent_event_id).lower() if expected_parent_event_id else None
            ),
            "claim": {},
            "dispatch": {},
            "session": {},
            "outcome": {"status": FAILED, "code": exc.code},
        }
    except Exception as exc:
        receipt = {
            "schema_version": 1,
            "profile": profile,
            "event_id": requested_event_id.lower(),
            "expected_parent_event_id": (
                str(expected_parent_event_id).lower() if expected_parent_event_id else None
            ),
            "claim": {},
            "dispatch": {},
            "session": {},
            "outcome": {
                "status": FAILED,
                "code": "replay_internal_error",
                "error_type": type(exc).__name__,
            },
        }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt.get("outcome", {}).get("status") == "COMPLETED" else 1
