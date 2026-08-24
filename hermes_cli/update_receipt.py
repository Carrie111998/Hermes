"""Structured update receipts + post-update fleet version verification.

Phase 1 of the fleet-update reliability plan (#91277): the updater must
*prove* its outcome instead of assuming it.

Two additive capabilities, both designed so a failure inside them can never
break an update (every public entry point is exception-swallowing):

1. **Update receipt** — a machine-readable JSON record of what one
   ``hermes update`` run discovered, did, skipped (and why), written to
   ``<HERMES_HOME>/logs/update_receipts/``. Silent-failure classes this
   makes visible: #88848 (helper died after "success" printed), #74973
   (restart silently skipped), #85753 (restart phase never ran), #81193
   (desktop shows failure for a successful update).

2. **Fleet version verification** — after the restart phase, read every
   profile's ``gateway_state.json``, compare each live gateway's stamped
   ``code_sha`` (written by ``gateway/status.py`` on every runtime-status
   write) against the freshly-updated checkout's HEAD, and print a fleet
   version matrix. Mixed-version fleets (#88654, #69754, #77553, #56717)
   become a loud, actionable report instead of a latent state.

Deployment-kind awareness (docker/image-managed installs) rides on
``hermes_cli.build_info.get_code_identity()``: an image build reports
``source="build-file"`` and the receipt records that the install is not
in-place updatable.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import logging

logger = logging.getLogger(__name__)

_RECEIPT_DIR_NAME = "update_receipts"
_RECEIPT_KEEP = 20  # keep the last N receipts per profile home

# Module-level current receipt. ``hermes update`` is a single-threaded CLI
# command; a module singleton lets the 7k-line updater record steps from
# any depth without threading a handle through every helper.
_current: Optional["UpdateReceipt"] = None
_last_persistence_failure: Optional[dict[str, Any]] = None


def last_persistence_failure() -> Optional[dict[str, Any]]:
    """Return the most recent retryable receipt publication failure."""
    return dict(_last_persistence_failure) if _last_persistence_failure else None


def _safe_process_argv() -> list[str]:
    try:
        from hermes_cli.process_identity import redact_argv

        return redact_argv(sys.argv)
    except Exception:
        return ["[REDACTED]"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UpdateReceipt:
    """Collects the observable facts of one ``hermes update`` run."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {
            "schema": 1,
            "started_at": _utc_now_iso(),
            "finished_at": None,
            "argv": _safe_process_argv(),
            "pid": os.getpid(),
            "correlation_id": None,
            "origin": {},
            "outcome": "running",  # running | success | partial | failed
            "pre_update": {},
            "post_update": {},
            "steps": [],
            "skips": [],
            "gateway_restart": {},
            "fleet": [],
            # Phase 4 transaction facts.  These stay empty for the default
            # (non-canary) updater, preserving the existing behavior while
            # giving terminal, Desktop, Telegram, and Discord one typed
            # receipt contract when rollout/rollback is enabled.
            "checkpoint": {},
            "canary": {},
            "rollback": {},
        }
        try:
            from hermes_cli.build_info import get_code_identity

            self.data["pre_update"] = get_code_identity()
        except Exception:
            pass

    # -- recording ---------------------------------------------------------
    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.data["steps"].append(
            {"name": name, "ok": bool(ok), "detail": detail, "at": _utc_now_iso()}
        )

    def skip(self, name: str, reason: str) -> None:
        self.data["skips"].append(
            {"name": name, "reason": reason, "at": _utc_now_iso()}
        )

    def gateway_restart_result(
        self,
        *,
        restarted_services: list | None = None,
        relaunched_profiles: list | None = None,
        externally_supervised_profiles: list | None = None,
        killed_pids: list | None = None,
        failed_units: list | None = None,
        incomplete: bool = False,
        phase_error: str = "",
    ) -> None:
        self.data["gateway_restart"] = {
            "restarted_services": list(restarted_services or []),
            "relaunched_profiles": list(relaunched_profiles or []),
            "externally_supervised_profiles": list(
                externally_supervised_profiles or []
            ),
            "killed_pids": [int(p) for p in (killed_pids or [])],
            "failed_units": [str(u) for u in (failed_units or [])],
            "incomplete": bool(incomplete),
            "phase_error": phase_error,
        }

    def transaction_result(self, field: str, payload: dict[str, Any]) -> None:
        """Merge a typed transaction section into this receipt."""
        if field not in {"checkpoint", "canary", "rollback"}:
            raise ValueError(f"unknown update transaction field: {field}")
        current = self.data.get(field)
        if not isinstance(current, dict):
            current = {}
        current.update(dict(payload))
        current["at"] = _utc_now_iso()
        self.data[field] = current

    def update_context(self, correlation_id: str, origin: dict[str, Any]) -> None:
        self.data["correlation_id"] = str(correlation_id)
        self.data["origin"] = dict(origin)

    def finalize(self, outcome: str) -> None:
        self.data["outcome"] = outcome
        self.data["finished_at"] = _utc_now_iso()
        try:
            from hermes_cli.build_info import get_code_identity

            self.data["post_update"] = get_code_identity(refresh=True)
        except Exception:
            pass


