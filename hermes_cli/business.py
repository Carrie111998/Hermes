"""CLI operations for the governed company, books, and payment rails."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from hermes_cli import (
    accounting_db,
    authority_store,
    business_audit,
    compliance_db,
    finance_db,
    organization_db,
    operational_control,
    payment_controls,
    payments,
    regulatory_compliance,
    resource_budget,
)
from hermes_cli import objectives_db as db


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = parent_subparsers.add_parser(
        "business", help="Inspect the company, accounting, budgets, and payments"
    )
    parser.add_argument("--db", type=Path)
    sub = parser.add_subparsers(dest="business_command", required=True)
    sub.add_parser("status", help="Show organization, runway, and financial statements")
    sub.add_parser("payment-rails", help="List installed standalone payment rails")
    audit = sub.add_parser(
        "audit-export",
        help="Export a hash-verifiable financial decision lineage package",
    )
    audit.add_argument("--output", type=Path, required=True)
    sub.add_parser(
        "recovery-list", help="List and verify known-good authority snapshots"
    )
    sub.add_parser(
        "recovery-create", help="Create a verified known-good authority snapshot"
    )
    recovery_verify = sub.add_parser(
        "recovery-verify", help="Verify one authority snapshot"
    )
    recovery_verify.add_argument("--snapshot", type=Path, required=True)
    recovery_restore = sub.add_parser(
        "recovery-restore",
        help="Restore an offline authority snapshot and require reconciliation",
    )
    recovery_restore.add_argument("--snapshot", type=Path, required=True)
    recovery_restore.add_argument("--actor", default="human:operator")
    recovery_restore.add_argument("--reason", required=True)
    recovery_restore.add_argument("--evidence", required=True, metavar="JSON")
    sub.add_parser("approval-list", help="List exact one-use approval artifacts")
    approval_revoke = sub.add_parser(
        "approval-revoke", help="Revoke unexecuted exact approval authority"
    )
    approval_revoke.add_argument("artifact_id")
    approval_revoke.add_argument("--actor", default="human:operator")
    approval_revoke.add_argument("--reason", required=True)
    autonomy = sub.add_parser(
        "autonomy", help="Inspect or change the master autonomy mode"
    )
    autonomy.add_argument(
        "mode", nargs="?", choices=["autonomous", "paused", "manual"]
    )
    autonomy.add_argument("--actor", default="human:operator")
    autonomy.add_argument("--reason")
    interventions = sub.add_parser(
        "interventions", help="List the human advisor intervention queue"
    )
    interventions.add_argument(
        "--status", choices=["open", "resolved", "dismissed"], default="open"
    )
    resolve = sub.add_parser(
        "intervention-resolve", help="Resolve one intervention to a recorded option"
    )
    resolve.add_argument("intervention_id")
    resolve.add_argument("--option", required=True)
    resolve.add_argument("--actor", default="human:operator")
    resolve.add_argument("--evidence", required=True, metavar="JSON")
    compute_reconcile = sub.add_parser(
        "compute-reconcile",
        help="Reconcile one reserved CEO-planner cost with exact evidence",
    )
    compute_reconcile.add_argument("reservation_id")
    compute_reconcile.add_argument(
        "--status",
        choices=["included", "provider_confirmed"],
        required=True,
    )
    compute_reconcile.add_argument("--actual-minor", type=int, default=0)
    compute_reconcile.add_argument("--model")
    compute_reconcile.add_argument("--billing-provider")
    compute_reconcile.add_argument("--provider-reference")
    compute_reconcile.add_argument("--actor", default="human:operator")
    compute_reconcile.add_argument(
        "--evidence", required=True, metavar="JSON"
    )

    instrument = sub.add_parser("instrument-add", help="Register an opaque provider instrument")
    instrument.add_argument("--provider", required=True)
    instrument.add_argument("--provider-instrument-id", required=True)
    instrument.add_argument("--rail-type", required=True)
    instrument.add_argument("--currency", default="USD")
    instrument.add_argument("--label", required=True)
    instrument.add_argument("--last4")

    controls = sub.add_parser("spend-controls", help="Bind velocity and vendor controls")
    controls.add_argument("instrument_id")
    controls.add_argument("--max-transaction-minor", type=int, required=True)
    controls.add_argument("--max-daily-minor", type=int, required=True)
    controls.add_argument("--human-threshold-minor", type=int)
    controls.add_argument("--merchant-categories", default="")
    controls.add_argument("--payees", default="")
    controls.add_argument("--policy-version", required=True)

    provider = sub.add_parser(
        "provider-verify", help="Record jurisdictional payment-provider evidence"
    )
    provider.add_argument("--provider", required=True)
    provider.add_argument("--direction", choices=["inbound", "outbound"], required=True)
    provider.add_argument("--jurisdiction", required=True)
    provider.add_argument("--registry-authority", required=True)
    provider.add_argument("--registry-reference", required=True)
    provider.add_argument("--expires-at", type=int, required=True)
    provider.add_argument("--evidence", required=True, metavar="JSON")
    provider.add_argument("--aml-screening", action="store_true")
    provider.add_argument("--sanctions-screening", action="store_true")

    assess = sub.add_parser(
        "compliance-assess", help="Record time-bound regime applicability"
    )
    assess.add_argument("regime_id")
    assess.add_argument("--verdict", choices=["applicable", "not_applicable"], required=True)
    assess.add_argument("--rationale", required=True)
    assess.add_argument("--evidence", required=True, metavar="JSON")
    assess.add_argument("--assessed-by", required=True)
    assess.add_argument("--expires-at", type=int, required=True)

    invoice = sub.add_parser("invoice", help="Create a provider-hosted receivable")
    invoice.add_argument("--provider", required=True)
    invoice.add_argument("--amount-minor", type=int, required=True)
    invoice.add_argument("--currency", default="USD")
    invoice.add_argument("--customer", required=True, metavar="JSON")
    invoice.add_argument("--customer-jurisdiction", required=True)
    invoice.add_argument("--purpose", required=True)
    invoice.add_argument("--idempotency-key", required=True)
    invoice.add_argument("--objective-id")
    invoice.add_argument("--tax-minor", type=int, default=0)
    invoice.add_argument("--tax-rule-id")

    payout = sub.add_parser("pay", help="Send an exactly reserved provider payment")
    payout.add_argument("--provider", required=True)
    payout.add_argument("--objective-id", required=True)
    payout.add_argument("--action-id", required=True)
    payout.add_argument("--amount-minor", type=int, required=True)
    payout.add_argument("--currency", default="USD")
    payout.add_argument("--payee", required=True, metavar="JSON")
    payout.add_argument("--payee-jurisdiction", required=True)
    payout.add_argument("--payee-id", required=True)
    payout.add_argument("--instrument-id", required=True)
    payout.add_argument("--merchant-category", required=True)
    payout.add_argument("--purpose", required=True)
    payout.add_argument("--idempotency-key", required=True)

    reconcile = sub.add_parser("reconcile", help="Read back and reconcile a payment")
    reconcile.add_argument("payment_intent_id")
    parser.set_defaults(func=business_command)
    return parser


def _json_object(raw: str) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("party must be a JSON object")
    return value


def _context(conn, currency: str = "USD"):
    ceo = organization_db.active_ceo(conn)
    if ceo is None:
        raise RuntimeError("solo-founder organization has not been initialized")
    account = finance_db.operating_account_for_organization(
        conn, ceo["organization_id"], currency
    )
    if account is None:
        raise RuntimeError(f"no {currency} operating treasury account")
    return ceo, account


def build_business_snapshot(conn) -> dict:
    """Build the read-only operator projection from authoritative state."""
    ceo = organization_db.active_ceo(conn)
    if ceo is None:
        return {
            "configured": False,
            "reason": "solo-founder organization has not been initialized",
            "objectives": [],
            "exceptions": [],
        }
    organization_id = ceo["organization_id"]
    from hermes_cli.objective_worker import worker_health
    from hermes_cli.objective_triggers import list_triggers
    from hermes_cli.objective_maintenance import latest_run
    from hermes_cli.business_metrics import planning_snapshot
    from hermes_cli.authority_integrity import latest_posture
    from hermes_cli.authority_recovery import recovery_posture
    from hermes_cli.business_commitments import planning_snapshot as commitment_snapshot
    from hermes_cli.approval_artifacts import approval_posture
    from hermes_cli.outcome_attribution import planning_snapshot as outcome_snapshot
    from hermes_cli.runtime_deployment import posture as runtime_posture
    from hermes_cli.workforce_delegation import planning_snapshot as delegation_snapshot
    organization = conn.execute(
        "SELECT * FROM organizations WHERE id = ?", (organization_id,)
    ).fetchone()
    currency = str(organization["base_currency"])
    account = finance_db.operating_account_for_organization(
        conn, organization_id, currency
    )
    if account is None:
        raise RuntimeError(f"no {currency} operating treasury account")
    objectives = db.list_objectives(
        conn, include_terminal=True, organization_id=organization_id
    )
    exceptions = []
    for item in objectives:
        if item["status"] != "blocked":
            continue
        event = conn.execute(
            """SELECT payload_json FROM objective_events
               WHERE objective_id = ? AND kind IN (
                   'objective_transitioned', 'action_escalated'
               ) ORDER BY id DESC LIMIT 1""",
            (item["id"],),
        ).fetchone()
        payload = json.loads(event["payload_json"]) if event else {}
        exceptions.append(
            {
                "kind": "objective_blocked",
                "objective_id": item["id"],
                "summary": item["desired_outcome"],
                "reason": str(payload.get("reason") or payload.get("message") or payload),
            }
        )
    due_events = int(
        conn.execute(
            """SELECT COUNT(*) AS n FROM objective_inbox i
               JOIN objectives o ON o.id=i.objective_id
              WHERE i.status='pending' AND o.organization_id=?""",
            (organization_id,),
        ).fetchone()["n"]
    )
    now = int(time.time())
    queue = conn.execute(
        """SELECT
             COUNT(*) AS pending,
             COALESCE(SUM(CASE WHEN i.priority>=75 THEN 1 ELSE 0 END),0)
               AS high_priority,
             COALESCE(SUM(CASE WHEN i.deadline_at IS NOT NULL
                                    AND i.deadline_at<=? THEN 1 ELSE 0 END),0)
               AS overdue,
             MIN(i.created_at) AS oldest_created_at
           FROM objective_inbox i
           JOIN objectives o ON o.id=i.objective_id
          WHERE i.status='pending' AND o.organization_id=?""",
        (now, organization_id),
    ).fetchone()
    statements = accounting_db.financial_statements(conn, organization_id)
    treasury = {
        "account_id": account,
        "currency": currency,
        "balance_minor": finance_db.account_balance(conn, account),
        "reserved_minor": finance_db.reserved_balance(conn, account),
        "compute_committed_minor": finance_db.committed_compute_balance(
            conn, account
        ),
        "available_minor": finance_db.available_balance(conn, account),
    }
    counts: dict[str, int] = {}
    for item in objectives:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    memo = {
        "headline": (
            f"{counts.get('blocked', 0)} blocked objectives; "
            f"{due_events} events awaiting evaluation "
            f"({int(queue['high_priority'])} high priority, "
            f"{int(queue['overdue'])} overdue); "
            f"{treasury['available_minor']} {currency} minor units available"
        ),
        "decisions_required": len(exceptions),
        "objective_status_counts": counts,
        "generated_from": "authoritative_state",
    }
    from hermes_cli.config import load_config

    loaded_config = load_config()
    loaded_charter = loaded_config.get("agentic") or {}
    return {
        "configured": True,
        "organization": dict(organization),
        "ceo": ceo,
        "organization_chart": organization_db.organization_chart(
            conn, organization_id
        ),
        "objectives": objectives,
        "exceptions": exceptions,
        "pending_events": due_events,
        "event_queue": {
            "pending": int(queue["pending"]),
            "high_priority": int(queue["high_priority"]),
            "overdue": int(queue["overdue"]),
            "oldest_age_seconds": (
                max(0, now - int(queue["oldest_created_at"]))
                if queue["oldest_created_at"] is not None
                else None
            ),
            "admission_policy": "deterministic_priority_with_aging",
        },
        "treasury": treasury,
        "accounting": statements,
        "decision_memo": memo,
        "autonomy": operational_control.autonomy_state(conn),
        "interventions": operational_control.list_interventions(
            conn, organization_id=organization_id
        ),
        "workers": worker_health(conn),
        "triggers": list_triggers(conn, organization_id),
        "maintenance": latest_run(conn),
        "strategy_measurement": planning_snapshot(conn, organization_id),
        "authority_store": authority_store.status(loaded_config),
        "authority_integrity": latest_posture(conn, organization_id),
        "authority_recovery": recovery_posture(conn, organization_id),
        "commitments": commitment_snapshot(conn, organization_id),
        "approvals": approval_posture(conn, organization_id),
        "outcome_attribution": outcome_snapshot(conn, organization_id),
        "runtime_deployment": runtime_posture(
            conn,
            loaded_charter,
            stale_after_seconds=max(
                60,
                int(
                    loaded_charter.get("runtime_interval_seconds", 15)
                )
                * 3,
            ),
        ),
        "employee_delegation": delegation_snapshot(conn, organization_id),
        "execution_recovery": {
            "in_doubt": db.in_doubt_executions(conn, organization_id),
            "sensitive_data_included": False,
        },
        "compute_reservations": resource_budget.compute_reservation_posture(
            conn, organization_id
        ),
    }


def business_command(args: argparse.Namespace) -> int:
    if args.business_command == "recovery-restore":
        from hermes_cli import authority_recovery

        target = (
            args.db.expanduser().resolve()
            if getattr(args, "db", None) is not None
            else db.objectives_db_path()
        )
        result = authority_recovery.restore_snapshot(
            target_db=target,
            manifest_path=args.snapshot,
            actor=args.actor,
            reason=args.reason,
            evidence=_json_object(args.evidence),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    with db.connect_closing(getattr(args, "db", None)) as conn:
        command = args.business_command
        if command == "payment-rails":
            print(
                json.dumps(
                    {
                        "inbound": sorted(payments.load_inbound_payment_rails()),
                        "outbound": sorted(payments.load_outbound_payment_rails()),
                    },
                    indent=2,
                )
            )
            return 0
        if command == "status":
            value = build_business_snapshot(conn)
            print(json.dumps(value, indent=2, sort_keys=True))
            return 0
        if command in {"approval-list", "approval-revoke"}:
            from hermes_cli import approval_artifacts

            ceo = organization_db.active_ceo(conn)
            if ceo is None:
                raise RuntimeError("solo-founder organization has not been initialized")
            if command == "approval-revoke":
                approval_artifacts.revoke(
                    conn,
                    artifact_id=args.artifact_id,
                    actor=args.actor,
                    reason=args.reason,
                )
            print(
                json.dumps(
                    approval_artifacts.approval_posture(
                        conn, str(ceo["organization_id"])
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if command in {"recovery-list", "recovery-create"}:
            from hermes_cli import authority_recovery

            ceo = organization_db.active_ceo(conn)
            if ceo is None:
                raise RuntimeError("solo-founder organization has not been initialized")
            organization_id = str(ceo["organization_id"])
            root = (
                args.db.expanduser().resolve().parent / "business-recovery"
                if getattr(args, "db", None) is not None
                else None
            )
            if command == "recovery-create":
                value = authority_recovery.create_known_good_snapshot(
                    conn, organization_id=organization_id, root=root
                )
            else:
                value = authority_recovery.list_snapshots(
                    organization_id, root=root
                )
            print(json.dumps(value, indent=2, sort_keys=True))
            return 0
        if command == "recovery-verify":
            from hermes_cli import authority_recovery

            value = authority_recovery.verify_snapshot(args.snapshot)
            print(json.dumps(value, indent=2, sort_keys=True))
            return 0 if value["valid"] else 1
        if command == "autonomy":
            if args.mode is not None:
                if not args.reason:
                    raise ValueError("changing autonomy mode requires --reason")
                operational_control.set_autonomy_mode(
                    conn, mode=args.mode, actor=args.actor, reason=args.reason
                )
            print(json.dumps(operational_control.autonomy_state(conn), indent=2))
            return 0
        if command == "interventions":
            ceo = organization_db.active_ceo(conn)
            if ceo is None:
                raise RuntimeError("solo-founder organization has not been initialized")
            print(
                json.dumps(
                    operational_control.list_interventions(
                        conn,
                        status=args.status,
                        organization_id=ceo["organization_id"],
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if command == "intervention-resolve":
            ceo = organization_db.active_ceo(conn)
            if ceo is None:
                raise RuntimeError("solo-founder organization has not been initialized")
            operational_control.resolve_intervention(
                conn,
                args.intervention_id,
                option_id=args.option,
                actor=args.actor,
                evidence=_json_object(args.evidence),
                organization_id=ceo["organization_id"],
            )
            print(json.dumps({"status": "resolved", "id": args.intervention_id}))
            return 0
        if command == "compute-reconcile":
            ceo = organization_db.active_ceo(conn)
            if ceo is None:
                raise RuntimeError(
                    "solo-founder organization has not been initialized"
                )
            reservation = conn.execute(
                """SELECT * FROM planner_compute_reservations
                    WHERE id=? AND organization_id=?""",
                (args.reservation_id, ceo["organization_id"]),
            ).fetchone()
            if reservation is None:
                raise ValueError(
                    "compute reservation is outside the active organization"
                )
            supplied_evidence = _json_object(args.evidence)
            if not supplied_evidence:
                raise ValueError(
                    "compute reconciliation requires external evidence"
                )
            evidence = {
                **supplied_evidence,
                "advisor": args.actor,
            }
            reconciliation_id = (
                resource_budget.reconcile_compute_reservation(
                    conn,
                    reservation_id=args.reservation_id,
                    status=args.status,
                    actual_minor=args.actual_minor,
                    model=args.model,
                    billing_provider=args.billing_provider,
                    provider_reference=args.provider_reference,
                    evidence=evidence,
                )
            )
            intervention = conn.execute(
                """SELECT id FROM intervention_queue
                    WHERE dedupe_key=? AND status='open'""",
                (
                    "compute-cost-unreconciled:"
                    + args.reservation_id,
                ),
            ).fetchone()
            if intervention is not None:
                operational_control.resolve_intervention(
                    conn,
                    str(intervention["id"]),
                    option_id=(
                        "confirm_included"
                        if args.status == "included"
                        else "reconcile"
                    ),
                    actor=args.actor,
                    evidence={
                        **evidence,
                        "compute_reconciliation_id": reconciliation_id,
                    },
                    organization_id=ceo["organization_id"],
                )
            print(
                json.dumps(
                    {
                        "reservation_id": args.reservation_id,
                        "reconciliation_id": reconciliation_id,
                        "status": args.status,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if command == "audit-export":
            ceo = organization_db.active_ceo(conn)
            if ceo is None:
                raise RuntimeError("solo-founder organization has not been initialized")
            package = business_audit.export_audit_package(
                conn, ceo["organization_id"]
            )
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(package, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            output.chmod(0o600)
            print(
                json.dumps(
                    {"output": str(output), "sha256": package["package_sha256"]}
                )
            )
            return 0
        if command == "instrument-add":
            ceo, _ = _context(conn, args.currency)
            instrument_id = payment_controls.register_tokenized_instrument(
                conn,
                organization_id=ceo["organization_id"],
                provider=args.provider,
                provider_instrument_id=args.provider_instrument_id,
                rail_type=args.rail_type,
                currency=args.currency,
                label=args.label,
                last4=args.last4,
            )
            print(json.dumps({"instrument_id": instrument_id}))
            return 0
        if command == "spend-controls":
            control_id = payment_controls.set_spend_controls(
                conn,
                instrument_id=args.instrument_id,
                max_transaction_minor=args.max_transaction_minor,
                max_daily_minor=args.max_daily_minor,
                human_threshold_minor=args.human_threshold_minor,
                allowed_merchant_categories=[
                    value.strip() for value in args.merchant_categories.split(",")
                    if value.strip()
                ],
                allowed_payees=[
                    value.strip() for value in args.payees.split(",") if value.strip()
                ],
                policy_version=args.policy_version,
            )
            print(json.dumps({"spend_control_id": control_id}))
            return 0
        if command == "provider-verify":
            ceo = organization_db.active_ceo(conn)
            if ceo is None:
                raise RuntimeError("solo-founder organization has not been initialized")
            assessment_id = compliance_db.verify_payment_provider(
                conn,
                organization_id=ceo["organization_id"],
                provider=args.provider,
                direction=args.direction,
                jurisdiction=args.jurisdiction,
                registry_authority=args.registry_authority,
                registry_reference=args.registry_reference,
                aml_screening_delegated=args.aml_screening,
                sanctions_screening_delegated=args.sanctions_screening,
                verified_at=int(time.time()),
                expires_at=args.expires_at,
                evidence=_json_object(args.evidence),
            )
            print(json.dumps({"provider_assessment_id": assessment_id}))
            return 0
        if command == "compliance-assess":
            ceo = organization_db.active_ceo(conn)
            if ceo is None:
                raise RuntimeError("solo-founder organization has not been initialized")
            assessment_id = regulatory_compliance.assess_applicability(
                conn,
                organization_id=ceo["organization_id"],
                regime_id=args.regime_id,
                verdict=args.verdict,
                rationale=args.rationale,
                evidence=_json_object(args.evidence),
                assessed_by=args.assessed_by,
                expires_at=args.expires_at,
            )
            print(json.dumps({"applicability_assessment_id": assessment_id}))
            return 0

        service = payments.PaymentService(
            conn,
            inbound_rails=payments.load_inbound_payment_rails(),
            outbound_rails=payments.load_outbound_payment_rails(),
        )
        if command == "reconcile":
            result = service.reconcile(args.payment_intent_id)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        ceo, account = _context(conn, args.currency if hasattr(args, "currency") else "USD")
        common = {
            "organization_id": ceo["organization_id"],
            "account_id": account,
            "provider": args.provider,
            "amount_minor": args.amount_minor,
            "currency": args.currency,
            "purpose": args.purpose,
            "idempotency_key": args.idempotency_key,
        }
        if command == "invoice":
            result = service.create_receivable(
                **common,
                customer=_json_object(args.customer),
                customer_jurisdiction=args.customer_jurisdiction,
                objective_id=args.objective_id,
                tax_minor=args.tax_minor,
                tax_rule_id=args.tax_rule_id,
            )
        elif command == "pay":
            result = service.send_payable(
                **common,
                objective_id=args.objective_id,
                action_id=args.action_id,
                payee=_json_object(args.payee),
                payee_jurisdiction=args.payee_jurisdiction,
                payee_id=args.payee_id,
                instrument_id=args.instrument_id,
                merchant_category=args.merchant_category,
            )
        else:
            raise ValueError(f"unknown business command: {command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
