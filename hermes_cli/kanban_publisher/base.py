"""Publisher adapter contracts.

Adapters are trusted, narrow, versioned components.  They receive an already
approved immutable contract and may perform exactly one supported mutation.
They do not accept arbitrary methods, URLs, headers, payload fields, retries,
or redirects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from hermes_cli.kanban_store.reconciliation import ReconciliationResult
from hermes_cli.kanban_store.types import DispatchOutcome


@dataclass(frozen=True, slots=True)
class DispatchContract:
    dispatch_id: str
    intent_id: str
    kind: str
    publisher_principal: str
    adapter_version: str
    target: Mapping[str, object]
    application_headers: Mapping[str, str]
    request_body_bytes: bytes
    request_body_sha256: str
    wire_sha256: str
    marker: str


class PublisherAdapter(Protocol):
    kind: str
    version: str

    def dispatch(self, contract: DispatchContract) -> DispatchOutcome: ...

    def reconcile(self, contract: DispatchContract) -> ReconciliationResult: ...
