"""Calendar guard and supervisor-owned gateway recovery.

The hourly check writes a recovery request only after it proves a code-skew
relationship. Service outages and unverifiable identity are BLOCKED diagnostics,
not restart requests. The supervisor consumes requests outside the gateway
process and claims retry state under a short bounded lock before restarting.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterator

from hermes_constants import get_hermes_home
from gateway.code_skew import read_boot_record
from hermes_cli.gateway_identity import (
    GatewayIdentity,
    GatewayIdentityError,
    SERVICE_NAME,
    active_gateway_identity,
    short_fingerprint,
)

STATE_SCHEMA = 1
MAX_ATTEMPTS = 3
COOLDOWN_SECONDS = 300
RECOVERY_TIMEOUT_SECONDS = 120
POLL_SECONDS = 2
RECOVERY_WINDOW_SECONDS = 3600

SKEW = "SKEW"
SERVICE_DOWN = "SERVICE_DOWN"
UNVERIFIABLE = "UNVERIFIABLE"


def _state_path(home: Path) -> Path:
    return home / "gateway" / "calendar_guard_state.json"


def _request_path(home: Path) -> Path:
    return home / "gateway" / "calendar_guard_request.json"


def _lock_path(home: Path) -> Path:
    return home / "gateway" / "calendar_guard.lock"


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _load_json(path: Path, *, tolerate_invalid: bool = True) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except (ValueError, TypeError):
        if tolerate_invalid:
            return {}
        raise
    return value if isinstance(value, dict) else {}


@contextlib.contextmanager
def _exclusive_lock(path: Path, timeout: float = 5.0) -> Iterator[None]:
    """Use a bounded POSIX flock; fail closed when it cannot be acquired."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    locked = False
    try:
        try:
            import fcntl
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    time.sleep(0.05)
            if not locked:
                raise TimeoutError(f"timed out waiting for {path}")
        except ImportError:
            # The supervisor is POSIX-only; this keeps unit tests portable.
            locked = True
        yield
    finally:
        if locked:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        handle.close()


