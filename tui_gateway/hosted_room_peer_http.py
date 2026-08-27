"""Scoped HTTP client for peer hosted-room member turns."""

from __future__ import annotations

import errno
import hashlib
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from gateway.hosted_room_peer import HostedMemberDispatch, validate_room_link_url


_NOT_ADMITTED_ERRNOS = frozenset(
    value
    for name in (
        "ECONNREFUSED",
        "ENETDOWN",
        "ENETUNREACH",
        "EHOSTDOWN",
        "EHOSTUNREACH",
    )
    if (value := getattr(errno, name, None)) is not None
)


def _is_proven_pre_admission_failure(exc: BaseException) -> bool:
    """Return whether no HTTP connection could have carried the request."""
    reason: Any = exc
    while isinstance(reason, urllib.error.URLError):
        reason = reason.reason
    if isinstance(reason, socket.gaierror):
        return True
    return isinstance(reason, OSError) and reason.errno in _NOT_ADMITTED_ERRNOS


def _response_error_code(detail: str) -> str | None:
    """Extract a machine error code without returning response credentials."""
    try:
        payload = json.loads(detail)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        code = error["code"]
        message = str(error.get("message") or "").lower()
        # Older target gateways wrap grant expiry inside the generic dispatch
        # error. Normalize it locally until their wire code becomes specific.
        if code == "invalid_room_dispatch" and "room grant" in message:
            return "invalid_room_grant"
        return code
    return payload.get("code") if isinstance(payload.get("code"), str) else None


