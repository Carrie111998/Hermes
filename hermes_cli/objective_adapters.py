"""Edge adapters for the governed objective runtime.

The core runtime accepts typed proposals and calls registered executors and
verifiers.  This module connects those contracts to Charterforge's auxiliary model
and existing Kanban worker kernel without introducing a new model tool.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from hermes_cli.objective_runtime import (
    ActionProposal,
    ExecutionOutcome,
    PlanProposal,
    VerificationOutcome,
)
from hermes_cli.execution_boundary import (
    ExecutionBoundaryError,
    PayloadContract,
    validate_payload,
    validate_temporal_preconditions,
)


PLANNER_SYSTEM_PROMPT = """\
You are the planning component of a governed autonomous business runtime.
You propose; you do not authorize or execute.

Return exactly one JSON object:
{
  "assumptions": [],
  "tasks": [],
  "dependencies": [],
  "risks": [],
  "objective_complete_when_verified": false,
  "actions": [{
    "action_type": "...",
    "payload": {
      "system": "...",
      "target_resource": "...",
      "idempotency_key": "unique high-entropy operation key",
      "observed_state_at": 0,
      "max_state_age_seconds": 300,
      "state_evidence": {
        "source": "authoritative system read-back",
        "reference": "provider revision, commit, ETag, or evidence ID"
      },
      "compliance_context": {
        "jurisdictions": [],
        "activities": [],
        "data_classes": [],
        "entity_attributes": []
      }
    },
    "expected_outcome": "...",
    "required_capability": "...",
    "verification_method": "...",
    "risk_class": "low|medium|high|critical",
    "reversible": true,
    "rationale": "...",
    "estimated_cost_minor": 0
    ,"compensation": {
      "action_type": "...",
      "payload": {
        "system": "...",
        "target_resource": "...",
        "idempotency_key": "stable compensation operation key"
      },
      "required_capability": "...",
      "verification_method": "..."
    }
  }]
}

Rules:
- Decompose the complete strategy in tasks, but propose zero or one action:
  only the next external effect. Charterforge observes and replans after verifying it.
- Every reversible action must include an exact compensation contract using an
  available action type, capability, system, and verification method.
- Copy one exact available action contract. Never substitute its capability,
  system, or verification method.
- Include every field named by that contract's payload_required list. Use only
  payload_required and payload_optional fields; do not invent payload keys.
- Never expand authority, alter success criteria, invent credentials, or treat
  generated text as evidence.
- Never invent state evidence or observation time. Reference only authoritative
  evidence present in the event or objective snapshot. If none is present,
  return no action and identify the missing read-back in risks.
- Identify the jurisdictions, regulated activities, data classes, and entity
  attributes implicated by every action. Empty values are allowed only when
  the action truly has no external compliance exposure.
- Set objective_complete_when_verified=true only when the proposed actions can
  satisfy every objective success criterion. The independent verifier decides.
- For an objective.successor.required event, propose
  objectives.create_successor as the next effect. Give the successor a bounded,
  measurable outcome and registered verifier contracts; inherit permitted
  systems and every prohibition, and do not increase the predecessor budget.
- For a strategy metric or experiment review, use the immutable evaluation
  verdict and observation evidence hash. Stop or revise a strategy that is
  not_supported or off_track unless a bounded evidence-collection action is
  the explicit next step. Never manufacture an observation from model output.
- If authority or evidence is insufficient, return no actions and put the exact
  missing requirement in risks.
- Delegate only to an active worker listed in operating_context.workforce, and
  keep the task inside that worker's mandate, capabilities, systems, budget,
  and reporting line.
- Before communicating or accepting a dated customer, vendor, financial, SLA,
  renewal, or contractual promise, record it with commitments.create. Never
  leave a business obligation only in generated prose. Fulfil it only by
  referencing an existing passing verification whose method exactly matches
  the commitment contract.
