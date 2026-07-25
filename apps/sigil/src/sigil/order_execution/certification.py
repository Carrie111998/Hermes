from __future__ import annotations

from dataclasses import dataclass

from .adapter import ExecutionAdapter
from .models import (
    BrokerOrderStatus,
    ExecutionEnvironment,
    FillReconciliationStatus,
    SubmissionRequest,
)
from .policy import ExecutionPolicy
from .reconciliation import reconcile_order


@dataclass(frozen=True, slots=True)
class PaperAdapterCertificationResult:
    certified: bool
    checks: tuple[str, ...]
    blockers: tuple[str, ...]
    evidence_references: tuple[str, ...]


def certify_paper_execution_adapter(
    *,
    adapter: ExecutionAdapter,
    request: SubmissionRequest,
    policy: ExecutionPolicy | None = None,
) -> PaperAdapterCertificationResult:
    checks: list[str] = []
    blockers: list[str] = []
    evidence: set[str] = set(request.evidence_references)

    if adapter.provider_name.strip().lower() != "paper":
        blockers.append("adapter provider_name must be paper")
    else:
        checks.append("provider identity is paper")

    if request.provider != "paper":
        blockers.append("certification request provider must be paper")
    else:
        checks.append("request provider is paper")

    if request.environment is not ExecutionEnvironment.PAPER:
        blockers.append("certification request must use paper environment")
    else:
        checks.append("request environment is paper")

    if blockers:
        return PaperAdapterCertificationResult(
            certified=False,
            checks=tuple(sorted(checks)),
            blockers=tuple(sorted(set(blockers))),
            evidence_references=tuple(sorted(evidence)),
        )

    first_ack = adapter.submit_order(request)
    second_ack = adapter.submit_order(request)
    evidence.update(first_ack.evidence_references)
    evidence.update(second_ack.evidence_references)

    if first_ack != second_ack:
        blockers.append(
            "paper submission is not idempotent for client_order_id"
        )
    else:
        checks.append("submission is idempotent")

    if first_ack.provider_order_id is None:
        blockers.append("paper acknowledgement lacks provider_order_id")
        return PaperAdapterCertificationResult(
            certified=False,
            checks=tuple(sorted(checks)),
            blockers=tuple(sorted(set(blockers))),
            evidence_references=tuple(sorted(evidence)),
        )

    snapshot = adapter.get_order(first_ack.provider_order_id)
    fills = adapter.list_fills(first_ack.provider_order_id)
    evidence.update(snapshot.evidence_references)
    for fill in fills:
        evidence.update(fill.evidence_references)

    if snapshot.client_order_id != request.client_order_id:
        blockers.append("snapshot client_order_id mismatch")
    else:
        checks.append("snapshot is bound to client order")

    if snapshot.status is not BrokerOrderStatus.FILLED:
        blockers.append("certification order was not fully filled")
    else:
        checks.append("paper order reached filled state")

    reconciliation = reconcile_order(
        request=request,
        acknowledgement=first_ack,
        snapshot=snapshot,
        fills=fills,
        policy=policy or ExecutionPolicy(),
    )
    evidence.update(reconciliation.evidence_references)

    if reconciliation.status is not FillReconciliationStatus.FULLY_FILLED:
        blockers.append("paper order did not fully reconcile")
    elif reconciliation.blockers:
        blockers.extend(reconciliation.blockers)
    else:
        checks.append("paper fill fully reconciled")

    return PaperAdapterCertificationResult(
        certified=not blockers,
        checks=tuple(sorted(set(checks))),
        blockers=tuple(sorted(set(blockers))),
        evidence_references=tuple(sorted(evidence)),
    )