class PeerRunsHTTPError(RuntimeError):
    """Controlled peer HTTP failure."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
        not_admitted: bool = False,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.not_admitted = not_admitted
        self.status_code = status_code
        self.error_code = error_code
        self.needs_reauthorization = bool(
            status_code in {401, 403} and error_code == "invalid_room_grant"
        )


class PeerRunsHTTPClient:
    """Drive a peer's dedicated group session via scoped async Runs APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30,
        receipt_db_path: Path | str | None = None,
        poll_min_seconds: float = 0.1,
        poll_max_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        base_url, self.transport_security = validate_room_link_url(base_url)
        if api_key and len(api_key) < 16:
            raise ValueError("peer API key is missing or too short")
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = float(timeout_seconds)
        self.receipt_db_path = Path(receipt_db_path) if receipt_db_path else None
        if poll_min_seconds <= 0 or poll_max_seconds < poll_min_seconds:
            raise ValueError("peer polling bounds are invalid")
        self.poll_min_seconds = float(poll_min_seconds)
        self.poll_max_seconds = float(poll_max_seconds)
        self.clock = clock
        self._runs: dict[tuple[str, int], dict[str, Any]] = {}
        self._observation_key: tuple[str, int] | None = None
        self._status_cache: dict[str, dict[str, Any]] = {}
        self._recovery_backoff: dict[tuple[str, int], dict[str, Any]] = {}
        self._terminal_receipts: set[tuple[str, int]] = set()

    def bind_receipt_store(self, db_path: Path | str) -> None:
        """Attach the gateway-wide durable receipt store idempotently."""
        path = Path(db_path)
        if self.receipt_db_path not in {None, path}:
            raise PeerRunsHTTPError("peer receipt store changed")
        self.receipt_db_path = path

    def bind_observation(self, *, task_id: str, execution_generation: int) -> None:
        """Pin history/status reads to one exact logical task attempt."""
        key = (str(task_id or ""), int(execution_generation or 0))
        if not key[0] or key[1] < 1:
            raise PeerRunsHTTPError("peer observation identity is invalid")
        if self._observation_key != key:
            for terminal_key in self._terminal_receipts - {key}:
                self._runs.pop(terminal_key, None)
            self._terminal_receipts.intersection_update({key})
            self._observation_key = key
            self._status_cache.clear()
            self._recovery_backoff.clear()

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        room_grant: str | None = None,
    ) -> dict[str, Any]:
        from hermes_cli.urllib_security import open_credentialed_url

        request_headers = {
            "Authorization": (
                f"HermesRoom {room_grant}" if room_grant else f"Bearer {self.api_key}"
            ),
            "Content-Type": "application/json",
            "User-Agent": "Hermes-RoomLink/1.0",
        }
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=(
                json.dumps(body, separators=(",", ":")).encode("utf-8")
                if body is not None
                else None
            ),
            method=method,
            headers=request_headers,
        )
        try:
            with open_credentialed_url(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                detail = str(exc)
            error_code = _response_error_code(detail)
            message = (
                "peer room authorization needs renewal"
                if exc.code in {401, 403} and error_code == "invalid_room_grant"
                else f"peer rejected {method} {path} with HTTP {exc.code}: {detail}"
            )
            raise PeerRunsHTTPError(
                message,
                retryable=exc.code in {408, 425, 429} or exc.code >= 500,
                ambiguous=method == "POST" and exc.code >= 500,
                status_code=exc.code,
                error_code=error_code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            not_admitted = method == "POST" and _is_proven_pre_admission_failure(
                exc
            )
            raise PeerRunsHTTPError(
                f"peer is unreachable: {exc}",
                retryable=True,
                ambiguous=method == "POST" and not not_admitted,
                not_admitted=not_admitted,
            ) from exc
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise PeerRunsHTTPError("peer returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise PeerRunsHTTPError("peer returned a non-object response")
        return payload

    def prepare(
        self,
        *,
        room_id: str,
        profile: str,
        source: str,
        grant: str,
        create: bool,
        expected_session_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        if source != "bot_room":
            raise PeerRunsHTTPError("peer room source must be bot_room")
        self._require_room_grant(grant)
        logical_session = (
            "roomlink_"
            + hashlib.sha256(f"{room_id}\0{profile}".encode("utf-8")).hexdigest()[
                :32
            ]
        )
        if expected_session_id and expected_session_id != logical_session:
            raise PeerRunsHTTPError("peer room session identity changed")
        return {
            "session_id": logical_session,
            "title": f"Group: {room_id}",
            "source": source,
        }

    def dispatch(
        self,
        *,
        dispatch: Mapping[str, Any],
        grant: str,
    ) -> Mapping[str, Any]:
        checked = HostedMemberDispatch.from_mapping(dispatch)
        self._require_room_grant(grant)
        self.bind_observation(
            task_id=checked.task_id,
            execution_generation=checked.execution_generation,
        )
        return self._admit_dispatch(checked, grant=grant)

    def recover_dispatch(
        self,
        *,
        dispatch: Mapping[str, Any],
        grant: str,
    ) -> Mapping[str, Any]:
        """Recover one exact admission by receipt or idempotent POST replay."""
        checked = HostedMemberDispatch.from_mapping(dispatch)
        self._require_room_grant(grant)
        self.bind_observation(
            task_id=checked.task_id,
            execution_generation=checked.execution_generation,
        )
        existing = self._receipt_for_dispatch(checked)
        if existing is not None:
            expected = (
                checked.room_id,
                checked.member_id,
                checked.target_install_id,
                checked.target_profile,
            )
            stored = (
                existing["room_id"],
                existing["member_id"],
                existing["target_install_id"],
                existing["target_profile"],
            )
            if stored != expected:
                raise PeerRunsHTTPError(
                    "peer run receipt conflicts with the recovered dispatch"
                )
            return {
                "status": "accepted",
                "task_id": checked.task_id,
                "execution_generation": checked.execution_generation,
                "run_id": str(existing["run_id"]),
                "session_id": str(existing["session_id"]),
                "replayed": True,
            }
        key = (checked.task_id, checked.execution_generation)
        now = self.clock()
        backoff = self._recovery_backoff.get(key)
        if backoff is not None and now < float(backoff["next_attempt_at"]):
            raise PeerRunsHTTPError(
                "peer admission recovery is backing off",
                retryable=True,
                ambiguous=True,
            )
        try:
            recovered = self._admit_dispatch(checked, grant=grant)
        except PeerRunsHTTPError as exc:
            if exc.retryable or exc.ambiguous:
                delay = self._next_poll_delay(backoff)
                self._recovery_backoff = {
                    key: {
                        "delay": delay,
                        "next_attempt_at": now + delay,
                    }
                }
            raise
        self._recovery_backoff.pop(key, None)
        return recovered

    def _admit_dispatch(
        self,
        checked: HostedMemberDispatch,
        *,
        grant: str,
    ) -> Mapping[str, Any]:
        session_id = self._session_id(checked, grant=grant)
        idempotency_key = f"room:{checked.task_id}:{checked.execution_generation}"
        body = {
            "input": checked.prompt,
            "hosted_room_dispatch": checked.as_mapping(),
        }

        def admit() -> dict[str, Any]:
            return self._request(
                "/v1/runs",
                method="POST",
                body=body,
                headers={"Idempotency-Key": idempotency_key},
                room_grant=grant,
            )

        try:
            result = admit()
        except PeerRunsHTTPError as exc:
            if not exc.ambiguous:
                raise
            result = admit()
        run_id = str(result.get("run_id") or "")
        if not run_id:
            raise PeerRunsHTTPError("peer did not return a run id")
        receipt = {
            "run_id": run_id,
            "session_id": session_id,
            "room_id": checked.room_id,
            "member_id": checked.member_id,
            "task_id": checked.task_id,
            "execution_generation": checked.execution_generation,
            "target_install_id": checked.target_install_id,
            "target_profile": checked.target_profile,
        }
        if self.receipt_db_path is not None:
            from gateway import hosted_rooms

            hosted_rooms.upsert_remote_run_receipt(
                self.receipt_db_path,
                record=receipt,
            )
        self._runs[(checked.task_id, checked.execution_generation)] = receipt
        self._status_cache.pop(run_id, None)
        return {
            "status": "accepted",
            "task_id": checked.task_id,
            "execution_generation": checked.execution_generation,
            "run_id": run_id,
            "session_id": session_id,
            "replayed": bool(result.get("replayed", False)),
        }

    def _session_id(self, dispatch: HostedMemberDispatch, *, grant: str) -> str:
        existing = self._receipt_for_dispatch(dispatch)
        if existing:
            return str(existing["session_id"])
        prepared = self.prepare(
            room_id=dispatch.room_id,
            profile=dispatch.target_profile,
            source="bot_room",
            grant=grant,
            create=True,
        )
        if prepared is None:
            raise PeerRunsHTTPError("peer room session is unavailable")
        return str(prepared.get("session_id") or prepared.get("id") or "")

    def _observation_receipt(
        self, *, room_id: str, profile: str, session_id: str
    ) -> dict[str, Any] | None:
        record = None
        if self._observation_key is not None:
            task_id, execution_generation = self._observation_key
            record = self._runs.get(self._observation_key)
            if record is None and self.receipt_db_path is not None:
                from gateway import hosted_rooms

                record = hosted_rooms.remote_run_receipt(
                    self.receipt_db_path,
                    task_id=task_id,
                    execution_generation=execution_generation,
                )
        if record is None:
            return None
        if (
            record["room_id"] != room_id
            or record["target_profile"] != profile
            or record["session_id"] != session_id
        ):
            raise PeerRunsHTTPError("peer observation receipt changed scope")
        return record

    @staticmethod
    def _compact_run_status(status: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: status[key]
            for key in (
                "run_id",
                "status",
                "output",
                "error",
                "approval",
                "last_event",
            )
            if key in status
        }

    def _next_poll_delay(self, cached: Mapping[str, Any] | None) -> float:
        previous = (
            float(cached["delay"])
            if cached is not None
            else self.poll_min_seconds / 2
        )
        return min(
            self.poll_max_seconds,
            max(self.poll_min_seconds, previous * 2),
        )

    @staticmethod
    def _run_is_terminal(status: Mapping[str, Any]) -> bool:
        return status.get("status") in {
            "completed",
            "failed",
            "interrupted",
            "cancelled",
        }

    def _poll_receipt(
        self,
        record: Mapping[str, Any],
        *,
        grant: str,
    ) -> dict[str, Any]:
        run_id = str(record["run_id"])
        now = self.clock()
        cached = self._status_cache.get(run_id)
        if cached is not None:
            status = cached["status"]
            if self._run_is_terminal(status):
                return status
            if now < float(cached["next_poll_at"]):
                error = cached.get("error")
                if isinstance(error, PeerRunsHTTPError):
                    raise error
                return status
        try:
            status = self._compact_run_status(
                self._request(
                    f"/v1/runs/{urllib.parse.quote(run_id, safe='')}",
                    room_grant=self._require_room_grant(grant),
                )
            )
            if (
                str(status.get("run_id") or "") != run_id
                or status.get("status")
                not in {
                    "queued",
                    "running",
                    "waiting_for_approval",
                    "stopping",
                    "completed",
                    "failed",
                    "interrupted",
                    "cancelled",
                }
            ):
                raise PeerRunsHTTPError("peer returned a mismatched run status")
        except PeerRunsHTTPError as exc:
            delay = self._next_poll_delay(cached)
            self._status_cache = {
                run_id: {
                    "status": cached["status"] if cached is not None else {},
                    "error": exc,
                    "delay": delay,
                    "next_poll_at": now + delay,
                }
            }
            raise
        delay = self._next_poll_delay(cached)
        self._status_cache = {
            run_id: {
                "status": status,
                "delay": delay,
                "next_poll_at": now + delay,
            }
        }
        if self._run_is_terminal(status):
            self._terminal_receipts.add(
                (str(record["task_id"]), int(record["execution_generation"]))
            )
        return status

    def _receipt_for_dispatch(
        self, dispatch: HostedMemberDispatch
    ) -> dict[str, Any] | None:
        key = (dispatch.task_id, dispatch.execution_generation)
        record = self._runs.get(key)
        if record is not None or self.receipt_db_path is None:
            return record
        from gateway import hosted_rooms

        return hosted_rooms.remote_run_receipt(
            self.receipt_db_path,
            task_id=dispatch.task_id,
            execution_generation=dispatch.execution_generation,
        )

    def history(
        self,
        *,
        room_id: str,
        profile: str,
        session_id: str,
        grant: str,
    ) -> Sequence[Mapping[str, Any]]:
        receipt = self._observation_receipt(
            room_id=room_id,
            profile=profile,
            session_id=session_id,
        )
        if receipt is None:
            return []
        status = self._poll_receipt(receipt, grant=grant)
        state = str(status.get("status") or "")
        if state not in {"completed", "failed", "interrupted"}:
            return []
        return [
            {
                "role": "assistant",
                "task_id": receipt["task_id"],
                "execution_generation": receipt["execution_generation"],
                "status": "settled" if state == "completed" else "failed",
                "message_id": f"peer-run:{status.get('run_id')}",
                "content": status.get("output") or status.get("error") or "",
            }
        ]

    def status(
        self,
        *,
        room_id: str,
        profile: str,
        session_id: str,
        grant: str,
    ) -> Mapping[str, Any]:
        receipt = self._observation_receipt(
            room_id=room_id,
            profile=profile,
            session_id=session_id,
        )
        if receipt is None:
            return {"active": False, "task_id": None}
        status = self._poll_receipt(receipt, grant=grant)
        active_states = {"queued", "running", "waiting_for_approval", "stopping"}
        return {
            "active": status.get("status") in active_states,
            "task_id": receipt["task_id"],
            "execution_generation": receipt["execution_generation"],
            "status": status.get("status"),
            "run_id": status.get("run_id"),
            "approval": status.get("approval"),
        }

    def approve_receipt(
        self,
        *,
        task_id: str,
        execution_generation: int,
        request_id: str,
        choice: str,
        grant: str,
    ) -> Mapping[str, Any] | None:
        """Resolve approval for the exact durable remote run."""
        record = self._runs.get((task_id, execution_generation))
        if record is None and self.receipt_db_path is not None:
            from gateway import hosted_rooms

            record = hosted_rooms.remote_run_receipt(
                self.receipt_db_path,
                task_id=task_id,
                execution_generation=execution_generation,
            )
        if record is None:
            return None
        request_id = str(request_id or "").strip()
        if not request_id:
            raise PeerRunsHTTPError("an exact approval request_id is required")
        self._require_room_grant(grant)
        result = self._request(
            f"/v1/runs/{urllib.parse.quote(str(record['run_id']), safe='')}/approval",
            method="POST",
            body={"choice": choice, "request_id": request_id},
            room_grant=grant,
        )
        self._status_cache.pop(str(record["run_id"]), None)
        return result

    def stop(
        self,
        *,
        dispatch: Mapping[str, Any],
        grant: str,
    ) -> Mapping[str, Any] | None:
        checked = HostedMemberDispatch.from_mapping(dispatch)
        return self.stop_receipt(
            task_id=checked.task_id,
            execution_generation=checked.execution_generation,
            grant=grant,
        )

    def stop_receipt(
        self,
        *,
        task_id: str,
        execution_generation: int,
        grant: str,
    ) -> Mapping[str, Any] | None:
        """Stop the exact durable remote run after a home restart."""
        record = self._runs.get((task_id, execution_generation))
        if record is None and self.receipt_db_path is not None:
            from gateway import hosted_rooms

            record = hosted_rooms.remote_run_receipt(
                self.receipt_db_path,
                task_id=task_id,
                execution_generation=execution_generation,
            )
        if record is None:
            return None
        result = self._request(
            f"/v1/runs/{urllib.parse.quote(str(record['run_id']), safe='')}/stop",
            method="POST",
            body={},
            room_grant=self._require_room_grant(grant),
        )
        self._status_cache.pop(str(record["run_id"]), None)
        if self._run_is_terminal(result):
            self._terminal_receipts.add((str(task_id), int(execution_generation)))
        return result

    def issue_invitation(
        self,
        *,
        room_id: str,
        home_install_id: str,
        authority_gateway_id: str,
        authority_epoch: int,
        member_id: str,
        grant_id: str,
        ttl_seconds: float = 3600,
    ) -> Mapping[str, Any]:
        """Ask the target gateway to mint a scoped room-member grant."""
        if not self.api_key:
            raise PeerRunsHTTPError(
                "issuing an invitation requires the target gateway API key"
            )
        return self._request(
            "/v1/room-members/invitations",
            method="POST",
            body={
                "room_id": room_id,
                "home_install_id": home_install_id,
                "authority_gateway_id": authority_gateway_id,
                "authority_epoch": authority_epoch,
                "member_id": member_id,
                "grant_id": grant_id,
                "ttl_seconds": ttl_seconds,
            },
        )

    def refresh_grant(
        self,
        *,
        grant: str,
        ttl_seconds: float = 24 * 60 * 60,
    ) -> Mapping[str, Any]:
        """Renew dispatch access using only the still-valid scoped grant."""
        self._require_room_grant(grant)
        refreshed = self._request(
            "/v1/room-members/grants/refresh",
            method="POST",
            body={"ttl_seconds": ttl_seconds},
            room_grant=grant,
        )
        replacement = str(refreshed.get("grant") or "")
        if not replacement:
            raise PeerRunsHTTPError("peer returned no refreshed room grant")
        # Persist only after the target proves the replacement can authorize
        # the same scoped capability endpoint.
        self.probe(grant=replacement)
        return refreshed

    def revoke_grant(self, *, grant: str) -> Mapping[str, Any]:
        """Revoke this grant's exact room/home/target/profile scope."""
        self._require_room_grant(grant)
        return self._request(
            "/v1/room-members/grants/revoke",
            method="POST",
            body={},
            room_grant=grant,
        )

    def probe(self, *, grant: str) -> Mapping[str, Any]:
        """Verify gateway reachability and the live scoped capability catalog."""
        self._require_room_grant(grant)
        return self._request(
            "/v1/room-members/capabilities",
            room_grant=grant,
        )

    @staticmethod
    def _require_room_grant(grant: str) -> str:
        """Prevent scoped operations from falling back to broad Bearer auth."""
        value = str(grant or "")
        if not value or value in {"compat", "compatibility-only"}:
            raise PeerRunsHTTPError("a scoped room grant is required")
        return value