def _incident_key(
    boot: dict[str, object] | None,
    identity: GatewayIdentity,
    category: str,
    reason: str,
) -> str:
    boot_fp = str((boot or {}).get("fingerprint", "missing"))
    material = "|".join(
        (
            boot_fp,
            identity.fingerprint,
            category,
            reason,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _state(home: Path) -> dict[str, object]:
    try:
        value = _load_json(_state_path(home), tolerate_invalid=False)
    except (ValueError, TypeError):
        return {"schema": STATE_SCHEMA, "state_corrupt": True}
    return value if value.get("schema") == STATE_SCHEMA else {"schema": STATE_SCHEMA}


def _save_state(home: Path, state: dict[str, object]) -> None:
    state["schema"] = STATE_SCHEMA
    _atomic_json(_state_path(home), state)


def _format_issue(identity: GatewayIdentity | None, message: str) -> str:
    prefix = "Hermes Calendar Guard"
    if identity is not None:
        prefix += f" ({short_fingerprint(identity.fingerprint)})"
    return f"{prefix}: {message}"


def _record_check_outcome(
    *,
    home: Path,
    identity: GatewayIdentity | None,
    boot: dict[str, object] | None,
    category: str,
    reason: str,
    now: float,
    service_name: str,
    queue_recovery: bool,
) -> str:
    identity_for_key = identity or GatewayIdentity("unknown", "unknown", None, None, {})
    key = _incident_key(boot, identity_for_key, category, reason)
    try:
        with _exclusive_lock(_lock_path(home)):
            state = _state(home)
            if state.get("state_corrupt") is True:
                return _format_issue(identity, "guard state is corrupt; BLOCKED")
            same_incident = state.get("incident_key") == key
            if same_incident and state.get("recovery_exhausted") is True:
                if state.get("blocked_reported") is True:
                    return ""
                state.update(
                    {
                        "last_outcome": "BLOCKED",
                        "blocked_reported": True,
                        "notification_at": now,
                        "notification_cooldown_until": now + COOLDOWN_SECONDS,
                    }
                )
                _save_state(home, state)
                return _format_issue(
                    identity,
                    "recovery exhausted; BLOCKED after retry limit",
                )
            if same_incident and not queue_recovery and state.get("blocked_reported") is True:
                return ""
            if (
                same_incident
                and queue_recovery
                and _request_path(home).exists()
                and int(state.get("attempts", 0) or 0) > 0
            ):
                # The supervisor owns the retry schedule. The hourly path must
                # not recreate the same request or alert while it is pending.
                return ""
            if same_incident and float(
                state.get("notification_cooldown_until", 0) or 0
            ) > now:
                return ""
            if not same_incident:
                state.update(
                    {
                        "attempts": 0,
                        "next_attempt_at": 0,
                        "recovery_exhausted": False,
                        "blocked_reported": False,
                    }
                )
            if queue_recovery:
                _atomic_json(
                    _request_path(home),
                    {
                        "schema": STATE_SCHEMA,
                        "incident_key": key,
                        "service": service_name,
                        "reason": reason,
                        "requested_at": now,
                    },
                )
                outcome = "QUEUED"
                message = f"{reason}; recovery queued"
            else:
                outcome = "BLOCKED"
                state["blocked_reported"] = True
                message = f"{reason}; BLOCKED; no recovery request"
            state.update(
                {
                    "incident_key": key,
                    "last_outcome": outcome,
                    "last_error": reason,
                    "notification_at": now,
                    "notification_cooldown_until": now + COOLDOWN_SECONDS,
                }
            )
            _save_state(home, state)
    except TimeoutError:
        # A recovery process owns the lock; do not create an alert storm.
        return ""
    except OSError as exc:
        return _format_issue(identity, f"{reason}; state unavailable: {exc}")
    return _format_issue(identity, message)


def check_once(
    *,
    home: Path | None = None,
    project_root: Path | None = None,
    now: float | None = None,
    service_name: str = SERVICE_NAME,
    runner=None,
) -> str:
    """Run the hourly check and return output for the cron delivery target."""
    home = (home or get_hermes_home()).resolve()
    project_root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    now = time.time() if now is None else now
    identity: GatewayIdentity | None = None
    boot: dict[str, object] | None = None
    try:
        identity = active_gateway_identity(
            home, project_root=project_root, service_name=service_name, runner=runner
        )
        props = identity.service_properties
        if props.get("ActiveState") != "active" or props.get("SubState") != "running":
            reason = f"gateway service is {props.get('ActiveState')}/{props.get('SubState')}"
            return _record_check_outcome(
                home=home, identity=identity, boot=None, category=SERVICE_DOWN,
                reason=reason, now=now, service_name=service_name, queue_recovery=False,
            )
        boot = read_boot_record(home)
        if not boot:
            return _record_check_outcome(
                home=home, identity=identity, boot=boot, category=UNVERIFIABLE,
                reason="gateway boot fingerprint is missing or invalid", now=now,
                service_name=service_name, queue_recovery=False,
            )
        if boot.get("schema") == 0 and identity.source == "release":
            # The legacy one-line record is accepted for one migration window;
            # the next natural gateway boot writes the release-aware record.
            return ""
        boot_fingerprint = str(boot.get("fingerprint", ""))
        if identity.source == "release" and boot_fingerprint.startswith("git:"):
            return ""
        if boot.get("fingerprint") != identity.fingerprint:
            reason = (
                "gateway boot identity differs: "
                f"boot {short_fingerprint(str(boot.get('fingerprint')))}, "
                f"active {short_fingerprint(identity.fingerprint)}"
            )
            return _record_check_outcome(
                home=home, identity=identity, boot=boot, category=SKEW,
                reason=reason, now=now, service_name=service_name, queue_recovery=True,
            )
        boot_path = boot.get("release_path")
        if identity.release_path and boot_path:
            if Path(str(boot_path)).resolve() != identity.release_path.resolve():
                return _record_check_outcome(
                    home=home,
                    identity=identity,
                    boot=boot,
                    category=SKEW,
                    reason="gateway boot release path differs from active release",
                    now=now,
                    service_name=service_name,
                    queue_recovery=True,
                )
        return ""
    except GatewayIdentityError as exc:
        return _record_check_outcome(
            home=home, identity=identity, boot=boot, category=UNVERIFIABLE,
            reason=str(exc), now=now, service_name=service_name, queue_recovery=False,
        )
    except Exception as exc:
        return _format_issue(
            identity,
            f"unexpected guard failure: {type(exc).__name__}: {exc}; BLOCKED",
        )


def _restart_user_service(service_name: str, *, timeout: float = 90) -> None:
    # The unit file controls the restarted service environment. This pop only
    # prevents the systemctl client from inheriting gateway-local markers.
    env = os.environ.copy()
    env.pop("_HERMES_GATEWAY", None)
    env.pop("HERMES_GATEWAY_SESSION", None)
    subprocess.run(
        ["systemctl", "--user", "restart", service_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _claim_recovery(home: Path, now: float) -> tuple[str, int, str]:
    """Claim one attempt while holding the lock, then release it."""
    with _exclusive_lock(_lock_path(home)):
        request = _load_json(_request_path(home))
        if not request:
            return "idle", 0, ""
        state = _state(home)
        if state.get("state_corrupt") is True:
            return "idle", 0, ""
        attempts = int(state.get("attempts", 0) or 0)
        claimed_at = float(state.get("claimed_at", 0) or 0)
        if state.get("last_outcome") == "RUNNING":
            if claimed_at and now < claimed_at + RECOVERY_TIMEOUT_SECONDS:
                return "idle", attempts, str(request.get("service", SERVICE_NAME))
            state.update(
                {
                    "last_outcome": "BLOCKED",
                    "last_error": "previous recovery attempt expired",
                    "next_attempt_at": 0,
                }
            )
        if attempts >= MAX_ATTEMPTS:
            if state.get("recovery_exhausted") is True:
                return "idle", attempts, str(request.get("service", SERVICE_NAME))
            state.update(
                {
                    "last_outcome": "BLOCKED",
                    "last_error": "retry limit exceeded",
                    "recovery_exhausted": True,
                    "blocked_reported": False,
                }
            )
            _request_path(home).unlink(missing_ok=True)
            _save_state(home, state)
            return "exhausted", attempts, str(request.get("service", SERVICE_NAME))
        if float(state.get("next_attempt_at", 0) or 0) > now:
            return "idle", attempts, str(request.get("service", SERVICE_NAME))
        recovery_attempts = [
            float(value)
            for value in state.get("recovery_attempts", [])
            if isinstance(value, (int, float))
            and float(value) >= now - RECOVERY_WINDOW_SECONDS
        ]
        if len(recovery_attempts) >= MAX_ATTEMPTS:
            state.update(
                {
                    "last_outcome": "BLOCKED",
                    "last_error": "recovery restart limit exceeded",
                    "recovery_exhausted": True,
                    "blocked_reported": False,
                    "recovery_attempts": recovery_attempts,
                }
            )
            _request_path(home).unlink(missing_ok=True)
            _save_state(home, state)
            return "exhausted", attempts, str(request.get("service", SERVICE_NAME))
        attempts += 1
        recovery_attempts.append(now)
        state.update(
            {
                "attempts": attempts,
                "last_outcome": "RUNNING",
                "last_error": "",
                "next_attempt_at": now
                + min(3600, COOLDOWN_SECONDS * (2 ** (attempts - 1))),
                "recovery_exhausted": False,
                "blocked_reported": False,
                "claimed_at": now,
                "recovery_attempts": recovery_attempts,
            }
        )
        _save_state(home, state)
        return "claimed", attempts, str(request.get("service", SERVICE_NAME))


def _record_recovery_success(home: Path, now: float) -> None:
    with _exclusive_lock(_lock_path(home)):
        _request_path(home).unlink(missing_ok=True)
        state = _state(home)
        state.update(
            {
                "last_outcome": "RECOVERED",
                "resolved_at": now,
                "next_attempt_at": 0,
                "attempts": 0,
                "claimed_at": 0,
                "recovery_attempts": [],
                "notification_cooldown_until": 0,
                "recovery_exhausted": False,
                "blocked_reported": False,
            }
        )
        _save_state(home, state)


def _record_recovery_failure(home: Path, now: float, attempts: int, error: str) -> None:
    with _exclusive_lock(_lock_path(home)):
        state = _state(home)
        attempts = max(attempts, int(state.get("attempts", 0) or 0))
        exhausted = attempts >= MAX_ATTEMPTS
        state.update(
            {
                "attempts": attempts,
                "last_outcome": "BLOCKED",
                "last_error": error,
                "next_attempt_at": now
                + min(3600, COOLDOWN_SECONDS * (2 ** max(0, attempts - 1))),
                "recovery_exhausted": exhausted,
                "blocked_reported": False,
            }
        )
        if exhausted:
            _request_path(home).unlink(missing_ok=True)
        _save_state(home, state)


def recover_once(
    *,
    home: Path | None = None,
    project_root: Path | None = None,
    now: float | None = None,
    service_name: str = SERVICE_NAME,
    runner=None,
    restart=None,
    sleep=time.sleep,
) -> str:
    """Consume one request from the supervisor-owned recovery service."""
    home = (home or get_hermes_home()).resolve()
    project_root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    now = time.time() if now is None else now
    try:
        status, attempts, requested_service = _claim_recovery(home, now)
    except (OSError, TimeoutError):
        return ""
    if status == "idle":
        return ""
    if status == "exhausted":
        return "WARNING: Hermes Calendar Guard: recovery blocked after retry limit"
    effective_service = requested_service or service_name
    try:
        deadline = time.monotonic() + RECOVERY_TIMEOUT_SECONDS
        before = active_gateway_identity(
            home, project_root=project_root, service_name=effective_service, runner=runner
        )
        if restart is None:
            remaining = max(1.0, deadline - time.monotonic())
            _restart_user_service(effective_service, timeout=min(90.0, remaining))
        else:
            restart(effective_service)
        while time.monotonic() < deadline:
            try:
                after = active_gateway_identity(
                    home, project_root=project_root, service_name=effective_service, runner=runner
                )
                boot = read_boot_record(home)
                boot_path = boot.get("release_path") if boot else None
                path_matches = (
                    not after.release_path
                    or not boot_path
                    or Path(str(boot_path)).resolve() == after.release_path.resolve()
                )
                if (
                    after.service_properties.get("MainPID")
                    != before.service_properties.get("MainPID")
                    and boot
                    and boot.get("fingerprint") == after.fingerprint
                    and path_matches
                ):
                    _record_recovery_success(home, now)
                    return "OK: Hermes Calendar Guard: gateway recovery verified"
            except GatewayIdentityError:
                pass
            if time.monotonic() < deadline:
                sleep(POLL_SECONDS)
        raise GatewayIdentityError("post-restart identity verification timed out")
    except Exception as exc:
        try:
            _record_recovery_failure(home, now, attempts, str(exc))
        except (OSError, TimeoutError):
            return f"WARNING: Hermes Calendar Guard: recovery BLOCKED: {exc}; state unavailable"
        return f"WARNING: Hermes Calendar Guard: recovery BLOCKED: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes calendar guard")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--recover", action="store_true")
    args = parser.parse_args(argv)
    output = recover_once() if args.recover else check_once()
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
