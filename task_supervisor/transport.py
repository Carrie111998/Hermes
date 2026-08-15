"""Transport-confirmed owner notification adapters for task supervisor."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import subprocess
from typing import Protocol


@dataclass
class TransportResult:
    success: bool
    transport: str
    detail: str = ""
    provider_id: str | None = None


class OwnerTransport(Protocol):
    name: str

    def send(self, message: str) -> TransportResult: ...


class StdoutConfirmedTransport:
    """Test/manual transport: prints and synchronously confirms local emission.

    The disabled production manifest pins ``send-message`` instead. This adapter
    exists so deterministic tests can assert messages without pretending cron
    stdout has a post-delivery callback.
    """

    name = "stdout-confirmed"

    def send(self, message: str) -> TransportResult:
        return TransportResult(True, self.name, "stdout emission confirmed by caller")


class NullFailTransport:
    name = "null-fail"

    def __init__(self, detail: str = "simulated transport failure") -> None:
        self.detail = detail

    def send(self, message: str) -> TransportResult:
        return TransportResult(False, self.name, self.detail)


class SendMessageToolTransport:
    """Use Hermes' reviewed script-safe send path and require success.

    ``hermes send`` is the deterministic no-agent owner-notification CLI used
    by cron/watchdog scripts. It loads the runtime Hermes profile credentials
    and exits nonzero on backend failure, giving the supervisor the synchronous
    success/failure signal that cron stdout delivery cannot provide.
    """

    name = "send-message"

    def __init__(self, target: str | None = None) -> None:
        self.target = target or os.getenv("HERMES_TASK_SUPERVISOR_OWNER_TARGET", "telegram")

    def send(self, message: str) -> TransportResult:
        try:
            proc = subprocess.run(
                ["hermes", "send", "--to", self.target, "--json", message],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            return TransportResult(False, self.name, f"hermes send exception: {exc}")

        raw = (proc.stdout or "").strip()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        if proc.returncode != 0:
            detail = data.get("error") if isinstance(data, dict) else None
            detail = detail or (proc.stderr or proc.stdout or f"hermes send exited {proc.returncode}").strip()
            return TransportResult(False, self.name, detail)
        if data.get("success"):
            return TransportResult(True, self.name, data.get("note") or "send_message success", data.get("message_id") or data.get("id"))
        return TransportResult(False, self.name, data.get("error") or str(data))


def transport_from_name(name: str | None = None, *, target: str | None = None) -> OwnerTransport:
    resolved = (name or os.getenv("HERMES_TASK_SUPERVISOR_TRANSPORT") or "stdout-confirmed").strip().lower()
    if resolved in {"stdout", "stdout-confirmed", "test"}:
        return StdoutConfirmedTransport()
    if resolved in {"send-message", "send_message", "telegram", "owner"}:
        return SendMessageToolTransport(target)
    if resolved in {"fail", "null-fail"}:
        return NullFailTransport()
    raise ValueError(f"unknown owner transport: {resolved}")