"""


@dataclass(frozen=True)
class RegisteredActionContract:
    action_type: str
    required_capability: str
    target_system: str
    verification_method: str
    payload_required: tuple[str, ...] = ()
    payload_optional: tuple[str, ...] = ()


COMMON_ACTION_FIELDS: dict[str, type] = {
    "observed_state_at": int,
    "max_state_age_seconds": int,
    "state_evidence": dict,
    "compliance_context": dict,
    "change_freeze": dict,
    "not_before": int,
    "not_after": int,
}


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("objective planner did not return a JSON object")
        value = json.loads(raw[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("objective planner response must be a JSON object")
    return value


class AuxiliaryObjectivePlanner:
    """Structured planning through Charterforge's configured auxiliary model."""

    identity = "employee:ceo"

    def __init__(
        self,
        *,
        action_contracts: Sequence[RegisteredActionContract],
        timeout: float = 120,
        context_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
        authority_conn=None,
        resource_limits: Optional[Mapping[str, Any]] = None,
        planner_call_compute_reservation_minor: int = 0,
        enforce_treasury_budget: bool = False,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
    ):
        self.action_contracts = {
            contract.action_type: contract for contract in action_contracts
        }
        self.action_types = sorted(self.action_contracts)
        self.capabilities = sorted(
            {contract.required_capability for contract in action_contracts}
        )
        self.systems = sorted(
            {contract.target_system for contract in action_contracts}
        )
        self.verification_methods = sorted(
            {contract.verification_method for contract in action_contracts}
        )
        self.timeout = timeout
        self.context_provider = context_provider
        self.authority_conn = authority_conn
        self.resource_limits = (
            dict(resource_limits) if resource_limits is not None else None
        )
        self.planner_call_compute_reservation_minor = int(
            planner_call_compute_reservation_minor
        )
        self.enforce_treasury_budget = bool(enforce_treasury_budget)
        self.billing_provider = (
            str(billing_provider).strip() if billing_provider else None
        )
        self.billing_base_url = (
            str(billing_base_url).strip() if billing_base_url else None
        )
        self.accounts_resources_pre_call = (
            authority_conn is not None and self.resource_limits is not None
        )
        if (
            self.accounts_resources_pre_call
            and self.planner_call_compute_reservation_minor <= 0
        ):
            raise ValueError(
                "governed planner calls require a positive compute reservation"
            )

    def propose(
        self, snapshot: Mapping[str, Any], event: Mapping[str, Any]
    ) -> PlanProposal:
        from agent.auxiliary_client import call_llm

        bounded_snapshot = {
            "id": snapshot.get("id"),
            "desired_outcome": snapshot.get("desired_outcome"),
            "status": snapshot.get("status"),
            "constraints": snapshot.get("constraints"),
            "success_criteria": snapshot.get("success_criteria"),
            "termination": snapshot.get("termination"),
            "permitted_systems": snapshot.get("permitted_systems"),
            "prohibited_actions": snapshot.get("prohibited_actions"),
            "max_spend_minor": snapshot.get("max_spend_minor"),
            "latest_plan": (snapshot.get("plans") or [])[-1:] or [],
            "recent_actions": (snapshot.get("actions") or [])[-10:],
            "recent_verifications": (snapshot.get("verifications") or [])[-10:],
        }
        user_prompt = json.dumps(
            {
                "objective": bounded_snapshot,
                "event": {
                    "event_type": event.get("event_type"),
                    "payload": event.get("payload"),
                    "attempts": event.get("attempts"),
                    "received_at": event.get("created_at"),
                },
                "available": {
                    "action_contracts": [
                        asdict(self.action_contracts[name])
                        for name in self.action_types
                    ],
                },
                "operating_context": (
                    dict(self.context_provider())
                    if self.context_provider is not None
                    else {}
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        request_record = {
            "task": "objective_planner",
            "messages": messages,
            "temperature": 0,
            "max_tokens": 4096,
            "timeout": self.timeout,
        }
        compute_reservation_id = None
        if self.accounts_resources_pre_call:
            from hermes_cli import resource_budget

            # UTF-8 bytes are a conservative upper bound for tokenizer units.
            # Include protocol framing and reserve the entire output allowance.
            projected_input_tokens = (
                len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
                + 512
            )
            projected_output_tokens = int(request_record["max_tokens"])
            compute_reservation_id = resource_budget.reserve_planner_call(
                self.authority_conn,
                objective_id=str(snapshot.get("id") or ""),
                limits=self.resource_limits,
                input_tokens=projected_input_tokens,
                output_tokens=projected_output_tokens,
                estimated_compute_cost_minor=(
                    self.planner_call_compute_reservation_minor
                ),
                enforce_treasury=self.enforce_treasury_budget,
                inbox_event_id=(
                    str(event["id"]) if event.get("id") is not None else None
                ),
            )
            request_record["compute_reservation_id"] = (
                compute_reservation_id
            )
        started_at = time.time_ns()
        try:
            response = call_llm(
            task="objective_planner",
            messages=messages,
            temperature=0,
            max_tokens=4096,
            timeout=self.timeout,
            )
        except Exception as exc:
            if self.authority_conn is not None:
                from hermes_cli import planner_inferences

                planner_inferences.record(
                    self.authority_conn,
                    objective_id=str(snapshot.get("id") or ""),
                    inbox_event_id=(
                        str(event["id"]) if event.get("id") is not None else None
                    ),
                    planner_identity=self.identity,
                    task="objective_planner",
                    model=None,
                    request=request_record,
                    response_text=None,
                    parse_status="call_failed",
                    error=str(exc),
                    input_tokens=0,
                    output_tokens=0,
                    started_at=started_at,
                    finished_at=time.time_ns(),
                )
            raise
        input_tokens = int(
            getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0
        )
        output_tokens = int(
            getattr(getattr(response, "usage", None), "completion_tokens", 0) or 0
        )
        model = getattr(response, "model", None)
        if (
            compute_reservation_id is not None
            and self.billing_provider
            and self.billing_provider.lower() != "auto"
        ):
            from agent.usage_pricing import (
                estimate_usage_cost,
                normalize_usage,
            )
            from hermes_cli import resource_budget

            cost = estimate_usage_cost(
                str(model or ""),
                normalize_usage(
                    getattr(response, "usage", None),
                    provider=self.billing_provider,
                ),
                provider=self.billing_provider,
                base_url=self.billing_base_url,
            )
            if cost.status == "included" and cost.amount_usd == 0:
                resource_budget.reconcile_compute_reservation(
                    self.authority_conn,
                    reservation_id=compute_reservation_id,
                    status="included",
                    actual_minor=0,
                    model=str(model or ""),
                    billing_provider=self.billing_provider,
                    provider_reference=None,
                    evidence={
                        "cost_status": cost.status,
                        "cost_source": cost.source,
                        "pricing_version": cost.pricing_version,
                        "route_declared_in_config": True,
                    },
                )
        try:
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            if self.authority_conn is not None:
                from hermes_cli import planner_inferences

                planner_inferences.record(
                    self.authority_conn,
                    objective_id=str(snapshot.get("id") or ""),
                    inbox_event_id=(
                        str(event["id"]) if event.get("id") is not None else None
                    ),
                    planner_identity=self.identity,
                    task="objective_planner",
                    model=str(model) if model else None,
                    request=request_record,
                    response_text=None,
                    parse_status="invalid_response",
                    error="objective planner returned no message",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    started_at=started_at,
                    finished_at=time.time_ns(),
                )
            raise ValueError("objective planner returned no message") from exc
        try:
            data = _extract_json_object(raw)
        except Exception as exc:
            if self.authority_conn is not None:
                from hermes_cli import planner_inferences

                planner_inferences.record(
                    self.authority_conn,
                    objective_id=str(snapshot.get("id") or ""),
                    inbox_event_id=(
                        str(event["id"]) if event.get("id") is not None else None
                    ),
                    planner_identity=self.identity,
                    task="objective_planner",
                    model=str(model) if model else None,
                    request=request_record,
                    response_text=raw,
                    parse_status="invalid_response",
                    error=str(exc),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    started_at=started_at,
                    finished_at=time.time_ns(),
                )
            raise
        inference_id = None
        if self.authority_conn is not None:
            from hermes_cli import planner_inferences

            inference_id = planner_inferences.record(
                self.authority_conn,
                objective_id=str(snapshot.get("id") or ""),
                inbox_event_id=(
                    str(event["id"]) if event.get("id") is not None else None
                ),
                planner_identity=self.identity,
                task="objective_planner",
                model=str(model) if model else None,
                request=request_record,
                response_text=raw,
                parse_status="parsed",
                error=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                started_at=started_at,
                finished_at=time.time_ns(),
            )
        actions: list[ActionProposal] = []
        for item in data.get("actions") or []:
            if not isinstance(item, dict):
                raise ValueError("objective planner action must be an object")
            action_type = str(item.get("action_type", ""))
            capability = str(item.get("required_capability", ""))
            method = str(item.get("verification_method", ""))
            payload = item.get("payload")
            if action_type not in self.action_types:
                raise ValueError(f"planner proposed unavailable action type: {action_type}")
            contract = self.action_contracts[action_type]
            if capability != contract.required_capability:
                raise ValueError("planner capability does not match action contract")
            if method != contract.verification_method:
                raise ValueError("planner verifier does not match action contract")
            if (
                not isinstance(payload, dict)
                or payload.get("system") != contract.target_system
            ):
                raise ValueError("planner system does not match action contract")
            compensation_contract = item.get("compensation")
            if compensation_contract is not None:
                if not isinstance(compensation_contract, dict):
                    raise ValueError("planner compensation contract must be an object")
                comp_payload = compensation_contract.get("payload")
                comp_action_type = str(compensation_contract.get("action_type") or "")
                if comp_action_type not in self.action_contracts:
                    raise ValueError("planner proposed unavailable compensation action")
                comp_contract = self.action_contracts[comp_action_type]
                if (
                    compensation_contract.get("required_capability")
                    != comp_contract.required_capability
                ):
                    raise ValueError("planner compensation capability mismatch")
                if (
                    compensation_contract.get("verification_method")
                    != comp_contract.verification_method
                ):
                    raise ValueError("planner compensation verifier mismatch")
                if (
                    not isinstance(comp_payload, dict)
                    or comp_payload.get("system") != comp_contract.target_system
                ):
                    raise ValueError("planner compensation system mismatch")
            actions.append(
                ActionProposal(
                    action_type=action_type,
                    payload=payload,
                    expected_outcome=str(item.get("expected_outcome", "")).strip(),
                    required_capability=capability,
                    verification_method=method,
                    risk_class=str(item.get("risk_class", "")).strip(),
                    reversible=bool(item.get("reversible", False)),
                    rationale=str(item.get("rationale", "")).strip(),
                    estimated_cost_minor=item.get("estimated_cost_minor"),
                    compensation=(
                        dict(compensation_contract)
                        if isinstance(compensation_contract, dict)
                        else None
                    ),
                )
            )
        return PlanProposal(
            assumptions=data.get("assumptions") or [],
            tasks=data.get("tasks") or [],
            dependencies=data.get("dependencies") or [],
            risks=data.get("risks") or [],
            actions=actions,
            objective_complete_when_verified=bool(
                data.get("objective_complete_when_verified", False)
            ),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            inference_id=inference_id,
        )


def organization_planning_context(
    conn,
    organization_id: str,
) -> dict[str, Any]:
    """Return a credential-free, mandate-bound workforce view for planning."""
    import time

    from hermes_cli import (
        accounting_db,
        business_metrics,
        business_commitments,
        finance_db,
        outcome_attribution,
        objective_portfolio,
        organization_db,
        regulatory_compliance,
        workforce_delegation,
    )

    organization_db.ensure_schema(conn)
    finance_db.ensure_schema(conn)
    accounting_db.ensure_schema(conn)
    regulatory_compliance.ensure_schema(conn)
    objective_portfolio.ensure_schema(conn)
    business_metrics.ensure_schema(conn)
    business_commitments.ensure_schema(conn)
    outcome_attribution.ensure_schema(conn)
    workforce_delegation.ensure_schema(conn)
    organization = conn.execute(
        """SELECT id,name,purpose,base_currency,headcount_limit,
                  payroll_budget_minor
           FROM organizations WHERE id=?""",
        (organization_id,),
    ).fetchone()
    if organization is None:
        raise ValueError("active CEO organization is missing")
    rows = conn.execute(
        """SELECT e.* FROM employees e
           WHERE e.organization_id=? AND e.status='active'
           ORDER BY e.started_at,e.id""",
        (organization_id,),
    ).fetchall()
    workforce = []
    for row in rows:
        mandate = organization_db.get_current_mandate(conn, str(row["id"]))
        if mandate is None:
            raise ValueError(f"active employee {row['id']} has no mandate")
        workforce.append(
            {
                "employee_id": str(row["id"]),
                "profile_name": str(row["profile_name"]),
                "title": str(row["title"]),
                "corporate_level": str(row["level"]),
                "manager_employee_id": (
                    str(row["manager_id"]) if row["manager_id"] else None
                ),
                "employment_class": (
                    "contractor"
                    if row["employment_type"] == "contractor"
                    else "fte"
                ),
                "hired_for_objective_id": row["hired_for_objective_id"],
                "mandate": {
                    "id": mandate["id"],
                    "version": mandate["version"],
                    "purpose": mandate["purpose"],
                    "responsibilities": mandate["responsibilities"],
                    "decision_rights": mandate["decision_rights"],
                    "prohibited_actions": mandate["prohibited_actions"],
                    "capabilities": mandate["capabilities"],
                    "systems": mandate["systems"],
                    "toolsets": mandate["toolsets"],
                    "skills": mandate.get("skills") or [],
                    "budget_minor": mandate["budget_minor"],
                    "expires_at": mandate["expires_at"],
                    "escalation": mandate["escalation"],
                },
            }
        )
    treasury = []
    base_currency = str(organization["base_currency"])
    base_currency_available_minor = 0
    for account in conn.execute(
        """SELECT id,name,currency FROM treasury_accounts
           WHERE organization_id=? ORDER BY name,currency,id""",
        (organization_id,),
    ).fetchall():
        balance = finance_db.account_balance(conn, str(account["id"]))
        reserved = finance_db.reserved_balance(conn, str(account["id"]))
        available = finance_db.available_balance(conn, str(account["id"]))
        if str(account["currency"]) == base_currency:
            base_currency_available_minor += available
        treasury.append(
            {
                "name": str(account["name"]),
                "currency": str(account["currency"]),
                "balance_minor": balance,
                "reserved_minor": reserved,
                "available_minor": available,
            }
        )
    statements = accounting_db.financial_statements(conn, organization_id)
    now = int(time.time())
    burn = int(
        conn.execute(
            """SELECT COALESCE(SUM(line.debit_minor-line.credit_minor),0) AS burn
               FROM journal_lines line
               JOIN journal_entries entry
                 ON entry.id=line.journal_entry_id
               JOIN ledger_accounts account ON account.id=line.account_id
              WHERE entry.organization_id=?
                AND account.account_type='expense'
                AND entry.occurred_at>=?""",
            (organization_id, now - 30 * 86_400),
        ).fetchone()["burn"]
    )
    tax_obligations = [
        {
            "id": str(row["id"]),
            "due_at": int(row["due_at"]),
            "amount_minor": int(row["amount_minor"]),
            "currency": str(row["currency"]),
            "status": str(row["status"]),
        }
        for row in conn.execute(
            """SELECT id,due_at,amount_minor,currency,status
               FROM tax_obligations
              WHERE organization_id=? AND status NOT IN ('paid','filed')
              ORDER BY due_at,id LIMIT 20""",
            (organization_id,),
        ).fetchall()
    ]
    compliance_obligations = [
        {
            "id": str(row["id"]),
            "regime_id": str(row["regime_id"]),
            "name": str(row["name"]),
            "required_control": str(row["required_control"]),
            "effective_to": row["effective_to"],
            "status": str(row["status"]),
        }
        for row in conn.execute(
            """SELECT id,regime_id,name,required_control,effective_to,status
               FROM compliance_obligations
              WHERE organization_id=? AND status='active'
              ORDER BY effective_from,id LIMIT 20""",
            (organization_id,),
        ).fetchall()
    ]
    active_objectives = [
        {
            "id": str(row["id"]),
            "desired_outcome": str(row["desired_outcome"]),
            "status": str(row["status"]),
            "owner": row["owner"],
            "max_spend_minor": row["max_spend_minor"],
            "currency": row["currency"],
            "expires_at": row["expires_at"],
        }
        for row in conn.execute(
            """SELECT id,desired_outcome,status,owner,max_spend_minor,currency,
                      expires_at
               FROM objectives
              WHERE organization_id=? AND status NOT IN (
                'closed','cancelled','expired','abandoned','superseded','verified'
              )
              ORDER BY created_at,id LIMIT 50""",
            (organization_id,),
        ).fetchall()
    ]
    objective_relationships = [
        {
            "parent_objective_id": str(row["parent_objective_id"]),
            "child_objective_id": str(row["child_objective_id"]),
            "relationship": str(row["relationship"]),
            "allocated_budget_minor": int(row["allocated_budget_minor"]),
            "currency": row["currency"],
        }
        for row in conn.execute(
            """SELECT parent_objective_id,child_objective_id,relationship,
                      allocated_budget_minor,currency
               FROM objective_relationships
              WHERE organization_id=? ORDER BY created_at,id LIMIT 100""",
            (organization_id,),
        ).fetchall()
    ]
    return {
        "organization": dict(organization),
        "workforce": workforce,
        "delegation_contract": {
            "assignee_field": "profile_name",
            "requires_active_descendant_or_self": True,
            "credentials_included": False,
        },
        "finance": {
            "treasury": treasury,
            "base_currency": base_currency,
            "base_currency_available_minor": base_currency_available_minor,
            "recent_30_day_expenses_minor": burn,
            "runway_days": (
                (base_currency_available_minor * 30) // burn
                if burn > 0
                else None
            ),
            "runway_basis": (
                "trailing_30_day_ledger_expenses"
                if burn > 0
                else "no_recent_expense_history"
            ),
            "profit_and_loss": statements["profit_and_loss"],
            "balance_sheet": statements["balance_sheet"],
            "tax_liability_minor": statements["tax_liability_minor"],
            "open_tax_obligations": tax_obligations,
        },
        "compliance": {
            "active_obligations": compliance_obligations,
            "obligation_limit": 20,
        },
        "procurement": {
            "preference_order": ["existing", "foss", "build", "buy", "defer"],
            "foss_before_paid": True,
            "build_before_paid": True,
            "paid_requires_persistent_need_and_positive_roi": True,
            "hard_budget_source": "finance.base_currency_available_minor",
        },
        "portfolio": {
            "active_objectives": active_objectives,
            "relationships": objective_relationships,
            "objective_limit": 50,
            "relationship_limit": 100,
        },
        "strategy_measurement": business_metrics.planning_snapshot(
            conn, organization_id
        ),
        "commitments": business_commitments.planning_snapshot(
            conn, organization_id
        ),
        "outcome_attribution": outcome_attribution.planning_snapshot(
            conn, organization_id
        ),
        "employee_delegation": workforce_delegation.planning_snapshot(
            conn, organization_id
        ),
        "sensitive_data_included": False,
    }


class ActionExecutorRegistry:
    """Explicit action-type registry; unknown actions cannot execute."""

    def __init__(
        self,
        identity: str = "employee:ceo",
        *,
        authority_conn=None,
        failure_threshold: int = 3,
        cooldown_seconds: int = 900,
    ):
        self.identity = identity
        self._handlers: dict[str, Callable[[Mapping[str, Any]], ExecutionOutcome]] = {}
        self._contracts: dict[str, PayloadContract] = {}
        self._authority_contracts: dict[str, RegisteredActionContract] = {}
        self._authority_conn = authority_conn
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds

    @property
    def action_types(self) -> list[str]:
        return sorted(self._handlers)

    @property
    def authority_contracts(self) -> list[RegisteredActionContract]:
        return [
            self._authority_contracts[name]
            for name in sorted(self._authority_contracts)
        ]

    def register(
        self,
        action_type: str,
        handler: Callable[[Mapping[str, Any]], ExecutionOutcome],
        *,
        contract: Optional[PayloadContract] = None,
        required_capability: Optional[str] = None,
        target_system: Optional[str] = None,
        verification_method: Optional[str] = None,
    ) -> None:
        if not action_type.strip():
            raise ValueError("action_type must not be empty")
        self._handlers[action_type] = handler
        self._authority_contracts[action_type] = RegisteredActionContract(
            action_type=action_type,
            required_capability=required_capability or action_type,
            target_system=target_system or "",
            verification_method=verification_method or "",
            payload_required=(
                tuple(sorted(contract.required)) if contract is not None else ()
            ),
            payload_optional=(
                tuple(sorted(contract.optional)) if contract is not None else ()
            ),
        )
        if contract is not None:
            self._contracts[action_type] = contract

    def validate_proposal(self, action: ActionProposal) -> None:
        contract = self._authority_contracts.get(action.action_type)
        if contract is None:
            raise ValueError(
                f"no registered authority contract for {action.action_type}"
            )
        if action.required_capability != contract.required_capability:
            raise ValueError("action capability does not match executor contract")
        if action.verification_method != contract.verification_method:
            raise ValueError("action verifier does not match executor contract")
        if action.payload.get("system") != contract.target_system:
            raise ValueError("action system does not match executor contract")

    def execute(
        self, action_type: str, payload: Mapping[str, Any]
    ) -> ExecutionOutcome:
        handler = self._handlers.get(action_type)
        if handler is None:
            return ExecutionOutcome(
                status="failed",
                result={"error": f"no executor registered for {action_type}"},
            )
        try:
            validate_payload(payload, self._contracts.get(action_type))
            validate_temporal_preconditions(payload)
        except (ExecutionBoundaryError, TypeError, ValueError) as exc:
            return ExecutionOutcome(
                status="failed",
                result={"error": f"execution boundary rejected action: {exc}"},
            )
        if self._authority_conn is None:
            return handler(payload)
        from hermes_cli import operational_control

        # Recheck the kill-switch at the last deterministic boundary before
        # invoking an external handler.  Permit issuance and lease acquisition
        # can race with an operator pause; no new side effect should begin
        # after the pause has committed.
        operational_control.ensure_schema(self._authority_conn)
        try:
            operational_control.assert_autonomous(self._authority_conn)
        except operational_control.AutonomyRevokedError as exc:
            return ExecutionOutcome(
                status="failed",
                result={"error": f"autonomy execution is paused: {exc}"},
            )
        from hermes_cli import operation_circuit_breaker as breaker

        operation_key = f"{action_type}:{payload.get('system', '')}"
        try:
            breaker.assert_admissible(self._authority_conn, operation_key)
        except breaker.CircuitOpenError as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        outcome = handler(payload)
        breaker.record_outcome(
            self._authority_conn,
            operation_key=operation_key,
            succeeded=outcome.status == "succeeded",
            failure_threshold=self._failure_threshold,
            cooldown_seconds=self._cooldown_seconds,
            error=str(outcome.result.get("error", ""))
            if isinstance(outcome.result, Mapping)
            else "",
        )
        return outcome

    def execute_governed(
        self,
        action_id: str,
        objective_id: str,
        action_type: str,
        payload: Mapping[str, Any],
    ) -> ExecutionOutcome:
        """Supply authoritative IDs out-of-band without changing permitted payload."""
        contextual = dict(payload)
        contextual["_governance_action_id"] = action_id
        contextual["_governance_objective_id"] = objective_id
        return self.execute(action_type, contextual)


ActionVerifier = Callable[[ActionProposal, ExecutionOutcome], VerificationOutcome]
ObjectiveVerifier = Callable[[Mapping[str, Any], Mapping[str, Any]], VerificationOutcome]


class IndependentVerifierRegistry:
    """Deterministic verifier routing, separate from planner and executor."""

    def __init__(self, identity: str = "control:verification"):
        self.identity = identity
        self._action: dict[str, ActionVerifier] = {}
        self._objective: dict[str, ObjectiveVerifier] = {}

    @property
    def action_methods(self) -> list[str]:
        return sorted(self._action)

    @property
    def objective_methods(self) -> list[str]:
        return sorted(self._objective)

    def register_action(self, method: str, verifier: ActionVerifier) -> None:
        self._action[method] = verifier

    def register_objective(self, method: str, verifier: ObjectiveVerifier) -> None:
        self._objective[method] = verifier

    def verify(
        self, action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        verifier = self._action.get(action.verification_method)
        if verifier is None:
            from hermes_cli import verification_evidence

            return VerificationOutcome(
                "inconclusive",
                verification_evidence.build(
                    observer=self.identity,
                    source_kind="deterministic_check",
                    source_reference="verifier.registry",
                    facts={
                        "error": (
                            f"no verifier registered for "
                            f"{action.verification_method}"
                        )
                    },
                ),
            )
        outcome = verifier(action, execution)
        from hermes_cli import verification_evidence

        try:
            verification_evidence.validate(
                outcome.evidence, expected_observer=self.identity
            )
        except verification_evidence.EvidenceContractError as exc:
            return VerificationOutcome(
                "inconclusive",
                verification_evidence.build(
                    observer=self.identity,
                    source_kind="deterministic_check",
                    source_reference=f"verifier.contract:{action.verification_method}",
                    facts={"error": f"invalid independent evidence: {exc}"},
                ),
            )
        return outcome

    def verify_objective(
        self,
        snapshot: Mapping[str, Any],
        plan: PlanProposal,
        action_verifications: Sequence[VerificationOutcome],
    ) -> VerificationOutcome:
        criteria = snapshot.get("success_criteria") or []
        from hermes_cli import verification_evidence

        def concluded(verdict: str, facts: Mapping[str, Any]) -> VerificationOutcome:
            return VerificationOutcome(
                verdict,
                verification_evidence.build(
                    observer=self.identity,
                    source_kind="deterministic_check",
                    source_reference=f"objective:{snapshot.get('id', 'unknown')}",
                    facts=facts,
                ),
            )

        if not criteria:
            return concluded("inconclusive", {"error": "objective has no success criteria"})
        evidence: list[Any] = []
        for criterion in criteria:
            if not isinstance(criterion, dict):
                return concluded(
                    "inconclusive", {
                        "error": "success criteria must be structured verifier contracts",
                        "criterion": criterion,
                    },
                )
            method = str(criterion.get("verifier", ""))
            verifier = self._objective.get(method)
            if verifier is None:
                return concluded(
                    "inconclusive",
                    {"error": f"no objective verifier registered for {method}"},
                )
            outcome = verifier(snapshot, criterion.get("params") or {})

            try:
                verification_evidence.validate(
                    outcome.evidence, expected_observer=self.identity
                )
            except verification_evidence.EvidenceContractError as exc:
                return concluded(
                    "inconclusive", {
                        "error": f"invalid independent evidence for {method}: {exc}",
                        "criteria": evidence,
                    },
                )
            evidence.append(
                {"verifier": method, "verdict": outcome.verdict, "evidence": outcome.evidence}
            )
            if outcome.verdict != "pass":
                return concluded(outcome.verdict, {"criteria": evidence})
        return concluded("pass", {"criteria": evidence})


def register_kanban_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    board: Optional[str] = None,
    authority_conn=None,
    manager_employee_id: Optional[str] = None,
) -> None:
    """Register existing Kanban as a governed employee-delegation edge."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import workforce_delegation

    def create_task(payload: Mapping[str, Any]) -> ExecutionOutcome:
        required = ("title", "body", "assignee")
        missing = [key for key in required if not str(payload.get(key, "")).strip()]
        if missing:
            return ExecutionOutcome(
                "failed", {"error": f"missing Kanban fields: {', '.join(missing)}"}
            )
        if authority_conn is not None:
            from hermes_cli import organization_db

            if not manager_employee_id or not organization_db.may_delegate_to(
                authority_conn,
                manager_employee_id=manager_employee_id,
                assignee_profile=str(payload["assignee"]),
            ):
                return ExecutionOutcome(
                    "failed",
                    {
                        "error": (
                            "assignee is not an active employee in the manager's "
                            "reporting hierarchy"
                        )
                    },
                )
            ceo = organization_db.active_ceo(authority_conn)
            if ceo is None:
                return ExecutionOutcome("failed", {"error": "active CEO is missing"})
            try:
                grant_id, _ = workforce_delegation.create_grant(
                    authority_conn,
                    organization_id=str(ceo["organization_id"]),
                    objective_id=str(payload["_governance_objective_id"]),
                    action_id=str(payload["_governance_action_id"]),
                    manager_employee_id=str(manager_employee_id),
                    assignee_profile=str(payload["assignee"]),
                    title=str(payload["title"]),
                    body=str(payload["body"]),
                    capabilities=list(payload["task_capabilities"]),
                    systems=list(payload["task_systems"]),
                    toolsets=list(payload["task_toolsets"]),
                    skills=list(payload.get("skills") or []),
                    budget_minor=int(payload["task_budget_minor"]),
                    expires_at=int(payload["task_expires_at"]),
                )
                governed_body = (
                    workforce_delegation.worker_scope(authority_conn, grant_id)
                    + "\n\n## Assigned work\n"
                    + str(payload["body"])
                )
            except Exception as exc:
                return ExecutionOutcome(
                    "failed", {"error": f"employee task grant rejected: {exc}"}
                )
        else:
            grant_id = None
            governed_body = str(payload["body"])
        with kb.connect_closing(board=board) as conn:
            task_id = kb.create_task(
                conn,
                title=str(payload["title"]),
                body=governed_body,
                assignee=str(payload["assignee"]),
                created_by=executor.identity,
                priority=int(payload.get("priority", 0) or 0),
                idempotency_key=(
                    str(payload["idempotency_key"])
                    if payload.get("idempotency_key")
                    else None
                ),
                skills=list(payload.get("skills") or []) or None,
                tenant=(
                    str(ceo["organization_id"])
                    if authority_conn is not None
                    else None
                ),
                execution_contract_id=grant_id,
            )
        if authority_conn is not None and grant_id is not None:
            try:
                workforce_delegation.bind_task(
                    authority_conn,
                    grant_id=grant_id,
                    task_id=task_id,
                    board=board or kb.get_current_board(),
                )
            except Exception as exc:
                return ExecutionOutcome(
                    "failed",
                    {
                        "error": (
                            "Kanban task exists but employee grant binding failed: "
                            f"{exc}"
                        ),
                        "task_id": task_id,
                        "grant_id": grant_id,
                        "requires_reconciliation": True,
                    },
                    external_reference=task_id,
                )
        return ExecutionOutcome(
            "succeeded",
            {"task_id": task_id, "board": board or kb.get_current_board()},
            external_reference=task_id,
        )

    def task_created(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        task_id = execution.external_reference
        if not task_id:
            return VerificationOutcome(
                "fail",
                verification_evidence.build(
                    observer=verifier.identity,
                    source_kind="deterministic_check",
                    source_reference="execution.external_reference",
                    facts={"task_id_present": False},
                ),
            )
        with kb.connect_closing(board=board) as conn:
            task = kb.get_task(conn, task_id)
        if task is None:
            return VerificationOutcome(
                "fail",
                verification_evidence.build(
                    observer=verifier.identity,
                    source_kind="authoritative_database_readback",
                    source_reference=f"kanban:{task_id}",
                    facts={"task_id": task_id, "exists": False},
                ),
            )

        grant_facts: dict[str, Any] = {}
        grant_valid = authority_conn is None
        if authority_conn is not None:
            grant = workforce_delegation.grant_for_task(authority_conn, task_id)
            grant_valid = bool(
                grant
                and task.execution_contract_id == grant["id"]
                and task.assignee == grant["assignee_profile"]
                and task.tenant == grant["organization_id"]
                and hashlib.sha256(task.title.encode()).hexdigest()
                == grant["title_sha256"]
                and task.body
                == workforce_delegation.worker_scope(
                    authority_conn, str(grant["id"])
                )
                + "\n\n## Assigned work\n"
                + str(action.payload["body"])
            )
            grant_facts = {
                "grant_id": str(grant["id"]) if grant else None,
                "grant_valid": grant_valid,
                "mandate_id": str(grant["mandate_id"]) if grant else None,
            }
        return VerificationOutcome(
            "pass" if grant_valid else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"kanban:{task_id}",
                facts={
                    "task_id": task_id,
                    "status": task.status,
                    "assignee": task.assignee,
                    **grant_facts,
                },
            ),
        )

    def all_delegated_tasks_completed(
        snapshot: Mapping[str, Any], params: Mapping[str, Any]
    ) -> VerificationOutcome:
        task_ids = [
            str(result.get("external_reference"))
            for result in snapshot.get("execution_results") or []
            if result.get("external_reference")
        ]
        if not task_ids:
            from hermes_cli import verification_evidence

            return VerificationOutcome(
                "inconclusive",
                verification_evidence.build(
                    observer=verifier.identity,
                    source_kind="deterministic_check",
                    source_reference="objective.execution_results",
                    facts={"task_ids": [], "error": "no delegated Kanban tasks"},
                ),
            )
        evidence = []
        with kb.connect_closing(board=board) as conn:
            for task_id in task_ids:
                task = kb.get_task(conn, task_id)
                if task is None:
                    from hermes_cli import verification_evidence

                    return VerificationOutcome(
                        "fail",
                        verification_evidence.build(
                            observer=verifier.identity,
                            source_kind="authoritative_database_readback",
                            source_reference=f"kanban:{task_id}",
                            facts={"task_id": task_id, "exists": False},
                        ),
                    )
                evidence.append(
                    {
                        "task_id": task_id,
                        "status": task.status,
                        "result": task.result,
                    }
                )
                if authority_conn is not None:
                    try:
                        grant = workforce_delegation.validate_task_result_authority(
                            authority_conn, task_id
                        )
                        expected_body = (
                            workforce_delegation.worker_scope(
                                authority_conn, str(grant["id"])
                            )
                            + "\n\n## Assigned work\n"
                            + workforce_delegation.assigned_work_body(task.body)
                        )
                        grant_valid = bool(
                            task.execution_contract_id == grant["id"]
                            and task.assignee == grant["assignee_profile"]
                            and task.tenant == grant["organization_id"]
                            and hashlib.sha256(task.title.encode()).hexdigest()
                            == grant["title_sha256"]
                            and hashlib.sha256(
                                workforce_delegation.assigned_work_body(
                                    task.body
                                ).encode()
                            ).hexdigest()
                            == grant["body_sha256"]
                            and task.body == expected_body
                        )
                    except Exception as exc:
                        grant_valid = False
                        evidence[-1]["authority_error"] = str(exc)
                    evidence[-1]["grant_valid"] = grant_valid
                    if not grant_valid:
                        from hermes_cli import verification_evidence

                        return VerificationOutcome(
                            "fail",
                            verification_evidence.build(
                                observer=verifier.identity,
                                source_kind="authoritative_database_readback",
                                source_reference=f"kanban:{task_id}",
                                facts={"tasks": evidence},
                            ),
                        )
                if task.status != "done":
                    from hermes_cli import verification_evidence

                    return VerificationOutcome(
                        "inconclusive",
                        verification_evidence.build(
                            observer=verifier.identity,
                            source_kind="authoritative_database_readback",
                            source_reference=(
                                f"kanban-board:{board or kb.get_current_board()}"
                            ),
                            facts={"tasks": evidence},
                        ),
                    )
        from hermes_cli import verification_evidence

        return VerificationOutcome(
            "pass",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"kanban-board:{board or kb.get_current_board()}",
                facts={"tasks": evidence},
            ),
        )

    executor.register(
        "kanban.create_task",
        create_task,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "title": str,
                "body": str,
                "assignee": str,
                "task_capabilities": list,
                "task_systems": list,
                "task_toolsets": list,
                "task_budget_minor": int,
                "task_expires_at": int,
            },
            optional={
                **COMMON_ACTION_FIELDS,
                "priority": int,
                "skills": list,
            },
        ),
        required_capability="work.delegate",
        target_system="kanban",
        verification_method="kanban.task.created",
    )
    verifier.register_action("kanban.task.created", task_created)
    verifier.register_objective(
        "kanban.all_delegated_tasks_completed", all_delegated_tasks_completed
    )


def register_payment_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    authority_conn,
) -> None:
    """Expose governed inbound/outbound payment operations to the runtime."""
    from hermes_cli import accounting_db, finance_db, organization_db, payments, usage_billing

    ceo = organization_db.active_ceo(authority_conn)
    if ceo is None:
        return
    organization_id = ceo["organization_id"]
    organization = authority_conn.execute(
        "SELECT base_currency FROM organizations WHERE id = ?", (organization_id,)
    ).fetchone()
    currency = str(organization["base_currency"])
    account_id = finance_db.operating_account_for_organization(
        authority_conn, organization_id, currency
    )
    if account_id is None:
        return
    service = payments.PaymentService(
        authority_conn,
        inbound_rails=payments.load_inbound_payment_rails(),
        outbound_rails=payments.load_outbound_payment_rails(),
    )

    def revenue_at_least(
        snapshot: Mapping[str, Any], params: Mapping[str, Any]
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        minimum = int(params.get("amount_minor") or 0)
        requested_currency = str(params.get("currency") or currency).upper()
        statements = accounting_db.financial_statements(
            authority_conn, organization_id
        )
        actual = int(statements["profit_and_loss"]["revenue_minor"])
        passed = requested_currency == currency.upper() and actual >= minimum
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"ledger:{organization_id}:revenue",
                facts={
                    "currency": currency.upper(),
                    "minimum_minor": minimum,
                    "revenue_minor": actual,
                },
            ),
        )

    def books_balanced(
        snapshot: Mapping[str, Any], params: Mapping[str, Any]
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        statements = accounting_db.financial_statements(
            authority_conn, organization_id
        )
        balanced = bool(statements["balance_sheet"]["balanced"])
        return VerificationOutcome(
            "pass" if balanced else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"ledger:{organization_id}:trial-balance",
                facts={
                    "balanced": balanced,
                    "assets_minor": statements["balance_sheet"]["assets_minor"],
                    "liabilities_minor": statements["balance_sheet"][
                        "liabilities_minor"
                    ],
                    "current_earnings_minor": statements["balance_sheet"][
                        "current_earnings_minor"
                    ],
                },
            ),
        )

    def create_invoice(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            intent = service.create_receivable(
                organization_id=organization_id,
                account_id=account_id,
                provider=str(payload["provider"]),
                amount_minor=int(payload["amount_minor"]),
                currency=str(payload.get("currency") or currency),
                customer=dict(payload["customer"]),
                customer_jurisdiction=str(payload["customer_jurisdiction"]),
                purpose=str(payload["purpose"]),
                idempotency_key=str(payload["idempotency_key"]),
                objective_id=str(payload["_governance_objective_id"]),
                tax_minor=int(payload.get("tax_minor") or 0),
                tax_rule_id=(
                    str(payload["tax_rule_id"]) if payload.get("tax_rule_id") else None
                ),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "payment_intent_id": intent["id"],
                "status": intent["status"],
                "payment_url": intent["payment_url"],
            },
            external_reference=intent["id"],
        )

    def create_metered_invoice(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            customer = dict(payload["customer"])
            customer_ref = str(payload["customer_ref"])
            embedded_customer_ref = customer.get("customer_ref") or customer.get("id")
            if embedded_customer_ref is not None and str(embedded_customer_ref) != customer_ref:
                raise ValueError("customer_ref does not match the customer payload")
            existing_intent = authority_conn.execute(
                "SELECT id FROM payment_intents WHERE idempotency_key=?",
                (str(payload["idempotency_key"]),),
            ).fetchone()
            context = usage_billing.invoice_context(
                authority_conn,
                organization_id=organization_id,
                customer_ref=customer_ref,
                currency=currency,
                event_ids=payload["usage_event_ids"],
                existing_payment_intent_id=(
                    str(existing_intent["id"]) if existing_intent else None
                ),
            )
            tax_minor = 0
            tax_rule_id = None
            if payload.get("tax_rule_id"):
                tax_rule_id = str(payload["tax_rule_id"])
                tax_minor, tax_rule_id = accounting_db.calculate_tax_for_rule(
                    authority_conn,
                    organization_id=organization_id,
                    tax_rule_id=tax_rule_id,
                    jurisdiction=str(payload["customer_jurisdiction"]),
                    taxable_minor=int(context["amount_minor"]),
                    occurred_at=int(time.time()),
                )
            intent = service.create_receivable(
                organization_id=organization_id,
                account_id=account_id,
                provider=str(payload["provider"]),
                amount_minor=int(context["amount_minor"]) + int(tax_minor),
                currency=str(context["currency"]),
                customer=customer,
                customer_jurisdiction=str(payload["customer_jurisdiction"]),
                purpose=str(payload["purpose"]),
                idempotency_key=str(payload["idempotency_key"]),
                objective_id=str(payload["_governance_objective_id"]),
                tax_minor=int(tax_minor),
                tax_rule_id=tax_rule_id,
            )
            if int(intent["amount_minor"]) != int(context["amount_minor"]) + int(tax_minor):
                raise ValueError("idempotent metered invoice amount does not match usage events")
            usage_billing.allocate_events(
                authority_conn,
                event_ids=context["event_ids"],
                payment_intent_id=intent["id"],
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "payment_intent_id": intent["id"],
                "status": intent["status"],
                "payment_url": intent["payment_url"],
                "amount_minor": int(intent["amount_minor"]),
                "usage_amount_minor": context["amount_minor"],
                "tax_minor": tax_minor,
                "currency": context["currency"],
                "usage_event_ids": context["event_ids"],
            },
            external_reference=intent["id"],
        )

    def send_payment(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            merchant_category = str(payload["merchant_category"]).lower()
            if merchant_category in {
                "software",
                "software_and_services",
                "saas",
                "cloud_services",
            }:
                from hermes_cli import procurement_policy

                decision_id = str(
                    payload.get("procurement_decision_id") or ""
                )
                if not decision_id:
                    raise PermissionError(
                        "software purchase requires a procurement decision"
                    )
                procurement_policy.commit_software_purchase(
                    authority_conn,
                    decision_id=decision_id,
                    organization_id=organization_id,
                    objective_id=str(payload["_governance_objective_id"]),
                    action_id=str(payload["_governance_action_id"]),
                    amount_minor=int(payload["amount_minor"]),
                    currency=str(payload.get("currency") or currency),
                )
            intent = service.send_payable(
                organization_id=organization_id,
                account_id=account_id,
                objective_id=str(payload["_governance_objective_id"]),
                action_id=str(payload["_governance_action_id"]),
                provider=str(payload["provider"]),
                amount_minor=int(payload["amount_minor"]),
                currency=str(payload.get("currency") or currency),
                payee=dict(payload["payee"]),
                payee_jurisdiction=str(payload["payee_jurisdiction"]),
                instrument_id=str(payload["instrument_id"]),
                merchant_category=str(payload["merchant_category"]),
                payee_id=str(payload["payee_id"]),
                purpose=str(payload["purpose"]),
                idempotency_key=str(payload["idempotency_key"]),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded" if intent["status"] == "succeeded" else "failed",
            {"payment_intent_id": intent["id"], "status": intent["status"]},
            external_reference=intent["provider_reference"],
            actual_cost_minor=(
                int(intent["amount_minor"]) if intent["status"] == "succeeded" else None
            ),
            preserve_reservation=intent["status"] == "pending",
        )

    def reconcile_payment(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            intent = service.reconcile(str(payload["payment_intent_id"]))
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded" if intent["status"] == "succeeded" else "failed",
            {"payment_intent_id": intent["id"], "status": intent["status"]},
            external_reference=intent["provider_reference"],
            preserve_reservation=intent["status"] == "pending",
        )

    def payment_intent_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        intent_id = execution.result.get("payment_intent_id")
        from hermes_cli import verification_evidence

        try:
            intent = service.reconcile(str(intent_id))
        except Exception as exc:
            return VerificationOutcome(
                "inconclusive",
                verification_evidence.build(
                    observer=verifier.identity,
                    source_kind="provider_readback",
                    source_reference=f"payment:{intent_id}",
                    facts={"intent_id": intent_id, "readback_error": str(exc)},
                ),
            )
        row = authority_conn.execute(
            """SELECT * FROM payment_provider_readbacks WHERE id=?""",
            (intent["provider_readback_id"],),
        ).fetchone()
        if row is None:
            return VerificationOutcome(
                "inconclusive",
                verification_evidence.build(
                    observer=verifier.identity,
                    source_kind="provider_readback",
                    source_reference=f"payment:{intent_id}",
                    facts={"intent_id": intent_id, "readback_recorded": False},
                ),
            )
        allocation_facts = None
        if action.action_type in {"payments.create_invoice", "payments.create_metered_invoice"}:
            passed = bool(row["provider_reference"] and intent["payment_url"])
            if action.action_type == "payments.create_metered_invoice":
                from hermes_cli import usage_billing

                allocation_facts = usage_billing.allocation_readback(
                    authority_conn,
                    payment_intent_id=str(intent["id"]),
                    event_ids=(
                        execution.result.get("usage_event_ids")
                        or action.payload.get("usage_event_ids")
                        or []
                    ),
                    expected_amount_minor=int(intent["amount_minor"]) - int(intent.get("tax_minor") or 0),
                )
                passed = passed and bool(allocation_facts["passed"])
        else:
            passed = row["status"] == "succeeded" and bool(row["provider_reference"])
        return VerificationOutcome(
            "pass" if passed else "inconclusive",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="provider_readback",
                source_reference=f"{row['provider']}:{row['provider_reference']}",
                observation_id=row["id"],
                observed_at=row["observed_at"],
                facts={
                    "intent_id": intent_id,
                    "status": row["status"],
                    "amount_minor": row["amount_minor"],
                    "currency": row["currency"],
                    "provider_reference": row["provider_reference"],
                    "provider_evidence": json.loads(row["evidence_json"]),
                    **({"usage_allocation": allocation_facts} if allocation_facts else {}),
                },
            ),
        )

    executor.register(
        "payments.create_invoice",
        create_invoice,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "provider": str,
                "amount_minor": int,
                "currency": str,
                "customer": dict,
                "customer_jurisdiction": str,
                "purpose": str,
            },
            optional={
                **COMMON_ACTION_FIELDS,
                "tax_minor": int,
                "tax_rule_id": str,
            },
        ),
        required_capability="payments.receive",
        target_system="payments",
        verification_method="payments.provider_readback",
    )
    executor.register(
        "payments.create_metered_invoice",
        create_metered_invoice,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "provider": str,
                "customer": dict,
                "customer_ref": str,
                "customer_jurisdiction": str,
                "usage_event_ids": list,
                "purpose": str,
            },
            optional={**COMMON_ACTION_FIELDS, "tax_rule_id": str},
        ),
        required_capability="payments.receive",
        target_system="payments",
        verification_method="payments.provider_readback",
    )
    executor.register(
        "payments.send",
        send_payment,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "provider": str,
                "amount_minor": int,
                "currency": str,
                "payee": dict,
                "payee_jurisdiction": str,
                "instrument_id": str,
                "merchant_category": str,
                "payee_id": str,
                "purpose": str,
            },
            optional={
                **COMMON_ACTION_FIELDS,
                "procurement_decision_id": str,
            },
        ),
        required_capability="payments.send",
        target_system="payments",
        verification_method="payments.provider_readback",
    )
    executor.register(
        "payments.reconcile",
        reconcile_payment,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "payment_intent_id": str,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="payments.reconcile",
        target_system="payments",
        verification_method="payments.provider_readback",
    )
    verifier.register_action("payments.provider_readback", payment_intent_readback)
    verifier.register_objective("accounting.revenue_at_least", revenue_at_least)
    verifier.register_objective("accounting.books_balanced", books_balanced)


def register_accounting_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    authority_conn,
) -> None:
    """Expose evidence-bound fiscal-period and tax-assessment operations."""
    from hermes_cli import accounting_db, organization_db

    ceo = organization_db.active_ceo(authority_conn)
    if ceo is None:
        return
    organization_id = str(ceo["organization_id"])

    def open_period(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            period_id = accounting_db.open_fiscal_period(
                authority_conn,
                organization_id=organization_id,
                name=str(payload["name"]),
                starts_at=int(payload["starts_at"]),
                ends_at=int(payload["ends_at"]),
                evidence=dict(payload["evidence"]),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {"period_id": period_id},
            external_reference=period_id,
        )

    def assess_tax(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            obligation_id = accounting_db.record_tax_obligation(
                authority_conn,
                organization_id=organization_id,
                registration_id=str(payload["registration_id"]),
                period_start=int(payload["period_start"]),
                period_end=int(payload["period_end"]),
                due_at=int(payload["due_at"]),
                amount_minor=int(payload["amount_minor"]),
                currency=str(payload["currency"]),
                evidence=dict(payload["evidence"]),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {"tax_obligation_id": obligation_id},
            external_reference=obligation_id,
        )

    def close_period(payload: Mapping[str, Any]) -> ExecutionOutcome:
        period_id = str(payload["period_id"])
        try:
            accounting_db.close_fiscal_period(
                authority_conn,
                period_id,
                evidence=dict(payload["evidence"]),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {"period_id": period_id, "status": "closed"},
            external_reference=period_id,
        )

    def record_tax_filing(payload: Mapping[str, Any]) -> ExecutionOutcome:
        obligation_id = str(payload["tax_obligation_id"])
        try:
            event_id = accounting_db.record_tax_filing(
                authority_conn,
                obligation_id,
                filed_at=int(payload["filed_at"]),
                evidence=dict(payload["evidence"]),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "tax_obligation_id": obligation_id,
                "tax_event_id": event_id,
                "event_type": "filed",
            },
            external_reference=event_id,
        )

    def record_tax_payment(payload: Mapping[str, Any]) -> ExecutionOutcome:
        obligation_id = str(payload["tax_obligation_id"])
        try:
            event_id = accounting_db.record_tax_payment(
                authority_conn,
                obligation_id,
                paid_at=int(payload["paid_at"]),
                payment_intent_id=str(payload["payment_intent_id"]),
                evidence=dict(payload["evidence"]),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "tax_obligation_id": obligation_id,
                "tax_event_id": event_id,
                "event_type": "paid",
            },
            external_reference=event_id,
        )

    def accounting_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        if action.action_type in {
            "accounting.assess_tax_obligation",
            "accounting.record_tax_filing",
            "accounting.record_tax_payment",
        }:
            record_id = str(execution.result.get("tax_obligation_id") or "")
            expected_event = {
                "accounting.assess_tax_obligation": "assessed",
                "accounting.record_tax_filing": "filed",
                "accounting.record_tax_payment": "paid",
            }[action.action_type]
            row = authority_conn.execute(
                """SELECT o.*, e.id AS event_id
                   FROM tax_obligations o
                   LEFT JOIN tax_obligation_events e
                     ON e.obligation_id=o.id AND e.event_type=?
                   WHERE o.id=? AND o.organization_id=?""",
                (expected_event, record_id, organization_id),
            ).fetchone()
            facts = {
                "tax_obligation_id": record_id,
                "registration_id": str(row["registration_id"]) if row else None,
                "period_start": int(row["period_start"]) if row else None,
                "period_end": int(row["period_end"]) if row else None,
                "due_at": int(row["due_at"]) if row else None,
                "amount_minor": int(row["amount_minor"]) if row else None,
                "currency": str(row["currency"]) if row else None,
                "status": str(row["status"]) if row else None,
                "filed_at": int(row["filed_at"]) if row and row["filed_at"] else None,
                "paid_at": int(row["paid_at"]) if row and row["paid_at"] else None,
                "event_id": str(row["event_id"]) if row and row["event_id"] else None,
            }
            if action.action_type == "accounting.assess_tax_obligation":
                passed = bool(
                    row
                    and facts["registration_id"]
                    == str(action.payload["registration_id"])
                    and facts["period_start"] == int(action.payload["period_start"])
                    and facts["period_end"] == int(action.payload["period_end"])
                    and facts["due_at"] == int(action.payload["due_at"])
                    and facts["amount_minor"] == int(action.payload["amount_minor"])
                    and facts["currency"] == str(action.payload["currency"]).upper()
                    and facts["status"] in {"accrued", "filed", "paid"}
                    and facts["event_id"]
                )
            elif action.action_type == "accounting.record_tax_filing":
                passed = bool(
                    row
                    and facts["filed_at"] == int(action.payload["filed_at"])
                    and facts["status"] in {"filed", "paid"}
                    and facts["event_id"]
                )
            else:
                passed = bool(
                    row
                    and facts["paid_at"] == int(action.payload["paid_at"])
                    and facts["status"] == "paid"
                    and facts["event_id"]
                )
            source_reference = f"tax-obligation:{record_id}"
        else:
            record_id = str(execution.result.get("period_id") or "")
            expected_event = (
                "closed"
                if action.action_type == "accounting.close_period"
                else "opened"
            )
            row = authority_conn.execute(
                """SELECT p.*, e.id AS event_id
                   FROM fiscal_periods p
                   LEFT JOIN fiscal_period_events e
                     ON e.period_id=p.id AND e.event_type=?
                   WHERE p.id=? AND p.organization_id=?""",
                (expected_event, record_id, organization_id),
            ).fetchone()
            facts = {
                "period_id": record_id,
                "name": str(row["name"]) if row else None,
                "starts_at": int(row["starts_at"]) if row else None,
                "ends_at": int(row["ends_at"]) if row else None,
                "status": str(row["status"]) if row else None,
                "event_id": str(row["event_id"]) if row and row["event_id"] else None,
            }
            if action.action_type == "accounting.close_period":
                passed = bool(row and facts["status"] == "closed" and facts["event_id"])
            else:
                passed = bool(
                    row
                    and facts["name"] == str(action.payload["name"]).strip()
                    and facts["starts_at"] == int(action.payload["starts_at"])
                    and facts["ends_at"] == int(action.payload["ends_at"])
                    and facts["status"] == "open"
                    and facts["event_id"]
                )
            source_reference = f"fiscal-period:{record_id}"
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=source_reference,
                observation_id=facts.get("event_id"),
                facts=facts,
            ),
        )

    executor.register(
        "accounting.open_period",
        open_period,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "name": str,
                "starts_at": int,
                "ends_at": int,
                "evidence": dict,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="accounting.manage_periods",
        target_system="accounting",
        verification_method="accounting.record.readback",
    )
    executor.register(
        "accounting.assess_tax_obligation",
        assess_tax,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "registration_id": str,
                "period_start": int,
                "period_end": int,
                "due_at": int,
                "amount_minor": int,
                "currency": str,
                "evidence": dict,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="accounting.assess_tax",
        target_system="accounting",
        verification_method="accounting.record.readback",
    )
    executor.register(
        "accounting.close_period",
        close_period,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "period_id": str,
                "evidence": dict,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="accounting.close_period",
        target_system="accounting",
        verification_method="accounting.record.readback",
    )
    executor.register(
        "accounting.record_tax_filing",
        record_tax_filing,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "tax_obligation_id": str,
                "filed_at": int,
                "evidence": dict,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="accounting.file_tax",
        target_system="accounting",
        verification_method="accounting.record.readback",
    )
    executor.register(
        "accounting.record_tax_payment",
        record_tax_payment,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "tax_obligation_id": str,
                "paid_at": int,
                "payment_intent_id": str,
                "evidence": dict,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="accounting.record_tax_payment",
        target_system="accounting",
        verification_method="accounting.record.readback",
    )
    verifier.register_action("accounting.record.readback", accounting_readback)


def register_procurement_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    authority_conn,
) -> None:
    """Register budget-derived build/FOSS/buy evaluation."""
    from hermes_cli import organization_db, procurement_policy

    ceo = organization_db.active_ceo(authority_conn)
    if ceo is None:
        return
    organization_id = str(ceo["organization_id"])

    def evaluate(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            decision_id, decision = (
                procurement_policy.evaluate_procurement_from_state(
                    authority_conn,
                    organization_id=organization_id,
                    objective_id=str(payload["_governance_objective_id"]),
                    case=dict(payload["case"]),
                    source_evidence=dict(payload["source_evidence"]),
                    idempotency_key=str(payload["idempotency_key"]),
                    evaluated_by=executor.identity,
                )
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "decision_id": decision_id,
                "choice": decision.choice,
                "reason": decision.reason,
                "committed_cost_minor": decision.committed_cost_minor,
            },
            external_reference=decision_id,
        )

    def readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        decision_id = str(execution.result.get("decision_id") or "")
        row = authority_conn.execute(
            """SELECT * FROM procurement_decisions
               WHERE id=? AND organization_id=?""",
            (decision_id, organization_id),
        ).fetchone()
        passed = False
        if row is None:
            facts = {"decision_id": decision_id, "recorded": False}
        else:
            facts_for_hash = {
                "organization_id": organization_id,
                "objective_id": str(row["objective_id"]),
                "available_budget_minor": int(row["available_budget_minor"]),
                "currency": str(row["currency"]),
                "choice": str(row["choice"]),
                "committed_cost_minor": int(row["committed_cost_minor"]),
                "source_reference": str(
                    json.loads(row["source_evidence_json"])["reference"]
                ),
            }
            evidence_hash = hashlib.sha256(
                json.dumps(
                    facts_for_hash, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            facts = {
                **facts_for_hash,
                "decision_id": decision_id,
                "evidence_sha256": evidence_hash,
            }
            passed = (
                evidence_hash == row["evidence_sha256"]
                and facts["choice"] == execution.result.get("choice")
                and facts["committed_cost_minor"]
                == execution.result.get("committed_cost_minor")
            )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"procurement:{decision_id}",
                facts=facts,
            ),
        )

    executor.register(
        "procurement.evaluate",
        evaluate,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "case": dict,
                "source_evidence": dict,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="procurement.evaluate",
        target_system="procurement",
        verification_method="procurement.decision.readback",
    )
    verifier.register_action("procurement.decision.readback", readback)


def register_email_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    authority_conn,
    config: Mapping[str, Any],
    provider_config=None,
) -> None:
    """Register AgentMail only when its scoped secret and inbox are configured."""
    from hermes_cli import company_email, email_policy, organization_db

    ceo = organization_db.active_ceo(authority_conn)
    configured = provider_config or company_email.configured_agentmail(config)
    if ceo is None or configured is None:
        return
    organization_id = str(ceo["organization_id"])

    def send_email(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            company_email.validate_send(
                authority_conn,
                organization_id=organization_id,
                payload=payload,
            )
            current = company_email.usage(authority_conn, organization_id)
            capacity = email_policy.evaluate_agentmail_free_capacity(
                inboxes=1,
                emails_month=current["emails_month"],
                emails_day=current["emails_day"],
                storage_mb=0,
                webhook_endpoints=0,
            )
            if capacity.status == "capacity_exceeded":
                raise company_email.CompanyEmailError(capacity.reason)
            response = configured.provider.send(
                inbox_id=configured.inbox_id,
                recipients=[str(item) for item in payload["to"]],
                subject=str(payload["subject"]),
                text=str(payload["text"]),
                html=str(payload["html"]) if payload.get("html") else None,
                idempotency_key=str(payload["idempotency_key"]),
            )
            operation_id = company_email.record_send(
                authority_conn,
                organization_id=organization_id,
                objective_id=str(payload["_governance_objective_id"]),
                action_id=str(payload["_governance_action_id"]),
                inbox_id=configured.inbox_id,
                payload=payload,
                response=response,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "operation_id": operation_id,
                "inbox_id": configured.inbox_id,
                "message_id": response["message_id"],
                "thread_id": response.get("thread_id"),
            },
            external_reference=str(response["message_id"]),
        )

    def message_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        inbox_id = str(execution.result.get("inbox_id") or "")
        message_id = str(execution.result.get("message_id") or "")
        try:
            observed = configured.provider.get_message(
                inbox_id=inbox_id, message_id=message_id
            )
        except Exception as exc:
            return VerificationOutcome(
                "inconclusive",
                verification_evidence.build(
                    observer=verifier.identity,
                    source_kind="provider_readback",
                    source_reference=f"agentmail:{inbox_id}:{message_id}",
                    facts={"readback_error": str(exc)},
                ),
            )
        observed_to = observed.get("to") or []
        if isinstance(observed_to, str):
            observed_to = [observed_to]
        expected_to = sorted(str(item).strip().lower() for item in action.payload["to"])
        facts = {
            "message_id": str(observed.get("message_id") or ""),
            "thread_id": observed.get("thread_id"),
            "recipients": sorted(str(item).strip().lower() for item in observed_to),
            "subject_sha256": hashlib.sha256(
                str(observed.get("subject") or "").encode()
            ).hexdigest(),
        }
        passed = (
            facts["message_id"] == message_id
            and facts["recipients"] == expected_to
            and facts["subject_sha256"]
            == hashlib.sha256(
                str(action.payload["subject"]).encode()
            ).hexdigest()
        )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="provider_readback",
                source_reference=f"agentmail:{inbox_id}:{message_id}",
                facts=facts,
            ),
        )

    executor.register(
        "email.send",
        send_email,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "to": list,
                "subject": str,
                "text": str,
                "communication_type": str,
            },
            optional={
                **COMMON_ACTION_FIELDS,
                "html": str,
                "consent_basis": str,
                "sender_identity": str,
                "physical_address": str,
                "unsubscribe_url": str,
            },
        ),
        required_capability="email.send",
        target_system="agentmail",
        verification_method="email.provider_readback",
    )
    verifier.register_action("email.provider_readback", message_readback)


def register_workforce_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    authority_conn,
    config: Mapping[str, Any],
) -> None:
    """Register evidence-derived hiring and least-privilege provisioning."""
    from hermes_cli import (
        employee_provisioning,
        hiring_policy,
        organization_db,
    )

    ceo = organization_db.active_ceo(authority_conn)
    if ceo is None:
        return
    organization_id = str(ceo["organization_id"])
    configured_policy = {
        **hiring_policy.default_hiring_policy(),
        **dict(((config.get("agentic") or {}).get("organization") or {})),
    }

    def evaluate_hire(payload: Mapping[str, Any]) -> ExecutionOutcome:
        case = dict(payload["case"])
        case["objective_id"] = str(payload["_governance_objective_id"])
        try:
            decision_id, decision = hiring_policy.evaluate_hiring_case_from_state(
                authority_conn,
                organization_id=organization_id,
                case=case,
                policy=configured_policy,
                idempotency_key=str(payload["idempotency_key"]),
                evaluated_by=executor.identity,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "decision_id": decision_id,
                "objective_id": case["objective_id"],
                "verdict": decision.verdict,
                "reason": decision.reason,
                "employment_class": decision.employment_class,
            },
            external_reference=decision_id,
        )

    def materialize_hire(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            employee_id = hiring_policy.materialize_hiring_decision(
                authority_conn,
                str(payload["decision_id"]),
                display_name=str(payload["display_name"]),
                title=str(payload["title"]),
                level=str(payload["level"]),
                manager_id=str(payload["manager_employee_id"]),
                mandate=dict(payload["mandate"]),
                actor=executor.identity,
            )
            employee = organization_db.get_employee_record(
                authority_conn, employee_id
            )
            if employee["status"] == "approved":
                employee_provisioning.provision_employee_profile(
                    authority_conn,
                    employee_id,
                    actor=executor.identity,
                    profile_name=str(payload["profile_name"]),
                    source_config=dict(config),
                )
            employee = organization_db.get_employee_record(
                authority_conn, employee_id
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded" if employee["status"] == "active" else "failed",
            {
                "employee_id": employee_id,
                "profile_name": employee["profile_name"],
                "status": employee["status"],
                "decision_id": str(payload["decision_id"]),
            },
            external_reference=employee_id,
        )

    def decision_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        decision_id = str(execution.result.get("decision_id") or "")
        row = authority_conn.execute(
            "SELECT * FROM hiring_decisions WHERE id=? AND organization_id=?",
            (decision_id, organization_id),
        ).fetchone()
        facts: dict[str, Any]
        passed = False
        if row is None:
            facts = {"decision_id": decision_id, "recorded": False}
        else:
            evidence_json = str(row["derived_evidence_json"])
            evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
            facts = {
                "decision_id": decision_id,
                "recorded": True,
                "objective_id": str(row["objective_id"]),
                "verdict": str(row["verdict"]),
                "employment_class": str(row["employment_class"]),
                "evidence_sha256": evidence_hash,
            }
            passed = (
                evidence_hash == row["evidence_sha256"]
                and facts["objective_id"]
                == str(execution.result.get("objective_id") or "")
                and facts["verdict"] == execution.result.get("verdict")
                and facts["employment_class"]
                == execution.result.get("employment_class")
            )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"hiring-decision:{decision_id}",
                facts=facts,
            ),
        )

    def employee_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence
        from hermes_cli.profiles import get_profile_dir, read_profile_meta

        employee_id = str(execution.result.get("employee_id") or "")
        row = authority_conn.execute(
            """SELECT e.*,g.decision_id,g.employment_class
               FROM employees e
               JOIN hiring_engagements g ON g.employee_id=e.id
              WHERE e.id=? AND e.organization_id=?""",
            (employee_id, organization_id),
        ).fetchone()
        facts: dict[str, Any]
        passed = False
        if row is None:
            facts = {"employee_id": employee_id, "recorded": False}
        else:
            profile_dir = get_profile_dir(str(row["profile_name"]))
            try:
                profile_meta = read_profile_meta(profile_dir)
            except Exception:
                profile_meta = {}
            facts = {
                "employee_id": employee_id,
                "recorded": True,
                "status": str(row["status"]),
                "decision_id": str(row["decision_id"]),
                "employment_class": str(row["employment_class"]),
                "manager_employee_id": str(row["manager_id"]),
                "profile_name": str(row["profile_name"]),
                "profile_employee_id": str(
                    profile_meta.get("employee_id") or ""
                ),
                "profile_mandate_id": str(
                    profile_meta.get("mandate_id") or ""
                ),
            }
            passed = (
                facts["status"] == "active"
                and facts["decision_id"] == str(action.payload["decision_id"])
                and facts["manager_employee_id"]
                == str(action.payload["manager_employee_id"])
                and facts["profile_name"] == str(action.payload["profile_name"])
                and facts["profile_employee_id"] == employee_id
                and bool(facts["profile_mandate_id"])
            )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="deterministic_check",
                source_reference=f"employee-profile:{employee_id}",
                facts=facts,
            ),
        )

    executor.register(
        "organization.evaluate_hire",
        evaluate_hire,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "case": dict,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="organization.hire.evaluate",
        target_system="organization",
        verification_method="organization.hiring_decision.readback",
    )
    executor.register(
        "organization.materialize_hire",
        materialize_hire,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "decision_id": str,
                "display_name": str,
                "title": str,
                "level": str,
                "manager_employee_id": str,
                "mandate": dict,
                "profile_name": str,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="organization.hire",
        target_system="organization",
        verification_method="organization.employee_profile.readback",
    )
    verifier.register_action(
        "organization.hiring_decision.readback", decision_readback
    )
    verifier.register_action(
        "organization.employee_profile.readback", employee_readback
    )


def register_portfolio_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    authority_conn,
    config: Mapping[str, Any],
) -> None:
    """Register bounded objective decomposition and exact cancellation."""
    from hermes_cli import objective_portfolio, organization_db

    ceo = organization_db.active_ceo(authority_conn)
    if ceo is None:
        return
    organization_config = (
        ((config.get("agentic") or {}).get("organization") or {})
    )
    max_active = int(
        organization_config.get("max_active_objectives", 25) or 25
    )

    def create_child(payload: Mapping[str, Any]) -> ExecutionOutcome:
        criteria = list(payload["success_criteria"])
        available_verifiers = set(verifier.objective_methods)
        missing = sorted(
            {
                str(item.get("verifier") or "")
                for item in criteria
                if str(item.get("verifier") or "") not in available_verifiers
            }
        )
        if missing:
            return ExecutionOutcome(
                "failed",
                {"error": "unregistered child success verifiers: " + ", ".join(missing)},
            )
        try:
            child_id, created = objective_portfolio.create_child_objective(
                authority_conn,
                parent_objective_id=str(payload["_governance_objective_id"]),
                desired_outcome=str(payload["desired_outcome"]),
                success_criteria=criteria,
                termination_conditions=list(payload["termination_conditions"]),
                permitted_systems=list(payload["permitted_systems"]),
                prohibited_actions=list(payload["prohibited_actions"]),
                constraints=payload.get("constraints") or [],
                allocated_budget_minor=int(payload["allocated_budget_minor"]),
                currency=(
                    str(payload["currency"]) if payload.get("currency") else None
                ),
                expires_at=int(payload["expires_at"]),
                idempotency_key=str(payload["idempotency_key"]),
                created_by=executor.identity,
                max_active_objectives=max_active,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "child_objective_id": child_id,
                "parent_objective_id": str(payload["_governance_objective_id"]),
                "created": created,
            },
            external_reference=child_id,
        )

    def cancel_child(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            child_id = objective_portfolio.cancel_child_by_creation_key(
                authority_conn,
                parent_objective_id=str(payload["_governance_objective_id"]),
                idempotency_key=str(payload["creation_idempotency_key"]),
                actor=executor.identity,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "child_objective_id": child_id,
                "parent_objective_id": str(payload["_governance_objective_id"]),
                "status": "cancelled",
            },
            external_reference=child_id,
        )

    def create_successor(payload: Mapping[str, Any]) -> ExecutionOutcome:
        criteria = list(payload["success_criteria"])
        available_verifiers = set(verifier.objective_methods)
        missing = sorted(
            {
                str(item.get("verifier") or "")
                for item in criteria
                if str(item.get("verifier") or "") not in available_verifiers
            }
        )
        if missing:
            return ExecutionOutcome(
                "failed",
                {
                    "error": (
                        "unregistered successor success verifiers: "
                        + ", ".join(missing)
                    )
                },
            )
        predecessor_id = str(payload["_governance_objective_id"])
        try:
            successor_id, created = objective_portfolio.create_successor_objective(
                authority_conn,
                predecessor_objective_id=predecessor_id,
                desired_outcome=str(payload["desired_outcome"]),
                success_criteria=criteria,
                termination_conditions=list(payload["termination_conditions"]),
                permitted_systems=list(payload["permitted_systems"]),
                prohibited_actions=list(payload["prohibited_actions"]),
                constraints=payload.get("constraints") or [],
                allocated_budget_minor=int(payload["allocated_budget_minor"]),
                currency=(
                    str(payload["currency"]) if payload.get("currency") else None
                ),
                expires_at=int(payload["expires_at"]),
                idempotency_key=str(payload["idempotency_key"]),
                created_by=executor.identity,
                max_active_objectives=max_active,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "successor_objective_id": successor_id,
                "predecessor_objective_id": predecessor_id,
                "created": created,
            },
            external_reference=successor_id,
        )

    def child_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        child_id = str(execution.result.get("child_objective_id") or "")
        parent_id = str(execution.result.get("parent_objective_id") or "")
        row = authority_conn.execute(
            """SELECT r.*,o.status,o.success_criteria_json,o.permitted_systems_json,
                      o.max_spend_minor,o.expires_at
               FROM objective_relationships r
               JOIN objectives o ON o.id=r.child_objective_id
              WHERE r.parent_objective_id=? AND r.child_objective_id=?""",
            (parent_id, child_id),
        ).fetchone()
        if row is None:
            facts = {"child_objective_id": child_id, "recorded": False}
            passed = False
        else:
            inbox = authority_conn.execute(
                """SELECT status FROM objective_inbox
                   WHERE objective_id=? AND event_type='objective.accepted'
                   ORDER BY created_at,id LIMIT 1""",
                (child_id,),
            ).fetchone()
            facts = {
                "child_objective_id": child_id,
                "parent_objective_id": parent_id,
                "recorded": True,
                "status": str(row["status"]),
                "allocated_budget_minor": int(row["allocated_budget_minor"]),
                "expires_at": int(row["expires_at"]),
                "success_criteria": json.loads(row["success_criteria_json"]),
                "permitted_systems": json.loads(row["permitted_systems_json"]),
                "wake_event_status": str(inbox["status"]) if inbox else None,
            }
            passed = (
                facts["status"] == "accepted"
                and facts["allocated_budget_minor"]
                == int(action.payload["allocated_budget_minor"])
                and facts["success_criteria"]
                == list(action.payload["success_criteria"])
                and facts["permitted_systems"]
                == list(action.payload["permitted_systems"])
                and facts["wake_event_status"] in {"pending", "processing", "completed"}
            )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"objective-relationship:{parent_id}:{child_id}",
                facts=facts,
            ),
        )

    def cancellation_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        child_id = str(execution.result.get("child_objective_id") or "")
        row = authority_conn.execute(
            "SELECT status FROM objectives WHERE id=?", (child_id,)
        ).fetchone()
        status = str(row["status"]) if row else "missing"
        return VerificationOutcome(
            "pass" if status == "cancelled" else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"objective:{child_id}",
                facts={"child_objective_id": child_id, "status": status},
            ),
        )

    def successor_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        successor_id = str(execution.result.get("successor_objective_id") or "")
        predecessor_id = str(
            execution.result.get("predecessor_objective_id") or ""
        )
        row = authority_conn.execute(
            """SELECT r.*,o.status,o.success_criteria_json,
                      o.permitted_systems_json,o.prohibited_actions_json,
                      o.max_spend_minor,o.expires_at
                 FROM objective_relationships r
                 JOIN objectives o ON o.id=r.child_objective_id
                WHERE r.parent_objective_id=? AND r.child_objective_id=?
                  AND r.relationship='succeeds'""",
            (predecessor_id, successor_id),
        ).fetchone()
        inbox = authority_conn.execute(
            """SELECT status FROM objective_inbox
                WHERE objective_id=? AND event_type='objective.accepted'
                ORDER BY created_at,id LIMIT 1""",
            (successor_id,),
        ).fetchone()
        cadence = authority_conn.execute(
            """SELECT status FROM objective_schedules
                WHERE objective_id=? AND event_type='ceo.operating_review'
                ORDER BY created_at,id LIMIT 1""",
            (successor_id,),
        ).fetchone()
        if row is None:
            facts = {"successor_objective_id": successor_id, "recorded": False}
            passed = False
        else:
            facts = {
                "successor_objective_id": successor_id,
                "predecessor_objective_id": predecessor_id,
                "recorded": True,
                "relationship": str(row["relationship"]),
                "status": str(row["status"]),
                "allocated_budget_minor": int(row["allocated_budget_minor"]),
                "expires_at": int(row["expires_at"]),
                "success_criteria": json.loads(row["success_criteria_json"]),
                "permitted_systems": json.loads(row["permitted_systems_json"]),
                "prohibited_actions": json.loads(row["prohibited_actions_json"]),
                "wake_event_status": str(inbox["status"]) if inbox else None,
                "operating_cadence_status": (
                    str(cadence["status"]) if cadence else "not_configured"
                ),
            }
            passed = (
                facts["relationship"] == "succeeds"
                and facts["status"] == "accepted"
                and facts["allocated_budget_minor"]
                == int(action.payload["allocated_budget_minor"])
                and facts["success_criteria"]
                == list(action.payload["success_criteria"])
                and facts["permitted_systems"]
                == list(action.payload["permitted_systems"])
                and facts["prohibited_actions"]
                == list(action.payload["prohibited_actions"])
                and facts["wake_event_status"]
                in {"pending", "processing", "completed"}
                and facts["operating_cadence_status"]
                in {"active", "not_configured"}
            )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=(
                    f"objective-succession:{predecessor_id}:{successor_id}"
                ),
                facts=facts,
            ),
        )

    executor.register(
        "objectives.create_child",
        create_child,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "desired_outcome": str,
                "success_criteria": list,
                "termination_conditions": list,
                "permitted_systems": list,
                "prohibited_actions": list,
                "allocated_budget_minor": int,
                "expires_at": int,
            },
            optional={**COMMON_ACTION_FIELDS, "constraints": list, "currency": str},
        ),
        required_capability="objectives.create",
        target_system="objectives",
        verification_method="objectives.child.readback",
    )
    executor.register(
        "objectives.cancel_child",
        cancel_child,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "creation_idempotency_key": str,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="objectives.cancel",
        target_system="objectives",
        verification_method="objectives.cancellation.readback",
    )
    executor.register(
        "objectives.create_successor",
        create_successor,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "desired_outcome": str,
                "success_criteria": list,
                "termination_conditions": list,
                "permitted_systems": list,
                "prohibited_actions": list,
                "allocated_budget_minor": int,
                "expires_at": int,
            },
            optional={**COMMON_ACTION_FIELDS, "constraints": list, "currency": str},
        ),
        required_capability="objectives.create",
        target_system="objectives",
        verification_method="objectives.successor.readback",
    )
    verifier.register_action("objectives.child.readback", child_readback)
    verifier.register_action("objectives.successor.readback", successor_readback)
    verifier.register_action(
        "objectives.cancellation.readback", cancellation_readback
    )


def register_strategy_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    authority_conn,
) -> None:
    """Register governed KPI contracts and evidence-driven experiments."""
    from hermes_cli import business_metrics, objectives_db
    from hermes_cli import verification_evidence

    def org_id(payload: Mapping[str, Any]) -> str:
        objective = objectives_db.objective_to_dict(
            authority_conn, str(payload["_governance_objective_id"])
        )
        return str(objective["organization_id"])

    def register_metric(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            metric_id, created = business_metrics.register_metric(
                authority_conn,
                organization_id=org_id(payload),
                metric_key=str(payload["metric_key"]),
                name=str(payload["name"]),
                unit=str(payload["unit"]),
                preferred_direction=str(payload["preferred_direction"]),
                source_system=str(payload["source_system"]),
                verifier=str(payload["observation_verifier"]),
                idempotency_key=str(payload["idempotency_key"]),
                created_by=executor.identity,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {"metric_id": metric_id, "created": created},
            external_reference=metric_id,
        )

    def define_target(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            target_id, created = business_metrics.define_target(
                authority_conn,
                organization_id=org_id(payload),
                objective_id=str(payload["_governance_objective_id"]),
                metric_id=str(payload["metric_id"]),
                comparator=str(payload["comparator"]),
                target_scaled=int(payload["target_scaled"]),
                review_interval_seconds=int(payload["review_interval_seconds"]),
                max_observation_age_seconds=int(
                    payload["max_observation_age_seconds"]
                ),
                first_review_at=int(payload["first_review_at"]),
                idempotency_key=str(payload["idempotency_key"]),
                created_by=executor.identity,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {"target_id": target_id, "created": created},
            external_reference=target_id,
        )

    def start_experiment(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            experiment_id, created = business_metrics.start_experiment(
                authority_conn,
                organization_id=org_id(payload),
                objective_id=str(payload["_governance_objective_id"]),
                name=str(payload["name"]),
                hypothesis=str(payload["hypothesis"]),
                metric_id=str(payload["metric_id"]),
                comparator=str(payload["comparator"]),
                success_threshold_scaled=int(payload["success_threshold_scaled"]),
                starts_at=int(payload["starts_at"]),
                ends_at=int(payload["ends_at"]),
                max_spend_minor=int(payload["max_spend_minor"]),
                currency=(
                    str(payload["currency"]) if payload.get("currency") else None
                ),
                idempotency_key=str(payload["idempotency_key"]),
                created_by=executor.identity,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {"experiment_id": experiment_id, "created": created},
            external_reference=experiment_id,
        )

    def decide_experiment(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            status = business_metrics.decide_experiment(
                authority_conn,
                organization_id=org_id(payload),
                experiment_id=str(payload["experiment_id"]),
                decision=str(payload["decision"]),
                reason=str(payload["reason"]),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {"experiment_id": str(payload["experiment_id"]), "status": status},
            external_reference=str(payload["experiment_id"]),
        )

    def metric_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        metric_id = str(execution.result.get("metric_id") or "")
        row = authority_conn.execute(
            """SELECT metric_key,name,unit,preferred_direction,source_system,
                      verifier FROM business_metrics WHERE id=?""",
            (metric_id,),
        ).fetchone()
        facts = dict(row) if row else {"recorded": False}
        passed = bool(
            row
            and str(row["metric_key"]) == str(action.payload["metric_key"])
            and str(row["verifier"])
            == str(action.payload["observation_verifier"])
        )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"business-metric:{metric_id}",
                facts=facts,
            ),
        )

    def target_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        target_id = str(execution.result.get("target_id") or "")
        row = authority_conn.execute(
            """SELECT t.metric_id,t.comparator,t.target_scaled,
                      t.review_interval_seconds,s.status,s.next_review_at
                 FROM metric_targets t
                 JOIN metric_target_state s ON s.target_id=t.id
                WHERE t.id=?""",
            (target_id,),
        ).fetchone()
        facts = dict(row) if row else {"recorded": False}
        passed = bool(
            row
            and str(row["metric_id"]) == str(action.payload["metric_id"])
            and str(row["comparator"]) == str(action.payload["comparator"])
            and int(row["target_scaled"]) == int(action.payload["target_scaled"])
            and str(row["status"]) == "active"
        )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"metric-target:{target_id}",
                facts=facts,
            ),
        )

    def experiment_readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        experiment_id = str(execution.result.get("experiment_id") or "")
        row = authority_conn.execute(
            """SELECT e.metric_id,e.hypothesis,e.success_threshold_scaled,
                      e.ends_at,e.max_spend_minor,s.status
                 FROM strategy_experiments e
                 JOIN strategy_experiment_state s ON s.experiment_id=e.id
                WHERE e.id=?""",
            (experiment_id,),
        ).fetchone()
        facts = dict(row) if row else {"recorded": False}
        expected_status = str(execution.result.get("status") or "running")
        passed = bool(row and str(row["status"]) == expected_status)
        if row and "metric_id" in action.payload:
            passed = (
                passed
                and str(row["metric_id"]) == str(action.payload["metric_id"])
                and str(row["hypothesis"]) == str(action.payload["hypothesis"])
                and int(row["success_threshold_scaled"])
                == int(action.payload["success_threshold_scaled"])
            )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"strategy-experiment:{experiment_id}",
                facts=facts,
            ),
        )

    executor.register(
        "strategy.register_metric",
        register_metric,
        contract=PayloadContract(
            required={
                "system": str, "target_resource": str, "idempotency_key": str,
                "metric_key": str, "name": str, "unit": str,
                "preferred_direction": str, "source_system": str,
                "observation_verifier": str,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="strategy.manage",
        target_system="strategy",
        verification_method="strategy.metric.readback",
    )
    executor.register(
        "strategy.define_target",
        define_target,
        contract=PayloadContract(
            required={
                "system": str, "target_resource": str, "idempotency_key": str,
                "metric_id": str, "comparator": str, "target_scaled": int,
                "review_interval_seconds": int,
                "max_observation_age_seconds": int,
                "first_review_at": int,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="strategy.manage",
        target_system="strategy",
        verification_method="strategy.target.readback",
    )
    executor.register(
        "strategy.start_experiment",
        start_experiment,
        contract=PayloadContract(
            required={
                "system": str, "target_resource": str, "idempotency_key": str,
                "name": str, "hypothesis": str, "metric_id": str,
                "comparator": str, "success_threshold_scaled": int,
                "starts_at": int, "ends_at": int, "max_spend_minor": int,
            },
            optional={**COMMON_ACTION_FIELDS, "currency": str},
        ),
        required_capability="strategy.manage",
        target_system="strategy",
        verification_method="strategy.experiment.readback",
    )
    executor.register(
        "strategy.decide_experiment",
        decide_experiment,
        contract=PayloadContract(
            required={
                "system": str, "target_resource": str, "idempotency_key": str,
                "experiment_id": str, "decision": str, "reason": str,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="strategy.manage",
        target_system="strategy",
        verification_method="strategy.experiment.readback",
    )
    verifier.register_action("strategy.metric.readback", metric_readback)
    verifier.register_action("strategy.target.readback", target_readback)
    verifier.register_action("strategy.experiment.readback", experiment_readback)


def register_commitment_adapters(
    executor: ActionExecutorRegistry,
    verifier: IndependentVerifierRegistry,
    *,
    authority_conn,
) -> None:
    """Register exact contracts for durable promises and evidenced fulfillment."""
    from hermes_cli import business_commitments, organization_db

    ceo = organization_db.active_ceo(authority_conn)
    if ceo is None:
        return
    organization_id = str(ceo["organization_id"])

    def create(payload: Mapping[str, Any]) -> ExecutionOutcome:
        required_verifier = str(payload["required_verifier"])
        if required_verifier not in verifier.action_methods:
            return ExecutionOutcome(
                "failed",
                {"error": f"unregistered fulfillment verifier: {required_verifier}"},
            )
        try:
            commitment_id, created = business_commitments.create_commitment(
                authority_conn,
                organization_id=organization_id,
                objective_id=str(payload["_governance_objective_id"]),
                kind=str(payload["kind"]),
                title=str(payload["title"]),
                description=str(payload["description"]),
                counterparty_type=str(payload["counterparty_type"]),
                counterparty_reference=str(payload["counterparty_reference"]),
                source_system=str(payload["source_system"]),
                source_reference=str(payload["source_reference"]),
                due_at=int(payload["due_at"]),
                grace_seconds=int(payload["grace_seconds"]),
                required_verifier=required_verifier,
                financial_exposure_minor=int(payload["financial_exposure_minor"]),
                currency=str(payload["currency"]) if payload.get("currency") else None,
                idempotency_key=str(payload["idempotency_key"]),
                created_by=executor.identity,
                supersedes_id=(
                    str(payload["supersedes_id"])
                    if payload.get("supersedes_id")
                    else None
                ),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "commitment_id": commitment_id,
                "created": created,
                "contract_sha256": authority_conn.execute(
                    "SELECT contract_sha256 FROM business_commitments WHERE id=?",
                    (commitment_id,),
                ).fetchone()["contract_sha256"],
            },
            external_reference=commitment_id,
        )

    def cancel(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            commitment_id = business_commitments.cancel_by_creation_key(
                authority_conn,
                organization_id=organization_id,
                idempotency_key=str(payload["creation_idempotency_key"]),
                actor=executor.identity,
                reason=str(payload["reason"]),
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {"commitment_id": commitment_id, "status": "cancelled"},
            external_reference=commitment_id,
        )

    def fulfill(payload: Mapping[str, Any]) -> ExecutionOutcome:
        try:
            commitment_id = business_commitments.fulfill_commitment(
                authority_conn,
                commitment_id=str(payload["commitment_id"]),
                verification_id=str(payload["verification_id"]),
                actor=executor.identity,
            )
        except Exception as exc:
            return ExecutionOutcome("failed", {"error": str(exc)})
        return ExecutionOutcome(
            "succeeded",
            {
                "commitment_id": commitment_id,
                "verification_id": str(payload["verification_id"]),
                "status": "fulfilled",
            },
            external_reference=commitment_id,
        )

    def readback(
        action: ActionProposal, execution: ExecutionOutcome
    ) -> VerificationOutcome:
        from hermes_cli import verification_evidence

        commitment_id = str(execution.result.get("commitment_id") or "")
        row = authority_conn.execute(
            """SELECT * FROM business_commitments
               WHERE id=? AND organization_id=?""",
            (commitment_id, organization_id),
        ).fetchone()
        facts = {
            "commitment_id": commitment_id,
            "recorded": row is not None,
            "status": str(row["status"]) if row else None,
            "contract_sha256": str(row["contract_sha256"]) if row else None,
            "fulfilment_verification_id": (
                str(row["fulfilment_verification_id"])
                if row and row["fulfilment_verification_id"]
                else None
            ),
        }
        if action.action_type == "commitments.create":
            passed = (
                row is not None
                and facts["contract_sha256"]
                == execution.result.get("contract_sha256")
                and facts["status"] == "active"
            )
        elif action.action_type == "commitments.cancel":
            passed = row is not None and facts["status"] == "cancelled"
        else:
            passed = (
                row is not None
                and facts["status"] == "fulfilled"
                and facts["fulfilment_verification_id"]
                == str(action.payload["verification_id"])
            )
        return VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"business-commitment:{commitment_id}",
                facts=facts,
            ),
        )

    executor.register(
        "commitments.create",
        create,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "kind": str,
                "title": str,
                "description": str,
                "counterparty_type": str,
                "counterparty_reference": str,
                "source_system": str,
                "source_reference": str,
                "due_at": int,
                "grace_seconds": int,
                "required_verifier": str,
                "financial_exposure_minor": int,
            },
            optional={**COMMON_ACTION_FIELDS, "currency": str, "supersedes_id": str},
        ),
        required_capability="commitments.manage",
        target_system="commitments",
        verification_method="commitment.record.readback",
    )
    executor.register(
        "commitments.cancel",
        cancel,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "creation_idempotency_key": str,
                "reason": str,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="commitments.manage",
        target_system="commitments",
        verification_method="commitment.cancelled.readback",
    )
    executor.register(
        "commitments.fulfill",
        fulfill,
        contract=PayloadContract(
            required={
                "system": str,
                "target_resource": str,
                "idempotency_key": str,
                "commitment_id": str,
                "verification_id": str,
            },
            optional=COMMON_ACTION_FIELDS,
        ),
        required_capability="commitments.manage",
        target_system="commitments",
        verification_method="commitment.fulfillment.readback",
    )
    verifier.register_action("commitment.record.readback", readback)
    verifier.register_action("commitment.cancelled.readback", readback)
    verifier.register_action("commitment.fulfillment.readback", readback)