def _receipt_dir() -> Path:
    from hermes_cli.config import get_hermes_home
    from hermes_cli.update_rollout import validate_no_reparse_topology

    directory = get_hermes_home() / "logs" / _RECEIPT_DIR_NAME
    validate_no_reparse_topology(directory)
    return directory


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish one complete receipt document with a same-directory replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        temp.unlink(missing_ok=True)


def _atomic_create_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish an immutable complete receipt without replacing a winner."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link is an atomic no-replace publication on POSIX and Windows.
        # Readers can never observe a partially-written retained receipt.
        os.link(temp, path)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        temp.unlink(missing_ok=True)


def begin_update_receipt() -> None:
    """Start recording a new update receipt. Never raises."""
    global _current
    try:
        _current = UpdateReceipt()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not start update receipt: %s", exc)
        _current = None


def record_step(name: str, ok: bool, detail: str = "") -> None:
    """Record one update step outcome. No-op when no receipt is active."""
    try:
        if _current is not None:
            _current.step(name, ok, detail)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record update step %s: %s", name, exc)


def record_skip(name: str, reason: str) -> None:
    """Record a skipped step WITH the reason it was skipped."""
    try:
        if _current is not None:
            _current.skip(name, reason)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record update skip %s: %s", name, exc)


def record_gateway_restart(**kwargs: Any) -> None:
    """Record the gateway restart phase outcome (see UpdateReceipt)."""
    try:
        if _current is not None:
            _current.gateway_restart_result(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record gateway restart result: %s", exc)


def _record_transaction(field: str, payload: dict[str, Any]) -> None:
    """Attach a checkpoint/canary/rollback result. Never raises."""
    try:
        if _current is not None:
            _current.transaction_result(field, payload)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record update %s result: %s", field, exc)


def record_checkpoint(**payload: Any) -> None:
    """Record the external pre-update checkpoint identity/state."""
    _record_transaction("checkpoint", payload)


def record_canary(**payload: Any) -> None:
    """Record the canary-first rollout plan and health result."""
    _record_transaction("canary", payload)


def record_rollback(**payload: Any) -> None:
    """Record an automatic or explicit rollback and its verification."""
    _record_transaction("rollback", payload)


def record_update_context(correlation_id: str, **origin: Any) -> None:
    """Record stable invocation identity shared with bot pending state."""
    try:
        if _current is not None:
            _current.update_context(correlation_id, dict(origin))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record update invocation context: %s", exc)


def finalize_update_receipt(
    outcome: str, fleet: list | None = None, stop_reason: str = ""
) -> Optional[Path]:
    """Finalize + persist the receipt, retaining it across failed publication.

    The retained receipt is the primary durable record.  The in-memory receipt
    is only cleared after that no-replace publication succeeds; a failed
    retained write remains retryable.  A latest-pointer failure is reported
    separately because the retained record is already durable.
    """
    global _current, _last_persistence_failure
    receipt = _current
    if receipt is None:
        return None
    try:
        receipt.finalize(outcome)
        if stop_reason:
            receipt.data["stop_reason"] = stop_reason
        if fleet is not None:
            receipt.data["fleet"] = fleet
        directory = _receipt_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        correlation_id = str(receipt.data.get("correlation_id") or "")
        correlation_token = "".join(
            char if char.isalnum() or char in {"-", "_"} else "_"
            for char in correlation_id
        )[:64] or "uncorrelated"
        path = directory / (
            f"update_{stamp}_{os.getpid()}_{correlation_token}_"
            f"{uuid.uuid4().hex}.json"
        )
        receipt.data["persistence"] = {
            "retained_write": "success",
        }
        try:
            _atomic_create_json(path, receipt.data)
        except Exception as exc:
            _last_persistence_failure = {
                "stage": "retained_write",
                "retryable": True,
                "error": str(exc),
            }
            receipt.data["persistence"] = {
                "retained_write": "failed",
                "retryable": True,
                "error": str(exc),
            }
            logger.warning("Could not retain update receipt: %s", exc)
            return None
        # The primary record is now durable; exactly-once callers must not
        # publish it again, even if the convenience pointer cannot be written.
        _current = None
        latest = directory / "latest.json"
        latest_payload = dict(receipt.data)
        latest_payload["persistence"] = {
            "retained_write": "success",
            "latest_pointer": "success",
            "retryable": False,
        }
        try:
            _atomic_write_json(latest, latest_payload)
        except Exception as exc:
            _last_persistence_failure = {
                "stage": "latest_pointer",
                "retryable": True,
                "retained_path": str(path),
                "error": str(exc),
            }
            logger.warning("Could not update latest receipt pointer: %s", exc)
        else:
            _last_persistence_failure = None
        _prune_old_receipts(directory)
        return path
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not write update receipt: %s", exc)
        return None


def finalize_pending_update_receipt(
    exit_code: Optional[int] = None, stop_reason: str = ""
) -> Optional[Path]:
    """Command-boundary safety net: persist a still-open receipt, if any.

    ``hermes update`` has many early-termination paths (Windows
    concurrent-instance preflight, venv-holder refusal, head-pinned no-op,
    fetch failure — all ``sys.exit``) that predate the inner finalize
    call sites. Any receipt still open when the update COMMAND unwinds is
    finalized here so every post-begin run leaves a record — the
    refused/failed runs are exactly the ones a receipt matters most for
    (review on #91283). No-op when no receipt is open (the inner paths
    already finalized — exactly-once via the popped singleton) or when
    recording was never started. Never raises.

    Outcome mapping: exit 0/None → ``success`` (a path that completed
    without an explicit inner finalize), exit 2 → ``refused`` (the
    updater's preflight-refusal convention), anything else → ``failed``.
    """
    if _current is None:
        return None
    if exit_code in (0, None):
        outcome = "success"
    elif exit_code == 2:
        outcome = "refused"
    else:
        outcome = "failed"
    try:
        receipt = _current
        if receipt is not None and exit_code is not None:
            receipt.data["exit_code"] = int(exit_code)
    except Exception:
        pass
    return finalize_update_receipt(outcome, stop_reason=stop_reason)


def _pending_receipt_correlations(directory: Path) -> Optional[set[str]]:
    """Return bot correlations whose terminal delivery is still pending.

    ``None`` means the action marker exists but could not be read; pruning then
    fails closed rather than deleting the only receipt the reconnecting bot
    may need to verify.
    """

    home = directory.parent.parent
    correlations: set[str] = set()
    for name in (".update_pending.claimed.json", ".update_pending.json"):
        path = home / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        correlation_id = str(payload.get("correlation_id") or "").strip()
        if correlation_id:
            correlations.add(correlation_id)
    return correlations


def _prune_old_receipts(directory: Path) -> None:
    try:
        pinned = _pending_receipt_correlations(directory)
        if pinned is None:
            return
        receipts = sorted(
            (p for p in directory.glob("update_*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in receipts[_RECEIPT_KEEP:]:
            try:
                if pinned:
                    payload = json.loads(stale.read_text(encoding="utf-8"))
                    if (
                        isinstance(payload, dict)
                        and str(payload.get("correlation_id") or "") in pinned
                    ):
                        continue
                stale.unlink()
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
    except Exception:
        pass


def read_latest_receipt() -> Optional[dict[str, Any]]:
    """Read the most recent update receipt, or None. Never raises."""
    try:
        path = _receipt_dir() / "latest.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def read_receipt_for_correlation(correlation_id: str) -> Optional[dict[str, Any]]:
    """Read the retained receipt for one exact invocation identity.

    ``latest.json`` is only a dashboard convenience pointer and can advance
    while a bot completion is waiting for its platform to reconnect.  Search
    the immutable retained receipts when that pointer belongs to another run.
    Never raises.
    """

    expected = str(correlation_id or "").strip()
    if not expected:
        return None
    try:
        latest = read_latest_receipt()
        if isinstance(latest, dict) and str(latest.get("correlation_id") or "") == expected:
            return latest

        directory = _receipt_dir()
        candidates = sorted(
            (path for path in directory.glob("update_*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, dict)
                and str(payload.get("correlation_id") or "") == expected
            ):
                return payload
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Fleet version verification
# ---------------------------------------------------------------------------

def collect_fleet_versions(
    *, pre_restart_pids: Optional[list[int]] = None
) -> list[dict[str, Any]]:
    """Snapshot every profile's gateway code identity vs. the current tree.

    Returns one entry per profile home that has a ``gateway_state.json``
    whose persisted ``(pid, start_time)`` identifies the live process — or
    that describes a gateway that should still be live::

        {"profile": str, "pid": int, "code_sha": str|None,
         "code_version": str|None, "state": "current"|"stale"|"unknown"|"down"}

    ``stale``   — gateway stamped a code_sha that differs from the updated
                  checkout's HEAD (it is still serving pre-update modules).
    ``unknown`` — gateway predates the code-identity stamp (started before
                  this feature landed) or identity could not be resolved.
    ``down``    — the gateway was ALIVE when this update started
                  (``pre_restart_pids``), its runtime status still says
                  running, but the PID is dead and no successor rewrote the
                  record: the restart phase stopped it and nothing came
                  back. Without this row a killed-and-never-replaced gateway
                  produced NO entry at all and the matrix passed silently
                  (Phase-1 verification gap, #88848/#74973 class).

    Rollout safety: ``down`` requires membership in ``pre_restart_pids`` —
    a stale state file from a long-dead gateway (machine reboot, manual
    kill weeks ago) must NOT fail every future update. Callers that don't
    have a pre-restart snapshot (``None``/empty) get the historical
    behavior: dead PIDs are skipped.
    Never raises; a probe failure yields an empty list.
    """
    # Runtime-status states that mean "this record does not describe a
    # gateway that should be running now" — no down row for these.
    _NOT_EXPECTED_STATES = {"stopped", "startup_failed"}
    _pre_restart = {int(p) for p in (pre_restart_pids or []) if isinstance(p, int)}
    results: list[dict[str, Any]] = []
    try:
        from hermes_cli.build_info import get_code_identity

        expected_sha = (get_code_identity(refresh=True) or {}).get("sha")
    except Exception:
        expected_sha = None

    try:
        from gateway.status import (
            _pid_exists,
            read_runtime_status,
            runtime_status_pid_incarnation_is_live,
        )
        from hermes_cli.profiles import (
            _get_default_hermes_home,
            _get_profiles_root,
            _PROFILE_ID_RE,
        )

        homes: list[tuple[str, Path]] = []
        default_home = _get_default_hermes_home()
        if default_home.is_dir():
            homes.append(("default", default_home))
        profiles_root = _get_profiles_root()
        if profiles_root.is_dir():
            for entry in sorted(profiles_root.iterdir()):
                if entry.is_dir() and entry.name != "default" and _PROFILE_ID_RE.match(entry.name):
                    homes.append((entry.name, entry))

        for profile, home in homes:
            # Prefer the gateway-owned control socket (#92091): a live
            # `identify` answer is authoritative — no PID-reuse or stale-file
            # heuristics. Fall back to gateway_state.json for gateways that
            # predate the socket or whose socket didn't bind.
            identity = None
            try:
                from gateway.control_socket import identify_gateway

                identity = identify_gateway(home)
            except Exception:
                identity = None
            if identity:
                raw_pid = identity.get("pid")
                try:
                    pid = int(raw_pid) if raw_pid is not None else None
                except (TypeError, ValueError):
                    pid = None
                if pid is not None:
                    code_sha = identity.get("code_sha")
                    if not code_sha or not expected_sha:
                        state = "unknown"
                    elif str(code_sha) == str(expected_sha):
                        state = "current"
                    else:
                        state = "stale"
                    results.append(
                        {
                            "profile": profile,
                            "pid": pid,
                            "code_sha": str(code_sha) if code_sha else None,
                            "code_version": identity.get("code_version"),
                            "state": state,
                            "source": "socket",
                        }
                    )
                    continue
            status_path = home / "gateway_state.json"
            record = read_runtime_status(status_path)
            if not record:
                continue
            raw_pid = record.get("pid")
            try:
                pid = int(raw_pid) if raw_pid is not None else None
            except (TypeError, ValueError):
                continue
            if pid is None:
                continue
            if not runtime_status_pid_incarnation_is_live(record):
                # Dead PID: a DOWN row only when this exact pid was alive at
                # update start AND the record still claims a running state —
                # "the restart phase stopped it and nothing came back."
                # Everything else (clean stop, startup failure, stale record
                # from a long-dead gateway) keeps the historical no-row
                # behavior so the feature's rollout can't false-positive.
                gw_state = record.get("gateway_state")
                try:
                    pid_is_dead = not _pid_exists(pid)
                except Exception:
                    # An unavailable liveness probe is not proof of death.
                    pid_is_dead = False
                if (
                    pid_is_dead
                    and pid in _pre_restart
                    and isinstance(gw_state, str)
                    and gw_state
                    and gw_state not in _NOT_EXPECTED_STATES
                ):
                    results.append(
                        {
                            "profile": profile,
                            "pid": pid,
                            "code_sha": None,
                            "code_version": record.get("code_version"),
                            "state": "down",
                        }
                    )
                continue
            code_sha = record.get("code_sha")
            if not code_sha or not expected_sha:
                state = "unknown"
            elif str(code_sha) == str(expected_sha):
                state = "current"
            else:
                state = "stale"
            results.append(
                {
                    "profile": profile,
                    "pid": pid,
                    "code_sha": str(code_sha) if code_sha else None,
                    "code_version": record.get("code_version"),
                    "state": state,
                }
            )
    except Exception as exc:
        logger.debug("Fleet version probe failed: %s", exc)
    return results


def print_fleet_version_matrix(fleet: list[dict[str, Any]]) -> bool:
    """Print the post-update fleet version matrix.

    Returns True when at least one gateway is provably stale (still
    serving pre-update code) OR provably down (was running, killed by the
    restart phase, nothing came back), so the caller can escalate.
    ``unknown`` entries are reported but do NOT fail the update: gateways
    started before the code-identity stamp existed have no sha to compare,
    and failing on them would turn this feature's own rollout into a
    false-positive storm.
    """
    if not fleet:
        return False
    any_stale = False
    any_down = False
    print()
    print("Fleet version check:")
    for entry in fleet:
        sha = entry.get("code_sha")
        short = sha[:8] if isinstance(sha, str) and sha else "?"
        state = entry.get("state")
        profile = entry.get("profile")
        pid = entry.get("pid")
        if state == "current":
            print(f"  ✓ {profile} (pid {pid}) @ {short} — up to date")
        elif state == "stale":
            any_stale = True
            print(f"  ✗ {profile} (pid {pid}) @ {short} — STALE (pre-update code)")
        elif state == "down":
            any_down = True
            print(
                f"  ✗ {profile} — DOWN (gateway was running before the "
                f"update; pid {pid} is gone and nothing replaced it)"
            )
        else:
            print(
                f"  ? {profile} (pid {pid}) — version unknown "
                "(gateway predates version stamping; restart to enable)"
            )
    if any_stale or any_down:
        print()
        if any_stale:
            print("  ⚠ Stale gateways keep serving pre-update code until restarted:")
        if any_down:
            print("  ⚠ Down gateways stopped serving messaging entirely — restart them:")
        print("      hermes gateway restart                # active profile")
        print("      hermes -p <profile> gateway restart   # named profile")
    return any_stale or any_down
