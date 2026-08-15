"""Versioned Hermes delivery adapter with exact target and stream identity."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from hermes_cli.kanban_store.canonical import sha256_hex
from hermes_cli.kanban_store.reconciliation import ReconciliationResult
from hermes_cli.kanban_store.types import DispatchDisposition, DispatchOutcome

from .base import DispatchContract

Delivery = Callable[[dict[str, object], bytes, dict[str, str]], dict[str, object]]
Lookup = Callable[[dict[str, object], str], tuple[bool, list[dict[str, object]]]]
KeyResolver = Callable[[str], bytes]


class HermesDeliveryAdapter:
    kind = "hermes.delivery.v1"
    version = "hermes-delivery-v1"

    def __init__(self, *, deliver: Delivery, lookup: Lookup, key_resolver: KeyResolver) -> None:
        self._deliver = deliver
        self._lookup = lookup
        self._key_resolver = key_resolver

    def _headers(self, contract: DispatchContract) -> dict[str, str]:
        headers = dict(contract.application_headers)
        if headers.get("X-Hermes-Kanban-Wire") != "<wire-sha256>":
            raise ValueError("wire header template mismatch")
        if headers.get("X-Hermes-Kanban-Signature") != "sha256=<controller-derived-signature>":
            raise ValueError("signature header template mismatch")
        key = self._key_resolver(contract.publisher_principal)
        if len(key) < 32:
            raise ValueError("publisher signing key unavailable")
        signature = hmac.new(key, contract.request_body_bytes, hashlib.sha256).hexdigest()
        headers["X-Hermes-Kanban-Wire"] = contract.wire_sha256
        headers["X-Hermes-Kanban-Signature"] = f"sha256={signature}"
        return headers

    def dispatch(self, contract: DispatchContract) -> DispatchOutcome:
        if contract.adapter_version != self.version:
            return DispatchOutcome(
                DispatchDisposition.DEFINITE_NO_EFFECT, detail_code="adapter_version_mismatch"
            )
        if sha256_hex(contract.request_body_bytes) != contract.request_body_sha256:
            return DispatchOutcome(
                DispatchDisposition.DEFINITE_NO_EFFECT, detail_code="stored_body_digest_mismatch"
            )
        try:
            result = self._deliver(dict(contract.target), contract.request_body_bytes, self._headers(contract))
        except ValueError as exc:
            return DispatchOutcome(
                DispatchDisposition.DEFINITE_NO_EFFECT, detail_code=str(exc)[:128]
            )
        except Exception as exc:
            return DispatchOutcome(
                DispatchDisposition.AMBIGUOUS, detail_code=type(exc).__name__
            )
        state = str(result.get("state", "ambiguous"))
        if state == "success" and result.get("remote_identity"):
            return DispatchOutcome(
                DispatchDisposition.SUCCESS,
                remote_identity=str(result["remote_identity"]),
                detail_code=str(result.get("detail_code") or "delivered"),
            )
        if state == "definite_no_effect":
            return DispatchOutcome(
                DispatchDisposition.DEFINITE_NO_EFFECT,
                detail_code=str(result.get("detail_code") or "platform_rejected")[:128],
            )
        return DispatchOutcome(
            DispatchDisposition.AMBIGUOUS,
            detail_code=str(result.get("detail_code") or "platform_ambiguous")[:128],
        )

    def reconcile(self, contract: DispatchContract) -> ReconciliationResult:
        try:
            complete, matches = self._lookup(dict(contract.target), contract.marker)
        except Exception as exc:
            return ReconciliationResult(False, (), type(exc).__name__, {})
        normalized = tuple(
            {
                **dict(item),
                "marker": contract.marker,
                "publisher_principal": contract.publisher_principal,
            }
            for item in matches
        )
        return ReconciliationResult(bool(complete), normalized, "platform_lookup", {})
