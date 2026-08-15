"""Single-host publisher controller."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from pathlib import Path
from typing import Mapping

from hermes_cli.kanban_store.canonical import (
    WIRE_PREFIX,
    canonical_json_bytes,
    sha256_hex,
)
from hermes_cli.kanban_store.publication import (
    claim_dispatch,
    load_dispatch_contract,
    mark_dispatch_started,
    record_dispatch_outcome,
)
from hermes_cli.kanban_store.reconciliation import (
    begin_reconciliation,
    finish_reconciliation,
)
from hermes_cli.kanban_store.types import ContractError

from .base import DispatchContract, PublisherAdapter


class ControllerLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(self.fd, f"{os.getpid()} {socket.gethostname()} {time.time_ns()}\n".encode())
        os.fsync(self.fd)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _contract(raw: Mapping[str, object]) -> DispatchContract:
    """Revalidate every approved byte and manifest field before dispatch.

    The projection columns are convenient query surfaces, not a second source
    of truth.  The exact canonical manifest and body are authoritative.  Any
    drift between those bytes, the approval-bound columns, or the body freezes
    the operation before the first application byte can be written.
    """

    prepared = bytes(raw["prepared_bytes"])
    expected_wire = sha256_hex(WIRE_PREFIX + prepared)
    if expected_wire != raw["wire_sha256"]:
        raise ContractError("stored manifest digest mismatch")
    try:
        manifest = json.loads(prepared.decode("utf-8"))
    except Exception as exc:
        raise ContractError("stored manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ContractError("stored manifest must be an object")
    if canonical_json_bytes(manifest) != prepared:
        raise ContractError("stored manifest is not canonical V1 JSON")

    fields = {
        "schema",
        "intent_id",
        "kind",
        "required",
        "publisher_principal",
        "adapter_version",
        "target",
        "payload",
        "marker",
        "request_body_sha256",
        "request_body_length",
        "application_headers",
    }
    if set(manifest) != fields:
        raise ContractError("stored manifest fields do not match V1")
    if manifest["schema"] != "hermes.kanban.publication-intent.v1":
        raise ContractError("stored manifest schema drifted")

    body = bytes(raw["request_body_bytes"])
    body_sha = hashlib.sha256(body).hexdigest()
    if body_sha != raw["request_body_sha256"]:
        raise ContractError("stored application body digest mismatch")
    if manifest["request_body_sha256"] != body_sha:
        raise ContractError("manifest body digest drifted")
    if manifest["request_body_length"] != len(body):
        raise ContractError("manifest body length drifted")
    if canonical_json_bytes(manifest["payload"]) != body:
        raise ContractError("application body differs from sealed payload")

    expected_columns = {
        "intent_id": str(raw["intent_id"]),
        "kind": str(raw["kind"]),
        "required": bool(raw["required"]),
        "publisher_principal": str(raw["publisher_principal"]),
        "adapter_version": str(raw["adapter_version"]),
        "target": dict(raw["target"]),
        "payload": dict(raw["payload"]),
        "marker": str(raw["marker"]),
        "application_headers": dict(raw["application_headers"]),
    }
    for name, expected in expected_columns.items():
        if manifest[name] != expected:
            raise ContractError(f"sealed manifest drifted from {name} projection")

    return DispatchContract(
        dispatch_id=str(raw["dispatch_id"]),
        intent_id=str(raw["intent_id"]),
        kind=str(raw["kind"]),
        publisher_principal=str(raw["publisher_principal"]),
        adapter_version=str(raw["adapter_version"]),
        target=dict(raw["target"]),
        application_headers=dict(raw["application_headers"]),
        request_body_bytes=body,
        request_body_sha256=str(raw["request_body_sha256"]),
        wire_sha256=str(raw["wire_sha256"]),
        marker=str(raw["marker"]),
    )


class PublisherController:
    def __init__(self, *, conn, controller_id: str, adapters: Mapping[str, PublisherAdapter]) -> None:
        self.conn = conn
        self.controller_id = controller_id
        self.adapters = dict(adapters)

    def dispatch_approval(self, approval_id: str):
        dispatch_id = claim_dispatch(
            self.conn, approval_id=approval_id, controller_id=self.controller_id
        )
        raw = load_dispatch_contract(self.conn, dispatch_id)
        contract = _contract(raw)
        adapter = self.adapters.get(contract.kind)
        if adapter is None:
            raise ContractError(f"no adapter for {contract.kind}")
        if adapter.version != contract.adapter_version:
            raise ContractError("configured adapter version differs from approval")
        # This durable fact precedes the first possible application byte.  A
        # crash after it is ambiguous and therefore not redispatched.
        mark_dispatch_started(self.conn, dispatch_id)
        outcome = adapter.dispatch(contract)
        record_dispatch_outcome(self.conn, dispatch_id, outcome)
        return outcome

    def reconcile_intent(self, intent_id: str, *, actor: str) -> str:
        reconciliation_id = begin_reconciliation(self.conn, intent_id=intent_id, actor=actor)
        raw = self.conn.execute(
            """
            SELECT d.dispatch_id, i.*
              FROM publication_intents i
              JOIN publication_dispatches d ON d.intent_id=i.intent_id
             WHERE i.intent_id=?
            """,
            (intent_id,),
        ).fetchone()
        if not raw:
            raise KeyError(intent_id)
        # Rehydrate through the approval-bound loader so target, principal,
        # adapter, and wire digest are revalidated again.
        contract = _contract(load_dispatch_contract(self.conn, raw["dispatch_id"]))
        adapter = self.adapters.get(contract.kind)
        if adapter is None or adapter.version != contract.adapter_version:
            raise ContractError("reconciliation adapter is unavailable or drifted")
        result = adapter.reconcile(contract)
        return finish_reconciliation(
            self.conn, reconciliation_id=reconciliation_id, result=result
        )
