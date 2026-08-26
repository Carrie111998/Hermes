"""Trusted-fleet HTTP client for peer hosted-room member turns.

This compatibility adapter proves the Desktop-independent data path against
the existing API server. It requires an explicitly trusted peer API key and is
not the final room-scoped grant boundary; callers must opt in deliberately.
"""

from __future__ import annotations

import json
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gateway.hosted_room_peer import HostedMemberDispatch, validate_room_link_url


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
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.status_code = status_code
        self.error_code = error_code
        self.needs_reauthorization = bool(
            status_code in {401, 403} and error_code == "invalid_room_grant"
        )


class PeerRunsHTTPClient:
    """Drive a trusted peer's dedicated group session via async Runs API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        trusted_fleet_compatibility: bool = False,
        timeout_seconds: float = 30,
        receipt_db_path: Path | str | None = None,
    ) -> None:
        base_url, self.transport_security = validate_room_link_url(base_url)
        if api_key and len(api_key) < 16:
            raise ValueError("peer API key is missing or too short")
        self.base_url = base_url
        self.api_key = api_key
        self.trusted_fleet_compatibility = bool(trusted_fleet_compatibility)
        self.timeout_seconds = float(timeout_seconds)
        self.receipt_db_path = Path(receipt_db_path) if receipt_db_path else None
        self._runs: dict[tuple[str, int], dict[str, Any]] = {}

    def bind_receipt_store(self, db_path: Path | str) -> None:
        """Attach the gateway-wide durable receipt store idempotently."""
        path = Path(db_path)
        if self.receipt_db_path not in {None, path}:
            raise PeerRunsHTTPError("peer receipt store changed")
        self.receipt_db_path = path

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
            raise PeerRunsHTTPError(
                f"peer is unreachable: {exc}",
                retryable=True,
                ambiguous=method == "POST",
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
        if grant not in {"compat", "compatibility-only"}:
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
        if not self.trusted_fleet_compatibility:
            raise PeerRunsHTTPError("broad peer compatibility requires explicit opt-in")
        title = f"Group: {room_id}"
        query = urllib.parse.urlencode({
            "limit": 200,
            "title": title,
            "include_hidden": 1,
        })
        listing = self._request(f"/api/sessions?{query}")
        for row in listing.get("data") or []:
            if not isinstance(row, dict) or str(row.get("title") or "") != title:
                continue
            session_id = str(row.get("id") or row.get("session_id") or "")
            if expected_session_id and session_id != expected_session_id:
                raise PeerRunsHTTPError("peer room session identity changed")
            return row
        if not create:
            return None
        created = self._request(
            "/api/sessions",
            method="POST",
            body={"title": title, "source": source},
        )
        session = (
            created.get("session")
            if isinstance(created.get("session"), dict)
            else created
        )
        session_id = str(session.get("id") or session.get("session_id") or "")
        if not session_id:
            raise PeerRunsHTTPError("peer did not return a room session id")
        return {**session, "session_id": session_id, "profile": profile}

    def dispatch(
        self,
        *,
        dispatch: Mapping[str, Any],
        grant: str,
    ) -> Mapping[str, Any]:
        checked = HostedMemberDispatch.from_mapping(dispatch)
        session_id = self._session_id(checked, grant=grant)
        idempotency_key = f"room:{checked.task_id}:{checked.execution_generation}"
        result = self._request(
            "/v1/runs",
            method="POST",
            body=(
                {"input": checked.prompt, "session_id": session_id}
                if grant in {"compat", "compatibility-only"}
                else {
                    "input": checked.prompt,
                    "hosted_room_dispatch": checked.as_mapping(),
                }
            ),
            headers={"Idempotency-Key": idempotency_key},
            room_grant=(None if grant in {"compat", "compatibility-only"} else grant),
        )
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

    def _run_statuses_for_room(
        self, *, room_id: str, profile: str, session_id: str, grant: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        records = self._receipts_for_room(
            room_id=room_id,
            profile=profile,
            session_id=session_id,
        )
        return [
            (
                self._request(
                    f"/v1/runs/{record['run_id']}",
                    room_grant=(None if grant in {"compat", "compatibility-only"} else grant),
                ),
                record,
            )
            for record in records
        ]

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

    def _receipts_for_room(
        self, *, room_id: str, profile: str, session_id: str
    ) -> list[dict[str, Any]]:
        records = [
            record
            for record in self._runs.values()
            if record["room_id"] == room_id
            and record["target_profile"] == profile
            and record["session_id"] == session_id
        ]
        if self.receipt_db_path is not None:
            from gateway import hosted_rooms

            records = hosted_rooms.list_remote_run_receipts(
                self.receipt_db_path,
                room_id=room_id,
                target_profile=profile,
                session_id=session_id,
            )
        return records

    def history(
        self,
        *,
        room_id: str,
        profile: str,
        session_id: str,
        grant: str,
    ) -> Sequence[Mapping[str, Any]]:
        found = self._run_statuses_for_room(
            room_id=room_id,
            profile=profile,
            session_id=session_id,
            grant=grant,
        )
        messages = []
        for status, receipt in found:
            state = str(status.get("status") or "")
            if state not in {"completed", "failed", "interrupted"}:
                continue
            messages.append(
                {
                    "role": "assistant",
                    "task_id": receipt["task_id"],
                    "execution_generation": receipt["execution_generation"],
                    "status": "settled" if state == "completed" else "failed",
                    "message_id": f"peer-run:{status.get('run_id')}",
                    "content": status.get("output") or status.get("error") or "",
                }
            )
        return messages

    def status(
        self,
        *,
        room_id: str,
        profile: str,
        session_id: str,
        grant: str,
    ) -> Mapping[str, Any]:
        found = self._run_statuses_for_room(
            room_id=room_id,
            profile=profile,
            session_id=session_id,
            grant=grant,
        )
        if not found:
            return {"active": False, "task_id": None}
        active_states = {"queued", "running", "waiting_for_approval", "stopping"}
        status, receipt = next(
            (
                item
                for item in reversed(found)
                if item[0].get("status") in active_states
            ),
            found[-1],
        )
        return {
            "active": status.get("status") in active_states,
            "task_id": receipt["task_id"],
            "status": status.get("status"),
            "run_id": status.get("run_id"),
        }

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
        return self._request(
            f"/v1/runs/{urllib.parse.quote(str(record['run_id']), safe='')}/stop",
            method="POST",
            body={},
            room_grant=(None if grant in {"compat", "compatibility-only"} else grant),
        )

    def issue_invitation(
        self,
        *,
        room_id: str,
        home_install_id: str,
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
        if grant in {"compat", "compatibility-only"}:
            return {"grant": grant}
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
        if grant in {"compat", "compatibility-only"}:
            return {"revoked": False, "compatibility": True}
        return self._request(
            "/v1/room-members/grants/revoke",
            method="POST",
            body={},
            room_grant=grant,
        )

    def probe(self, *, grant: str) -> Mapping[str, Any]:
        """Verify gateway reachability and the live scoped capability catalog."""
        return self._request(
            "/v1/room-members/capabilities",
            room_grant=grant,
        )
